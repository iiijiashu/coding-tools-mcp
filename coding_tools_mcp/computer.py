from __future__ import annotations

import ctypes
import io
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Sequence


class ComputerUnavailable(RuntimeError):
    """Raised when desktop automation is unavailable on the current platform/session."""


def require_windows() -> None:
    if os.name != "nt":
        raise ComputerUnavailable("Computer Use currently supports Windows only.")


def _user32() -> Any:
    require_windows()
    return ctypes.WinDLL("user32", use_last_error=True)


def virtual_screen_metrics() -> dict[str, int]:
    user32 = _user32()
    # Virtual-screen metrics cover all attached monitors and may have a negative origin.
    x = int(user32.GetSystemMetrics(76))  # SM_XVIRTUALSCREEN
    y = int(user32.GetSystemMetrics(77))  # SM_YVIRTUALSCREEN
    width = int(user32.GetSystemMetrics(78))  # SM_CXVIRTUALSCREEN
    height = int(user32.GetSystemMetrics(79))  # SM_CYVIRTUALSCREEN
    return {"x": x, "y": y, "width": width, "height": height}


def cursor_position() -> tuple[int, int]:
    user32 = _user32()

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    point = POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        raise OSError(ctypes.get_last_error(), "GetCursorPos failed")
    return int(point.x), int(point.y)


def capture_screen_png(
    *,
    all_screens: bool = True,
    max_width: int = 2000,
    max_height: int = 2000,
    max_bytes: int = 5_242_880,
) -> tuple[bytes, dict[str, Any]]:
    require_windows()
    try:
        from PIL import ImageGrab
    except ImportError as exc:  # pragma: no cover - production packaging guard
        raise ComputerUnavailable(
            "Pillow is required for computer_screenshot; install coding-tools-mcp[image]."
        ) from exc

    try:
        image = ImageGrab.grab(all_screens=all_screens)
    except Exception as exc:  # noqa: BLE001 - desktop/session errors vary by Windows build
        raise ComputerUnavailable(f"Desktop capture failed: {exc}") from exc

    original_width, original_height = image.size
    resized = False
    if image.width > max_width or image.height > max_height:
        image.thumbnail((max_width, max_height))
        resized = True

    def encode_png() -> bytes:
        stream = io.BytesIO()
        image.save(stream, format="PNG", optimize=False)
        return stream.getvalue()

    data = encode_png()
    # A busy desktop can produce a large PNG. Shrink progressively rather than
    # returning a huge MCP image block or failing immediately.
    while len(data) > max_bytes and image.width > 320 and image.height > 200:
        next_width = max(320, int(image.width * 0.82))
        next_height = max(200, int(image.height * 0.82))
        if (next_width, next_height) == image.size:
            break
        image.thumbnail((next_width, next_height))
        resized = True
        data = encode_png()

    if len(data) > max_bytes:
        raise ComputerUnavailable(
            f"Desktop screenshot is {len(data)} bytes after resizing, above max_bytes={max_bytes}."
        )

    cursor_x, cursor_y = cursor_position()
    return data, {
        "width": int(image.width),
        "height": int(image.height),
        "original_width": int(original_width),
        "original_height": int(original_height),
        "resized": resized,
        "cursor_x": cursor_x,
        "cursor_y": cursor_y,
        "virtual_screen": virtual_screen_metrics(),
    }


def _check_point(x: int, y: int) -> None:
    metrics = virtual_screen_metrics()
    left = metrics["x"]
    top = metrics["y"]
    right = left + metrics["width"]
    bottom = top + metrics["height"]
    if not (left <= x < right and top <= y < bottom):
        raise ValueError(
            f"Point ({x}, {y}) is outside the virtual screen "
            f"[{left},{top}]..[{right - 1},{bottom - 1}]."
        )


def move_cursor(x: int, y: int) -> None:
    _check_point(x, y)
    user32 = _user32()
    if not user32.SetCursorPos(int(x), int(y)):
        raise OSError(ctypes.get_last_error(), "SetCursorPos failed")


_BUTTON_FLAGS = {
    "left": (0x0002, 0x0004),
    "right": (0x0008, 0x0010),
    "middle": (0x0020, 0x0040),
}


def _mouse_event(flags: int, data: int = 0) -> None:
    user32 = _user32()
    # mouse_event is retained by Windows for compatibility and is sufficient for
    # local desktop automation; secure-desktop/UAC isolation still applies.
    user32.mouse_event(int(flags), 0, 0, ctypes.c_ulong(int(data)).value, 0)


def click_mouse(
    *,
    x: int | None = None,
    y: int | None = None,
    button: str = "left",
    clicks: int = 1,
    interval_ms: int = 100,
) -> None:
    if (x is None) != (y is None):
        raise ValueError("x and y must be supplied together")
    if x is not None and y is not None:
        move_cursor(x, y)
    try:
        down, up = _BUTTON_FLAGS[button]
    except KeyError as exc:
        raise ValueError(f"Unsupported mouse button: {button}") from exc
    for index in range(clicks):
        _mouse_event(down)
        _mouse_event(up)
        if index + 1 < clicks and interval_ms:
            time.sleep(interval_ms / 1000.0)


def scroll_mouse(*, delta: int, x: int | None = None, y: int | None = None) -> None:
    if (x is None) != (y is None):
        raise ValueError("x and y must be supplied together")
    if x is not None and y is not None:
        move_cursor(x, y)
    _mouse_event(0x0800, int(delta) * 120)  # MOUSEEVENTF_WHEEL, WHEEL_DELTA


def drag_mouse(
    *,
    x: int,
    y: int,
    to_x: int,
    to_y: int,
    button: str = "left",
    duration_ms: int = 300,
) -> None:
    _check_point(x, y)
    _check_point(to_x, to_y)
    try:
        down, up = _BUTTON_FLAGS[button]
    except KeyError as exc:
        raise ValueError(f"Unsupported mouse button: {button}") from exc
    move_cursor(x, y)
    _mouse_event(down)
    try:
        steps = max(1, min(120, duration_ms // 12 if duration_ms else 1))
        sleep_seconds = (duration_ms / 1000.0 / steps) if duration_ms else 0.0
        for step in range(1, steps + 1):
            ratio = step / steps
            move_cursor(
                round(x + (to_x - x) * ratio),
                round(y + (to_y - y) * ratio),
            )
            if sleep_seconds:
                time.sleep(sleep_seconds)
    finally:
        _mouse_event(up)


_VK_CODES = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "pause": 0x13,
    "capslock": 0x14,
    "escape": 0x1B,
    "esc": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pagedown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "insert": 0x2D,
    "delete": 0x2E,
    "win": 0x5B,
    "windows": 0x5B,
}
for _index in range(1, 25):
    _VK_CODES[f"f{_index}"] = 0x6F + _index


def _keyboard_input(*, vk: int = 0, scan: int = 0, flags: int = 0) -> None:
    user32 = _user32()
    from ctypes import wintypes

    ulong_ptr = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ulong_ptr),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ulong_ptr),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("union", INPUTUNION)]

    item = INPUT()
    item.type = 1  # INPUT_KEYBOARD
    item.union.ki = KEYBDINPUT(int(vk), int(scan), int(flags), 0, 0)
    sent = int(user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(INPUT)))
    if sent != 1:
        raise OSError(ctypes.get_last_error(), "SendInput failed")


def _keyboard_event(vk: int, *, key_up: bool = False) -> None:
    _keyboard_input(vk=int(vk), flags=0x0002 if key_up else 0)  # KEYEVENTF_KEYUP


def _vk_for_key(key: str) -> tuple[int, list[int]]:
    normalized = key.strip().lower()
    if normalized in _VK_CODES:
        return _VK_CODES[normalized], []
    if len(key) != 1:
        raise ValueError(f"Unsupported key: {key}")
    user32 = _user32()
    value = int(user32.VkKeyScanW(ord(key)))
    if value == -1:
        raise ValueError(f"Windows cannot map key: {key!r}")
    vk = value & 0xFF
    shift_state = (value >> 8) & 0xFF
    modifiers: list[int] = []
    if shift_state & 1:
        modifiers.append(_VK_CODES["shift"])
    if shift_state & 2:
        modifiers.append(_VK_CODES["ctrl"])
    if shift_state & 4:
        modifiers.append(_VK_CODES["alt"])
    return vk, modifiers


def press_key(key: str) -> None:
    vk, modifiers = _vk_for_key(key)
    for modifier in modifiers:
        _keyboard_event(modifier)
    try:
        _keyboard_event(vk)
        _keyboard_event(vk, key_up=True)
    finally:
        for modifier in reversed(modifiers):
            _keyboard_event(modifier, key_up=True)


def hotkey(keys: Sequence[str]) -> None:
    if not keys:
        raise ValueError("hotkey requires at least one key")
    pressed: list[int] = []
    try:
        for key in keys:
            vk, implicit = _vk_for_key(key)
            # Explicit hotkey definitions should contain their own modifiers;
            # retain implicit Shift only for printable uppercase/symbol keys.
            for modifier in implicit:
                if modifier not in pressed:
                    _keyboard_event(modifier)
                    pressed.append(modifier)
            _keyboard_event(vk)
            pressed.append(vk)
    finally:
        for vk in reversed(pressed):
            _keyboard_event(vk, key_up=True)


def type_text(text: str, *, delay_ms: int = 0) -> None:
    require_windows()
    # KEYEVENTF_UNICODE consumes UTF-16 code units. Encoding first also handles
    # non-BMP characters as the surrogate pair Windows expects.
    encoded = text.encode("utf-16-le")
    for index in range(0, len(encoded), 2):
        unit = int.from_bytes(encoded[index : index + 2], "little")
        _keyboard_input(scan=unit, flags=0x0004)  # KEYEVENTF_UNICODE
        _keyboard_input(scan=unit, flags=0x0004 | 0x0002)
        if delay_ms:
            time.sleep(delay_ms / 1000.0)


_SHOW_COMMANDS = {
    "hidden": 0,
    "normal": 1,
    "minimized": 2,
    "maximized": 3,
}


def launch_gui(
    path: Path,
    *,
    arguments: Sequence[str] = (),
    cwd: Path | None = None,
    show: str = "normal",
) -> dict[str, Any]:
    require_windows()
    if show not in _SHOW_COMMANDS:
        raise ValueError(f"Unsupported show mode: {show}")
    if not path.is_file():
        raise ValueError(f"GUI launch target is not a file: {path}")

    argument_text = subprocess.list2cmdline([str(item) for item in arguments]) if arguments else None
    startfile_options: dict[str, Any] = {"show_cmd": _SHOW_COMMANDS[show]}
    if argument_text is not None:
        startfile_options["arguments"] = argument_text
    if cwd is not None:
        startfile_options["cwd"] = str(cwd)
    try:
        os.startfile(  # type: ignore[attr-defined]
            str(path),
            "open",
            **startfile_options,
        )
    except OSError as exc:
        if getattr(exc, "winerror", None) == 740:
            raise ComputerUnavailable(
                "Windows reports that this program requires elevation. Computer Use intentionally cannot approve or bypass the UAC secure desktop."
            ) from exc
        raise
    return {
        "launched": True,
        "method": "shell_open",
        "show": show,
        "argument_count": len(arguments),
    }
