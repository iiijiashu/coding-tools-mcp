from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from .recovery import RecoveryBudget


class RehydrateBudgetExceeded(ValueError):
    """Protected and actionable state exceed the rehydrate budget."""


@dataclass(frozen=True)
class ProtectedTaskState:
    """High-integrity state that must survive context compaction unchanged.

    Transcript text and raw tool payloads are deliberately absent from this
    schema.  Callers should persist those as artifacts and place only stable
    references in ``evidence_refs``.
    """

    task_id: str
    attempt_id: str
    base_sha: str | None = None
    workspace: str | None = None
    acceptance_criteria: tuple[str, ...] = ()
    accepted_results: tuple[str, ...] = ()
    superseded_attempts: tuple[str, ...] = ()
    stale_results: tuple[str, ...] = ()
    cleanup_targets: tuple[str, ...] = ()
    cleanup_receipts: tuple[str, ...] = ()
    completed: tuple[str, ...] = ()
    verified: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    next_action: str | None = None
    recovery_budget: RecoveryBudget | None = None

    def protected_dict(self) -> dict[str, object]:
        """Return the fields whose values must never be summary-generated."""

        return {
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "base_sha": self.base_sha,
            "workspace": self.workspace,
            "acceptance_criteria": list(self.acceptance_criteria),
            "accepted_results": list(self.accepted_results),
            "superseded_attempts": list(self.superseded_attempts),
            "stale_results": list(self.stale_results),
            "cleanup_targets": list(self.cleanup_targets),
            "cleanup_receipts": list(self.cleanup_receipts),
            "recovery_budget": asdict(self.recovery_budget)
            if self.recovery_budget
            else None,
        }

    def fingerprint(self) -> str:
        canonical = json.dumps(
            self.protected_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


def render_rehydrate(state: ProtectedTaskState, *, max_chars: int = 8_192) -> str:
    """Render bounded, transcript-free state for a fresh agent context.

    Protected identity/acceptance/result/cleanup fields are atomic: if they do
    not fit, this function raises instead of silently truncating them. Blockers,
    next action and pending work also require space before optional history.
    Every state value is JSON
    quoted so embedded newlines cannot masquerade as new authoritative fields.
    """

    if max_chars <= 0:
        raise ValueError("max_chars must be positive")

    protected = _render_protected(state)
    if len(protected) > max_chars:
        raise RehydrateBudgetExceeded(
            f"protected state requires {len(protected)} chars but budget is {max_chars}"
        )

    critical = _render_list_section("Blockers", state.blockers)
    if state.next_action:
        critical += f"\nNext action:\n- {_quote(state.next_action)}\n"
    critical += _render_list_section("Pending", state.pending)
    if len(protected) + len(critical) > max_chars:
        raise RehydrateBudgetExceeded(
            "protected state and actionable state exceed the rehydrate budget"
        )
    parts = [protected, critical]
    remaining = max_chars - len(protected) - len(critical)
    optional_sections = (
        ("Evidence refs", state.evidence_refs),
        ("Verified", state.verified),
        ("Completed", state.completed),
    )
    for title, items in optional_sections:
        rendered = _render_list_section(title, items)
        if not rendered:
            continue
        if len(rendered) <= remaining:
            parts.append(rendered)
            remaining -= len(rendered)
            continue
        clipped = _clip_section(title, items, remaining)
        if clipped:
            parts.append(clipped)
            remaining -= len(clipped)
        break

    return "".join(parts)


def _render_protected(state: ProtectedTaskState) -> str:
    lines = [
        "Protected task state (authoritative; do not infer replacements):",
        f"- task_id: {_quote(state.task_id)}",
        f"- attempt_id: {_quote(state.attempt_id)}",
        f"- base_sha: {_quote(state.base_sha)}",
        f"- workspace: {_quote(state.workspace)}",
        f"- fingerprint: {_quote(state.fingerprint())}",
    ]
    lines.extend(_list_lines("Acceptance criteria", state.acceptance_criteria))
    lines.extend(_list_lines("Accepted results", state.accepted_results))
    lines.extend(_list_lines("Superseded attempts", state.superseded_attempts))
    lines.extend(_list_lines("Stale results", state.stale_results))
    lines.extend(_list_lines("Cleanup targets", state.cleanup_targets))
    lines.extend(_list_lines("Cleanup receipts", state.cleanup_receipts))
    if state.recovery_budget is not None:
        lines.append(f"Recovery budget: {_quote(asdict(state.recovery_budget))}")
    return "\n".join(lines) + "\n"


def _quote(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _list_lines(title: str, items: tuple[str, ...]) -> list[str]:
    lines = [f"{title}:"]
    if not items:
        lines.append("- -")
        return lines
    lines.extend(f"- {_quote(item)}" for item in items)
    return lines


def _render_list_section(title: str, items: tuple[str, ...]) -> str:
    if not items:
        return ""
    return (
        "\n" + "\n".join([f"{title}:", *(f"- {_quote(item)}" for item in items)]) + "\n"
    )


def _clip_section(title: str, items: tuple[str, ...], remaining: int) -> str:
    if remaining <= 0:
        return ""
    prefix = f"\n{title}:\n"
    marker = "- [remaining items omitted: rehydrate budget]\n"
    if len(prefix) + len(marker) > remaining:
        return ""
    output = prefix
    for item in items:
        line = f"- {_quote(item)}\n"
        if len(output) + len(line) + len(marker) > remaining:
            output += marker
            return output
        output += line
    return output
