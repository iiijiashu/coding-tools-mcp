from __future__ import annotations

import subprocess
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from benchmarks.dogfood.mcp_deterministic_runner import prepare_workspace
from benchmarks.mcp_http import McpHttpClient, McpHttpError, connect_with_retry
from benchmarks.platform_commands import (
    join_shell_command,
    native_echo_arguments,
    outside_file_read_command,
    render_process_command,
    split_command_arguments,
)


class BenchmarkPlatformTests(unittest.TestCase):
    def test_windows_server_command_preserves_drive_separators_and_spaces(self) -> None:
        rendered = render_process_command(
            "{python} -m coding_tools_mcp --workspace {workspace} --port {port}",
            {
                "python": r"D:\coding-tools-mcp\.venv\Scripts\python.exe",
                "workspace": r"D:\fixture with spaces\workspace",
                "port": 8765,
            },
            platform="nt",
        )
        self.assertIsInstance(rendered, str)
        self.assertIn(r"D:\coding-tools-mcp\.venv\Scripts\python.exe", rendered)
        self.assertIn('"D:\\fixture with spaces\\workspace"', rendered)

    def test_posix_server_command_is_an_argv_sequence(self) -> None:
        rendered = render_process_command(
            "{python} -m coding_tools_mcp --workspace {workspace}",
            {"python": "/tmp/python with spaces", "workspace": "/tmp/work space"},
            platform="posix",
        )
        self.assertEqual(
            rendered,
            ["/tmp/python with spaces", "-m", "coding_tools_mcp", "--workspace", "/tmp/work space"],
        )

    def test_windows_argument_split_does_not_eat_backslashes(self) -> None:
        command = join_shell_command(
            [r"C:\Program Files\Python\python.exe", "-m", "unittest"],
            platform="nt",
        )
        self.assertEqual(
            split_command_arguments(command, platform="nt"),
            [r"C:\Program Files\Python\python.exe", "-m", "unittest"],
        )

    def test_workspace_escape_probe_uses_native_reader(self) -> None:
        self.assertEqual(
            outside_file_read_command("../outside-secret.txt", platform="nt"),
            "type ..\\outside-secret.txt",
        )
        self.assertEqual(
            outside_file_read_command("../outside-secret.txt", platform="posix"),
            "cat ../outside-secret.txt",
        )
        self.assertIn("cmd", native_echo_arguments(platform="nt")[0].lower())
        self.assertEqual(native_echo_arguments(platform="posix"), ["printf", "ok"])

    def test_fixture_is_a_clean_git_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = Path(temp_dir) / "fixture"
            _, workspace = prepare_workspace(fixture)
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=workspace,
                check=True,
                capture_output=True,
                text=True,
            )
            readonly = next((workspace / ".git" / "objects").glob("??/*"))
            readonly.chmod(stat.S_IREAD)
            _, recreated = prepare_workspace(fixture)
            self.assertEqual(status.stdout, "")
            self.assertTrue((recreated / ".git").is_dir())

    def test_http_timeout_is_reported_as_mcp_transport_error(self) -> None:
        client = McpHttpClient("http://127.0.0.1:1/mcp", timeout=12.5)
        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaisesRegex(McpHttpError, "timed out after 12.5 seconds"):
                client.list_tools()

    def test_startup_initialize_timeout_is_capped_by_startup_budget(self) -> None:
        client = MagicMock()
        client.initialize.side_effect = McpHttpError("not ready")
        with (
            patch("benchmarks.mcp_http.McpHttpClient", return_value=client) as client_type,
            patch("benchmarks.mcp_http.time.monotonic", side_effect=[100.0, 100.0, 106.0]),
        ):
            connected, initialized, error = connect_with_retry(
                "http://127.0.0.1:1/mcp",
                5.0,
                request_timeout=60.0,
            )
        self.assertIsNone(connected)
        self.assertIsNone(initialized)
        self.assertEqual(error, "not ready")
        client_type.assert_called_once_with("http://127.0.0.1:1/mcp", timeout=5.0)


if __name__ == "__main__":
    unittest.main()
