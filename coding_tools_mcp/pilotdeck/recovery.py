from __future__ import annotations

import time
import math
from dataclasses import dataclass, replace
from enum import StrEnum

from .errors import AgentErrorCategory, CanonicalAgentError


class RecoveryAction(StrEnum):
    RETRY_SAME = "retry_same"
    FALLBACK = "fallback"
    COMPACT_AND_RETRY = "compact_and_retry"
    FAIL = "fail"


@dataclass(frozen=True)
class RecoveryBudget:
    """Serialize this state with the task before carrying out a recovery."""

    retries_used: int = 0
    compactions_used: int = 0
    fallbacks_used: int = 0
    max_total: int = 3
    max_compactions: int = 1
    max_fallbacks: int = 1
    deadline_at: float | None = None

    def __post_init__(self) -> None:
        for value in (
            self.retries_used,
            self.compactions_used,
            self.fallbacks_used,
            self.max_total,
            self.max_compactions,
            self.max_fallbacks,
        ):
            if type(value) is not int or value < 0:
                raise ValueError(
                    "recovery counts and limits must be non-negative integers"
                )
        if self.deadline_at is not None and (
            type(self.deadline_at) not in (int, float)
            or not math.isfinite(self.deadline_at)
        ):
            raise ValueError("recovery deadline must be a finite Unix timestamp")

    @property
    def total_used(self) -> int:
        return self.retries_used + self.compactions_used + self.fallbacks_used


@dataclass(frozen=True)
class RecoveryDecision:
    action: RecoveryAction
    reason: str
    budget: RecoveryBudget | None = None


def decide_recovery(
    error: CanonicalAgentError,
    *,
    retries_used: int = 0,
    max_same_executor_retries: int = 1,
    content_emitted: bool = False,
    side_effects_possible: bool = False,
    budget: RecoveryBudget | None = None,
    now: float | None = None,
) -> RecoveryDecision:
    """Choose a bounded recovery action without another model call.

    Once user-visible content has streamed, replaying another executor can
    duplicate output.  Likewise, an attempt that may already have produced side
    effects is never replayed unless the error explicitly marks replay safe.
    """

    current = budget or RecoveryBudget(retries_used=retries_used)
    if current.total_used >= current.max_total:
        return RecoveryDecision(
            RecoveryAction.FAIL, "recovery_budget_exhausted", current
        )
    if (
        current.deadline_at is not None
        and (time.time() if now is None else now) >= current.deadline_at
    ):
        return RecoveryDecision(
            RecoveryAction.FAIL, "recovery_deadline_exceeded", current
        )
    if content_emitted:
        return RecoveryDecision(RecoveryAction.FAIL, "content_already_emitted", current)

    if side_effects_possible and not error.safe_to_replay:
        return RecoveryDecision(
            RecoveryAction.FAIL, "unsafe_to_replay_after_possible_side_effects", current
        )

    if error.category is AgentErrorCategory.CONTEXT_OVERFLOW and error.compact_allowed:
        if (
            not error.safe_to_replay
            or current.compactions_used >= current.max_compactions
        ):
            return RecoveryDecision(
                RecoveryAction.FAIL, "compaction_not_safe_or_budget_exhausted", current
            )
        return RecoveryDecision(
            RecoveryAction.COMPACT_AND_RETRY,
            "context_overflow_recoverable",
            replace(current, compactions_used=current.compactions_used + 1),
        )

    if (
        error.retryable
        and error.safe_to_replay
        and current.retries_used < max(0, max_same_executor_retries)
    ):
        return RecoveryDecision(
            RecoveryAction.RETRY_SAME,
            "bounded_same_executor_retry",
            replace(current, retries_used=current.retries_used + 1),
        )

    if (
        error.fallback_allowed
        and error.safe_to_replay
        and current.fallbacks_used < current.max_fallbacks
    ):
        return RecoveryDecision(
            RecoveryAction.FALLBACK,
            "provider_or_executor_fallback",
            replace(current, fallbacks_used=current.fallbacks_used + 1),
        )

    return RecoveryDecision(RecoveryAction.FAIL, "no_safe_recovery", current)
