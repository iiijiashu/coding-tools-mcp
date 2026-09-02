from __future__ import annotations

import json
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest.mock import patch

import scripts.local_http_mcp_watchdog as watchdog

from scripts.local_http_mcp_watchdog import (
    ControlPlaneLease,
    build_parser,
    decide_action,
    inspect_control_plane_lease,
    lease_is_active,
    load_state,
    save_state,
)


class LocalHttpMcpWatchdogTests(unittest.TestCase):
    def test_authenticated_backend_identity_arguments_are_required(self) -> None:
        parser = build_parser()
        base = [
            "--task-name", "task",
            "--health-url", "http://127.0.0.1/health",
            "--event-log", "events.jsonl",
        ]
        with self.assertRaises(SystemExit):
            parser.parse_args(base)

    def test_authenticated_backend_identity_arguments_must_be_non_empty(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "--task-name", "task",
                    "--health-url", "http://127.0.0.1/health",
                    "--mcp-url", "",
                    "--expected-workspace", "D:/coding-tools-mcp",
                    "--authorization-header-file", "authorization.txt",
                    "--event-log", "events.jsonl",
                ]
            )

    def test_non_running_decision_matrix(self) -> None:
        cases = (
            ("Disabled", False, "disabled", 2),
            ("Ready", False, "start", 3),
            ("Running", True, "healthy", 0),
            ("Queued", False, "wait", 2),
        )
        for state, healthy, expected_action, expected_failures in cases:
            with self.subTest(state=state, healthy=healthy):
                decision = decide_action(
                    state,
                    healthy,
                    consecutive_failures=2,
                    failure_threshold=3,
                )
                self.assertEqual(decision.action, expected_action)
                self.assertEqual(decision.consecutive_failures, expected_failures)

    def test_running_task_requires_three_consecutive_restartable_failures(self) -> None:
        first = decide_action("Running", False, 0, 3, restartable=True)
        second = decide_action(
            "Running", False, first.consecutive_failures, 3, restartable=True
        )
        third = decide_action(
            "Running", False, second.consecutive_failures, 3, restartable=True
        )

        self.assertEqual(first.action, "degraded")
        self.assertEqual(first.consecutive_failures, 1)
        self.assertEqual(second.action, "degraded")
        self.assertEqual(second.consecutive_failures, 2)
        self.assertEqual(third.action, "restart")
        self.assertEqual(third.consecutive_failures, 3)

    def test_http_task_reregistration_requires_one_exec_action(self) -> None:
        completed = watchdog.subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch.object(watchdog, "run_powershell", return_value=completed) as runner:
            watchdog.reregister_task("http task", stop_running=False)
        script = runner.call_args.args[2]
        self.assertIn("$actionNodes", script)
        self.assertIn("LocalName -ne 'Exec'", script)

    def test_lifecycle_mutation_holds_the_shared_control_plane_lock(self) -> None:
        events: list[str] = []

        @contextmanager
        def locked(_state_root: Path):
            events.append("lock_enter")
            try:
                yield
            finally:
                events.append("lock_exit")

        args = SimpleNamespace(
            control_plane_lease_file=Path("state") / "active.json",
            control_plane_stale_seconds=300.0,
            task_name="http task",
            task_path="\\",
        )
        with (
            patch.object(watchdog, "process_lock", side_effect=locked),
            patch.object(
                watchdog,
                "inspect_control_plane_lease",
                return_value=ControlPlaneLease("inactive", "missing"),
            ),
            patch.object(
                watchdog,
                "start_task",
                side_effect=lambda *_args: events.append("start"),
            ),
        ):
            performed, lease = watchdog.run_guarded_lifecycle_action(args, "start")

        self.assertTrue(performed)
        self.assertEqual(lease.action, "inactive")
        self.assertEqual(events, ["lock_enter", "start", "lock_exit"])

    def test_lifecycle_mutation_holds_when_manager_owns_the_shared_lock(self) -> None:
        args = SimpleNamespace(
            control_plane_lease_file=Path("state") / "active.json",
            control_plane_stale_seconds=300.0,
            task_name="http task",
            task_path="\\",
        )
        with (
            patch.object(
                watchdog,
                "process_lock",
                side_effect=watchdog.LeaseBusy("busy"),
            ),
            patch.object(watchdog, "start_task") as starter,
        ):
            performed, lease = watchdog.run_guarded_lifecycle_action(args, "start")

        self.assertFalse(performed)
        self.assertEqual(lease.reason, "manager_lock_busy")
        starter.assert_not_called()

    def test_running_restart_is_rate_limited_after_an_unrecovered_attempt(self) -> None:
        decision = decide_action(
            "Running",
            False,
            3,
            3,
            restartable=True,
            start_allowed=False,
        )

        self.assertEqual(decision.action, "restart_backoff")
        self.assertEqual(decision.consecutive_failures, 4)

    def test_non_retryable_backend_failure_never_restarts_the_task(self) -> None:
        decision = decide_action(
            "Running", False, 99, 3, restartable=False
        )
        self.assertEqual(decision.action, "blocked")
        self.assertEqual(decision.consecutive_failures, 99)

    def test_active_control_plane_transaction_holds_lifecycle_mutation(self) -> None:
        decision = decide_action(
            "Running",
            False,
            2,
            3,
            restartable=True,
            start_allowed=True,
            transaction_active=True,
        )
        self.assertEqual(decision.action, "transaction_hold")
        self.assertEqual(decision.consecutive_failures, 2)

    def test_lease_detection_fails_closed_for_malformed_active_state(self) -> None:
        with TemporaryDirectory() as temporary:
            active = Path(temporary) / "active.json"
            self.assertFalse(lease_is_active(active))
            active.write_text("not-json", encoding="utf-8")
            self.assertTrue(lease_is_active(active))
            active.write_text('{"operation_id":"' + "a" * 32 + '"}', encoding="utf-8")
            self.assertTrue(lease_is_active(active))

    def test_failed_control_plane_receipt_requires_recovery_not_permanent_hold(self) -> None:
        with TemporaryDirectory() as temporary:
            state_root = Path(temporary)
            operation_id = "a" * 32
            active = state_root / "active.json"
            active.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "operation_id": operation_id,
                        "created_at": "2026-09-02T01:49:22.767Z",
                    }
                ),
                encoding="utf-8",
            )
            receipts = state_root / "receipts"
            receipts.mkdir()
            (receipts / f"{operation_id}.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "operation_id": operation_id,
                        "state": "failed",
                        "updated_at": "2026-09-02T01:54:23.384Z",
                    }
                ),
                encoding="utf-8",
            )

            lease = inspect_control_plane_lease(active)

            self.assertEqual(lease.action, "recover")
            self.assertEqual(lease.operation_id, operation_id)
            self.assertEqual(lease.reason, "terminal_failed")

    def test_fresh_control_plane_lease_without_receipt_holds(self) -> None:
        with TemporaryDirectory() as temporary:
            active = Path(temporary) / "active.json"
            active.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "operation_id": "b" * 32,
                        "created_at": "2026-09-02T01:49:22.767Z",
                    }
                ),
                encoding="utf-8",
            )

            lease = inspect_control_plane_lease(
                active,
                now_epoch=1788313800.0,
                stale_after_seconds=300.0,
            )

            self.assertEqual(lease.action, "hold")
            self.assertEqual(lease.reason, "transaction_starting")

    def test_running_recovery_uses_receipt_heartbeat_instead_of_original_lease_age(self) -> None:
        with TemporaryDirectory() as temporary:
            state_root = Path(temporary)
            operation_id = "c" * 32
            active = state_root / "active.json"
            active.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "operation_id": operation_id,
                        "created_at": "1970-01-01T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            receipts = state_root / "receipts"
            receipts.mkdir()
            (receipts / f"{operation_id}.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "operation_id": operation_id,
                        "state": "running",
                        "updated_at": "1970-01-01T00:16:35Z",
                    }
                ),
                encoding="utf-8",
            )

            lease = inspect_control_plane_lease(
                active,
                now_epoch=1000.0,
                stale_after_seconds=300.0,
            )

            self.assertEqual(lease.action, "hold")
            self.assertEqual(lease.reason, "manager_active")
            self.assertEqual(lease.age_seconds, 5.0)

    def test_recoverable_lease_is_executed_then_rechecked_before_decision(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            event_log = root / "events.jsonl"
            args = SimpleNamespace(
                task_name="http task",
                task_path="\\",
                health_url="http://127.0.0.1:18765/.well-known/mcp.json",
                mcp_url="http://127.0.0.1:18765/mcp",
                expected_workspace="D:/coding-tools-mcp",
                required_tool=[],
                probe_timeout_seconds=1.0,
                authorization_header_file=root / "authorization.txt",
                event_log=str(event_log),
                state_file=root / "state.json",
                failure_threshold=3,
                start_cooldown_seconds=120.0,
                control_plane_stale_seconds=300.0,
                startup_grace_seconds=0.0,
                control_plane_lease_file=root / "active.json",
            )
            unhealthy = watchdog.BackendProbe(
                False,
                "transport",
                "connect",
                True,
                args.mcp_url,
                "connection refused",
            )
            healthy = watchdog.BackendProbe(
                True,
                "healthy",
                "tools/list",
                False,
                args.mcp_url,
                "ready",
                tool_count=25,
            )
            leases = [
                ControlPlaneLease("recover", "terminal_failed", "a" * 32, 600.0),
                ControlPlaneLease("inactive", "missing"),
            ]
            with (
                patch.object(watchdog, "load_authorization_header", return_value="Bearer test"),
                patch.object(watchdog, "task_state", side_effect=["Running", "Running"]) as task_state,
                patch.object(watchdog, "probe", return_value=True),
                patch.object(watchdog, "probe_mcp_backend", side_effect=[unhealthy, healthy]),
                patch.object(watchdog, "inspect_control_plane_lease", side_effect=leases),
                patch.object(watchdog, "recover_control_plane_transaction", return_value=True) as recover,
            ):
                self.assertEqual(watchdog.run_once(args, None), 0)

            recover.assert_called_once_with(args.control_plane_lease_file)
            self.assertEqual(task_state.call_count, 2)
            event = json.loads(event_log.read_text(encoding="utf-8"))
            self.assertEqual(event["action"], "healthy")
            self.assertFalse(event["transaction_active"])
            self.assertTrue(event["control_plane_recovery_attempted"])
            self.assertTrue(event["control_plane_recovery_ok"])

    def test_metadata_health_failure_degrades_even_when_mcp_probe_succeeds(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = SimpleNamespace(
                task_name="http task",
                task_path="\\",
                health_url="http://127.0.0.1:18765/.well-known/mcp.json",
                mcp_url="http://127.0.0.1:18765/mcp",
                expected_workspace="D:/coding-tools-mcp",
                required_tool=[],
                probe_timeout_seconds=1.0,
                authorization_header_file=root / "authorization.txt",
                event_log=str(root / "events.jsonl"),
                state_file=root / "state.json",
                failure_threshold=3,
                start_cooldown_seconds=120.0,
                control_plane_stale_seconds=300.0,
                startup_grace_seconds=0.0,
                control_plane_lease_file=root / "active.json",
            )
            backend = watchdog.BackendProbe(
                True,
                "ready",
                "tools/list",
                False,
                args.mcp_url,
                "ready",
                tool_count=25,
            )
            with (
                patch.object(watchdog, "load_authorization_header", return_value="Bearer test"),
                patch.object(watchdog, "task_state", return_value="Running"),
                patch.object(watchdog, "probe_mcp_backend", return_value=backend),
                patch.object(watchdog, "probe", return_value=False) as metadata_probe,
                patch.object(
                    watchdog,
                    "inspect_control_plane_lease",
                    return_value=ControlPlaneLease("inactive", "missing"),
                ),
            ):
                self.assertEqual(watchdog.run_once(args, None), 0)

            metadata_probe.assert_called_once_with(args.health_url, args.probe_timeout_seconds)
            event = json.loads(Path(args.event_log).read_text(encoding="utf-8"))
            self.assertEqual(event["action"], "degraded")
            self.assertFalse(event["health_url_ok"])

    def test_operator_disabled_task_is_never_reenabled(self) -> None:
        decision = decide_action("Disabled", False, 9, 3, restartable=True)
        self.assertEqual(decision.action, "disabled")
        self.assertEqual(decision.consecutive_failures, 9)

    def test_ready_task_is_rate_limited_after_a_recent_start(self) -> None:
        decision = decide_action(
            "Ready",
            False,
            1,
            3,
            restartable=True,
            start_allowed=False,
        )
        self.assertEqual(decision.action, "start_backoff")
        self.assertEqual(decision.consecutive_failures, 2)

    def test_ready_task_with_healthy_backend_never_starts_a_second_instance(self) -> None:
        decision = decide_action(
            "Ready",
            True,
            2,
            3,
            restartable=True,
            start_allowed=True,
        )
        self.assertEqual(decision.action, "orphan_healthy")
        self.assertEqual(decision.consecutive_failures, 0)

    def test_ready_task_with_nonretryable_identity_failure_is_blocked(self) -> None:
        decision = decide_action(
            "Ready",
            False,
            2,
            3,
            restartable=False,
            start_allowed=True,
        )
        self.assertEqual(decision.action, "blocked")
        self.assertEqual(decision.consecutive_failures, 2)

    def test_watchdog_state_is_atomic_and_round_trips(self) -> None:
        with TemporaryDirectory() as temporary:
            state_file = Path(temporary) / "state.json"
            save_state(
                state_file,
                consecutive_failures=2,
                last_action="degraded",
                last_start_epoch=1234.5,
            )
            state = load_state(state_file)

            self.assertEqual(state["consecutive_failures"], 2)
            self.assertEqual(state["last_action"], "degraded")
            self.assertEqual(state["last_start_epoch"], 1234.5)
            self.assertFalse(state_file.with_name("state.json.tmp").exists())

    def test_start_uses_guarded_task_reregistration(self) -> None:
        digest = watchdog.hashlib.sha256(
            Path("config/local-mcp-http-task.json").read_bytes()
        ).hexdigest()
        self.assertEqual(
            watchdog.expected_http_task_arguments(),
            f'"{watchdog.EXPECTED_HTTP_TASK_SCRIPT}" '
            f'--config "{watchdog.EXPECTED_HTTP_TASK_CONFIG}" '
            f'--expect-config-sha256 {digest}',
        )
        with patch.object(watchdog, "reregister_task") as reregister:
            watchdog.start_task("http task")
        reregister.assert_called_once_with("http task", stop_running=False, task_path="\\")

    def test_restart_fails_closed_when_previous_task_does_not_stop(self) -> None:
        completed = watchdog.subprocess.CompletedProcess(
            args=[], returncode=32, stdout="", stderr=""
        )
        with (
            patch.object(watchdog, "run_powershell", return_value=completed),
            self.assertRaisesRegex(RuntimeError, "did not stop"),
        ):
            watchdog.reregister_task("http task", stop_running=True)

    def test_loop_continues_after_one_iteration_error(self) -> None:
        with TemporaryDirectory() as temporary:
            event_log = Path(temporary) / "watchdog-events.jsonl"
            args = SimpleNamespace(event_log=str(event_log))
            with (
                patch.object(watchdog, "run_once", side_effect=[RuntimeError("boom"), 0]) as run,
                patch.object(watchdog.time, "sleep") as sleep,
            ):
                self.assertEqual(
                    watchdog.run_loop(
                        args,
                        "Bearer test",
                        interval_seconds=60,
                        max_iterations=2,
                    ),
                    0,
                )
            self.assertEqual(run.call_count, 2)
            sleep.assert_called_once_with(60)
            event = json.loads(event_log.read_text(encoding="utf-8"))
            self.assertEqual(event["action"], "watchdog_error")


if __name__ == "__main__":
    unittest.main()
