from __future__ import annotations

import unittest
import json
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from coding_tools_mcp.control_plane_runtime_contract import load_runtime_contract
from scripts.control_plane_doctor import evaluate_task_contracts, evaluate_runtime_contract, overall_status
from scripts.mcp_backend_probe import BackendProbe

RUNTIME_CONTRACT = load_runtime_contract()
HTTP_TASK = RUNTIME_CONTRACT.task("http").name
HTTP_WATCHDOG_TASK = RUNTIME_CONTRACT.task("http_watchdog").name
TUNNEL_TASK = RUNTIME_CONTRACT.task("tunnel").name
TUNNEL_WATCHDOG_TASK = RUNTIME_CONTRACT.task("tunnel_watchdog").name
CONTROL_PLANE_MANAGER_TASK = RUNTIME_CONTRACT.task("manager").name
REQUIRED_TOOLS = RUNTIME_CONTRACT.tool_catalog.required_tools
APPROVED_TUNNEL_ARGUMENTS_SHA256 = RUNTIME_CONTRACT.tunnel.main_arguments_sha256
REPO_ROOT = Path(__file__).resolve().parents[1]


class ControlPlaneDoctorTests(unittest.TestCase):
    def test_public_diagnostic_constants_come_from_the_runtime_manifest(self) -> None:
        self.assertEqual(HTTP_TASK, RUNTIME_CONTRACT.task("http").name)
        self.assertEqual(HTTP_WATCHDOG_TASK, RUNTIME_CONTRACT.task("http_watchdog").name)
        self.assertEqual(TUNNEL_TASK, RUNTIME_CONTRACT.task("tunnel").name)
        self.assertEqual(TUNNEL_WATCHDOG_TASK, RUNTIME_CONTRACT.task("tunnel_watchdog").name)
        self.assertEqual(CONTROL_PLANE_MANAGER_TASK, RUNTIME_CONTRACT.task("manager").name)
        self.assertEqual(REQUIRED_TOOLS, RUNTIME_CONTRACT.tool_catalog.required_tools)
        self.assertEqual(
            APPROVED_TUNNEL_ARGUMENTS_SHA256,
            RUNTIME_CONTRACT.tunnel.main_arguments_sha256,
        )

    def test_doctor_requires_the_full_computer_use_catalog(self) -> None:
        self.assertEqual(
            set(REQUIRED_TOOLS),
            {
                "server_info",
                "computer_screenshot",
                "computer_mouse",
                "computer_keyboard",
                "computer_launch",
            },
        )

    def test_ready_requires_both_runtime_owners_and_all_health_layers(self) -> None:
        tasks = {
            HTTP_TASK: {"state": "Running", "run_level": "Highest"},
            HTTP_WATCHDOG_TASK: {"enabled": True},
            TUNNEL_TASK: {"state": "Running"},
            TUNNEL_WATCHDOG_TASK: {"enabled": True},
            CONTROL_PLANE_MANAGER_TASK: {"enabled": True, "run_level": "Highest"},
        }
        self.assertEqual(overall_status(True, True, True, tasks, True), "READY")
        self.assertEqual(overall_status(True, True, False, tasks, True), "DEGRADED")
        self.assertEqual(overall_status(False, False, False, tasks, True), "FAILED")

        tasks[HTTP_TASK]["run_level"] = "Limited"
        self.assertEqual(overall_status(True, True, True, tasks, True), "DEGRADED")

        tasks[HTTP_TASK]["run_level"] = "Highest"
        tasks[HTTP_WATCHDOG_TASK]["enabled"] = False
        self.assertEqual(overall_status(True, True, True, tasks, True), "DEGRADED")

        tasks[HTTP_WATCHDOG_TASK]["enabled"] = True
        tasks[TUNNEL_TASK]["state"] = "Ready"
        self.assertEqual(overall_status(True, True, True, tasks, True), "DEGRADED")

        tasks[TUNNEL_TASK]["state"] = "Running"
        tasks[TUNNEL_WATCHDOG_TASK]["enabled"] = False
        self.assertEqual(overall_status(True, True, True, tasks, True), "DEGRADED")

        tasks[TUNNEL_WATCHDOG_TASK]["enabled"] = True
        tasks[CONTROL_PLANE_MANAGER_TASK]["run_level"] = "Limited"
        self.assertEqual(overall_status(True, True, True, tasks, True), "DEGRADED")

        tasks[CONTROL_PLANE_MANAGER_TASK]["run_level"] = "Highest"
        self.assertEqual(overall_status(True, True, True, tasks, False), "DEGRADED")
        self.assertEqual(
            overall_status(
                True,
                True,
                True,
                tasks,
                True,
                task_contract_ok=False,
            ),
            "DEGRADED",
        )

    def test_task_contract_requires_running_singleton_supervisors_and_full_catalog(self) -> None:
        pythonw = str(REPO_ROOT / ".venv" / "Scripts" / "pythonw.exe")
        http_script = str(REPO_ROOT / "scripts" / "run_local_mcp_http_task.py")
        http_watchdog_script = str(REPO_ROOT / "scripts" / "local_http_mcp_watchdog.py")
        tunnel_watchdog_script = str(REPO_ROOT / "scripts" / "secure_tunnel_watchdog.py")
        manager_script = str(REPO_ROOT / "scripts" / "control_plane_transaction.py")
        catalog_sha256 = "a" * 64
        manager_state_root = (
            Path.home()
            / ".local"
            / "state"
            / "tunnel-client"
            / "control-plane"
            / "local-d-drive-coding-tools"
        )
        tunnel_watchdog_prefix = (
            f'"{tunnel_watchdog_script}" '
            f'--main-task-name "{TUNNEL_TASK}" --main-task-path \\ '
            '--tunnel-client "C:/tunnel-client.exe" '
            f"--tunnel-client-sha256 {RUNTIME_CONTRACT.tunnel.executable_sha256}"
        )
        base = {
            "enabled": True,
            "multiple_instances": "IgnoreNew",
            "logon_type": "Interactive",
            "registration_trigger": True,
            "logon_trigger": True,
            "restart_on_failure": True,
            "task_path": "\\",
            "working_directory": "",
            "action_count": 1,
            "action_type": "Exec",
        }
        tasks = {
            HTTP_TASK: {
                **base,
                "state": "Running",
                "run_level": "Highest",
                "restart_on_failure": False,
                "execute": pythonw,
                "arguments": f'"{http_script}"',
                "time_trigger": False,
            },
            TUNNEL_TASK: {
                **base,
                "state": "Running",
                "run_level": "Limited",
                "restart_on_failure": False,
                "execute": "C:/tunnel-client.exe",
                "execute_sha256": RUNTIME_CONTRACT.tunnel.executable_sha256,
                "arguments_sha256": RUNTIME_CONTRACT.tunnel.main_arguments_sha256,
                "arguments": "run --profile-file profile.yaml --pid.file tunnel.pid --health.url-file health.url",
                "time_trigger": False,
            },
            HTTP_WATCHDOG_TASK: {
                **base,
                "state": "Running",
                "run_level": "Highest",
                "execute": pythonw,
                "arguments": (
                    f'"{http_watchdog_script}" '
                    f"--loop-interval-seconds 60 --expected-tool-catalog-sha256 {catalog_sha256}"
                ),
                "time_trigger": False,
            },
            TUNNEL_WATCHDOG_TASK: {
                **base,
                "state": "Running",
                "run_level": "Limited",
                "execute": pythonw,
                "arguments": (
                    f"{tunnel_watchdog_prefix} "
                    f"--loop-interval-seconds 15 --expected-tool-catalog-sha256 {catalog_sha256} "
                    f"--main-task-arguments-sha256 {APPROVED_TUNNEL_ARGUMENTS_SHA256}"
                ),
                "time_trigger": False,
            },
            CONTROL_PLANE_MANAGER_TASK: {
                **base,
                "state": "Ready",
                "run_level": "Highest",
                "execute": pythonw,
                "arguments": (
                    f'"{manager_script}" execute '
                    f'--state-root "{manager_state_root}" '
                    "--loop-interval-seconds 60"
                ),
                "time_trigger": False,
            },
        }

        self.assertTrue(evaluate_task_contracts(tasks, catalog_sha256)["ok"])

        tasks[HTTP_TASK]["arguments"] = (
            '"D:\\evil.py" --label '
            f'"{http_script}"'
        )
        contract = evaluate_task_contracts(tasks, catalog_sha256)
        self.assertFalse(contract["ok"])
        self.assertIn("http_script", contract["violations"])
        tasks[HTTP_TASK]["arguments"] = (
            f'"{http_script}"'
        )

        tasks[TUNNEL_TASK]["execute_sha256"] = "0" * 64
        contract = evaluate_task_contracts(tasks, catalog_sha256)
        self.assertFalse(contract["ok"])
        self.assertIn("tunnel_executable_fingerprint", contract["violations"])
        tasks[TUNNEL_TASK]["execute_sha256"] = RUNTIME_CONTRACT.tunnel.executable_sha256

        tasks[TUNNEL_TASK]["arguments_sha256"] = "0" * 64
        contract = evaluate_task_contracts(tasks, catalog_sha256)
        self.assertFalse(contract["ok"])
        self.assertIn("tunnel_arguments_fingerprint", contract["violations"])
        tasks[TUNNEL_TASK]["arguments_sha256"] = RUNTIME_CONTRACT.tunnel.main_arguments_sha256

        tasks[TUNNEL_WATCHDOG_TASK]["arguments"] = tasks[TUNNEL_WATCHDOG_TASK][
            "arguments"
        ].replace('"C:/tunnel-client.exe"', '"C:/other-client.exe"')
        contract = evaluate_task_contracts(tasks, catalog_sha256)
        self.assertFalse(contract["ok"])
        self.assertIn("tunnel_watchdog_tunnel_client", contract["violations"])
        tasks[TUNNEL_WATCHDOG_TASK]["arguments"] = tasks[TUNNEL_WATCHDOG_TASK][
            "arguments"
        ].replace('"C:/other-client.exe"', '"C:/tunnel-client.exe"')

        tasks[TUNNEL_WATCHDOG_TASK]["arguments"] = tasks[TUNNEL_WATCHDOG_TASK][
            "arguments"
        ].replace(RUNTIME_CONTRACT.tunnel.executable_sha256, "0" * 64)
        contract = evaluate_task_contracts(tasks, catalog_sha256)
        self.assertFalse(contract["ok"])
        self.assertIn("tunnel_watchdog_tunnel_client_fingerprint", contract["violations"])
        tasks[TUNNEL_WATCHDOG_TASK]["arguments"] = tasks[TUNNEL_WATCHDOG_TASK][
            "arguments"
        ].replace("0" * 64, RUNTIME_CONTRACT.tunnel.executable_sha256)

        tasks[CONTROL_PLANE_MANAGER_TASK]["arguments"] = tasks[CONTROL_PLANE_MANAGER_TASK][
            "arguments"
        ].replace(" execute ", " register ")
        contract = evaluate_task_contracts(tasks, catalog_sha256)
        self.assertFalse(contract["ok"])
        self.assertIn("manager_arguments", contract["violations"])
        tasks[CONTROL_PLANE_MANAGER_TASK]["arguments"] = tasks[CONTROL_PLANE_MANAGER_TASK][
            "arguments"
        ].replace(" register ", " execute ")

        tasks[HTTP_WATCHDOG_TASK]["trigger_types"] = [
            "RegistrationTrigger",
            "LogonTrigger",
            "BootTrigger",
        ]
        contract = evaluate_task_contracts(tasks, catalog_sha256)
        self.assertFalse(contract["ok"])
        self.assertIn("http_watchdog_unexpected_trigger_type", contract["violations"])
        tasks[HTTP_WATCHDOG_TASK]["trigger_types"] = [
            "RegistrationTrigger",
            "LogonTrigger",
            "SessionStateChangeTrigger",
        ]
        tasks[HTTP_TASK]["action_count"] = 2
        contract = evaluate_task_contracts(tasks, catalog_sha256)
        self.assertFalse(contract["ok"])
        self.assertIn("http_action_contract", contract["violations"])
        tasks[HTTP_TASK]["action_count"] = 1
        tasks[HTTP_WATCHDOG_TASK]["arguments"] = (
            f'"{http_watchdog_script}" '
            "--loop-interval-seconds 60"
        )
        contract = evaluate_task_contracts(tasks, catalog_sha256)
        self.assertFalse(contract["ok"])
        self.assertIn("http_watchdog_tool_catalog_fingerprint", contract["violations"])

        tasks[HTTP_WATCHDOG_TASK]["arguments"] = (
            f'"{http_watchdog_script}" '
            f"--loop-interval-seconds 60 --expected-tool-catalog-sha256 {catalog_sha256}"
        )
        tasks[TUNNEL_WATCHDOG_TASK]["arguments"] = (
            f"{tunnel_watchdog_prefix} "
            f"--loop-interval-seconds 60 --expected-tool-catalog-sha256 {catalog_sha256}"
        )
        contract = evaluate_task_contracts(tasks, catalog_sha256)
        self.assertFalse(contract["ok"])
        self.assertIn("tunnel_watchdog_loop_interval", contract["violations"])

        tasks[TUNNEL_WATCHDOG_TASK]["arguments"] = (
            f"{tunnel_watchdog_prefix} "
            f"--loop-interval-seconds 15 --expected-tool-catalog-sha256 {catalog_sha256}"
        )
        contract = evaluate_task_contracts(tasks, catalog_sha256)
        self.assertFalse(contract["ok"])
        self.assertIn("tunnel_watchdog_main_task_fingerprint", contract["violations"])

        live_catalog_sha256 = "b" * 64
        for task_name in (HTTP_WATCHDOG_TASK, TUNNEL_WATCHDOG_TASK):
            tasks[task_name]["arguments"] = tasks[task_name]["arguments"].replace(
                catalog_sha256,
                live_catalog_sha256,
            )
        tasks[TUNNEL_WATCHDOG_TASK]["arguments"] += (
            f" --main-task-arguments-sha256 {APPROVED_TUNNEL_ARGUMENTS_SHA256}"
        )
        contract = evaluate_task_contracts(tasks, live_catalog_sha256)
        self.assertTrue(contract["ok"])

        tasks[HTTP_WATCHDOG_TASK]["arguments"] = tasks[HTTP_WATCHDOG_TASK][
            "arguments"
        ].replace(live_catalog_sha256, catalog_sha256)
        contract = evaluate_task_contracts(tasks, live_catalog_sha256)
        self.assertFalse(contract["ok"])
        self.assertIn("http_watchdog_tool_catalog_fingerprint", contract["violations"])

    def test_doctor_import_does_not_read_the_default_runtime_manifest(self) -> None:
        code = (
            "from pathlib import Path;"
            "import coding_tools_mcp.control_plane_runtime_contract as contract;"
            "contract.DEFAULT_RUNTIME_CONTRACT_PATH=Path('definitely-missing-contract.json');"
            "import scripts.control_plane_doctor;"
            "print('ok')"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[1],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "ok")

    def test_ready_requires_pinned_dangerous_high_integrity_runtime_contract(self) -> None:
        with TemporaryDirectory() as temporary:
            config = Path(temporary) / "task.json"
            config.write_text(
                json.dumps(
                    {
                        "log_file": "C:/logs/mcp.log",
                        "auth_header_file": "C:/secrets/backend-authorization.txt",
                        "require_config_sha256_pin": True,
                        "require_windows_administrator": True,
                        "server_args": [
                            "--workspace", "D:/coding-tools-mcp",
                            "--permission-mode", "dangerous",
                            "--dangerously-allow-any-local-path",
                            "--enable-computer-use",
                        ],
                    }
                ),
                encoding="utf-8",
            )
            digest = sha256(config.read_bytes()).hexdigest()
            task_arguments = f'"runner.py" --expect-config-sha256 {digest}'
            backend = BackendProbe(
                True,
                "ready",
                "server_info",
                False,
                "http://127.0.0.1:18765/mcp",
                "ready",
                permission_mode="dangerous",
                filesystem_scope="all_local_drives",
                dangerously_skip_all_permissions=True,
                dangerously_allow_any_local_path=True,
                annotation_override=None,
                computer_use_enabled=True,
            )

            contract = evaluate_runtime_contract(config, task_arguments, backend)
            self.assertTrue(contract["ok"])

            backend = BackendProbe(
                True,
                "ready",
                "server_info",
                False,
                "http://127.0.0.1:18765/mcp",
                "ready",
                permission_mode="trusted",
            )
            contract = evaluate_runtime_contract(config, task_arguments, backend)
            self.assertFalse(contract["ok"])
            self.assertIn("runtime_permission_mode", contract["violations"])


if __name__ == "__main__":
    unittest.main()
