"""WinBackend unit tests runnable on Linux CI (no Windows GUI required)."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

os.environ["DESKTOP_ACT_REAP_INTERVAL"] = "0"
os.environ["DESKTOP_ACT_RELEASE_ON_EXIT"] = "0"

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "mcp-server" / "server.py"

spec = importlib.util.spec_from_file_location("desktop_act_server_win", SERVER)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
sys.modules["desktop_act_server_win"] = mod

try:
    import fastmcp  # noqa: F401
except ImportError:  # pragma: no cover
    import types

    fake = types.ModuleType("fastmcp")

    class _FastMCP:
        def __init__(self, *a, **k):
            pass

        def tool(self, *a, **k):
            def deco(fn):
                return fn

            return deco

        def run(self):
            pass

    fake.FastMCP = _FastMCP
    sys.modules["fastmcp"] = fake

spec.loader.exec_module(mod)


REQUIRED_METHODS = (
    "screenshot",
    "click",
    "type_text",
    "keypress",
    "scroll",
    "list_windows",
    "launch_app",
    "ensure_desktop",
    "acquire_desktop",
    "heartbeat_desktop",
    "release_desktop",
    "list_desktops",
    "reap_idle_desktops",
)


def test_winbackend_class_exists_with_full_surface():
    assert hasattr(mod, "WinBackend")
    wb = mod.WinBackend()
    assert wb.name == "windows"
    assert wb.WIN_SESSION_ID == "win-main"
    for name in REQUIRED_METHODS:
        assert callable(getattr(wb, name)), f"missing method {name}"


def test_winbackend_primitives_guard_off_windows():
    """On non-Windows hosts, GUI primitives must not call SendInput/ImageGrab."""
    if mod.IS_WIN:
        return  # real Windows host: skip the off-platform guard
    wb = mod.WinBackend()

    async def _run():
        shot = await wb.screenshot("", "", "jpeg", 0, True)
        assert shot.get("ok") is False
        assert "Windows" in (shot.get("error") or "")

        click = await wb.click(10, 10, 1, False, "")
        assert click.get("ok") is False

        typed = await wb.type_text("hi", "", 5)
        assert typed.get("ok") is False

        key = await wb.keypress("return", [], "")
        assert key.get("ok") is False

        scroll = await wb.scroll("down", 3, -1, -1, "")
        assert scroll.get("ok") is False

        wins = await wb.list_windows("")
        assert wins.get("ok") is True
        assert wins.get("windows") == []

        launch = await wb.launch_app("notepad", "")
        assert launch.get("ok") is False

    asyncio.run(_run())


def test_winbackend_leases_on_any_platform():
    """Lease metadata works without a real Windows desktop."""
    wb = mod.WinBackend()
    owner = f"win-test-{os.getpid()}"

    async def _run():
        ens = await wb.ensure_desktop(owner_id=owner)
        assert ens.get("ok") is True
        assert ens.get("session_id") == wb.WIN_SESSION_ID
        assert ens.get("owner_id") == owner
        assert int(ens.get("lease_until") or 0) > 0

        hb = await wb.heartbeat_desktop(wb.WIN_SESSION_ID, owner_id=owner)
        assert hb.get("ok") is True

        other = await wb.ensure_desktop(owner_id="other-owner")
        # Exclusive lease: other owner sees shared warning while first holds lease
        if other.get("shared"):
            assert other.get("other_owner") == owner

        listed = await wb.list_desktops()
        assert listed.get("ok") is True
        assert listed.get("count", 0) >= 1

        rel = await wb.release_desktop(wb.WIN_SESSION_ID)
        assert rel.get("ok") is True
        assert rel.get("released") == wb.WIN_SESSION_ID

        reaped = await wb.reap_idle_desktops()
        assert reaped.get("ok") is True

    asyncio.run(_run())


def test_backend_dispatch_names():
    assert mod.BACKEND.name in ("x11", "mac", "windows")
    if mod.IS_WIN:
        assert mod.BACKEND.name == "windows"
        assert isinstance(mod.BACKEND, mod.WinBackend)


def test_winbackend_take_screenshot_calls_encode_screenshot_correctly():
    """Regression: WinBackend._take_screenshot must pass display+grab_ms to _encode_screenshot."""
    wb = mod.WinBackend()

    # Mock a 10x20 RGB image and a stable screen size.
    class FakeImg:
        mode = "RGB"
        width = 10
        height = 20

        def tobytes(self):
            return b"\x00" * (10 * 20 * 3)

    calls = []

    def fake_encode(img, display, grab_ms, fmt, max_width, use_cache):
        calls.append({"display": display, "grab_ms": grab_ms, "fmt": fmt, "max_width": max_width})
        return {
            "path": "/tmp/fake.jpg",
            "width": 5,
            "height": 10,
            "sha": "abc123",
            "cached": False,
            "grab_ms": grab_ms,
            "save_ms": 1,
        }

    wb._screen_size = lambda: (1920, 1080)
    orig = mod._encode_screenshot
    mod._encode_screenshot = fake_encode
    try:
        result = wb._take_screenshot(region="", fmt="jpeg", max_width=1280, use_cache=True)
    finally:
        mod._encode_screenshot = orig

    assert len(calls) == 1
    assert calls[0]["display"] == "win:main"
    assert calls[0]["grab_ms"] >= 0
    assert calls[0]["fmt"] == "jpeg"
    assert calls[0]["max_width"] == 1280
    assert result["ok"] is True
    assert result["path"] == "/tmp/fake.jpg"
    assert result["display"] == "win:main"
    assert result["width"] == 5
    assert result["height"] == 10
    assert result["sha"] == "abc123"
    assert result["screen"] == "1920x1080"


if __name__ == "__main__":
    test_winbackend_class_exists_with_full_surface()
    test_winbackend_primitives_guard_off_windows()
    test_winbackend_leases_on_any_platform()
    test_backend_dispatch_names()
    test_winbackend_take_screenshot_calls_encode_screenshot_correctly()
    print("ok")
