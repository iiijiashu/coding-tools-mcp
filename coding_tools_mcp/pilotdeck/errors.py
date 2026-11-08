from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class AgentErrorCategory(StrEnum):
    """Stable failure classes used by recovery and routing policy."""

    TRANSPORT = "transport"
    TIMEOUT = "timeout"
    PROVIDER = "provider"
    AUTH = "auth"
    BILLING = "billing"
    CONTEXT_OVERFLOW = "context_overflow"
    INVALID_OUTPUT = "invalid_output"
    CAPABILITY_MISSING = "capability_missing"
    PERMISSION_DENIED = "permission_denied"
    STALE_RESULT = "stale_result"
    PROCESS_CRASH = "process_crash"
    TASK_FAILURE = "task_failure"
    INTERNAL = "internal"


@dataclass(frozen=True)
class CanonicalAgentError:
    """Provider-agnostic error contract for long-running agent attempts.

    Policy flags are explicit instead of inferred from message text.  This keeps
    recovery deterministic and prevents the web/client model from having to
    guess whether an operation is safe to repeat.
    """

    code: str
    category: AgentErrorCategory
    message: str
    retryable: bool = False
    fallback_allowed: bool = False
    compact_allowed: bool = False
    safe_to_replay: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "category": self.category.value,
            "message": self.message,
            "retryable": self.retryable,
            "fallback_allowed": self.fallback_allowed,
            "compact_allowed": self.compact_allowed,
            "safe_to_replay": self.safe_to_replay,
            "details": dict(self.details),
        }
