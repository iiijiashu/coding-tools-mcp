from __future__ import annotations

import json
import os
import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.control_plane_transaction import (
    LeaseBusy,
    _recover_incomplete_update,
    _replace_file_atomic,
    _set_task_arguments,
    build_request,
    execute_active,
    expected_main_task_arguments,
    install_request,
    process_lock,
    replace_expected_pin,
    run_manager_loop,
    start_manager,
    validate_main_task_contract,
    validate_state_root_security_payload,
    validate_generation,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


class ControlPlaneTransactionTests(unittest.TestCase):
    def test_restart_request_requires_a_concrete_sha256_generation(self) -> None:
        request = build_request("restart", "a" * 64)

        self.assertEqual(request["operation"], "restart")
        self.assertEqual(request["expected_sha256"], "a" * 64)
        self.assertRegex(str(request["operation_id"]), r"^[a-f0-9]{32}$")

        with self.assertRaisesRegex(ValueError, "SHA-256"):
            build_request("restart", "latest")

    def test_generation_cas_binds_config_bytes_and_task_action(self) -> None:
        with TemporaryDirectory() as temporary:
            config = Path(temporary) / "task.json"
            config.write_text('{"log_file":"x","server_args":["--workspace","D:/"]}', encoding="utf-8")
            digest = sha256(config.read_bytes()).hexdigest()
            arguments = f'"runner.py" --expect-config-sha256 {digest}'

            self.assertEqual(validate_generation(config, arguments, digest), digest)
            with self.assertRaisesRegex(RuntimeError, "config generation changed"):
                validate_generation(config, arguments, "b" * 64)
            with self.assertRaisesRegex(RuntimeError, "task pin"):
                validate_generation(config, '"runner.py" --expect-config-sha256 ' + "c" * 64, digest)

    def test_active_lease_is_exclusive_and_request_is_durable(self) -> None:
        with TemporaryDirectory() as temporary:
            state_root = Path(temporary)
            first = build_request("restart", "a" * 64)
            install_request(first, state_root)

            active = json.loads((state_root / "active.json").read_text(encoding="utf-8"))
            durable = json.loads(
                (state_root / "requests" / f'{first["operation_id"]}.json').read_text(encoding="utf-8")
            )
            self.assertEqual(active["operation_id"], first["operation_id"])
            self.assertEqual(durable, first)

            with self.assertRaises(LeaseBusy):
                install_request(build_request("restart", "a" * 64), state_root)

    @unittest.skipUnless(os.name == "nt", "Windows lock regression")
    def test_failed_process_lock_acquire_does_not_unlock_an_unowned_byte(self) -> None:
        with TemporaryDirectory() as temporary:
            with patch("msvcrt.locking", side_effect=PermissionError("busy")) as locking:
                with self.assertRaises(LeaseBusy):
                    with process_lock(Path(temporary)):
                        self.fail("busy lock must not be entered")
        self.assertEqual(locking.call_count, 1)

    def test_update_writes_backup_and_replacement_through_atomic_helper(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_root = root / "state"
            config = root / "task.json"
            candidate = root / "candidate.json"
            original_bytes = b'{"version":1}\n'
            candidate_bytes = b'{"version":2}\n'
            config.write_bytes(original_bytes)
            candidate.write_bytes(candidate_bytes)
            expected = "a" * 64
            request = build_request(
                "update",
                expected,
                candidate=candidate,
                candidate_sha256=sha256(candidate_bytes).hexdigest(),
            )
            install_request(request, state_root)

            with (
                patch("scripts.control_plane_transaction.CONFIG", config),
                patch("scripts.control_plane_transaction.validate_state_root_security"),
                patch("scripts.control_plane_transaction.validate_main_task_contract"),
                patch(
                    "scripts.control_plane_transaction.task_snapshot",
                    return_value={"Arguments": f'runner --expect-config-sha256 {expected}'},
                ),
                patch(
                    "scripts.control_plane_transaction._validate_candidate",
                    return_value=candidate_bytes,
                ),
                patch(
                    "scripts.control_plane_transaction._replace_file_atomic",
                    wraps=_replace_file_atomic,
                ) as atomic_replace,
                patch("scripts.control_plane_transaction._set_task_arguments"),
                patch("scripts.control_plane_transaction._restart_main_task"),
                patch(
                    "scripts.control_plane_transaction._doctor_snapshot",
                    return_value={"status": "READY"},
                ),
            ):
                self.assertEqual(execute_active(state_root), 0)

            backup_path = (
                state_root / "backups" / f'{request["operation_id"]}-config.json'
            )
            self.assertEqual(
                [call.args[0] for call in atomic_replace.call_args_list],
                [backup_path, config],
            )
            self.assertEqual(backup_path.read_bytes(), original_bytes)
            self.assertEqual(config.read_bytes(), candidate_bytes)

    def test_task_pin_replacement_is_exact_and_fails_closed_on_drift(self) -> None:
        old = "a" * 64
        new = "b" * 64
        arguments = f'"runner.py" --expect-config-sha256 {old}'
        self.assertEqual(
            replace_expected_pin(arguments, old, new),
            f'"runner.py" --expect-config-sha256 {new}',
        )
        with self.assertRaisesRegex(RuntimeError, "task pin"):
            replace_expected_pin(arguments, "c" * 64, new)

    def test_main_task_contract_rejects_action_or_principal_drift(self) -> None:
        digest = sha256(Path("config/local-mcp-http-task.json").read_bytes()).hexdigest()
        expected_arguments = expected_main_task_arguments(digest)
        runner = REPO_ROOT / "scripts" / "run_local_mcp_http_task.py"
        config = REPO_ROOT / "config" / "local-mcp-http-task.json"
        self.assertEqual(
            expected_arguments,
            f'"{runner}" '
            f'--config "{config}" '
            f'--expect-config-sha256 {digest}',
        )
        task = {
            "Enabled": True,
            "RunLevel": "Highest",
            "LogonType": "Interactive",
            "MultipleInstances": "IgnoreNew",
            "ActionCount": 1,
            "ActionType": "Exec",
            "Execute": str(REPO_ROOT / ".venv" / "Scripts" / "pythonw.exe"),
            "Arguments": expected_arguments,
            "WorkingDirectory": "",
            "TaskPath": "\\",
            "IdentityMatch": True,
        }

        validate_main_task_contract(task, digest)
        with self.assertRaisesRegex(RuntimeError, "executable"):
            validate_main_task_contract({**task, "Execute": "C:\\Windows\\System32\\cmd.exe"}, digest)
        with self.assertRaisesRegex(RuntimeError, "arguments"):
            validate_main_task_contract({**task, "Arguments": expected_arguments + " --extra"}, digest)
        with self.assertRaisesRegex(RuntimeError, "Interactive"):
            validate_main_task_contract({**task, "LogonType": "S4U"}, digest)
        with self.assertRaisesRegex(RuntimeError, "IgnoreNew"):
            validate_main_task_contract({**task, "MultipleInstances": None}, digest)
        with self.assertRaisesRegex(RuntimeError, "principal"):
            validate_main_task_contract({**task, "IdentityMatch": False}, digest)
        with self.assertRaisesRegex(RuntimeError, "TaskPath"):
            validate_main_task_contract({**task, "TaskPath": "\\Other\\"}, digest)
        with self.assertRaisesRegex(RuntimeError, "exactly one Exec action"):
            validate_main_task_contract({**task, "ActionCount": 2}, digest)
        with self.assertRaisesRegex(RuntimeError, "exactly one Exec action"):
            validate_main_task_contract({**task, "ActionType": "ComHandler"}, digest)

    def test_terminal_receipt_without_journal_only_releases_stale_lease(self) -> None:
        with TemporaryDirectory() as temporary:
            state_root = Path(temporary)
            operation_id = "d" * 32
            (state_root / "receipts").mkdir()
            (state_root / "active.json").write_text(
                json.dumps({"schema_version": 1, "operation_id": operation_id}),
                encoding="utf-8",
            )
            (state_root / "receipts" / f"{operation_id}.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "operation_id": operation_id,
                        "state": "succeeded",
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch("scripts.control_plane_transaction.validate_state_root_security"),
                patch(
                    "scripts.control_plane_transaction.task_snapshot",
                    side_effect=AssertionError("terminal operation must not run twice"),
                ),
            ):
                self.assertEqual(execute_active(state_root), 0)
            self.assertFalse((state_root / "active.json").exists())

    def test_control_plane_state_root_rejects_medium_writable_acl(self) -> None:
        secure = {
            "owner_sid": "S-1-5-32-544",
            "protected": True,
            "high_integrity": True,
            "has_reparse_point": False,
            "rules": [
                {"sid": "S-1-5-18", "allow": True, "writable": True},
                {"sid": "S-1-5-32-544", "allow": True, "writable": True},
                {"sid": "S-1-5-21-1-2-3-1001", "allow": True, "writable": False},
            ],
        }
        validate_state_root_security_payload(secure)
        insecure = {
            **secure,
            "rules": [
                *secure["rules"],
                {"sid": "S-1-5-21-1-2-3-1001", "allow": True, "writable": True},
            ],
        }
        with self.assertRaisesRegex(RuntimeError, "writable"):
            validate_state_root_security_payload(insecure)

    def test_failed_prepared_restart_is_recovered_and_releases_lease(self) -> None:
        with TemporaryDirectory() as temporary:
            state_root = Path(temporary)
            operation_id = "e" * 32
            for name in ("requests", "journals", "receipts"):
                (state_root / name).mkdir()
            (state_root / "active.json").write_text(
                json.dumps({"schema_version": 1, "operation_id": operation_id}),
                encoding="utf-8",
            )
            (state_root / "requests" / f"{operation_id}.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "operation_id": operation_id,
                        "operation": "restart",
                        "expected_sha256": "a" * 64,
                    }
                ),
                encoding="utf-8",
            )
            (state_root / "journals" / f"{operation_id}.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "operation_id": operation_id,
                        "operation": "restart",
                        "phase": "prepared",
                        "old_backend_pid": 100,
                    }
                ),
                encoding="utf-8",
            )
            (state_root / "receipts" / f"{operation_id}.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "operation_id": operation_id,
                        "state": "failed",
                    }
                ),
                encoding="utf-8",
            )
            snapshot = {
                "status": "READY",
                "port_owner": {"pid": 200},
                "runtime_contract": {"actual_config_sha256": "a" * 64},
                "local_mcp": {"permission_mode": "dangerous"},
                "tunnel": {"ok": True},
            }
            with (
                patch("scripts.control_plane_transaction.validate_state_root_security"),
                patch("scripts.control_plane_transaction._listener_pid", return_value=100),
                patch("scripts.control_plane_transaction._restart_main_task") as restart,
                patch("scripts.control_plane_transaction._doctor_snapshot", return_value=snapshot),
            ):
                self.assertEqual(execute_active(state_root), 0)
            restart.assert_called_once_with()
            receipt = json.loads(
                (state_root / "receipts" / f"{operation_id}.json").read_text(encoding="utf-8")
            )
            self.assertEqual(receipt["state"], "succeeded")
            self.assertTrue(receipt["recovered"])
            self.assertFalse((state_root / "active.json").exists())
            self.assertFalse((state_root / "journals" / f"{operation_id}.json").exists())

    def test_update_recovery_reopens_terminal_receipt_before_doctor(self) -> None:
        with TemporaryDirectory() as temporary:
            state_root = Path(temporary)
            operation_id = "f" * 32
            config = state_root / "task.json"
            backup = state_root / "backup.json"
            config.write_bytes(b"new config")
            backup.write_bytes(b"old config")
            old_digest = "a" * 64
            new_digest = "b" * 64
            old_arguments = f"runner --expect-config-sha256 {old_digest}"
            journal = {
                "backup_path": str(backup),
                "old_arguments": old_arguments,
                "old_sha256": old_digest,
                "new_sha256": new_digest,
            }

            def ready_only_while_recovery_is_active() -> dict[str, object]:
                receipt = json.loads(
                    (state_root / "receipts" / f"{operation_id}.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(receipt["state"], "running")
                self.assertTrue(receipt["recovered"])
                return {"status": "READY"}

            with (
                patch("scripts.control_plane_transaction.CONFIG", config),
                patch("scripts.control_plane_transaction._set_task_arguments"),
                patch("scripts.control_plane_transaction._restart_main_task"),
                patch(
                    "scripts.control_plane_transaction._doctor_snapshot",
                    side_effect=ready_only_while_recovery_is_active,
                ),
            ):
                _recover_incomplete_update(state_root, operation_id, journal)

            receipt = json.loads(
                (state_root / "receipts" / f"{operation_id}.json").read_text(encoding="utf-8")
            )
            self.assertEqual(receipt["state"], "rolled_back")
            self.assertEqual(config.read_bytes(), b"old config")

    def test_long_lived_manager_retries_a_failed_transaction_iteration(self) -> None:
        with (
            patch("scripts.control_plane_transaction.execute_active", side_effect=[1, 0]) as execute,
            patch("scripts.control_plane_transaction.time.sleep") as sleep,
        ):
            self.assertEqual(run_manager_loop(Path("state"), interval_seconds=60, max_iterations=2), 0)
        self.assertEqual(execute.call_count, 2)
        sleep.assert_called_once_with(60)

    def test_long_lived_manager_survives_and_records_an_iteration_exception(self) -> None:
        with TemporaryDirectory() as temporary:
            state_root = Path(temporary)
            with (
                patch(
                    "scripts.control_plane_transaction.execute_active",
                    side_effect=[RuntimeError("transient"), 0],
                ) as execute,
                patch("scripts.control_plane_transaction.time.sleep") as sleep,
            ):
                self.assertEqual(
                    run_manager_loop(state_root, interval_seconds=60, max_iterations=2),
                    0,
                )
            self.assertEqual(execute.call_count, 2)
            sleep.assert_called_once_with(60)
            event = json.loads(
                (state_root / "manager-events.jsonl").read_text(encoding="utf-8")
            )
            self.assertEqual(event["action"], "manager_iteration_error")
            self.assertEqual(event["error_type"], "RuntimeError")

    def test_manager_start_uses_guarded_registration_trigger(self) -> None:
        with patch("scripts.control_plane_transaction._run_powershell", return_value="") as runner:
            start_manager()
        script = runner.call_args.args[0]
        environment = runner.call_args.args[1]
        self.assertIn("RegistrationTrigger", script)
        self.assertIn("TimeTrigger", script)
        self.assertIn("MultipleInstancesPolicy", script)
        self.assertIn("InteractiveToken", script)
        self.assertIn("WorkingDirectory", script)
        self.assertIn("$actionNodes", script)
        self.assertIn("LocalName -ne 'Exec'", script)
        self.assertIn("WindowsIdentity", script)
        self.assertIn("Register-ScheduledTask", script)
        self.assertNotIn("Start-ScheduledTask", script)
        self.assertIn("--loop-interval-seconds 60", environment["CTM_ARGUMENTS"])

    def test_task_argument_update_revalidates_exact_root_task_contract(self) -> None:
        with patch("scripts.control_plane_transaction._run_powershell", return_value="") as runner:
            _set_task_arguments("new arguments", expected_existing_arguments=("old arguments",))
        script = runner.call_args.args[0]
        environment = runner.call_args.args[1]
        self.assertIn("-TaskPath '\\'", script)
        self.assertIn("WindowsIdentity", script)
        self.assertIn("MultipleInstances", script)
        self.assertIn("$actionNodes", script)
        self.assertIn("LocalName -ne 'Exec'", script)
        self.assertIn("$decodedAllowed=ConvertFrom-Json $env:CTM_ALLOWED_ARGUMENTS", script)
        self.assertIn("$allowed=@($decodedAllowed)", script)
        self.assertNotIn("$allowed=@(ConvertFrom-Json $env:CTM_ALLOWED_ARGUMENTS)", script)
        self.assertIn("CTM_ALLOWED_ARGUMENTS", environment)
        self.assertEqual(environment["CTM_ARGUMENTS"], "new arguments")


if __name__ == "__main__":
    unittest.main()
