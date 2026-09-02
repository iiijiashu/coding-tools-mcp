from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from coding_tools_mcp.control_plane import (
    control_plane_command_violation,
    is_protected_control_plane_path,
)
from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp.server import Runtime


class ControlPlaneGuardTests(unittest.TestCase):
    def test_raw_task_lifecycle_mutations_are_blocked(self) -> None:
        commands = (
            "Stop-ScheduledTask -TaskName 'Coding Tools MCP HTTP - Local D Drive'",
            "Start-ScheduledTask -TaskName 'OpenAI Secure MCP Tunnel - Local D Drive Coding Tools'",
            "Disable-ScheduledTask -TaskName 'Coding Tools MCP Control Plane Manager - Local D Drive'",
            'schtasks /end /tn "Coding Tools MCP HTTP - Local D Drive"',
            'schtasks /run /tn "Coding Tools MCP HTTP Watchdog - Local D Drive"',
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertIsNotNone(control_plane_command_violation(command))

    def test_dynamic_or_implicit_task_mutation_targets_fail_closed(self) -> None:
        commands = (
            "$n=('Coding Tools MCP HTTP' + ' - Local D Drive'); Stop-ScheduledTask -TaskName $n",
            "Get-ScheduledTask -TaskName $env:TASK_NAME | Disable-ScheduledTask",
            "Stop-ScheduledTask -InputObject $task",
            (
                "Get-ScheduledTask -TaskName 'Unrelated Backup Job' > $null; "
                "Stop-ScheduledTask -InputObject $task"
            ),
            "schtasks /end /tn %TASK_NAME%",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertIsNotNone(control_plane_command_violation(command))

    def test_literal_unrelated_task_mutations_remain_allowed(self) -> None:
        commands = (
            "Stop-ScheduledTask -TaskName 'Unrelated Backup Job'",
            'schtasks /run /tn "Unrelated Backup Job"',
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertIsNone(control_plane_command_violation(command))

    def test_read_only_task_and_config_queries_remain_allowed(self) -> None:
        commands = (
            "Get-ScheduledTask -TaskName 'Coding Tools MCP HTTP - Local D Drive'",
            "Get-FileHash config/local-mcp-http-task.json -Algorithm SHA256",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertIsNone(control_plane_command_violation(command))

    def test_direct_production_config_writes_are_blocked(self) -> None:
        commands = (
            "Set-Content config/local-mcp-http-task.json '{}'",
            "Set-Content config/control-plane-runtime-contract-v1.json '{}'",
            "Copy-Item candidate.json config/local-mcp-http-task.json",
            "Copy-Item candidate.json config/control-plane-runtime-contract-v1.json",
            "python -c \"open('config/local-mcp-http-task.json','w').write('{}')\"",
            "powershell -File artifacts/runtime-repair-20260901-1745/pin-main-task.ps1",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertIsNotNone(control_plane_command_violation(command))

    def test_only_exact_control_plane_contract_paths_are_protected(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_config = root / "config" / "local-mcp-http-task.json"
            runtime_contract = root / "config" / "control-plane-runtime-contract-v1.json"
            candidate = root / "artifacts" / "candidate.json"

            self.assertTrue(is_protected_control_plane_path(task_config, root))
            self.assertTrue(is_protected_control_plane_path(runtime_contract, root))
            self.assertFalse(is_protected_control_plane_path(candidate, root))

    def test_runtime_enforces_guard_even_in_dangerous_mode(self) -> None:
        with TemporaryDirectory() as temporary:
            runtime = Runtime(Path(temporary), permission_mode="dangerous")
            try:
                with self.assertRaises(ToolFailure) as raised:
                    runtime._check_command_policy(
                        "Stop-ScheduledTask -TaskName 'Coding Tools MCP HTTP - Local D Drive'",
                        {},
                    )
            finally:
                runtime.close()

        self.assertEqual(raised.exception.code, "CONTROL_PLANE_LEASE_REQUIRED")


if __name__ == "__main__":
    unittest.main()
