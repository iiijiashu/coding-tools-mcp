from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_tools_mcp import server as server_module
from coding_tools_mcp import computer as computer_module
from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp.project_context import ProjectContext
from coding_tools_mcp.server import Runtime


EMPTY_CONTEXT = ProjectContext((), (), ())


class ComputerToolTests(unittest.TestCase):
    def runtime(self, root: Path, *, enabled: bool, permission_mode: str = "trusted") -> Runtime:
        return Runtime(
            root,
            enable_computer_use=enabled,
            permission_mode=permission_mode,
            project_context=EMPTY_CONTEXT,
        )

    def test_computer_tools_are_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            disabled = self.runtime(Path(tmp), enabled=False)
            enabled = self.runtime(Path(tmp), enabled=True)
            try:
                self.assertNotIn("computer_screenshot", disabled.exposed_tool_names())
                for name in (
                    "computer_screenshot",
                    "computer_mouse",
                    "computer_keyboard",
                    "computer_launch",
                ):
                    self.assertIn(name, enabled.exposed_tool_names())
            finally:
                disabled.close()
                enabled.close()

    def test_safe_mode_blocks_computer_control(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self.runtime(Path(tmp), enabled=True, permission_mode="safe")
            try:
                with self.assertRaises(ToolFailure) as raised:
                    runtime.computer_screenshot({})
            finally:
                runtime.close()
        self.assertEqual(raised.exception.code, "PERMISSION_REQUIRED")
        self.assertEqual(raised.exception.details.get("permission"), "computer_control")

    def test_screenshot_routes_image_bytes_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self.runtime(Path(tmp), enabled=True)
            metadata = {
                "width": 1600,
                "height": 900,
                "original_width": 2560,
                "original_height": 1440,
                "resized": True,
                "cursor_x": 100,
                "cursor_y": 200,
                "virtual_screen": {"x": 0, "y": 0, "width": 2560, "height": 1440},
            }
            try:
                with (
                    patch.object(server_module.os, "name", "nt"),
                    patch.object(server_module.computer_tools, "capture_screen_png", return_value=(b"png", metadata)),
                ):
                    payload = runtime.computer_screenshot({})
            finally:
                runtime.close()
        self.assertEqual(payload["mime_type"], "image/png")
        self.assertEqual(payload["bytes"], 3)
        self.assertEqual(payload["cursor_x"], 100)
        self.assertIn("_mcp_image_data", payload)

    def test_mouse_click_routes_without_shelling_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self.runtime(Path(tmp), enabled=True)
            try:
                with (
                    patch.object(server_module.os, "name", "nt"),
                    patch.object(server_module.computer_tools, "click_mouse") as click,
                    patch.object(server_module.computer_tools, "cursor_position", return_value=(321, 654)),
                ):
                    payload = runtime.computer_mouse(
                        {"action": "click", "x": 10, "y": 20, "button": "left", "clicks": 2}
                    )
            finally:
                runtime.close()
        click.assert_called_once_with(x=10, y=20, button="left", clicks=2, interval_ms=100)
        self.assertEqual((payload["cursor_x"], payload["cursor_y"]), (321, 654))

    def test_keyboard_type_does_not_echo_text(self) -> None:
        secretish_text = "do-not-echo-this"
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self.runtime(Path(tmp), enabled=True)
            try:
                with (
                    patch.object(server_module.os, "name", "nt"),
                    patch.object(server_module.computer_tools, "type_text") as typed,
                ):
                    payload = runtime.computer_keyboard(
                        {"action": "type", "text": secretish_text, "delay_ms": 1}
                    )
            finally:
                runtime.close()
        typed.assert_called_once_with(secretish_text, delay_ms=1)
        self.assertEqual(payload["characters_typed"], len(secretish_text))
        self.assertNotIn(secretish_text, repr(payload))

    def test_launch_is_confined_to_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "installer.exe"
            target.write_bytes(b"placeholder")
            runtime = self.runtime(root, enabled=True)
            try:
                with (
                    patch.object(server_module.os, "name", "nt"),
                    patch.object(
                        server_module.computer_tools,
                        "launch_gui",
                        return_value={"launched": True, "method": "shell_open", "show": "normal", "argument_count": 0},
                    ) as launched,
                ):
                    payload = runtime.computer_launch({"path": "installer.exe", "wait_ms": 0})
                with self.assertRaises(ToolFailure):
                    runtime.computer_launch({"path": "../outside.exe", "wait_ms": 0})
            finally:
                runtime.close()
        launched.assert_called_once()
        self.assertTrue(payload["launched"])

    def test_shell_open_omits_optional_none_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "acceptance.txt"
            target.write_text("", encoding="utf-8")
            with patch.object(computer_module.os, "startfile", create=True) as startfile:
                result = computer_module.launch_gui(target)
        startfile.assert_called_once_with(str(target), "open", show_cmd=1)
        self.assertEqual(result["argument_count"], 0)


if __name__ == "__main__":
    unittest.main()
