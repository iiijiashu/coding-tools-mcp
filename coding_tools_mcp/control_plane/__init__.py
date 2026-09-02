"""Deterministic control-plane primitives for long-running agent work.

This package is intentionally side-effect free at import time.  Nothing here is
registered with the MCP server until a caller explicitly wires it in.
"""

from .errors import AgentErrorCategory, CanonicalAgentError
from .lifecycle import (
    LifecycleDispatchResult,
    LifecycleEffect,
    LifecycleEvent,
    LifecycleRuntime,
)
from .recovery import RecoveryAction, RecoveryDecision, decide_recovery
from .rehydrate import ProtectedTaskState, RehydrateBudgetExceeded, render_rehydrate

__all__ = [
    "AgentErrorCategory",
    "CanonicalAgentError",
    "LifecycleDispatchResult",
    "LifecycleEffect",
    "LifecycleEvent",
    "LifecycleRuntime",
    "ProtectedTaskState",
    "RecoveryAction",
    "RecoveryDecision",
    "RehydrateBudgetExceeded",
    "decide_recovery",
    "render_rehydrate",
]
