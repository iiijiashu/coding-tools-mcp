from __future__ import annotations

import importlib.util
import hashlib
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "secure_tunnel_watchdog.py"
SPEC = importlib.util.spec_from_file_location("secure_tunnel_watchdog", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
watchdog = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = watchdog
SPEC.loader.exec_module(watchdog)


class SecureTunnelWatchdogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend_probe = patch.object(
            watchdog,
            "probe_mcp_backend",
            return_value=watchdog.BackendProbe(
                True,
                "ready",
                "server_info",
                False,
                "http://127.0.0.1:18765/mcp",
                "MCP backend is ready",
                workspace="D:/coding-tools-mcp",
                tool_count=22,
            ),
        )
        self.backend_probe.start()
        self.addCleanup(self.backend_probe.stop)

    def test_backend_gate_requires_consecutive_failures_before_stopping_tunnel(self) -> None:
        first = watchdog.decide_backend_action(
            "Running",
            backend_ok=False,
            consecutive_failures=0,
            failure_threshold=2,
        )
        second = watchdog.decide_backend_action(
            "Running",
            backend_ok=False,
            consecutive_failures=1,
            failure_threshold=2,
        )
        self.assertEqual(first, "backend_degraded")
        self.assertEqual(second, "stop_backend_unavailable")

    def test_backend_gate_holds_a_stopped_tunnel_until_mcp_is_ready(self) -> None:
        decision = watchdog.decide_backend_action(
            "Ready",
            backend_ok=False,
            consecutive_failures=1,
            failure_threshold=2,
        )
        self.assertEqual(decision, "hold_backend_unavailable")

    def test_backend_gate_allows_normal_tunnel_health_when_mcp_is_ready(self) -> None:
        self.assertEqual(
            watchdog.decide_backend_action(
                "Running",
                backend_ok=True,
                consecutive_failures=1,
                failure_threshold=2,
            ),
            "proceed",
        )

    def test_backend_flap_preserves_recovery_backoff_and_stops_only_after_threshold(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            watchdog.save_state(
                config.state_file,
                consecutive_failures=0,
                last_action="started_healthy",
                recovery_attempts=2,
                next_recovery_at=2000.0,
                last_recovery_at=1000.0,
                healthy_since_at=1400.0,
            )
            unavailable = watchdog.BackendProbe(
                False,
                "transport",
                "connect",
                True,
                config.backend_url,
                "backend unavailable",
            )
            with (
                patch.object(watchdog, "probe_mcp_backend", return_value=unavailable),
                patch.object(
                    watchdog,
                    "task_status",
                    return_value=watchdog.TaskStatus("Running", 267009),
                ),
                patch.object(watchdog, "stop_task") as stop,
            ):
                self.assertEqual(watchdog.run_once(config), 1)
                stop.assert_not_called()
                first = watchdog.load_state(config.state_file)
                self.assertEqual(first["last_action"], "backend_degraded")
                self.assertEqual(first["consecutive_failures"], 0)
                self.assertEqual(first["backend_consecutive_failures"], 1)
                self.assertEqual(first["recovery_attempts"], 2)
                self.assertEqual(first["next_recovery_at"], 2000.0)

                self.assertEqual(watchdog.run_once(config), 1)
                stop.assert_called_once_with(config)

            second = watchdog.load_state(config.state_file)
            self.assertEqual(second["last_action"], "stop_backend_unavailable")
            self.assertEqual(second["consecutive_failures"], 0)
            self.assertEqual(second["backend_consecutive_failures"], 2)
            self.assertEqual(second["recovery_attempts"], 2)
            self.assertEqual(second["next_recovery_at"], 2000.0)
            self.assertEqual(second["last_recovery_at"], 1000.0)

    def test_transport_failure_does_not_advance_backend_stop_budget(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            watchdog.save_state(
                config.state_file,
                consecutive_failures=1,
                backend_consecutive_failures=0,
                last_action="degraded",
            )
            unavailable = watchdog.BackendProbe(
                False,
                "transport",
                "connect",
                True,
                config.backend_url,
                "backend unavailable",
            )
            with (
                patch.object(watchdog, "probe_mcp_backend", return_value=unavailable),
                patch.object(
                    watchdog,
                    "task_status",
                    return_value=watchdog.TaskStatus("Running", 267009),
                ),
                patch.object(watchdog, "stop_task") as stop,
            ):
                self.assertEqual(watchdog.run_once(config), 1)
            stop.assert_not_called()
            state = watchdog.load_state(config.state_file)
            self.assertEqual(state["consecutive_failures"], 1)
            self.assertEqual(state["backend_consecutive_failures"], 1)

    def test_backend_failure_does_not_advance_transport_restart_budget(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            watchdog.save_state(
                config.state_file,
                consecutive_failures=0,
                backend_consecutive_failures=1,
                last_action="backend_degraded",
            )
            healthy_backend = watchdog.BackendProbe(
                True,
                "ready",
                "server_info",
                False,
                config.backend_url,
                "ready",
                workspace=config.backend_workspace,
                tools=("server_info", "computer_screenshot"),
            )
            unhealthy_tunnel = watchdog.HealthProbe(
                watchdog.LOCAL_UNHEALTHY,
                False,
                False,
                False,
                False,
            )
            with (
                patch.object(watchdog, "probe_mcp_backend", return_value=healthy_backend),
                patch.object(
                    watchdog,
                    "task_status",
                    return_value=watchdog.TaskStatus("Running", 267009),
                ),
                patch.object(watchdog, "probe_health", return_value=unhealthy_tunnel),
                patch.object(watchdog, "restart_task") as restart,
            ):
                self.assertEqual(watchdog.run_once(config), 0)
            restart.assert_not_called()
            state = watchdog.load_state(config.state_file)
            self.assertEqual(state["consecutive_failures"], 1)
            self.assertEqual(state["backend_consecutive_failures"], 0)

    def test_ready_task_is_started_even_after_zero_exit(self) -> None:
        decision = watchdog.decide_action("Ready", None, 0, 2)
        self.assertEqual(decision.action, "start")
        self.assertEqual(decision.consecutive_failures, 0)

    def test_running_task_requires_two_consecutive_health_failures(self) -> None:
        first = watchdog.decide_action("Running", watchdog.LOCAL_UNHEALTHY, 0, 2)
        second = watchdog.decide_action(
            "Running", watchdog.LOCAL_UNHEALTHY, first.consecutive_failures, 2
        )
        self.assertEqual(first.action, "degraded")
        self.assertEqual(first.consecutive_failures, 1)
        self.assertEqual(second.action, "restart")
        self.assertEqual(second.consecutive_failures, 2)

    def test_command_deadlines_do_not_consume_transport_restart_budget(self) -> None:
        first = watchdog.decide_action(
            "Running",
            watchdog.COMMAND_DEADLINE_OBSERVED,
            1,
            2,
        )
        self.assertEqual(first.action, "command_deadline_observed")
        self.assertEqual(first.consecutive_failures, 0)

    def test_healthy_tunnel_with_new_command_deadline_is_not_restarted(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            healthy = watchdog.HealthProbe(watchdog.HEALTHY, True, True, True, True)
            with (
                patch.object(
                    watchdog,
                    "task_status",
                    return_value=watchdog.TaskStatus("Running", 267009),
                ),
                patch.object(watchdog, "probe_health", return_value=healthy),
                patch.object(
                    watchdog,
                    "scan_deadline_drops",
                    return_value=("2026-09-02T15:00:00+08:00", 2),
                ),
                patch.object(watchdog, "restart_task") as restart,
            ):
                self.assertEqual(watchdog.run_once(config), 0)

            restart.assert_not_called()
            state = watchdog.load_state(config.state_file)
            self.assertEqual(state["last_action"], "command_deadline_observed")
            self.assertEqual(state["consecutive_failures"], 0)
            event = json.loads(config.event_log.read_text(encoding="utf-8"))
            self.assertEqual(event["health_kind"], watchdog.COMMAND_DEADLINE_OBSERVED)
            self.assertTrue(event["healthy"])
            self.assertEqual(event["new_deadline_drops"], 2)

    def test_deadline_log_scan_baselines_then_reports_only_new_events(self) -> None:
        with TemporaryDirectory() as temporary:
            log = Path(temporary) / "tunnel.jsonl"
            old = {
                "time": "2026-08-27T00:01:00+08:00",
                "msg": watchdog.DEADLINE_MESSAGE,
            }
            log.write_text(json.dumps(old) + "\n", encoding="utf-8")
            cursor, count = watchdog.scan_deadline_drops(log, "")
            self.assertEqual(cursor, old["time"])
            self.assertEqual(count, 0)
            newer = {
                "time": "2026-08-27T00:02:00+08:00",
                "msg": watchdog.DEADLINE_MESSAGE,
            }
            with log.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(newer) + "\n")
            next_cursor, count = watchdog.scan_deadline_drops(log, cursor)
            self.assertEqual(next_cursor, newer["time"])
            self.assertEqual(count, 1)

    def test_health_success_resets_failure_counter(self) -> None:
        decision = watchdog.decide_action("Running", watchdog.HEALTHY, 9, 2)
        self.assertEqual(decision.action, "healthy")
        self.assertEqual(decision.consecutive_failures, 0)

    def test_disabled_task_is_never_started(self) -> None:
        decision = watchdog.decide_action("Disabled", None, 1, 2)
        self.assertEqual(decision.action, "disabled")
        self.assertEqual(decision.consecutive_failures, 0)

    def test_unknown_transient_state_is_left_alone(self) -> None:
        decision = watchdog.decide_action("Queued", None, 1, 2)
        self.assertEqual(decision.action, "wait")
        self.assertEqual(decision.consecutive_failures, 1)

    def test_control_plane_failure_does_not_restart_live_local_process(self) -> None:
        decision = watchdog.decide_action("Running", watchdog.CONTROL_PLANE_DEGRADED, 1, 2)
        self.assertEqual(decision.action, "control_plane_degraded")
        self.assertEqual(decision.consecutive_failures, 0)

    def test_state_write_is_atomic_and_round_trips(self) -> None:
        with TemporaryDirectory() as temporary:
            state_file = Path(temporary) / "state.json"
            watchdog.save_state(state_file, consecutive_failures=3, last_action="degraded")
            payload = watchdog.load_state(state_file)
            self.assertEqual(payload["consecutive_failures"], 3)
            self.assertEqual(payload["last_action"], "degraded")
            self.assertFalse(state_file.with_name("state.json.tmp").exists())
            json.loads(state_file.read_text(encoding="utf-8"))

    def test_event_log_rotates_at_a_fixed_size(self) -> None:
        with TemporaryDirectory() as temporary:
            event_log = Path(temporary) / "events.jsonl"
            event_log.write_text("x" * 128, encoding="utf-8")
            watchdog.append_event(event_log, max_bytes=64, backups=2, action="healthy")
            self.assertTrue(event_log.with_name("events.jsonl.1").is_file())
            event = json.loads(event_log.read_text(encoding="utf-8"))
            self.assertEqual(event["action"], "healthy")

    def test_recovery_backoff_caps_at_last_delay(self) -> None:
        config = self._config(Path.cwd())
        self.assertEqual(watchdog.recovery_delay(config, 1), 60.0)
        self.assertEqual(watchdog.recovery_delay(config, 99), 1800.0)

    def test_task_queries_are_scoped_by_name_and_path(self) -> None:
        config = self._config(Path.cwd())
        completed = watchdog.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"State":"Ready","LastTaskResult":0}',
            stderr="",
        )
        with patch.object(watchdog, "run_powershell", return_value=completed) as runner:
            status = watchdog.task_status(config)
        self.assertEqual(status.state, "Ready")
        script = runner.call_args.args[1]
        self.assertIn("-TaskPath $env:OPENAI_TUNNEL_TASK_PATH", script)

    def test_start_uses_guarded_task_reregistration(self) -> None:
        config = self._config(Path.cwd())
        with patch.object(watchdog, "reregister_task") as reregister:
            watchdog.start_task(config)
        reregister.assert_called_once_with(config)

    def test_reregistration_requires_the_main_task_arguments_pin(self) -> None:
        config = replace(self._config(Path.cwd()), main_task_arguments_sha256=None)
        with self.assertRaisesRegex(RuntimeError, "arguments SHA-256 pin is required"):
            watchdog.reregister_task(config)

    def test_restart_rejects_a_missing_pin_before_any_task_mutation(self) -> None:
        config = replace(self._config(Path.cwd()), main_task_arguments_sha256=None)
        with (
            patch.object(watchdog, "run_powershell") as runner,
            self.assertRaisesRegex(RuntimeError, "arguments SHA-256 pin is required"),
        ):
            watchdog.restart_task(config)
        runner.assert_not_called()

    def test_tunnel_client_fingerprint_is_checked_before_any_runtime_probe(self) -> None:
        config = replace(self._config(Path.cwd()), tunnel_client_sha256="0" * 64)
        with (
            patch.object(watchdog, "task_status") as task_status,
            patch.object(watchdog, "probe_mcp_backend") as backend_probe,
            self.assertRaisesRegex(RuntimeError, "tunnel client fingerprint"),
        ):
            watchdog.run_once(config)
        task_status.assert_not_called()
        backend_probe.assert_not_called()

    def test_tunnel_reregistration_rejects_contract_drift_and_stop_timeout(self) -> None:
        config = self._config(Path.cwd())
        for returncode, message in (
            (27, "principal"),
            (28, "run level"),
            (29, "single-instance"),
            (30, "arguments"),
            (31, "working directory"),
            (32, "did not stop"),
        ):
            with self.subTest(returncode=returncode):
                completed = watchdog.subprocess.CompletedProcess(
                    args=[], returncode=returncode, stdout="", stderr=""
                )
                with (
                    patch.object(watchdog, "run_powershell", return_value=completed),
                    self.assertRaisesRegex(RuntimeError, message),
                ):
                    if returncode == 32:
                        watchdog.restart_task(config)
                    else:
                        watchdog.reregister_task(config)

        completed = watchdog.subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch.object(watchdog, "run_powershell", return_value=completed) as runner:
            watchdog.reregister_task(config)
        script = runner.call_args.args[1]
        self.assertIn("$actionNodes", script)
        self.assertIn("LocalName -ne 'Exec'", script)

    def test_loop_continues_after_one_iteration_error(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            with (
                patch.object(watchdog, "run_once", side_effect=[RuntimeError("boom"), 0]) as run,
                patch.object(watchdog.time, "sleep") as sleep,
            ):
                self.assertEqual(
                    watchdog.run_loop(config, interval_seconds=60, max_iterations=2),
                    0,
                )
            self.assertEqual(run.call_count, 2)
            sleep.assert_called_once_with(60)
            event = json.loads(config.event_log.read_text(encoding="utf-8"))
            self.assertEqual(event["action"], "watchdog_error")

    def test_ready_task_respects_persisted_crash_loop_cooldown(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            watchdog.save_state(
                config.state_file,
                consecutive_failures=0,
                last_action="started_unready",
                recovery_attempts=2,
                next_recovery_at=2000.0,
                last_recovery_at=1000.0,
            )
            statuses = [
                watchdog.TaskStatus("Ready", 0),
                watchdog.TaskStatus("Running", 267009),
            ]
            with (
                patch.object(watchdog, "task_status", side_effect=statuses),
                patch.object(watchdog, "start_task") as starter,
                patch.object(watchdog.time, "time", return_value=1500.0),
            ):
                self.assertEqual(watchdog.run_once(config), 0)
            starter.assert_not_called()
            state = watchdog.load_state(config.state_file)
            self.assertEqual(state["last_action"], "start_cooldown")
            self.assertEqual(state["recovery_attempts"], 2)
            self.assertEqual(state["next_recovery_at"], 2000.0)

    def test_running_restart_respects_persisted_recovery_cooldown(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            watchdog.save_state(
                config.state_file,
                consecutive_failures=1,
                last_action="restarted_unready",
                recovery_attempts=2,
                next_recovery_at=2000.0,
                last_recovery_at=1000.0,
            )
            unhealthy = watchdog.HealthProbe(
                watchdog.LOCAL_UNHEALTHY, False, False, False, False
            )
            with (
                patch.object(
                    watchdog, "task_status", return_value=watchdog.TaskStatus("Running", 267009)
                ),
                patch.object(watchdog, "probe_health", return_value=unhealthy),
                patch.object(watchdog, "restart_task") as restart,
                patch.object(watchdog.time, "time", return_value=1500.0),
            ):
                self.assertEqual(watchdog.run_once(config), 0)
            restart.assert_not_called()
            state = watchdog.load_state(config.state_file)
            self.assertEqual(state["last_action"], "restart_cooldown")
            self.assertEqual(state["recovery_attempts"], 2)

    def test_refused_start_reregisters_exact_task_before_final_health_check(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            unhealthy = watchdog.HealthProbe(
                watchdog.LOCAL_UNHEALTHY, False, False, False, False
            )
            healthy = watchdog.HealthProbe(watchdog.HEALTHY, True, True, True, True)
            statuses = [
                watchdog.TaskStatus("Ready", 0),
                watchdog.TaskStatus("Ready", 0x800710E0),
                watchdog.TaskStatus("Running", 267009),
            ]
            with (
                patch.object(watchdog, "task_status", side_effect=statuses),
                patch.object(watchdog, "start_task") as starter,
                patch.object(watchdog, "reregister_task") as reregister,
                patch.object(watchdog, "wait_for_health", side_effect=[unhealthy, healthy]),
                patch.object(watchdog.time, "time", side_effect=[1000.0, 1001.0]),
            ):
                self.assertEqual(watchdog.run_once(config), 0)
            starter.assert_called_once_with(config)
            reregister.assert_called_once_with(config)
            state = watchdog.load_state(config.state_file)
            self.assertEqual(state["last_action"], "start_reregistered_healthy")

    def test_brief_healthy_period_does_not_reset_recovery_backoff(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            watchdog.save_state(
                config.state_file,
                consecutive_failures=0,
                last_action="started_healthy",
                recovery_attempts=2,
                next_recovery_at=2000.0,
                last_recovery_at=1300.0,
                healthy_since_at=1400.0,
            )
            healthy = watchdog.HealthProbe(watchdog.HEALTHY, True, True, True, True)
            with (
                patch.object(
                    watchdog, "task_status", return_value=watchdog.TaskStatus("Running", 267009)
                ),
                patch.object(watchdog, "probe_health", return_value=healthy),
                patch.object(watchdog.time, "time", return_value=1500.0),
            ):
                self.assertEqual(watchdog.run_once(config), 0)
            state = watchdog.load_state(config.state_file)
            self.assertEqual(state["last_action"], "healthy")
            self.assertEqual(state["recovery_attempts"], 2)
            self.assertEqual(state["healthy_since_at"], 1400.0)

    def test_stable_healthy_period_resets_recovery_backoff(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            watchdog.save_state(
                config.state_file,
                consecutive_failures=0,
                last_action="started_healthy",
                recovery_attempts=3,
                next_recovery_at=2000.0,
                last_recovery_at=900.0,
                healthy_since_at=1000.0,
            )
            healthy = watchdog.HealthProbe(watchdog.HEALTHY, True, True, True, True)
            with (
                patch.object(
                    watchdog, "task_status", return_value=watchdog.TaskStatus("Running", 267009)
                ),
                patch.object(watchdog, "probe_health", return_value=healthy),
                patch.object(watchdog.time, "time", return_value=1400.0),
            ):
                self.assertEqual(watchdog.run_once(config), 0)
            state = watchdog.load_state(config.state_file)
            self.assertEqual(state["last_action"], "healthy_stable")
            self.assertEqual(state["recovery_attempts"], 0)
            self.assertEqual(state["next_recovery_at"], 0.0)

    def test_successful_start_keeps_backoff_until_stability_window(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            healthy = watchdog.HealthProbe(watchdog.HEALTHY, True, True, True, True)
            statuses = [
                watchdog.TaskStatus("Ready", 0),
                watchdog.TaskStatus("Running", 267009),
            ]
            with (
                patch.object(watchdog, "task_status", side_effect=statuses),
                patch.object(watchdog, "start_task"),
                patch.object(watchdog, "wait_for_health", return_value=healthy),
                patch.object(watchdog.time, "time", side_effect=[1000.0, 1001.0]),
            ):
                self.assertEqual(watchdog.run_once(config), 0)
            state = watchdog.load_state(config.state_file)
            self.assertEqual(state["last_action"], "started_healthy")
            self.assertEqual(state["recovery_attempts"], 1)
            self.assertEqual(state["next_recovery_at"], 1060.0)
            self.assertEqual(state["healthy_since_at"], 1001.0)

    def test_unready_restart_keeps_backoff_lease(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            watchdog.save_state(
                config.state_file,
                consecutive_failures=1,
                last_action="degraded",
            )
            unhealthy = watchdog.HealthProbe(
                watchdog.LOCAL_UNHEALTHY, False, False, False, False
            )
            statuses = [
                watchdog.TaskStatus("Running", 267009),
                watchdog.TaskStatus("Running", 267009),
            ]
            with (
                patch.object(watchdog, "task_status", side_effect=statuses),
                patch.object(watchdog, "probe_health", return_value=unhealthy),
                patch.object(watchdog, "restart_task") as restart,
                patch.object(watchdog, "wait_for_health", return_value=unhealthy),
                patch.object(watchdog.time, "time", return_value=1000.0),
            ):
                self.assertEqual(watchdog.run_once(config), 0)
            restart.assert_called_once_with(config)
            state = watchdog.load_state(config.state_file)
            self.assertEqual(state["last_action"], "restarted_unready")
            self.assertEqual(state["recovery_attempts"], 1)
            self.assertEqual(state["next_recovery_at"], 1060.0)

    def test_slow_initial_poll_does_not_consume_failure_budget(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root)
            statuses = [
                watchdog.TaskStatus("Ready", 0),
                watchdog.TaskStatus("Running", 267009),
            ]
            unready = watchdog.HealthProbe(
                watchdog.CONTROL_PLANE_DEGRADED, False, True, False, False
            )
            with (
                patch.object(watchdog, "task_status", side_effect=statuses),
                patch.object(watchdog, "start_task"),
                patch.object(watchdog, "wait_for_health", return_value=unready),
                patch.object(watchdog.time, "time", return_value=1000.0),
            ):
                self.assertEqual(watchdog.run_once(config), 0)
            state = watchdog.load_state(config.state_file)
            self.assertEqual(state["consecutive_failures"], 0)
            self.assertEqual(state["last_action"], "started_unready")
            self.assertEqual(state["recovery_attempts"], 1)

    def test_main_records_error_when_no_console_stream_exists(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            with (
                patch.object(watchdog, "parse_args", return_value=config),
                patch.object(watchdog, "run_once", side_effect=RuntimeError("boom")),
                patch.object(watchdog.sys, "stderr", None),
            ):
                self.assertEqual(watchdog.main([]), 1)
            event = json.loads(config.event_log.read_text(encoding="utf-8"))
            self.assertEqual(event["action"], "watchdog_error")
            self.assertEqual(event["error_type"], "RuntimeError")

    @staticmethod
    def _config(root: Path) -> object:
        return watchdog.Config(
            main_task_name="test tunnel",
            tunnel_client=Path(sys.executable),
            tunnel_client_sha256=hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
            health_url_file=root / "health.url",
            pid_file=root / "tunnel.pid",
            state_file=root / "watchdog-state.json",
            event_log=root / "watchdog-events.jsonl",
            powershell=Path(sys.executable),
            failure_threshold=2,
            startup_grace_seconds=0,
            health_timeout_seconds=1,
            backend_url="http://127.0.0.1:18765/mcp",
            backend_workspace="D:/coding-tools-mcp",
            backend_required_tools=("server_info", "computer_screenshot"),
            main_task_arguments_sha256="a" * 64,
        )


if __name__ == "__main__":
    unittest.main()
