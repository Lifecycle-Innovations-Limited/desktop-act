#!/usr/bin/env python3
"""desktop-act MCP — computer-use primitives + multi-desktop pool + autonomous act() loop.

Architecture:
  * OS-agnostic backend abstraction dispatched on platform.system():
      - X11Backend (Linux): persistent Xlib connection cache + Xvnc/websockify pool.
      - MacBackend (macOS): screencapture + cliclick/osascript, single real session.
  * Multi-desktop pool (Xvnc + websockify) with file-locked allocation in /tmp (X11).
  * Optimized primitives: cached SHA-dedupe screenshots, JPEG default, batch syncs.
  * High-level act(goal) via claude-agent-sdk (CLI OAuth, no ANTHROPIC_API_KEY).

Auth path: claude-agent-sdk only (bundled CLI OAuth, Claude Max subscription).
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

from fastmcp import FastMCP

# ─── Module constants ────────────────────────────────────────────────────────
# Auto-detect plugin root: ${CLAUDE_PLUGIN_ROOT} (set by Claude Code) → __file__ parent → cwd.
PLUGIN_ROOT = Path(
    os.environ.get("CLAUDE_PLUGIN_ROOT")
    or os.environ.get("DESKTOP_ACT_ROOT")
    or Path(__file__).resolve().parent.parent
)
LOG_DIR = Path(os.environ.get("DESKTOP_ACT_LOG_DIR", PLUGIN_ROOT / "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
TMP_ROOT = Path(os.environ.get("DESKTOP_ACT_TMP", "/tmp"))
SHOT_DIR = TMP_ROOT / "desktop-act-shots"
SHOT_DIR.mkdir(parents=True, exist_ok=True)
POOL_PATH = TMP_ROOT / "desktop-act-pool.json"
POOL_LOCK = TMP_ROOT / "desktop-act-pool.lock"
SESSIONS_DIR = TMP_ROOT / "desktop-act-sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

IS_MAC: bool = platform.system().lower() == "darwin"

DEFAULT_DISPLAY: str = os.environ.get(
    "DESKTOP_ACT_DISPLAY", "mac:main" if IS_MAC else ":1"
)
DISPLAY_NUM_MIN: int = int(os.environ.get("DESKTOP_ACT_DISPLAY_MIN", "50"))
DISPLAY_NUM_MAX: int = int(os.environ.get("DESKTOP_ACT_DISPLAY_MAX", "99"))
VNC_PORT_BASE: int = int(os.environ.get("DESKTOP_ACT_VNC_PORT_BASE", "5900"))
NOVNC_PORT_BASE: int = int(os.environ.get("DESKTOP_ACT_NOVNC_PORT_BASE", "6082"))
DEFAULT_GEOMETRY: str = os.environ.get("DESKTOP_ACT_GEOMETRY", "1280x800")
# macOS single-session id (stable across the process lifetime).
MAC_SESSION_ID: str = "mac-main"
MAC_DISPLAY: str = "mac:main"
MAC_VNC_PORT: int = int(os.environ.get("DESKTOP_ACT_MAC_VNC_PORT", "5900"))
MAC_NOVNC_PORT: int = int(os.environ.get("DESKTOP_ACT_MAC_NOVNC_PORT", "6080"))
# The macOS system-framework Python always ships Quartz/pyobjc, which the venv
# (requirements.txt) does not. We shell out to it to synthesize scroll-wheel
# CGEvents — AppleScript has no working scroll verb.
MAC_SYSTEM_PYTHON: str = os.environ.get("DESKTOP_ACT_MAC_PYTHON", "/usr/bin/python3")
# Posts `amount` line-unit scroll-wheel events. argv: <dy> <dx>.
_MAC_SCROLL_PY = (
    "import sys\n"
    "from Quartz import (CGEventCreateScrollWheelEvent, CGEventPost, "
    "kCGHIDEventTap, kCGScrollEventUnitLine)\n"
    "dy, dx = int(sys.argv[1]), int(sys.argv[2])\n"
    "n = max(abs(dy), abs(dx)) or 1\n"
    "sy = (1 if dy > 0 else -1) if dy else 0\n"
    "sx = (1 if dx > 0 else -1) if dx else 0\n"
    "for _ in range(n):\n"
    "    ev = CGEventCreateScrollWheelEvent(None, kCGScrollEventUnitLine, 2, sy, sx)\n"
    "    CGEventPost(kCGHIDEventTap, ev)\n"
)
# Locate `claude` CLI: env override → PATH lookup → ~/.local/bin → ~/.claude/local.
_claude_default = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
CLAUDE_CLI = Path(os.environ.get("CLAUDE_CLI_PATH", _claude_default))

# ─── Logging (stderr — visible in FastMCP startup) ───────────────────────────
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[desktop-act] %(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("desktop-act")


# ─── Binary discovery (cached at startup) ────────────────────────────────────
def _which(*names: str) -> Optional[str]:
    """Return the first existing path among candidates."""
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


BIN: dict[str, Optional[str]] = {
    "Xvnc": _which("Xvnc"),
    "vncserver": _which("vncserver"),
    "websockify": _which("websockify"),
    "x11vnc": _which("x11vnc"),
    # WM preference order: openbox → fluxbox → metacity → mutter
    "wm": _which("openbox", "fluxbox", "metacity", "mutter"),
    "xterm": _which("xterm"),
    "xdotool": _which("xdotool"),
    # macOS system binaries (no-op on Linux).
    "cliclick": _which(
        "cliclick", "/opt/homebrew/bin/cliclick", "/usr/local/bin/cliclick"
    ),
    "screencapture": _which("screencapture", "/usr/sbin/screencapture"),
    "osascript": _which("osascript", "/usr/bin/osascript"),
    "open": _which("open", "/usr/bin/open"),
}
log.info("binaries: %s", {k: v for k, v in BIN.items() if v})
if IS_MAC:
    _missing_critical = [k for k in ("cliclick", "screencapture") if BIN[k] is None]
    if _missing_critical:
        log.warning(
            "missing critical macOS binaries: %s — primitives will fail "
            "(brew install cliclick)",
            _missing_critical,
        )
else:
    _missing_critical = [k for k in ("Xvnc", "websockify") if BIN[k] is None]
    if _missing_critical:
        log.warning(
            "missing critical binaries: %s — pool spawn will fail", _missing_critical
        )


# ─── Pool persistence (file-locked) ──────────────────────────────────────────
def _read_pool() -> dict[str, dict]:
    if not POOL_PATH.exists():
        return {}
    try:
        return json.loads(POOL_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write_pool(pool: dict[str, dict]) -> None:
    POOL_PATH.write_text(json.dumps(pool, indent=2, sort_keys=True))


class _PoolLock:
    """Cross-process flock guard for pool mutations."""

    def __enter__(self):
        self.fd = POOL_LOCK.open("w")
        fcntl.flock(self.fd.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fd.fileno(), fcntl.LOCK_UN)
        finally:
            self.fd.close()


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    """True if something is listening on host:port (connect succeeds)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False


def _display_in_use(display_num: int) -> bool:
    return Path(f"/tmp/.X{display_num}-lock").exists() or not _port_free(
        VNC_PORT_BASE + display_num
    )


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


# ─── SHA-dedupe screenshot cache (shared across backends) ────────────────────
_LAST_SHOT: dict[str, dict] = {}


def _encode_screenshot(
    img: Any,
    display: str,
    grab_ms: int,
    fmt: str,
    max_width: int,
    use_cache: bool,
) -> dict:
    """Shared PIL pipeline: SHA-dedupe, optional downscale, save, cache. Returns metadata.

    `img` must be a PIL RGB Image already grabbed by the backend.
    """
    from PIL import Image

    w, h = img.width, img.height
    sha = hashlib.sha256(img.tobytes()).hexdigest()[:16]

    if use_cache:
        prev = _LAST_SHOT.get(display)
        if prev and prev["sha"] == sha and Path(prev["path"]).exists():
            return {**prev, "cached": True, "grab_ms": grab_ms}

    if max_width and w > max_width:
        new_h = int(h * max_width / w)
        img = img.resize((max_width, new_h), Image.BILINEAR)

    ext = "jpg" if fmt == "jpeg" else "png"
    out = str(SHOT_DIR / f"shot-{sha[:8]}.{ext}")
    t1 = time.time()
    if fmt == "jpeg":
        img.save(out, "JPEG", quality=80, optimize=True)
    else:
        img.save(out, "PNG", optimize=False)
    save_ms = int((time.time() - t1) * 1000)

    result = {
        "path": out,
        "width": img.width,
        "height": img.height,
        "sha": sha,
        "cached": False,
        "grab_ms": grab_ms,
        "save_ms": save_ms,
    }
    _LAST_SHOT[display] = result
    return result


# ─── Backend protocol ────────────────────────────────────────────────────────
@runtime_checkable
class Backend(Protocol):
    """OS-agnostic desktop primitive surface.

    Methods mirror the @mcp.tool primitives 1:1. All return the same dict shapes
    the tools expose so callers never need to know which backend is active.
    Async to allow non-blocking subprocess / sleep within primitives.
    """

    name: str

    async def screenshot(
        self,
        session_id: str,
        region: str,
        fmt: str,
        max_width: int,
        use_cache: bool,
    ) -> dict: ...

    async def click(
        self, x: int, y: int, button: int, double: bool, session_id: str
    ) -> dict: ...

    async def type_text(self, text: str, session_id: str, delay_ms: int) -> dict: ...

    async def keypress(
        self, key: str, modifiers: Optional[list[str]], session_id: str
    ) -> dict: ...

    async def scroll(
        self, direction: str, amount: int, x: int, y: int, session_id: str
    ) -> dict: ...

    async def list_windows(self, session_id: str) -> dict: ...

    async def launch_app(self, command: str, session_id: str) -> dict: ...

    async def acquire_desktop(self, geometry: str) -> dict: ...

    async def release_desktop(self, session_id: str) -> dict: ...

    async def list_desktops(self) -> dict: ...


# ─── X11 backend (Linux) ─────────────────────────────────────────────────────
class X11Backend:
    """X11/Xvnc backend. Behavior is byte-for-byte identical to the original server."""

    name = "x11"

    def __init__(self) -> None:
        # Persistent X11 display connection cache (lazy-init per process).
        self._displays: dict[str, Any] = {}

    # ── X11 connection helpers ────────────────────────────────────────────
    def _get_display(self, name: str) -> Any:
        """Return a long-lived Xlib display, lazy-init per process."""
        from Xlib import display as xd

        d = self._displays.get(name)
        if d is None:
            d = xd.Display(name)
            self._displays[name] = d
        return d

    def _resolve_display(self, session_id: str = "") -> str:
        """Resolve a session_id to a display name. Empty → DEFAULT_DISPLAY."""
        if not session_id:
            return DEFAULT_DISPLAY
        pool = _read_pool()
        sess = pool.get(session_id)
        if not sess:
            raise ValueError(f"unknown session_id: {session_id}")
        return sess["display"]

    def _take_screenshot(
        self,
        display: str,
        region: str = "",
        fmt: str = "jpeg",
        max_width: int = 0,
        use_cache: bool = True,
    ) -> dict:
        """Grab screen, dedupe by SHA, optional downscale, return path + metadata."""
        from PIL import Image
        from Xlib import X

        d = self._get_display(display)
        root = d.screen().root
        g = root.get_geometry()
        if region:
            x, y, w, h = (int(v) for v in region.split(","))
        else:
            x, y, w, h = 0, 0, g.width, g.height
        t0 = time.time()
        raw = root.get_image(x, y, w, h, X.ZPixmap, 0xFFFFFFFF)
        grab_ms = int((time.time() - t0) * 1000)

        img = Image.frombytes("RGB", (w, h), raw.data, "raw", "BGRX")
        return _encode_screenshot(img, display, grab_ms, fmt, max_width, use_cache)

    # ── Primitives ────────────────────────────────────────────────────────
    async def screenshot(self, session_id, region, fmt, max_width, use_cache) -> dict:
        display = self._resolve_display(session_id)
        r = self._take_screenshot(display, region, fmt, max_width, use_cache)
        return {"ok": True, "display": display, **r}

    async def click(self, x, y, button, double, session_id) -> dict:
        from Xlib import X
        from Xlib.ext.xtest import fake_input

        display = self._resolve_display(session_id)
        d = self._get_display(display)
        d.screen().root.warp_pointer(x, y)
        d.sync()
        for _ in range(2 if double else 1):
            fake_input(d, X.ButtonPress, button)
            fake_input(d, X.ButtonRelease, button)
            d.sync()
            if double:
                await asyncio.sleep(0.05)
        return {
            "ok": True,
            "x": x,
            "y": y,
            "button": button,
            "double": double,
            "display": display,
        }

    async def type_text(self, text, session_id, delay_ms) -> dict:
        from Xlib import X, XK
        from Xlib.ext.xtest import fake_input

        display = self._resolve_display(session_id)
        d = self._get_display(display)
        shift_kc = d.keysym_to_keycode(XK.XK_Shift_L)
        for ch in text:
            ks = XK.string_to_keysym(ch) or XK.string_to_keysym(
                {" ": "space", "\n": "Return", "\t": "Tab"}.get(ch, ch)
            )
            if not ks:
                continue
            kc = d.keysym_to_keycode(ks)
            shift = ch.isupper() or ch in '~!@#$%^&*()_+{}|:"<>?'
            if shift:
                fake_input(d, X.KeyPress, shift_kc)
            fake_input(d, X.KeyPress, kc)
            fake_input(d, X.KeyRelease, kc)
            if shift:
                fake_input(d, X.KeyRelease, shift_kc)
            d.sync()
            if delay_ms:
                await asyncio.sleep(delay_ms / 1000)
        return {"ok": True, "chars": len(text), "display": display}

    async def keypress(self, key, modifiers, session_id) -> dict:
        from Xlib import X, XK
        from Xlib.ext.xtest import fake_input

        display = self._resolve_display(session_id)
        d = self._get_display(display)
        mods = modifiers or []
        mcodes = [d.keysym_to_keycode(XK.string_to_keysym(m + "_L")) for m in mods]
        kc = d.keysym_to_keycode(XK.string_to_keysym(key))
        for m in mcodes:
            fake_input(d, X.KeyPress, m)
        fake_input(d, X.KeyPress, kc)
        fake_input(d, X.KeyRelease, kc)
        for m in reversed(mcodes):
            fake_input(d, X.KeyRelease, m)
        d.sync()
        return {"ok": True, "key": key, "modifiers": mods, "display": display}

    async def scroll(self, direction, amount, x, y, session_id) -> dict:
        from Xlib import X
        from Xlib.ext.xtest import fake_input

        display = self._resolve_display(session_id)
        d = self._get_display(display)
        if x >= 0 and y >= 0:
            d.screen().root.warp_pointer(x, y)
            d.sync()
        btn = {"up": 4, "down": 5, "left": 6, "right": 7}.get(direction.lower(), 5)
        for _ in range(amount):
            fake_input(d, X.ButtonPress, btn)
            fake_input(d, X.ButtonRelease, btn)
            d.sync()
            if amount > 1:
                await asyncio.sleep(0.03)
        return {
            "ok": True,
            "direction": direction,
            "amount": amount,
            "display": display,
        }

    async def list_windows(self, session_id) -> dict:
        display = self._resolve_display(session_id)
        d = self._get_display(display)
        wins = []
        for w in d.screen().root.query_tree().children:
            try:
                name = w.get_wm_name()
                g = w.get_geometry()
                if name:
                    wins.append(
                        {"name": name, "x": g.x, "y": g.y, "w": g.width, "h": g.height}
                    )
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "windows": wins, "display": display}

    async def launch_app(self, command, session_id) -> dict:
        display = self._resolve_display(session_id)
        env = {**os.environ, "DISPLAY": display}
        proc = await asyncio.create_subprocess_shell(
            f"{command} &",
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.sleep(1.5)
        return {"ok": True, "command": command, "pid": proc.pid, "display": display}

    # ── Pool ──────────────────────────────────────────────────────────────
    def _spawn_desktop(self, display_num: int, geometry: str) -> dict:
        """Synchronously spawn Xvnc + WM + websockify. Returns session record. Blocking."""
        if not BIN["Xvnc"]:
            raise RuntimeError("Xvnc not installed")
        if not BIN["websockify"]:
            raise RuntimeError("websockify not installed")

        display = f":{display_num}"
        vnc_port = VNC_PORT_BASE + display_num
        novnc_port = NOVNC_PORT_BASE + (display_num - DISPLAY_NUM_MIN)
        session_id = f"desk-{uuid.uuid4().hex[:8]}"
        log_path = SESSIONS_DIR / f"{session_id}.log"
        log_fh = log_path.open("a")

        # Xvnc — no auth (loopback-bound), depth 24
        xvnc_cmd = [
            BIN["Xvnc"],
            display,
            "-geometry",
            geometry,
            "-depth",
            "24",
            "-SecurityTypes",
            "None",
            "-AlwaysShared",
            "-rfbport",
            str(vnc_port),
            "-interface",
            "127.0.0.1",
        ]
        xvnc = subprocess.Popen(
            xvnc_cmd,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

        # Wait up to 5s for X server to come up
        ready = False
        for _ in range(50):
            if Path(f"/tmp/.X{display_num}-lock").exists() and not _port_free(vnc_port):
                ready = True
                break
            if xvnc.poll() is not None:
                raise RuntimeError(
                    f"Xvnc exited early (code={xvnc.returncode}) — see {log_path}"
                )
            time.sleep(0.1)
        if not ready:
            xvnc.terminate()
            raise RuntimeError(
                f"Xvnc :{display_num} did not become ready within 5s — see {log_path}"
            )

        # Spawn WM if available (best-effort, non-fatal)
        wm_pid: Optional[int] = None
        if BIN["wm"]:
            try:
                wm = subprocess.Popen(
                    [BIN["wm"]],
                    env={**os.environ, "DISPLAY": display},
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                wm_pid = wm.pid
            except Exception as e:  # noqa: BLE001
                log.warning("WM spawn failed for %s: %s", display, e)

        # Spawn websockify (noVNC bridge)
        websockify = subprocess.Popen(
            [BIN["websockify"], str(novnc_port), f"127.0.0.1:{vnc_port}"],
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

        # Best-effort xterm so the screen isn't entirely empty
        if BIN["xterm"]:
            try:
                subprocess.Popen(
                    [BIN["xterm"], "-geometry", "80x24+10+10"],
                    env={**os.environ, "DISPLAY": display},
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except Exception:  # noqa: BLE001
                pass

        return {
            "session_id": session_id,
            "display": display,
            "display_num": display_num,
            "vnc_port": vnc_port,
            "novnc_port": novnc_port,
            "novnc_url": f"http://localhost:{novnc_port}",
            "geometry": geometry,
            "xvnc_pid": xvnc.pid,
            "wm_pid": wm_pid,
            "wm_bin": BIN["wm"],
            "websockify_pid": websockify.pid,
            "log_path": str(log_path),
            "created_at": int(time.time()),
        }

    async def acquire_desktop(self, geometry) -> dict:
        with _PoolLock():
            pool = _read_pool()
            used_nums = {s["display_num"] for s in pool.values()}
            chosen: Optional[int] = None
            for n in range(DISPLAY_NUM_MIN, DISPLAY_NUM_MAX + 1):
                if n in used_nums:
                    continue
                if _display_in_use(n):
                    continue
                chosen = n
                break
            if chosen is None:
                return {
                    "ok": False,
                    "error": f"no free display in :{DISPLAY_NUM_MIN}-:{DISPLAY_NUM_MAX}",
                }

            session = await asyncio.get_event_loop().run_in_executor(
                None, self._spawn_desktop, chosen, geometry
            )
            pool[session["session_id"]] = session
            _write_pool(pool)
            return {"ok": True, **session}

    async def release_desktop(self, session_id) -> dict:
        with _PoolLock():
            pool = _read_pool()
            sess = pool.get(session_id)
            if not sess:
                return {"ok": False, "error": f"unknown session_id: {session_id}"}

            # Close cached X11 connection
            d = self._displays.pop(sess["display"], None)
            if d is not None:
                try:
                    d.close()
                except Exception:  # noqa: BLE001
                    pass
            _LAST_SHOT.pop(sess["display"], None)

            killed: list[dict] = []
            for key in ("websockify_pid", "wm_pid", "xvnc_pid"):
                pid = sess.get(key)
                if pid and _pid_alive(pid):
                    try:
                        os.kill(pid, signal.SIGTERM)
                        killed.append({key: pid})
                    except OSError:
                        pass
            await asyncio.sleep(0.3)
            for key in ("websockify_pid", "wm_pid", "xvnc_pid"):
                pid = sess.get(key)
                if pid and _pid_alive(pid):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass

            # Cleanup X lock files
            for lock in (
                Path(f"/tmp/.X{sess['display_num']}-lock"),
                Path(f"/tmp/.X11-unix/X{sess['display_num']}"),
            ):
                try:
                    lock.unlink(missing_ok=True)
                except OSError:
                    pass

            del pool[session_id]
            _write_pool(pool)
            return {"ok": True, "released": session_id, "killed": killed}

    @staticmethod
    def _session_alive(sess: dict) -> bool:
        return _pid_alive(sess.get("xvnc_pid", 0))

    async def list_desktops(self) -> dict:
        pool = _read_pool()
        sessions = []
        for _sid, s in pool.items():
            sessions.append({**s, "alive": self._session_alive(s)})
        return {"ok": True, "count": len(sessions), "sessions": sessions}


# ─── macOS backend ───────────────────────────────────────────────────────────
class MacBackend:
    """macOS backend: screencapture + cliclick/osascript driving the one logged-in session.

    There is exactly one user desktop on macOS, so there is no Xvnc pool. acquire/list/
    release operate on a single stable session bound to the real display (mac:main), and
    release_desktop only tears down the optional websockify/VNC bridge — never the user
    session. Screen Sharing (built-in VNC, port 5900) is bridged to noVNC via websockify
    when available; if Screen Sharing is off, the session is still returned with a hint.
    """

    name = "mac"

    # button → (single-click verb, double-click verb). macOS/cliclick has no
    # native middle-click, so button=2 is reported as unsupported rather than
    # silently mis-mapped to a right-click. button 4/5 are scroll, handled in click().
    _BTN = {1: ("c", "dc"), 3: ("rc", "rc")}

    def __init__(self) -> None:
        self._bridge_pid: Optional[int] = None

    # ── helpers ───────────────────────────────────────────────────────────
    def _resolve_display(self, session_id: str = "") -> str:
        """macOS has exactly one session. Empty or MAC_SESSION_ID → the real display;
        any other id is rejected (there is no pool to look it up in)."""
        if not session_id or session_id == MAC_SESSION_ID:
            return MAC_DISPLAY
        raise ValueError(
            f"unknown session_id: {session_id} "
            f"(macOS has a single session: {MAC_SESSION_ID!r})"
        )

    async def _run(self, *argv: str) -> tuple[int, str, str]:
        """Run a system binary directly (no shell), return (rc, stdout, stderr)."""
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        return (
            proc.returncode or 0,
            out.decode(errors="replace"),
            err.decode(errors="replace"),
        )

    def _cliclick(self) -> str:
        c = BIN["cliclick"]
        if not c:
            raise RuntimeError("cliclick not installed — run: brew install cliclick")
        return c

    # ── primitives ────────────────────────────────────────────────────────
    async def screenshot(self, session_id, region, fmt, max_width, use_cache) -> dict:
        from PIL import Image

        display = self._resolve_display(session_id)
        if not BIN["screencapture"]:
            raise RuntimeError("screencapture not found")
        ext = "jpg" if fmt == "jpeg" else "png"
        cap_fmt = "jpg" if fmt == "jpeg" else "png"
        raw_path = SHOT_DIR / f"raw-{uuid.uuid4().hex[:8]}.{ext}"
        # -x silent, -t format, capture main display (default).
        argv = ["-x", "-t", cap_fmt]
        if region:
            x, y, w, h = (int(v) for v in region.split(","))
            argv += ["-R", f"{x},{y},{w},{h}"]
        argv.append(str(raw_path))
        t0 = time.time()
        rc, _out, err = await self._run(BIN["screencapture"], *argv)
        grab_ms = int((time.time() - t0) * 1000)
        if rc != 0 or not raw_path.exists():
            raise RuntimeError(f"screencapture failed (rc={rc}): {err.strip()}")
        try:
            img = Image.open(raw_path).convert("RGB")
            r = _encode_screenshot(img, display, grab_ms, fmt, max_width, use_cache)
        finally:
            try:
                raw_path.unlink(missing_ok=True)
            except OSError:
                pass
        return {"ok": True, "display": display, **r}

    async def click(self, x, y, button, double, session_id) -> dict:
        display = self._resolve_display(session_id)
        cc = self._cliclick()
        # 4/5 scroll buttons → delegate to scroll for X11 parity.
        if button in (4, 5):
            return await self.scroll(
                "up" if button == 4 else "down", 1, x, y, session_id
            )
        verbs = self._BTN.get(button)
        if verbs is None:
            return {
                "ok": False,
                "error": f"button {button} unsupported on macOS "
                "(no middle-click; use 1=left or 3=right)",
                "x": x,
                "y": y,
                "button": button,
                "double": double,
                "display": display,
            }
        # cliclick has no double-right verb, so synthesize it with two right-clicks.
        if double and button == 3:
            rc, _out, err = await self._run(cc, f"rc:{x},{y}", f"rc:{x},{y}")
        else:
            prefix = verbs[1] if double else verbs[0]
            rc, _out, err = await self._run(cc, f"{prefix}:{x},{y}")
        if rc != 0:
            raise RuntimeError(f"cliclick failed (rc={rc}): {err.strip()}")
        return {
            "ok": True,
            "x": x,
            "y": y,
            "button": button,
            "double": double,
            "display": display,
        }

    async def type_text(self, text, session_id, delay_ms) -> dict:
        display = self._resolve_display(session_id)
        cc = self._cliclick()
        # cliclick t: types literally; w:<ms> sets inter-keystroke wait.
        args = []
        if delay_ms:
            args.append(f"w:{delay_ms}")
        args.append(f"t:{text}")
        rc, _out, err = await self._run(cc, *args)
        if rc != 0:
            raise RuntimeError(f"cliclick failed (rc={rc}): {err.strip()}")
        return {"ok": True, "chars": len(text), "display": display}

    async def keypress(self, key, modifiers, session_id) -> dict:
        display = self._resolve_display(session_id)
        cc = self._cliclick()
        mods = modifiers or []
        # Map X11-style modifier names → cliclick kd/ku modifier names.
        mod_map = {
            "control": "ctrl",
            "ctrl": "ctrl",
            "alt": "alt",
            "option": "alt",
            "meta": "cmd",
            "super": "cmd",
            "command": "cmd",
            "cmd": "cmd",
            "shift": "shift",
        }
        cmods = [mod_map.get(m.lower(), m.lower()) for m in mods]
        # Map common X11 keysyms → cliclick key-press tokens (kp:).
        key_map = {
            "Return": "return",
            "Enter": "return",
            "Tab": "tab",
            "Escape": "esc",
            "space": "space",
            "BackSpace": "delete",
            "Delete": "fwd-delete",
            "Up": "arrow-up",
            "Down": "arrow-down",
            "Left": "arrow-left",
            "Right": "arrow-right",
            "Home": "home",
            "End": "end",
            "Page_Up": "page-up",
            "Page_Down": "page-down",
        }
        args: list[str] = []
        for m in cmods:
            args.append(f"kd:{m}")
        tok = key_map.get(key)
        if tok:
            args.append(f"kp:{tok}")
        elif len(key) == 1:
            # single character → type it (with modifiers held, e.g. cmd+a).
            args.append(f"t:{key}")
        else:
            args.append(f"kp:{key.lower()}")
        for m in reversed(cmods):
            args.append(f"ku:{m}")
        rc, _out, err = await self._run(cc, *args)
        if rc != 0:
            raise RuntimeError(f"cliclick failed (rc={rc}): {err.strip()}")
        return {"ok": True, "key": key, "modifiers": mods, "display": display}

    async def scroll(self, direction, amount, x, y, session_id) -> dict:
        display = self._resolve_display(session_id)
        # Optionally warp the pointer first (cliclick m:) so the scroll lands there.
        if x >= 0 and y >= 0 and BIN["cliclick"]:
            await self._run(BIN["cliclick"], f"m:{x},{y}")
        # Synthesize line-unit scroll-wheel CGEvents. AppleScript "System Events"
        # has no working `scroll` verb (errors -1708), so post the event via the
        # macOS system framework Python (always ships Quartz/pyobjc) — no new dep.
        # Wheel deltas: +y scrolls up, +x scrolls left.
        d = direction.lower()
        dy = dx = 0
        if d == "up":
            dy = amount
        elif d == "down":
            dy = -amount
        elif d == "left":
            dx = amount
        elif d == "right":
            dx = -amount
        else:
            dy = -amount
        rc, _out, err = await self._run(
            MAC_SYSTEM_PYTHON, "-c", _MAC_SCROLL_PY, str(dy), str(dx)
        )
        if rc != 0:
            raise RuntimeError(f"scroll failed (rc={rc}): {err.strip()}")
        return {
            "ok": True,
            "direction": direction,
            "amount": amount,
            "display": display,
        }

    async def list_windows(self, session_id) -> dict:
        display = self._resolve_display(session_id)
        if not BIN["osascript"]:
            raise RuntimeError("osascript not found")
        # Enumerate visible app windows + bounds via System Events; tab-delimited rows.
        script = (
            'set out to ""\n'
            'tell application "System Events"\n'
            "  repeat with p in (every process whose visible is true)\n"
            "    repeat with w in (every window of p)\n"
            "      try\n"
            "        set pos to position of w\n"
            "        set sz to size of w\n"
            '        set out to out & (name of p) & "\\t" & (name of w) & "\\t" & '
            '(item 1 of pos) & "\\t" & (item 2 of pos) & "\\t" & '
            '(item 1 of sz) & "\\t" & (item 2 of sz) & "\\n"\n'
            "      end try\n"
            "    end repeat\n"
            "  end repeat\n"
            "end tell\n"
            "return out"
        )
        rc, out, err = await self._run(BIN["osascript"], "-e", script)
        if rc != 0:
            # Accessibility not granted, or transient — degrade gracefully like X11.
            log.warning("list_windows osascript failed (rc=%s): %s", rc, err.strip())
            return {"ok": True, "windows": [], "display": display}
        wins: list[dict] = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) != 6:
                continue
            app, wname, px, py, sw, sh = parts
            try:
                wins.append(
                    {
                        "name": f"{app}: {wname}" if wname else app,
                        "x": int(px),
                        "y": int(py),
                        "w": int(sw),
                        "h": int(sh),
                    }
                )
            except ValueError:
                continue
        return {"ok": True, "windows": wins, "display": display}

    async def launch_app(self, command, session_id) -> dict:
        display = self._resolve_display(session_id)
        if not BIN["open"]:
            raise RuntimeError("open not found")
        # Heuristic: a bare token → treat as app name (open -a); else open the path/URL.
        if " " not in command and "/" not in command:
            argv = [BIN["open"], "-a", command]
        else:
            argv = [BIN["open"], command]
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _out, err = await proc.communicate()
        await asyncio.sleep(1.5)
        if proc.returncode != 0:
            return {
                "ok": False,
                "error": f"open failed: {err.decode(errors='replace').strip()}",
                "command": command,
                "display": display,
            }
        return {"ok": True, "command": command, "pid": proc.pid, "display": display}

    # ── single-session "pool" ────────────────────────────────────────────
    async def _start_bridge(self) -> dict:
        """Bridge built-in Screen Sharing (VNC :5900) → noVNC via websockify if possible.

        Never fails the acquire. Returns fields merged into the session record.
        """
        vnc_up = _port_open(MAC_VNC_PORT)
        extra: dict[str, Any] = {
            "vnc_port": MAC_VNC_PORT,
            "screen_sharing_enabled": vnc_up,
        }
        if not vnc_up:
            extra["vnc_hint"] = (
                "macOS Screen Sharing (VNC) is off — enable it in System Settings → "
                "General → Sharing → Screen Sharing to get a live noVNC view."
            )
            return extra
        if not BIN["websockify"]:
            extra["vnc_hint"] = (
                "Screen Sharing is on but websockify is missing — "
                "pip install websockify (or brew install websockify) for a noVNC bridge."
            )
            return extra
        if self._bridge_pid and _pid_alive(self._bridge_pid):
            extra["websockify_pid"] = self._bridge_pid
            extra["novnc_port"] = MAC_NOVNC_PORT
            extra["novnc_url"] = f"http://localhost:{MAC_NOVNC_PORT}"
            return extra
        if not _port_free(MAC_NOVNC_PORT):
            # Something already bridging on this port — reuse it.
            extra["novnc_port"] = MAC_NOVNC_PORT
            extra["novnc_url"] = f"http://localhost:{MAC_NOVNC_PORT}"
            return extra
        log_path = SESSIONS_DIR / f"{MAC_SESSION_ID}.log"
        log_fh = log_path.open("a")
        ws = subprocess.Popen(
            [BIN["websockify"], str(MAC_NOVNC_PORT), f"127.0.0.1:{MAC_VNC_PORT}"],
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self._bridge_pid = ws.pid
        extra["websockify_pid"] = ws.pid
        extra["novnc_port"] = MAC_NOVNC_PORT
        extra["novnc_url"] = f"http://localhost:{MAC_NOVNC_PORT}"
        extra["log_path"] = str(log_path)
        return extra

    def _session_record(self, geometry: str, bridge: dict) -> dict:
        return {
            "session_id": MAC_SESSION_ID,
            "display": MAC_DISPLAY,
            "geometry": geometry,
            "created_at": int(time.time()),
            "novnc_url": bridge.get("novnc_url"),
            **bridge,
        }

    @staticmethod
    def _session_alive(sess: dict) -> bool:
        """A mac session is live iff its real display is up (VNC reachable) — or, when
        Screen Sharing is off, the bridge port we recorded for it. Falls back to the
        VNC port for the canonical session."""
        if sess.get("session_id") == MAC_SESSION_ID:
            return _port_open(MAC_VNC_PORT)
        ws = sess.get("websockify_pid")
        if ws:
            return _pid_alive(ws)
        return False

    async def acquire_desktop(self, geometry) -> dict:
        # macOS: one real session. Persist a stable record; bridge VNC best-effort.
        bridge = await self._start_bridge()
        session = self._session_record(geometry, bridge)
        with _PoolLock():
            pool = _read_pool()
            # Pool hygiene: the shared /tmp pool may carry stale X11 (desk-*) entries
            # from a prior boot/OS. On mac, the pool must contain only mac-main.
            pool = {MAC_SESSION_ID: session}
            _write_pool(pool)
        return {"ok": True, **session}

    async def release_desktop(self, session_id) -> dict:
        # Only tear down the optional websockify bridge — never the user's session.
        with _PoolLock():
            pool = _read_pool()
            sess = pool.get(session_id)
            if not sess:
                return {"ok": False, "error": f"unknown session_id: {session_id}"}
            killed: list[dict] = []
            pid = sess.get("websockify_pid") or self._bridge_pid
            if pid and _pid_alive(pid):
                try:
                    os.kill(pid, signal.SIGTERM)
                    killed.append({"websockify_pid": pid})
                except OSError:
                    pass
            self._bridge_pid = None
            _LAST_SHOT.pop(sess["display"], None)
            pool.pop(session_id, None)
            _write_pool(pool)
            return {
                "ok": True,
                "released": session_id,
                "killed": killed,
                "note": "macOS user session is never terminated — only the noVNC bridge.",
            }

    async def list_desktops(self) -> dict:
        # Only ever surface the single mac session; drop any stale X11 entries.
        pool = _read_pool()
        sessions = []
        for _sid, s in pool.items():
            if s.get("session_id") != MAC_SESSION_ID:
                continue
            sessions.append({**s, "alive": self._session_alive(s)})
        return {"ok": True, "count": len(sessions), "sessions": sessions}


# ─── Backend dispatch ─────────────────────────────────────────────────────────
BACKEND: Backend = MacBackend() if IS_MAC else X11Backend()
log.info("active backend: %s (platform=%s)", BACKEND.name, platform.system())


def _resolve_display(session_id: str = "") -> str:
    """Module-level convenience used by compound tools — delegates to the backend."""
    return BACKEND._resolve_display(session_id)  # type: ignore[attr-defined]


# ─── FastMCP instance ────────────────────────────────────────────────────────
mcp = FastMCP(
    name="desktop-act",
    instructions=(
        "Computer-use primitives + multi-desktop pool + autonomous act() loop. "
        "OS-agnostic: X11/Xvnc pool on Linux, screencapture + cliclick on macOS. "
        "Call acquire_desktop() for a session; pass session_id to primitives. "
        "Optimized: SHA-dedupe screenshot cache, JPEG default."
    ),
)


# ─── Primitive tools (delegate to BACKEND) ───────────────────────────────────
@mcp.tool
async def screenshot(
    session_id: str = "",
    region: str = "",
    fmt: str = "jpeg",
    max_width: int = 1280,
    use_cache: bool = True,
) -> dict:
    """Capture the desktop with SHA dedupe cache + optional downscale.

    Args:
      session_id: pool session (empty → default desktop).
      region: "x,y,w,h" sub-rect (empty → full screen).
      fmt: 'jpeg' (default, smaller/faster) or 'png' (lossless).
      max_width: downscale width preserving aspect; 0 = full res.
      use_cache: skip resave when SHA matches previous frame.
    """
    try:
        return await BACKEND.screenshot(session_id, region, fmt, max_width, use_cache)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool
async def click(
    x: int, y: int, button: int = 1, double: bool = False, session_id: str = ""
) -> dict:
    """Click at absolute coords. button: 1=L 2=M 3=R 4=scrollUp 5=scrollDown."""
    try:
        return await BACKEND.click(x, y, button, double, session_id)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool
async def type_text(text: str, session_id: str = "", delay_ms: int = 5) -> dict:
    """Type a string into the focused window. Default delay_ms=5."""
    try:
        return await BACKEND.type_text(text, session_id, delay_ms)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool
async def keypress(
    key: str, modifiers: Optional[list[str]] = None, session_id: str = ""
) -> dict:
    """Press a key chord. e.g. key='Return', modifiers=['Control']."""
    try:
        return await BACKEND.keypress(key, modifiers, session_id)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool
async def scroll(
    direction: str, amount: int = 3, x: int = -1, y: int = -1, session_id: str = ""
) -> dict:
    """Scroll up|down|left|right. amount=clicks. Optional warp to x,y first."""
    try:
        return await BACKEND.scroll(direction, amount, x, y, session_id)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool
async def list_windows(session_id: str = "") -> dict:
    """Enumerate top-level windows + geometry."""
    try:
        return await BACKEND.list_windows(session_id)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool
async def launch_app(command: str, session_id: str = "") -> dict:
    """Spawn a GUI app (Linux: 'chromium'/'xterm'; macOS: app name or path via open)."""
    try:
        return await BACKEND.launch_app(command, session_id)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool
async def batch(actions: list[dict], session_id: str = "") -> dict:
    """Execute multiple primitives in one MCP round-trip.

    Each action: {"op": "click"|"type_text"|"keypress"|"scroll"|"sleep_ms", ...args}
    """
    results: list[dict] = []
    try:
        for a in actions:
            op = a.get("op")
            if op == "click":
                r = await click(
                    a["x"],
                    a["y"],
                    a.get("button", 1),
                    a.get("double", False),
                    session_id,
                )
            elif op == "type_text":
                r = await type_text(a["text"], session_id, a.get("delay_ms", 5))
            elif op == "keypress":
                r = await keypress(a["key"], a.get("modifiers"), session_id)
            elif op == "scroll":
                r = await scroll(
                    a["direction"],
                    a.get("amount", 3),
                    a.get("x", -1),
                    a.get("y", -1),
                    session_id,
                )
            elif op == "sleep_ms":
                await asyncio.sleep(int(a.get("ms", 100)) / 1000)
                r = {"ok": True, "slept_ms": a.get("ms")}
            else:
                r = {"ok": False, "error": f"unknown op: {op}"}
            results.append(r)
            if not r.get("ok"):
                break
        return {
            "ok": all(x.get("ok") for x in results),
            "results": results,
            "count": len(results),
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "results": results}


# ─── Compound single-round-trip tools ────────────────────────────────────────
@mcp.tool
async def observe(
    session_id: str = "",
    screenshot_format: str = "jpeg",
    screenshot_max_width: int = 1280,
) -> dict:
    """Screenshot + window list in one round-trip (no action taken)."""
    from fastmcp.utilities.types import Image

    try:
        t0 = time.time()
        display = _resolve_display(session_id)
        windows = await list_windows(session_id)
        shot = await BACKEND.screenshot(
            session_id, "", screenshot_format, screenshot_max_width, True
        )
        return {
            "ok": True,
            "display": display,
            "screenshot": shot,
            "image": Image(path=shot["path"]).to_image_content(),
            "windows": windows.get("windows", []),
            "total_ms": int((time.time() - t0) * 1000),
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool
async def act_step(
    action: dict,
    settle_ms: int = 200,
    screenshot_format: str = "jpeg",
    screenshot_max_width: int = 1280,
    session_id: str = "",
) -> dict:
    """ONE round-trip: execute action → settle → screenshot → return inline image + hint.

    action: {"op":"click","x":...,"y":...} or {"op":"type_text","text":"..."} etc.
            Use {"op":"batch","actions":[...]} for multi-step sequences.
    """
    from fastmcp.utilities.types import Image

    try:
        t0 = time.time()
        display = _resolve_display(session_id)
        op = action.get("op", "noop")
        if op == "batch":
            action_result = await batch(action.get("actions", []), session_id)
        elif op == "click":
            action_result = await click(
                action["x"],
                action["y"],
                action.get("button", 1),
                action.get("double", False),
                session_id,
            )
        elif op == "type_text":
            action_result = await type_text(
                action["text"], session_id, action.get("delay_ms", 5)
            )
        elif op == "keypress":
            action_result = await keypress(
                action["key"], action.get("modifiers"), session_id
            )
        elif op == "scroll":
            action_result = await scroll(
                action["direction"],
                action.get("amount", 3),
                action.get("x", -1),
                action.get("y", -1),
                session_id,
            )
        elif op == "launch_app":
            action_result = await launch_app(action["command"], session_id)
        elif op == "noop":
            action_result = {"ok": True, "noop": True}
        else:
            return {"ok": False, "error": f"unknown action.op: {op}"}

        pre_windows = await list_windows(session_id)
        if settle_ms:
            await asyncio.sleep(settle_ms / 1000)
        post_windows = await list_windows(session_id)
        shot = await BACKEND.screenshot(
            session_id, "", screenshot_format, screenshot_max_width, True
        )

        pre_n = len(pre_windows.get("windows", []))
        post_n = len(post_windows.get("windows", []))
        delta = post_n - pre_n
        if delta > 0:
            hint = f"new window(s) appeared ({delta}) — likely menu/dialog/launched app"
        elif delta < 0:
            hint = f"window(s) closed ({-delta}) — action may have triggered close"
        elif shot.get("cached"):
            hint = "screen unchanged — action may not have taken effect; verify focus/selector"
        else:
            hint = "screen changed (no window delta) — focus/content change; reason from screenshot"

        return {
            "ok": action_result.get("ok", False),
            "action_result": action_result,
            "screenshot": shot,
            "image": Image(path=shot["path"]).to_image_content(),
            "window_count_delta": delta,
            "hint": hint,
            "display": display,
            "total_ms": int((time.time() - t0) * 1000),
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ─── Multi-desktop pool tools (delegate to BACKEND) ──────────────────────────
@mcp.tool
async def acquire_desktop(geometry: str = DEFAULT_GEOMETRY) -> dict:
    """Acquire a desktop session.

    Linux: spawns a fresh Xvnc + websockify on the next free display.
    macOS: returns the single real logged-in session (no Xvnc) bound to mac:main,
    bridging built-in Screen Sharing → noVNC when available.

    Returns {session_id, display, novnc_url, geometry, ...}. Pass session_id to primitives.
    """
    try:
        return await BACKEND.acquire_desktop(geometry)
    except Exception as e:  # noqa: BLE001
        log.exception("acquire_desktop failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool
async def release_desktop(session_id: str) -> dict:
    """Release a session.

    Linux: kill the Xvnc + WM + websockify and free its ports.
    macOS: tear down only the noVNC bridge — the user session is never terminated.
    """
    try:
        return await BACKEND.release_desktop(session_id)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool
async def list_desktops() -> dict:
    """Return all active sessions in the pool."""
    try:
        return await BACKEND.list_desktops()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ─── High-level act() — claude-agent-sdk OAuth loop ──────────────────────────
ACT_SYSTEM = """You are the desktop-act executor. You drive a desktop via the
desktop-act MCP primitives (screenshot, click, type_text, keypress, scroll, list_windows,
launch_app, observe, act_step, batch). The human watches via noVNC.

Loop tightly: observe → reason → act_step → check result → repeat. Stay concise.
Stop when the goal is complete or 3 consecutive actions fail."""


@mcp.tool
async def act(
    goal: str,
    max_iterations: int = 20,
    timeout_seconds: int = 600,
    model: str = "claude-sonnet-4-6",
    session_id: str = "",
) -> dict:
    """Autonomous goal execution via claude-agent-sdk (CLI OAuth, no API key).

    Args:
      goal: natural-language objective.
      max_iterations: turn cap for the inner loop.
      timeout_seconds: hard wall-clock cap.
      model: model id for the inner agent.
      session_id: target a specific pool session (empty → default desktop).
    """
    run_id = uuid.uuid4().hex[:10]
    log_path = LOG_DIR / f"act-{run_id}.log"
    start = time.time()

    try:
        from claude_agent_sdk import ClaudeAgentOptions, query
    except ImportError as e:
        return {
            "ok": False,
            "error": f"claude-agent-sdk not installed: {e}",
            "hint": "venv missing — re-run plugin install",
        }

    sys_prompt = ACT_SYSTEM
    if session_id:
        sys_prompt += f"\n\nTarget desktop session_id={session_id} for all primitives."

    opts = ClaudeAgentOptions(
        system_prompt=sys_prompt,
        permission_mode="bypassPermissions",
        max_turns=max_iterations,
        model=model,
    )

    chunks: list[str] = []
    try:

        async def _run():
            async for msg in query(prompt=goal, options=opts):
                chunks.append(str(msg))

        await asyncio.wait_for(_run(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        log_path.write_text(
            "\n---\n".join(chunks) + f"\n\n*** TIMEOUT after {timeout_seconds}s ***"
        )
        return {
            "ok": False,
            "error": f"timeout after {timeout_seconds}s",
            "turns_seen": len(chunks),
            "log_path": str(log_path),
        }
    except Exception as e:  # noqa: BLE001
        log_path.write_text(
            "\n---\n".join(chunks) + f"\n\n*** ERROR {type(e).__name__}: {e} ***"
        )
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "turns_seen": len(chunks),
            "log_path": str(log_path),
        }

    log_path.write_text("\n---\n".join(chunks))
    final = chunks[-1] if chunks else ""
    shots = list(
        dict.fromkeys(
            re.findall(
                r"/tmp/desktop-act-shots/[A-Za-z0-9_.-]+\.(?:png|jpg)",
                "\n".join(chunks),
            )
        )
    )

    return {
        "ok": True,
        "result": final[-3000:],
        "turns": len(chunks),
        "screenshots": [p for p in shots if Path(p).exists()],
        "log_path": str(log_path),
        "duration_seconds": int(time.time() - start),
        "backend": "claude-agent-sdk (OAuth/Max sub)",
        "session_id": session_id or None,
    }


# ─── Status ──────────────────────────────────────────────────────────────────
@mcp.tool
async def status() -> dict:
    """Report MCP health, active backend, binary discovery, and pool state."""
    try:
        import claude_agent_sdk

        sdk_ver = getattr(claude_agent_sdk, "__version__", "?")
        sdk_ok = True
    except ImportError:
        sdk_ok = False
        sdk_ver = "missing"

    pool = _read_pool()
    return {
        "ok": sdk_ok and CLAUDE_CLI.exists(),
        "platform": platform.system(),
        "backend": BACKEND.name,
        "claude_agent_sdk": sdk_ver,
        "claude_cli": str(CLAUDE_CLI) if CLAUDE_CLI.exists() else "missing",
        "auth_path": "OAuth via Claude Max subscription (no API key)",
        "default_display": DEFAULT_DISPLAY,
        "shot_dir": str(SHOT_DIR),
        "log_dir": str(LOG_DIR),
        "binaries": {k: v or "MISSING" for k, v in BIN.items()},
        "pool_size": len(pool),
        "pool_sessions": [
            {
                "session_id": s["session_id"],
                "display": s["display"],
                "novnc_url": s.get("novnc_url"),
                "geometry": s["geometry"],
                # Per-session liveness via the active backend (not a blanket probe).
                "alive": BACKEND._session_alive(s),  # type: ignore[attr-defined]
            }
            for s in pool.values()
            # On mac, only surface the canonical session — never stale X11 rows.
            if not IS_MAC or s.get("session_id") == MAC_SESSION_ID
        ],
        "display_range": "mac:main (single session)"
        if IS_MAC
        else f":{DISPLAY_NUM_MIN}-:{DISPLAY_NUM_MAX}",
    }


if __name__ == "__main__":
    log.info(
        "desktop-act MCP starting — backend=%s pool=%s default_display=%s",
        BACKEND.name,
        POOL_PATH,
        DEFAULT_DISPLAY,
    )
    mcp.run()
