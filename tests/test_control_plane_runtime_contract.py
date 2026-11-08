from __future__ import annotations

import json
import subprocess
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from tempfile import TemporaryDirectory

from coding_tools_mcp.control_plane_runtime_contract import (
    DEFAULT_RUNTIME_CONTRACT_PATH,
    RuntimeContractError,
    load_runtime_contract,
)


class ControlPlaneRuntimeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = json.loads(DEFAULT_RUNTIME_CONTRACT_PATH.read_text(encoding="utf-8"))

    def _load_payload(self, payload: object):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "contract.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return load_runtime_contract(path)

    def test_default_manifest_loads_as_immutable_contract(self) -> None:
        contract = load_runtime_contract()

        self.assertEqual(contract.schema_version, 1)
        self.assertEqual(contract.task_path, "\\")
        self.assertEqual(contract.pythonw_relative, ".venv/Scripts/pythonw.exe")
        self.assertIn("computer_screenshot", contract.tool_catalog.required_tools)
        self.assertFalse(hasattr(contract.tool_catalog, "sha256"))
        self.assertEqual(contract.tunnel.main_arguments_sha256, "e57538e1" + "c619b731da65b26c48d50b566b6af9fa3a90ecb79497a4b44a13388d")
        self.assertEqual(tuple(task.key for task in contract.tasks), (
            "http",
            "http_watchdog",
            "tunnel",
            "tunnel_watchdog",
            "manager",
        ))
        self.assertEqual(contract.task("http_watchdog").loop_interval_seconds, 60)
        self.assertEqual(contract.task("tunnel_watchdog").loop_interval_seconds, 15)
        self.assertTrue(contract.task("tunnel_watchdog").pin_tool_catalog)
        self.assertTrue(contract.task("tunnel_watchdog").pin_main_arguments)
        self.assertFalse(contract.task("manager").pin_tool_catalog)
        with self.assertRaises(FrozenInstanceError):
            contract.task_path = "changed"  # type: ignore[misc]

    def test_cli_emits_normalized_json_without_inline_code_quoting(self) -> None:
        module_path = (
            Path(__file__).resolve().parents[1]
            / "coding_tools_mcp"
            / "control_plane_runtime_contract.py"
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(module_path),
                "--contract",
                str(DEFAULT_RUNTIME_CONTRACT_PATH),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["tasks"]["tunnel_watchdog"]["loop_interval_seconds"], 15)

    def test_rejects_unknown_and_missing_keys_at_every_contract_level(self) -> None:
        cases: list[dict[str, object]] = []

        unknown_top = json.loads(json.dumps(self.payload))
        unknown_top["secret_file"] = "C:/secret"
        cases.append(unknown_top)
        missing_top = json.loads(json.dumps(self.payload))
        del missing_top["task_path"]
        cases.append(missing_top)
        unknown_catalog = json.loads(json.dumps(self.payload))
        unknown_catalog["tool_catalog"]["extra"] = True
        cases.append(unknown_catalog)
        missing_tunnel = json.loads(json.dumps(self.payload))
        del missing_tunnel["tunnel"]["main_arguments_sha256"]
        cases.append(missing_tunnel)
        unknown_task = json.loads(json.dumps(self.payload))
        unknown_task["tasks"]["http"]["extra"] = False
        cases.append(unknown_task)

        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(RuntimeContractError):
                    self._load_payload(payload)

    def test_rejects_invalid_hashes_and_duplicate_required_tools(self) -> None:
        for section, key, value in (
            ("tunnel", "main_arguments_sha256", "0" * 63),
            ("tunnel", "main_arguments_sha256", "A" * 64),
            ("tunnel", "executable_sha256", "f" * 63),
        ):
            payload = json.loads(json.dumps(self.payload))
            payload[section][key] = value
            with self.subTest(section=section, value=value):
                with self.assertRaises(RuntimeContractError):
                    self._load_payload(payload)

        payload = json.loads(json.dumps(self.payload))
        payload["tool_catalog"]["required_tools"].append("server_info")
        with self.assertRaises(RuntimeContractError):
            self._load_payload(payload)

        payload = json.loads(json.dumps(self.payload))
        payload["tool_catalog"]["required_tools"].remove("computer_keyboard")
        with self.assertRaises(RuntimeContractError):
            self._load_payload(payload)

    def test_schema_v1_rejects_weakened_role_invariants(self) -> None:
        mutations = (
            ("http_watchdog", "must_be_running", False),
            ("http_watchdog", "restart_on_failure", False),
            ("http_watchdog", "pin_tool_catalog", False),
            ("http_watchdog", "required_triggers", ["registration"]),
            ("tunnel_watchdog", "pin_main_arguments", False),
            ("tunnel_watchdog", "loop_interval_seconds", 1),
            ("manager", "restart_on_failure", False),
            ("tunnel", "run_level", "Highest"),
            ("http", "name", "lookalike task"),
        )
        for task_key, field, value in mutations:
            payload = json.loads(json.dumps(self.payload))
            payload["tasks"][task_key][field] = value
            with self.subTest(task=task_key, field=field, value=value):
                with self.assertRaises(RuntimeContractError):
                    self._load_payload(payload)

    def test_rejects_absolute_and_parent_traversing_script_paths(self) -> None:
        for value in (
            "D:/coding-tools-mcp/scripts/run_local_mcp_http_task.py",
            "/scripts/run_local_mcp_http_task.py",
            "scripts/../run_local_mcp_http_task.py",
            "..\\scripts\\run_local_mcp_http_task.py",
        ):
            payload = json.loads(json.dumps(self.payload))
            payload["tasks"]["http"]["script_relative"] = value
            with self.subTest(value=value):
                with self.assertRaises(RuntimeContractError):
                    self._load_payload(payload)

    def test_rejects_invalid_task_sets_keys_and_types(self) -> None:
        payload = json.loads(json.dumps(self.payload))
        del payload["tasks"]["manager"]
        with self.assertRaises(RuntimeContractError):
            self._load_payload(payload)

        payload = json.loads(json.dumps(self.payload))
        payload["tasks"]["surprise"] = payload["tasks"].pop("manager")
        with self.assertRaises(RuntimeContractError):
            self._load_payload(payload)

        mutations = (
            ("http", "must_be_running", 1),
            ("http", "loop_interval_seconds", True),
            ("http", "required_triggers", ["registration", 1]),
            ("manager", "run_level", "Admin"),
            ("manager", "script_relative", 7),
            ("tunnel", "script_relative", ""),
        )
        for task_key, field, value in mutations:
            payload = json.loads(json.dumps(self.payload))
            payload["tasks"][task_key][field] = value
            with self.subTest(task=task_key, field=field, value=value):
                with self.assertRaises(RuntimeContractError):
                    self._load_payload(payload)


if __name__ == "__main__":
    unittest.main()
