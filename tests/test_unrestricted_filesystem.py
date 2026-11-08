from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp import server as server_module


class UnrestrictedFilesystemTests(unittest.TestCase):
    def test_unrestricted_paths_require_dangerous_permission_mode(self) -> None:
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ToolFailure) as raised:
                server_module.Runtime(
                    Path(tmp),
                    permission_mode="trusted",
                    allow_any_local_path=True,
                )
        self.assertEqual(raised.exception.code, "INVALID_ARGUMENT")

    def test_dangerous_mode_stays_workspace_confined_without_explicit_switch(self) -> None:
        with TemporaryDirectory() as workspace_tmp, TemporaryDirectory() as outside_tmp:
            outside_file = Path(outside_tmp) / "outside.txt"
            outside_file.write_text("outside\n", encoding="utf-8")
            runtime = server_module.Runtime(Path(workspace_tmp), permission_mode="dangerous")
            try:
                with self.assertRaises(ToolFailure) as raised:
                    runtime.resolve_for_write(str(outside_file))
                self.assertEqual(raised.exception.code, "ABSOLUTE_PATH_DENIED")
            finally:
                runtime.close()

    def test_explicit_unrestricted_mode_supports_absolute_read_write_and_workdir(self) -> None:
        with TemporaryDirectory() as workspace_tmp, TemporaryDirectory() as outside_tmp:
            workspace = Path(workspace_tmp)
            outside = Path(outside_tmp)
            source = outside / "source.txt"
            source.write_text("outside marker\n", encoding="utf-8")
            target = outside / "created.txt"
            runtime = server_module.Runtime(
                workspace,
                permission_mode="dangerous",
                allow_any_local_path=True,
                auth_token="test-bearer-token-0123456789",
                transport="http",
            )
            try:
                read = runtime.read_file({"path": str(source)})
                self.assertEqual(read["content"].splitlines(), ["outside marker"])

                listed = runtime.list_dir({"path": str(outside), "include_hidden": True})
                self.assertIn(str(source.resolve()), [item["path"] for item in listed["entries"]])

                files = runtime.list_files(
                    {"path": str(outside), "patterns": ["*.txt"], "include_hidden": True}
                )
                self.assertIn(str(source.resolve()), [item["path"] for item in files["files"]])

                searched = runtime.search_text({"path": str(outside), "query": "outside marker"})
                self.assertEqual(searched["total_matches"], 1, searched)

                result = runtime.apply_patch(
                    {
                        "patch": (
                            "*** Begin Patch\n"
                            f"*** Add File: {target}\n"
                            "+created outside workspace\n"
                            "*** End Patch"
                        )
                    }
                )
                self.assertEqual(result["affected_files"][0]["operation"], "add", result)
                self.assertEqual(target.read_text(encoding="utf-8"), "created outside workspace\n")

                command = runtime.exec_command(
                    {
                        "cmd": "cd" if os.name == "nt" else "pwd",
                        "workdir": str(outside),
                        "yield_time_ms": 3000,
                    }
                )
                self.assertEqual(command["status"], "exited", command)
                self.assertEqual(command["exit_code"], 0, command)

                info = runtime.server_info_payload()
                self.assertTrue(info["dangerously_allow_any_local_path"])
                self.assertEqual(info["filesystem_scope"], "all_local_drives")
            finally:
                runtime.close()

    def test_unrestricted_mode_still_rejects_relative_parent_escape(self) -> None:
        with TemporaryDirectory() as workspace_tmp:
            runtime = server_module.Runtime(
                Path(workspace_tmp),
                permission_mode="dangerous",
                allow_any_local_path=True,
                auth_token="test-bearer-token-0123456789",
                transport="http",
            )
            try:
                with self.assertRaises(ToolFailure) as raised:
                    runtime.resolve_existing("../outside")
                self.assertEqual(raised.exception.code, "PATH_OUTSIDE_WORKSPACE")
            finally:
                runtime.close()

    @unittest.skipUnless(os.name == "nt", "Windows local-path contract")
    def test_unrestricted_mode_rejects_unc_device_and_ads_paths(self) -> None:
        with TemporaryDirectory() as workspace_tmp:
            runtime = server_module.Runtime(
                Path(workspace_tmp),
                permission_mode="dangerous",
                allow_any_local_path=True,
                auth_token="test-bearer-token-0123456789",
                transport="http",
            )
            try:
                for path in (
                    r"\\server\share\file.txt",
                    r"\\?\C:\Windows\win.ini",
                    r"\\.\PhysicalDrive0",
                    r"C:\Windows\win.ini:stream",
                ):
                    with self.subTest(path=path), self.assertRaises(ToolFailure) as raised:
                        runtime.workspace.resolve_for_read(path)
                    self.assertEqual(raised.exception.code, "INVALID_ARGUMENT")
            finally:
                runtime.close()

    @unittest.skipUnless(os.name == "nt", "Windows local-path contract")
    def test_unrestricted_mode_revalidates_canonical_target_and_reserved_devices(self) -> None:
        with TemporaryDirectory() as workspace_tmp, TemporaryDirectory() as outside_tmp:
            source = Path(outside_tmp) / "source.txt"
            source.write_text("source\n", encoding="utf-8")
            runtime = server_module.Runtime(
                Path(workspace_tmp),
                permission_mode="dangerous",
                allow_any_local_path=True,
                auth_token="test-bearer-token-0123456789",
                transport="http",
            )
            try:
                windows_path_type = type(source)
                with patch.object(
                    windows_path_type,
                    "resolve",
                    return_value=Path(r"\\server\share\source.txt"),
                ):
                    with self.assertRaises(ToolFailure) as raised:
                        runtime.workspace.resolve_existing(str(source))
                self.assertEqual(raised.exception.code, "INVALID_ARGUMENT")

                for path in (
                    r"C:\NUL",
                    r"C:\NUL.txt",
                    r"C:\safe\CON.log",
                    r"C:\safe\COM1.txt",
                    r"C:\safe\LPT9",
                ):
                    with self.subTest(path=path), self.assertRaises(ToolFailure) as raised:
                        runtime.workspace.resolve_for_write(path)
                    self.assertEqual(raised.exception.code, "INVALID_ARGUMENT")

                mapped_candidate = str(Path(outside_tmp) / "mapped-drive-proof.txt")
                with patch.object(
                    runtime.workspace,
                    "_windows_drive_type",
                    return_value=4,
                    create=True,
                ):
                    with self.assertRaises(ToolFailure) as raised:
                        runtime.workspace.resolve_for_write(mapped_candidate)
                self.assertEqual(raised.exception.code, "INVALID_ARGUMENT")
            finally:
                runtime.close()

    def test_unrestricted_runtime_requires_authenticated_http(self) -> None:
        with TemporaryDirectory() as workspace_tmp:
            for kwargs in (
                {"transport": "http"},
                {"transport": "stdio", "auth_token": "test-bearer-token-0123456789"},
            ):
                with self.subTest(kwargs=kwargs):
                    with self.assertRaises(ToolFailure) as raised:
                        server_module.Runtime(
                            Path(workspace_tmp),
                            permission_mode="dangerous",
                            allow_any_local_path=True,
                            **kwargs,
                        )
                    self.assertEqual(raised.exception.code, "INVALID_ARGUMENT")

    def test_cli_switch_requires_dangerous_mode(self) -> None:
        parser = server_module.build_parser()
        trusted = parser.parse_args(
            ["--permission-mode", "trusted", "--dangerously-allow-any-local-path"]
        )
        with self.assertRaises(ValueError):
            server_module.runtime_policy_from_args(trusted)

        dangerous = parser.parse_args(
            ["--permission-mode", "dangerous", "--dangerously-allow-any-local-path"]
        )
        self.assertTrue(server_module.runtime_policy_from_args(dangerous).allow_any_local_path)

    def test_unrestricted_http_requires_authentication(self) -> None:
        parser = server_module.build_parser()
        with TemporaryDirectory() as workspace_tmp:
            args = parser.parse_args(
                [
                    "--workspace",
                    workspace_tmp,
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "0",
                    "--permission-mode",
                    "dangerous",
                    "--dangerously-allow-any-local-path",
                ]
            )
            with (
                patch.dict(
                    os.environ,
                    {
                        "CODING_TOOLS_MCP_AUTH_TOKEN": "",
                        "CODING_TOOLS_MCP_AUTH_MODE": "",
                        "CODING_TOOLS_MCP_OAUTH_MODE": "",
                    },
                    clear=False,
                ),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(server_module.run_http(args), 2)

    def test_unrestricted_stdio_is_rejected(self) -> None:
        parser = server_module.build_parser()
        with TemporaryDirectory() as workspace_tmp:
            args = parser.parse_args(
                [
                    "--workspace",
                    workspace_tmp,
                    "--stdio",
                    "--permission-mode",
                    "dangerous",
                    "--dangerously-allow-any-local-path",
                ]
            )
            with redirect_stderr(io.StringIO()):
                self.assertEqual(server_module.run_stdio(args), 2)


if __name__ == "__main__":
    unittest.main()
