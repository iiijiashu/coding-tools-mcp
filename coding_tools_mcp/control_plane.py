from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .control_plane_runtime_contract import CANONICAL_TASK_INVARIANTS


PROTECTED_CONTROL_PLANE_FILES = (
    Path("config/local-mcp-http-task.json"),
    Path("config/control-plane-runtime-contract-v1.json"),
)
LEGACY_MUTATION_HELPERS = ("pin-main-task.ps1",)
CONTROL_PLANE_TASK_NAMES = tuple(
    str(CANONICAL_TASK_INVARIANTS[key]["name"])
    for key in ("http", "http_watchdog", "tunnel", "tunnel_watchdog", "manager")
)

_TASK_MUTATION_RE = re.compile(
    r"(?i)(?:\b(?:start|stop|set|register|unregister|enable|disable)-scheduledtask\b"
    r"|\bschtasks(?:\.exe)?\b[^\r\n]*(?:/run|/end|/create|/delete|/change)\b)"
)
_POWERSHELL_TASK_TARGET_RE = re.compile(
    r"(?i)(?<!\S)-TaskName(?:\s+|=)(?:\"([^\"]+)\"|'([^']+)'|([^\s;&|]+))"
)
_SCHTASKS_TARGET_RE = re.compile(
    r"(?i)(?<!\S)/tn(?:\s+|:)(?:\"([^\"]+)\"|'([^']+)'|([^\s;&|]+))"
)
_DYNAMIC_TARGET_MARKERS = frozenset("$%(){}+`*")
_COMMAND_BOUNDARY_RE = re.compile(r"[;|&\r\n]+")
_CONFIG_MUTATION_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:set|add|clear)-content\b|\bout-file\b|"
    r"\b(?:copy|move|remove|rename|new)-item\b|"
    r"\b(?:write_text|write_bytes|replace|unlink|rename)\s*\(|"
    r"\bopen\s*\([^\r\n]{0,512}['\"](?:w|a|x|\+)[bt+]*['\"]|"
    r"(?:^|[\s;&|])(?:>|>>)(?!=)"
    r")"
)


@dataclass(frozen=True)
class ControlPlaneViolation:
    code: str
    message: str
    evidence: str


def _normalized_command(command: str) -> str:
    return " ".join(command.replace("\\", "/").split()).casefold()


def _task_mutation_targets(command: str) -> tuple[str, ...] | None:
    targets: list[str] = []
    for pattern in (_POWERSHELL_TASK_TARGET_RE, _SCHTASKS_TARGET_RE):
        for match in pattern.finditer(command):
            target = next((value for value in match.groups() if value is not None), "")
            if not target or any(marker in target for marker in _DYNAMIC_TARGET_MARKERS):
                return None
            targets.append(target)
    return tuple(targets) if targets else None


def control_plane_command_violation(command: str) -> ControlPlaneViolation | None:
    """Reject direct mutation of the shared production control plane.

    This is an accidental-mutation guard for MCP command execution. The
    transaction manager remains the authority for lifecycle and config changes.
    It is deliberately enforced before the general dangerous-mode bypass.
    """

    normalized = _normalized_command(command)
    legacy_helper = next((name for name in LEGACY_MUTATION_HELPERS if name in normalized), None)
    if legacy_helper is not None:
        return ControlPlaneViolation(
            "CONTROL_PLANE_LEASE_REQUIRED",
            "Legacy direct control-plane mutation helpers are disabled; use the transaction manager.",
            legacy_helper,
        )
    for invocation in _COMMAND_BOUNDARY_RE.split(command):
        if not _TASK_MUTATION_RE.search(invocation):
            continue
        normalized_invocation = _normalized_command(invocation)
        named_task = next(
            (name for name in CONTROL_PLANE_TASK_NAMES if name.casefold() in normalized_invocation),
            None,
        )
        if named_task is not None:
            return ControlPlaneViolation(
                "CONTROL_PLANE_LEASE_REQUIRED",
                "Shared MCP task lifecycle changes require the control-plane transaction manager.",
                named_task,
            )
        targets = _task_mutation_targets(invocation)
        if targets is None or re.search(r"(?i)(?<!\S)-InputObject(?:\s|=)", invocation):
            return ControlPlaneViolation(
                "CONTROL_PLANE_LEASE_REQUIRED",
                "Scheduled-task mutation targets must be explicit literals so protected tasks cannot be selected indirectly.",
                "unresolved scheduled-task target",
            )

    protected_file = next(
        (path for path in PROTECTED_CONTROL_PLANE_FILES if path.name.casefold() in normalized),
        None,
    )
    if protected_file is not None:
        if _CONFIG_MUTATION_RE.search(command):
            return ControlPlaneViolation(
                "CONTROL_PLANE_LEASE_REQUIRED",
                "Control-plane contracts may only be changed by the control-plane transaction manager.",
                protected_file.as_posix(),
            )
    return None


def is_protected_control_plane_path(path: Path, workspace_root: Path) -> bool:
    actual = path.resolve(strict=False)
    expected = {
        str((workspace_root / protected).resolve(strict=False)).casefold()
        for protected in PROTECTED_CONTROL_PLANE_FILES
    }
    return str(actual).casefold() in expected
