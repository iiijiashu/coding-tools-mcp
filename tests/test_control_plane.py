from __future__ import annotations

import unittest

from coding_tools_mcp.control_plane import (
    AgentErrorCategory,
    CanonicalAgentError,
    LifecycleEffect,
    LifecycleEvent,
    LifecycleRuntime,
    ProtectedTaskState,
    RecoveryAction,
    RehydrateBudgetExceeded,
    decide_recovery,
    render_rehydrate,
)


class RecoveryPolicyTests(unittest.TestCase):
    def test_retry_is_bounded_and_requires_replay_safety(self) -> None:
        error = CanonicalAgentError(
            code="provider_timeout",
            category=AgentErrorCategory.TIMEOUT,
            message="provider timed out before producing output",
            retryable=True,
            fallback_allowed=True,
            safe_to_replay=True,
        )
        first = decide_recovery(error, retries_used=0, max_same_executor_retries=1)
        second = decide_recovery(error, retries_used=1, max_same_executor_retries=1)
        self.assertEqual(first.action, RecoveryAction.RETRY_SAME)
        self.assertEqual(second.action, RecoveryAction.FALLBACK)

    def test_streamed_content_disables_retry_and_fallback(self) -> None:
        error = CanonicalAgentError(
            code="transport_reset",
            category=AgentErrorCategory.TRANSPORT,
            message="connection reset",
            retryable=True,
            fallback_allowed=True,
            safe_to_replay=True,
        )
        decision = decide_recovery(error, content_emitted=True)
        self.assertEqual(decision.action, RecoveryAction.FAIL)
        self.assertEqual(decision.reason, "content_already_emitted")

    def test_possible_side_effects_require_explicit_replay_safety(self) -> None:
        error = CanonicalAgentError(
            code="executor_disconnected",
            category=AgentErrorCategory.TRANSPORT,
            message="executor disconnected after a write-capable turn",
            retryable=True,
            fallback_allowed=True,
            safe_to_replay=False,
        )
        decision = decide_recovery(error, side_effects_possible=True)
        self.assertEqual(decision.action, RecoveryAction.FAIL)

    def test_context_overflow_prefers_compaction(self) -> None:
        error = CanonicalAgentError(
            code="context_overflow",
            category=AgentErrorCategory.CONTEXT_OVERFLOW,
            message="context exceeds provider limit",
            compact_allowed=True,
            safe_to_replay=True,
        )
        decision = decide_recovery(error)
        self.assertEqual(decision.action, RecoveryAction.COMPACT_AND_RETRY)


class LifecycleRuntimeTests(unittest.TestCase):
    def test_non_blocking_failure_does_not_stop_later_handlers(self) -> None:
        runtime = LifecycleRuntime()
        calls: list[str] = []

        def failing(_event: LifecycleEvent) -> None:
            calls.append("failing")
            raise RuntimeError("telemetry unavailable")

        def succeeding(_event: LifecycleEvent) -> LifecycleEffect:
            calls.append("succeeding")
            return LifecycleEffect("receipt", {"id": "cleanup-1"})

        runtime.register("cleanup_finished", failing, blocking=False)
        runtime.register("cleanup_finished", succeeding, blocking=False)
        result = runtime.dispatch(LifecycleEvent("cleanup_finished", "task-1", "attempt-1"))

        self.assertEqual(calls, ["failing", "succeeding"])
        self.assertTrue(result.ok)
        self.assertEqual(len(result.non_blocking_errors), 1)
        self.assertEqual(result.effects[0].payload["id"], "cleanup-1")

    def test_blocking_failure_stops_dispatch(self) -> None:
        runtime = LifecycleRuntime()
        calls: list[str] = []

        def persistence(_event: LifecycleEvent) -> None:
            calls.append("persistence")
            raise RuntimeError("result was not durably stored")

        def cleanup(_event: LifecycleEvent) -> None:
            calls.append("cleanup")

        runtime.register("result_accepted", persistence, blocking=True)
        runtime.register("result_accepted", cleanup, blocking=False)
        result = runtime.dispatch(LifecycleEvent("result_accepted", "task-1", "attempt-1"))

        self.assertEqual(calls, ["persistence"])
        self.assertFalse(result.ok)
        self.assertEqual(len(result.blocking_errors), 1)


class RehydrateContractTests(unittest.TestCase):
    def _state(self, **overrides: object) -> ProtectedTaskState:
        values: dict[str, object] = {
            "task_id": "task-current",
            "attempt_id": "attempt-07",
            "base_sha": "abc123",
            "workspace": "D:/coding-tools-mcp",
            "acceptance_criteria": ("collector accepts result", "cleanup is exact"),
            "accepted_results": ("result-good",),
            "superseded_attempts": ("attempt-03",),
            "stale_results": ("result-late-03",),
            "cleanup_targets": ("chat:child-91",),
            "cleanup_receipts": ("cleanup:child-91:verified",),
            "completed": ("canonical error contract",),
            "verified": ("unit tests pass",),
            "pending": ("wire lifecycle into multichat",),
            "evidence_refs": ("artifact://result-good",),
            "next_action": "integrate with current local multichat baseline",
        }
        values.update(overrides)
        return ProtectedTaskState(**values)  # type: ignore[arg-type]

    def test_protected_fields_and_cleanup_receipts_are_exact(self) -> None:
        output = render_rehydrate(self._state(), max_chars=8_192)
        self.assertIn("task_id: task-current", output)
        self.assertIn("result-good", output)
        self.assertIn("attempt-03", output)
        self.assertIn("result-late-03", output)
        self.assertIn("cleanup:child-91:verified", output)
        self.assertNotIn("transcript", output.lower())
        self.assertNotIn("chat history", output.lower())

    def test_protected_state_is_never_silently_truncated(self) -> None:
        state = self._state(acceptance_criteria=("x" * 500,))
        with self.assertRaises(RehydrateBudgetExceeded):
            render_rehydrate(state, max_chars=300)

    def test_optional_working_state_can_be_clipped_to_budget(self) -> None:
        state = self._state(completed=tuple(f"completed-{index}-" + "x" * 80 for index in range(40)))
        baseline = render_rehydrate(self._state(completed=()), max_chars=2_000)
        output = render_rehydrate(state, max_chars=max(2_000, len(baseline) + 200))
        self.assertLessEqual(len(output), max(2_000, len(baseline) + 200))
        self.assertIn("remaining items omitted", output)
        self.assertIn("cleanup:child-91:verified", output)

    def test_fingerprint_changes_only_for_protected_state(self) -> None:
        original = self._state()
        working_change = self._state(completed=("different working summary",))
        protected_change = self._state(accepted_results=("result-other",))
        self.assertEqual(original.fingerprint(), working_change.fingerprint())
        self.assertNotEqual(original.fingerprint(), protected_change.fingerprint())


if __name__ == "__main__":
    unittest.main()
