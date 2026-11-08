from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class LifecycleEvent:
    type: str
    task_id: str
    attempt_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LifecycleEffect:
    """A small, explicit output produced by a lifecycle handler."""

    type: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LifecycleHandlerError:
    handler: str
    event_type: str
    message: str


@dataclass(frozen=True)
class LifecycleDispatchResult:
    effects: tuple[LifecycleEffect, ...] = ()
    blocking_errors: tuple[LifecycleHandlerError, ...] = ()
    non_blocking_errors: tuple[LifecycleHandlerError, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.blocking_errors


LifecycleHandler = Callable[
    [LifecycleEvent],
    LifecycleEffect | Iterable[LifecycleEffect] | None,
]


@dataclass(frozen=True)
class _RegisteredHandler:
    callback: LifecycleHandler
    blocking: bool
    name: str


class LifecycleRuntime:
    """Deterministic event dispatcher with explicit failure severity.

    Handlers are invoked in registration order.  A failing blocking handler is
    recorded and dispatch stops immediately; non-blocking failures are recorded
    and the remaining handlers continue.  The runtime itself performs no I/O.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[_RegisteredHandler]] = defaultdict(list)

    def register(
        self,
        event_type: str,
        handler: LifecycleHandler,
        *,
        blocking: bool = False,
        name: str | None = None,
    ) -> None:
        if not event_type.strip():
            raise ValueError("event_type must not be empty")
        resolved_name = name or getattr(handler, "__name__", handler.__class__.__name__)
        self._handlers[event_type].append(
            _RegisteredHandler(handler, blocking, resolved_name)
        )

    def dispatch(self, event: LifecycleEvent) -> LifecycleDispatchResult:
        effects: list[LifecycleEffect] = []
        blocking_errors: list[LifecycleHandlerError] = []
        non_blocking_errors: list[LifecycleHandlerError] = []

        for registration in self._handlers.get(event.type, ()):
            try:
                produced = registration.callback(event)
                if produced is None:
                    continue
                if isinstance(produced, LifecycleEffect):
                    effects.append(produced)
                else:
                    effects.extend(produced)
            except Exception as exc:  # lifecycle policy decides whether failure blocks
                error = LifecycleHandlerError(
                    handler=registration.name,
                    event_type=event.type,
                    message=str(exc),
                )
                if registration.blocking:
                    blocking_errors.append(error)
                    break
                non_blocking_errors.append(error)

        return LifecycleDispatchResult(
            effects=tuple(effects),
            blocking_errors=tuple(blocking_errors),
            non_blocking_errors=tuple(non_blocking_errors),
        )
