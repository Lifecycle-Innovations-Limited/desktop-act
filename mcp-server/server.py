#!/usr/bin/env python3
"""desktop-act MCP — computer-use primitives + multi-desktop pool + autonomous act() loop.

Architecture:
  * Persistent X11 connection cache (one Xlib.Display per display name, reused).
  * Multi-desktop pool (Xvnc + websockify) with file-locked allocation in /tmp.
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
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

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

DEFAULT_DISPLAY: str = os.environ.get("DESKTOP_ACT_DISPLAY", ":1")
DISPLAY_NUM_MIN: int = int(os.environ.get("DESKTOP_ACT_DISPLAY_MIN", "50"))
DISPLAY_NUM_MAX: int = int(os.environ.get("DESKTOP_ACT_DISPLAY_MAX", "99"))
VNC_PORT_BASE: int = int(os.environ.get("DESKTOP_ACT_VNC_PORT_BASE", "5900"))
NOVNC_PORT_BASE: int = int(os.environ.get("DESKTOP_ACT_NOVNC_PORT_BASE", "6082"))
DEFAULT_GEOMETRY: str = os.environ.get("DESKTOP_ACT_GEOMETRY", "1280x800")
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
}
log.info("binaries: %s", {k: v for k, v in BIN.items() if v})
_missing_critical = [k for k in ("Xvnc", "websockify") if BIN[k] is None]
if _missing_critical:
    log.warning(
        "missing critical binaries: %s — pool spawn will fail", _missing_critical
    )


# ─── Persistent X11 display connection cache ─────────────────────────────────
_DISPLAYS: dict[str, Any] = {}
_LAST_SHOT: dict[str, dict] = {}


def _get_display(name: str) -> Any:
    """Return a long-lived Xlib display, lazy-init per process."""
    from Xlib import display as xd

    d = _DISPLAYS.get(name)
    if d is None:
        d = xd.Display(name)
        _DISPLAYS[name] = d
    return d


def _resolve_display(session_id: str = "") -> str:
    """Resolve a session_id to a display name. Empty → DEFAULT_DISPLAY."""
    if not session_id:
        return DEFAULT_DISPLAY
    pool = _read_pool()
    sess = pool.get(session_id)
    if not sess:
        raise ValueError(f"unknown session_id: {session_id}")
    return sess["display"]


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


# ─── X11 primitive helper ───────────────────────────────────────────────────
def _take_screenshot(
    display: str,
    region: str = "",
    fmt: str = "jpeg",
    max_width: int = 0,
    use_cache: bool = True,
) -> dict:
    """Grab screen, dedupe by SHA, optional downscale, return path + metadata."""
    from PIL import Image
    from Xlib import X

    d = _get_display(display)
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


# ─── FastMCP instance ────────────────────────────────────────────────────────
mcp = FastMCP(
    name="desktop-act",
    instructions=(
        "Computer-use primitives + multi-desktop pool + autonomous act() loop. "
        "Default desktop at http://<host>:6081 (noVNC). "
        "Call acquire_desktop() for an isolated session; pass session_id to primitives. "
        "Optimized: persistent X11 connections, SHA-dedupe cache, JPEG default."
    ),
)


# ─── Primitive tools ────────────────────────────────────────────────────────
@mcp.tool
async def screenshot(
    session_id: str = "",
    region: str = "",
    fmt: str = "jpeg",
    max_width: int = 1280,
    use_cache: bool = True,
) -> dict:
    """Capture the X11 desktop with SHA dedupe cache + optional downscale.

    Args:
      session_id: pool session (empty → default :1).
      region: "x,y,w,h" sub-rect (empty → full screen).
      fmt: 'jpeg' (default, smaller/faster) or 'png' (lossless).
      max_width: downscale width preserving aspect; 0 = full res.
      use_cache: skip resave when SHA matches previous frame.
    """
    try:
        display = _resolve_display(session_id)
        r = _take_screenshot(display, region, fmt, max_width, use_cache)
        return {"ok": True, "display": display, **r}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool
async def click(
    x: int, y: int, button: int = 1, double: bool = False, session_id: str = ""
) -> dict:
    """Click at absolute coords. button: 1=L 2=M 3=R 4=scrollUp 5=scrollDown."""
    try:
        from Xlib import X
        from Xlib.ext.xtest import fake_input

        display = _resolve_display(session_id)
        d = _get_display(display)
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
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool
async def type_text(text: str, session_id: str = "", delay_ms: int = 5) -> dict:
    """Type a string into the focused window. Default delay_ms=5."""
    try:
        from Xlib import X, XK
        from Xlib.ext.xtest import fake_input

        display = _resolve_display(session_id)
        d = _get_display(display)
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
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool
async def keypress(
    key: str, modifiers: Optional[list[str]] = None, session_id: str = ""
) -> dict:
    """Press a key chord. e.g. key='Return', modifiers=['Control']."""
    try:
        from Xlib import X, XK
        from Xlib.ext.xtest import fake_input

        display = _resolve_display(session_id)
        d = _get_display(display)
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
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool
async def scroll(
    direction: str, amount: int = 3, x: int = -1, y: int = -1, session_id: str = ""
) -> dict:
    """Scroll up|down|left|right. amount=clicks. Optional warp to x,y first."""
    try:
        from Xlib import X
        from Xlib.ext.xtest import fake_input

        display = _resolve_display(session_id)
        d = _get_display(display)
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
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool
async def list_windows(session_id: str = "") -> dict:
    """Enumerate top-level X11 windows + geometry."""
    try:
        display = _resolve_display(session_id)
        d = _get_display(display)
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
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool
async def launch_app(command: str, session_id: str = "") -> dict:
    """Spawn a GUI app on the display (e.g. 'chromium', 'xterm', 'firefox-esr')."""
    try:
        display = _resolve_display(session_id)
        env = {**os.environ, "DISPLAY": display}
        proc = await asyncio.create_subprocess_shell(
            f"{command} &",
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.sleep(1.5)
        return {"ok": True, "command": command, "pid": proc.pid, "display": display}
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
        shot = _take_screenshot(
            display, "", screenshot_format, screenshot_max_width, use_cache=True
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
        shot = _take_screenshot(
            display, "", screenshot_format, screenshot_max_width, use_cache=True
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


# ─── Multi-desktop pool ──────────────────────────────────────────────────────
def _spawn_desktop(display_num: int, geometry: str) -> dict:
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


@mcp.tool
async def acquire_desktop(geometry: str = DEFAULT_GEOMETRY) -> dict:
    """Spawn a fresh Xvnc + websockify on the next free display.

    Returns {session_id, display, vnc_port, novnc_port, novnc_url, geometry}.
    Pass the session_id to any primitive to target this isolated desktop.
    """
    try:
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
                None, _spawn_desktop, chosen, geometry
            )
            pool[session["session_id"]] = session
            _write_pool(pool)
            return {"ok": True, **session}
    except Exception as e:  # noqa: BLE001
        log.exception("acquire_desktop failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool
async def release_desktop(session_id: str) -> dict:
    """Kill the Xvnc + WM + websockify for a session and free its ports."""
    try:
        with _PoolLock():
            pool = _read_pool()
            sess = pool.get(session_id)
            if not sess:
                return {"ok": False, "error": f"unknown session_id: {session_id}"}

            # Close cached X11 connection
            d = _DISPLAYS.pop(sess["display"], None)
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
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool
async def list_desktops() -> dict:
    """Return all active sessions in the pool."""
    try:
        pool = _read_pool()
        sessions = []
        for _sid, s in pool.items():
            alive = _pid_alive(s.get("xvnc_pid", 0))
            sessions.append({**s, "alive": alive})
        return {"ok": True, "count": len(sessions), "sessions": sessions}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ─── High-level act() — claude-agent-sdk OAuth loop ──────────────────────────
ACT_SYSTEM = """You are the desktop-act executor. You drive an X11 desktop via the
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
      session_id: target a specific pool session (empty → default :1).
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
    """Report MCP health, binary discovery, and pool state."""
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
                "novnc_url": s["novnc_url"],
                "geometry": s["geometry"],
                "alive": _pid_alive(s.get("xvnc_pid", 0)),
            }
            for s in pool.values()
        ],
        "display_range": f":{DISPLAY_NUM_MIN}-:{DISPLAY_NUM_MAX}",
    }


if __name__ == "__main__":
    log.info(
        "desktop-act MCP starting — pool=%s default_display=%s",
        POOL_PATH,
        DEFAULT_DISPLAY,
    )
    mcp.run()
