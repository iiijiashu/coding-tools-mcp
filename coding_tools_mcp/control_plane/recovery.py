from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .errors import AgentErrorCategory, CanonicalAgentError


class RecoveryAction(StrEnum):
    RETRY_SAME = "retry_same"
    FALLBACK = "fallback"
    COMPACT_AND_RETRY = "compact_and_retry"
    FAIL = "fail"


@dataclass(frozen=True)
class RecoveryDecision:
    action: RecoveryAction
    reason: str


def decide_recovery(
    error: CanonicalAgentError,
    *,
    retries_used: int = 0,
    max_same_executor_retries: int = 1,
    content_emitted: bool = False,
    side_effects_possible: bool = False,
) -> RecoveryDecision:
    """Choose a bounded recovery action without another model call.

    Once user-visible content has streamed, replaying another executor can
    duplicate output.  Likewise, an attempt that may already have produced side
    effects is never replayed unless the error explicitly marks replay safe.
    """

    if content_emitted:
        return RecoveryDecision(RecoveryAction.FAIL, "content_already_emitted")

    if side_effects_possible and not error.safe_to_replay:
        return RecoveryDecision(RecoveryAction.FAIL, "unsafe_to_replay_after_possible_side_effects")

    if error.category is AgentErrorCategory.CONTEXT_OVERFLOW and error.compact_allowed:
        return RecoveryDecision(RecoveryAction.COMPACT_AND_RETRY, "context_overflow_recoverable")

    if (
        error.retryable
        and error.safe_to_replay
        and retries_used < max(0, max_same_executor_retries)
    ):
        return RecoveryDecision(RecoveryAction.RETRY_SAME, "bounded_same_executor_retry")

    if error.fallback_allowed and error.safe_to_replay:
        return RecoveryDecision(RecoveryAction.FALLBACK, "provider_or_executor_fallback")

    return RecoveryDecision(RecoveryAction.FAIL, "no_safe_recovery")
