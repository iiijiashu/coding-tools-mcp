from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmarks.mcp_http import McpHttpClient


def structured(result: dict[str, Any], tool: str) -> dict[str, Any]:
    payload = result.get("structuredContent")
    if not isinstance(payload, dict):
        raise RuntimeError(f"{tool} returned no structuredContent")
    if result.get("isError") is True or payload.get("ok") is False:
        error = payload.get("error")
        raise RuntimeError(f"{tool} failed: {error if isinstance(error, dict) else 'unknown error'}")
    return payload


def foreground_window() -> dict[str, Any]:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    handle = user32.GetForegroundWindow()
    if not handle:
        raise RuntimeError("Windows reported no foreground window")
    length = int(user32.GetWindowTextLengthW(handle))
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(handle, buffer, length + 1)
    rectangle = RECT()
    if not user32.GetWindowRect(handle, ctypes.byref(rectangle)):
        raise OSError(ctypes.get_last_error(), "GetWindowRect failed")
    return {
        "handle": int(handle),
        "title": buffer.value,
        "left": int(rectangle.left),
        "top": int(rectangle.top),
        "right": int(rectangle.right),
        "bottom": int(rectangle.bottom),
    }


def find_window(title_fragment: str) -> dict[str, Any] | None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    matches: list[dict[str, Any]] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @callback_type
    def visit(handle: int, _state: int) -> bool:
        if not user32.IsWindowVisible(handle):
            return True
        length = int(user32.GetWindowTextLengthW(handle))
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, buffer, length + 1)
        if title_fragment.lower() not in buffer.value.lower():
            return True
        matches.append({"handle": int(handle), "title": buffer.value})
        return False

    user32.EnumWindows(visit, 0)
    if not matches:
        return None
    handle = int(matches[0]["handle"])
    user32.ShowWindow(handle, 9)  # SW_RESTORE
    user32.BringWindowToTop(handle)
    user32.SetForegroundWindow(handle)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        current = foreground_window()
        if int(current["handle"]) == handle:
            return current
        time.sleep(0.05)
    return None


def close_window(handle: int) -> None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.PostMessageW(int(handle), 0x0010, 0, 0)  # WM_CLOSE


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a bounded visible Computer Use desktop acceptance.")
    parser.add_argument("--endpoint", default="http://127.0.0.1:18765/mcp")
    parser.add_argument("--workspace", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).expanduser().resolve(strict=True)
    acceptance_dir = workspace / ".coding-tools-mcp" / "acceptance"
    target = acceptance_dir / "computer-use-acceptance.txt"
    acceptance_dir.mkdir(parents=True, exist_ok=True)
    marker = "COMPUTER_USE_ACCEPTANCE_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target.write_text("", encoding="utf-8")
    client = McpHttpClient(args.endpoint, timeout=15)
    foreground: dict[str, Any] | None = None
    closed = False
    try:
        client.initialize()
        before = structured(
            client.call_tool(
                "computer_screenshot",
                {"all_screens": True, "max_width": 1200, "max_height": 1200, "max_bytes": 2_000_000},
            ),
            "computer_screenshot",
        )
        launched = structured(
            client.call_tool(
                "computer_launch",
                {
                    "path": str(target.relative_to(workspace)).replace("\\", "/"),
                    "workdir": ".",
                    "show": "normal",
                    "wait_ms": 1000,
                },
            ),
            "computer_launch",
        )
        deadline = time.monotonic() + 5
        while True:
            foreground = find_window(target.name)
            if foreground is not None:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Launched window never became the verified foreground target; keyboard input was not sent"
                )
            time.sleep(0.2)

        width = int(foreground["right"]) - int(foreground["left"])
        height = int(foreground["bottom"]) - int(foreground["top"])
        if width < 200 or height < 160:
            raise RuntimeError("Verified foreground window is too small for a safe client-area click")
        x = int(foreground["left"]) + min(width - 40, max(80, width // 2))
        y = int(foreground["top"]) + min(height - 40, max(120, height // 2))
        moved = structured(
            client.call_tool("computer_mouse", {"action": "move", "x": x, "y": y}),
            "computer_mouse",
        )
        clicked = structured(
            client.call_tool(
                "computer_mouse",
                {"action": "click", "x": x, "y": y, "button": "left", "clicks": 1},
            ),
            "computer_mouse",
        )
        typed = structured(
            client.call_tool(
                "computer_keyboard",
                {"action": "type", "text": marker, "delay_ms": 1},
            ),
            "computer_keyboard",
        )
        structured(
            client.call_tool(
                "computer_keyboard", {"action": "hotkey", "keys": ["ctrl", "s"]}
            ),
            "computer_keyboard",
        )
        deadline = time.monotonic() + 5
        while marker not in target.read_text(encoding="utf-8", errors="replace"):
            if time.monotonic() >= deadline:
                raise RuntimeError("Keyboard input was sent, but the verified file did not persist the marker")
            time.sleep(0.2)
        after = structured(
            client.call_tool(
                "computer_screenshot",
                {"all_screens": True, "max_width": 1200, "max_height": 1200, "max_bytes": 2_000_000},
            ),
            "computer_screenshot",
        )
        structured(
            client.call_tool(
                "computer_keyboard", {"action": "hotkey", "keys": ["alt", "f4"]}
            ),
            "computer_keyboard",
        )
        time.sleep(0.3)
        remaining = find_window(target.name)
        if remaining is not None:
            close_window(int(remaining["handle"]))
        closed = True
        print(
            json.dumps(
                {
                    "ok": True,
                    "target": str(target),
                    "window_title": foreground["title"],
                    "launch": launched,
                    "mouse_move": moved,
                    "mouse_click": clicked,
                    "keyboard": typed,
                    "before_screenshot": {
                        key: before.get(key)
                        for key in ("bytes", "width", "height", "original_width", "original_height")
                    },
                    "after_screenshot": {
                        key: after.get(key)
                        for key in ("bytes", "width", "height", "original_width", "original_height")
                    },
                    "secure_desktop_control": False,
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return 0
    finally:
        if foreground is not None and not closed:
            try:
                close_window(int(foreground["handle"]))
            except Exception:
                pass
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        try:
            acceptance_dir.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
