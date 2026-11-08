from __future__ import annotations

import json
import unittest
from dataclasses import asdict, replace

from coding_tools_mcp.pilotdeck import (
    AgentErrorCategory,
    CanonicalAgentError,
    LifecycleEffect,
    LifecycleEvent,
    LifecycleRuntime,
    ProtectedTaskState,
    RecoveryAction,
    RecoveryBudget,
    RehydrateBudgetExceeded,
    decide_recovery,
    render_rehydrate,
)


class PilotDeckReliabilityTests(unittest.TestCase):
    def test_summary_preserves_actionable_state_before_completed_history(self) -> None:
        state = ProtectedTaskState(
            task_id="t",
            attempt_id="a",
            completed=tuple("finished-" + str(i) + "x" * 40 for i in range(200)),
            blockers=("authentication expired",),
            pending=("run acceptance",),
            next_action="refresh authentication",
        )
        text = render_rehydrate(state, max_chars=1000)
        self.assertLessEqual(len(text), 1000)
        for value in (
            "authentication expired",
            "run acceptance",
            "refresh authentication",
        ):
            self.assertIn(value, text)
        with self.assertRaises(RehydrateBudgetExceeded):
            render_rehydrate(replace(state, blockers=("x" * 2000,)), max_chars=1000)

    def test_recovery_budget_survives_serialization_and_exhausts(self) -> None:
        error = CanonicalAgentError(
            "timeout",
            AgentErrorCategory.TIMEOUT,
            "timeout",
            retryable=True,
            safe_to_replay=True,
            fallback_allowed=True,
        )
        first = decide_recovery(error)
        restored = RecoveryBudget(**json.loads(json.dumps(asdict(first.budget))))
        second = decide_recovery(error, budget=restored)
        third = decide_recovery(error, budget=second.budget)
        self.assertEqual(
            [first.action, second.action, third.action],
            [RecoveryAction.RETRY_SAME, RecoveryAction.FALLBACK, RecoveryAction.FAIL],
        )
        self.assertEqual(third.budget.total_used, 2)

    def test_compaction_and_total_deadline_are_bounded(self) -> None:
        error = CanonicalAgentError(
            "overflow",
            AgentErrorCategory.CONTEXT_OVERFLOW,
            "overflow",
            compact_allowed=True,
            safe_to_replay=True,
        )
        first = decide_recovery(error)
        self.assertEqual(first.action, RecoveryAction.COMPACT_AND_RETRY)
        self.assertEqual(
            decide_recovery(error, budget=first.budget).action, RecoveryAction.FAIL
        )
        self.assertEqual(
            decide_recovery(error, retries_used=100).action, RecoveryAction.FAIL
        )
        self.assertEqual(
            decide_recovery(error, budget=RecoveryBudget(deadline_at=5), now=5).action,
            RecoveryAction.FAIL,
        )
        self.assertEqual(
            decide_recovery(replace(error, safe_to_replay=False)).action,
            RecoveryAction.FAIL,
        )

    def test_recovery_budget_is_protected_during_rehydrate(self) -> None:
        state = ProtectedTaskState(
            "t", "a", recovery_budget=RecoveryBudget(fallbacks_used=1)
        )
        self.assertIn('"fallbacks_used": 1', render_rehydrate(state))
        self.assertNotEqual(
            state.fingerprint(),
            replace(state, recovery_budget=RecoveryBudget()).fingerprint(),
        )

    def test_failed_generator_does_not_publish_partial_effects(self) -> None:
        for blocking in (True, False):
            runtime = LifecycleRuntime()
            runtime.register("ready", lambda _: LifecycleEffect("previous-success"))

            def failed(_event):
                yield LifecycleEffect("partial-accepted")
                raise RuntimeError("persistence failed")

            runtime.register("ready", failed, blocking=blocking)
            result = runtime.dispatch(LifecycleEvent("ready", "t"))
            self.assertEqual(
                [effect.type for effect in result.effects], ["previous-success"]
            )
            self.assertEqual(result.ok, not blocking)

    def test_invalid_or_unbounded_effect_streams_are_rejected(self) -> None:
        runtime = LifecycleRuntime()

        def endless(_event):
            while True:
                yield LifecycleEffect("effect")

        runtime.register("ready", endless, blocking=True)
        result = runtime.dispatch(LifecycleEvent("ready", "t"))
        self.assertFalse(result.ok)
        self.assertEqual(result.effects, ())


if __name__ == "__main__":
    unittest.main()
