#!/usr/bin/env python3
"""Keep one exact Windows scheduled HTTP MCP task reachable on loopback."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    from .control_plane_transaction import LeaseBusy, process_lock
    from .mcp_backend_probe import (
        BackendProbe,
        load_authorization_header,
        probe_mcp_backend,
        tool_catalog_sha256,
    )
except ImportError:  # direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from control_plane_transaction import LeaseBusy, process_lock
    from mcp_backend_probe import (
        BackendProbe,
        load_authorization_header,
        probe_mcp_backend,
        tool_catalog_sha256,
    )


RUNNING = "Running"
READY = "Ready"
DISABLED = "Disabled"
ROOT = Path(__file__).resolve().parents[1]
EXPECTED_HTTP_TASK_EXECUTABLE = ROOT / ".venv" / "Scripts" / "pythonw.exe"
EXPECTED_HTTP_TASK_SCRIPT = ROOT / "scripts" / "run_local_mcp_http_task.py"
EXPECTED_HTTP_TASK_CONFIG = ROOT / "config" / "local-mcp-http-task.json"
CONTROL_PLANE_OPERATION_RE = re.compile(r"^[a-f0-9]{32}$")
CONTROL_PLANE_TERMINAL_RECOVERY_STATES = {"failed", "rejected", "rolled_back", "succeeded"}
DEFAULT_EVENT_LOG_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_EVENT_LOG_BACKUPS = 3
DEFAULT_CONTROL_PLANE_LEASE = (
    Path.home()
    / ".local"
    / "state"
    / "tunnel-client"
    / "control-plane"
    / "local-d-drive-coding-tools"
    / "active.json"
)


@dataclass(frozen=True)
class WatchdogDecision:
    action: str
    consecutive_failures: int


@dataclass(frozen=True)
class ControlPlaneLease:
    action: str
    reason: str
    operation_id: str | None = None
    age_seconds: float | None = None


def non_empty_text(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("value must not be empty")
    return value


def non_empty_path(value: str) -> Path:
    return Path(non_empty_text(value))


def sha256_value(value: str) -> str:
    if re.fullmatch(r"[A-Fa-f0-9]{64}", value) is None:
        raise argparse.ArgumentTypeError("SHA-256 must contain 64 hexadecimal characters")
    return value.lower()


def decide_action(
    task_state: str,
    health_ok: bool,
    consecutive_failures: int = 0,
    failure_threshold: int = 3,
    *,
    restartable: bool = True,
    start_allowed: bool = True,
    transaction_active: bool = False,
) -> WatchdogDecision:
    failures = max(0, int(consecutive_failures))
    threshold = max(2, int(failure_threshold))
    if task_state == DISABLED:
        return WatchdogDecision("disabled", failures)
    if transaction_active:
        return WatchdogDecision("transaction_hold", failures)
    if task_state == READY:
        if health_ok:
            return WatchdogDecision("orphan_healthy", 0)
        if not restartable:
            return WatchdogDecision("blocked", failures)
        failures += 1
        return WatchdogDecision(
            "start" if start_allowed else "start_backoff",
            failures,
        )
    if task_state == RUNNING and health_ok:
        return WatchdogDecision("healthy", 0)
    if task_state == RUNNING:
        if not restartable:
            return WatchdogDecision("blocked", failures)
        failures += 1
        return WatchdogDecision(
            (
                "restart" if start_allowed else "restart_backoff"
            ) if failures >= threshold else "degraded",
            failures,
        )
    return WatchdogDecision("wait", failures)


def load_state(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"consecutive_failures": 0, "last_action": "initial", "last_start_epoch": 0.0}
    if not isinstance(payload, dict):
        return {"consecutive_failures": 0, "last_action": "initial", "last_start_epoch": 0.0}
    failures = payload.get("consecutive_failures", 0)
    last_start_epoch = payload.get("last_start_epoch", 0.0)
    return {
        "consecutive_failures": max(0, int(failures)) if isinstance(failures, int) else 0,
        "last_action": str(payload.get("last_action", "unknown")),
        "last_start_epoch": (
            max(0.0, float(last_start_epoch))
            if isinstance(last_start_epoch, (int, float))
            else 0.0
        ),
    }


def save_state(
    path: Path,
    *,
    consecutive_failures: int,
    last_action: str,
    last_start_epoch: float = 0.0,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    payload = {
        "consecutive_failures": max(0, int(consecutive_failures)),
        "last_action": str(last_action),
        "last_start_epoch": max(0.0, float(last_start_epoch)),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    temporary.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _timestamp_epoch(value: object) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def inspect_control_plane_lease(
    path: Path,
    *,
    now_epoch: float | None = None,
    stale_after_seconds: float = 300.0,
) -> ControlPlaneLease:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ControlPlaneLease("inactive", "missing")
    except OSError:
        return ControlPlaneLease("hold", "unreadable")
    except (TypeError, ValueError, json.JSONDecodeError):
        return ControlPlaneLease("hold", "malformed")
    if not isinstance(payload, dict):
        return ControlPlaneLease("hold", "malformed")
    operation_id = str(payload.get("operation_id", ""))
    if CONTROL_PLANE_OPERATION_RE.fullmatch(operation_id) is None:
        return ControlPlaneLease("hold", "invalid_operation_id")
    created_epoch = _timestamp_epoch(payload.get("created_at"))
    if created_epoch is None:
        return ControlPlaneLease("hold", "invalid_created_at", operation_id)
    age_seconds = max(0.0, (time.time() if now_epoch is None else now_epoch) - created_epoch)
    receipt_path = path.parent / "receipts" / f"{operation_id}.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if age_seconds >= max(30.0, stale_after_seconds):
            return ControlPlaneLease("recover", "transaction_stale", operation_id, age_seconds)
        return ControlPlaneLease("hold", "transaction_starting", operation_id, age_seconds)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return ControlPlaneLease("hold", "receipt_unreadable", operation_id, age_seconds)
    if not isinstance(receipt, dict) or receipt.get("operation_id") != operation_id:
        return ControlPlaneLease("hold", "receipt_identity_mismatch", operation_id, age_seconds)
    receipt_updated_epoch = _timestamp_epoch(receipt.get("updated_at"))
    activity_epoch = max(
        created_epoch,
        receipt_updated_epoch if receipt_updated_epoch is not None else created_epoch,
    )
    activity_age_seconds = max(
        0.0,
        (time.time() if now_epoch is None else now_epoch) - activity_epoch,
    )
    receipt_state = str(receipt.get("state", ""))
    if receipt_state in CONTROL_PLANE_TERMINAL_RECOVERY_STATES:
        return ControlPlaneLease(
            "recover",
            f"terminal_{receipt_state}",
            operation_id,
            activity_age_seconds,
        )
    if activity_age_seconds >= max(30.0, stale_after_seconds):
        return ControlPlaneLease("recover", "manager_stale", operation_id, activity_age_seconds)
    return ControlPlaneLease("hold", "manager_active", operation_id, activity_age_seconds)


def lease_is_active(path: Path) -> bool:
    return inspect_control_plane_lease(path).action != "inactive"


def recover_control_plane_transaction(active_path: Path, *, timeout: float = 110.0) -> bool:
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "control_plane_transaction.py"),
            "execute",
            "--state-root",
            str(active_path.parent),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
    )
    return completed.returncode == 0


def powershell_executable() -> Path:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    candidate = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if candidate.is_file():
        return candidate
    raise RuntimeError("Windows PowerShell is unavailable")


def expected_http_task_arguments() -> str:
    digest = hashlib.sha256(EXPECTED_HTTP_TASK_CONFIG.read_bytes()).hexdigest()
    return (
        f'"{EXPECTED_HTTP_TASK_SCRIPT}" --config "{EXPECTED_HTTP_TASK_CONFIG}" '
        f"--expect-config-sha256 {digest}"
    )


def run_powershell(task_name: str, task_path: str, script: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["CODING_TOOLS_HTTP_TASK_NAME"] = task_name
    env["CODING_TOOLS_HTTP_TASK_PATH"] = task_path
    env["CODING_TOOLS_HTTP_TASK_EXECUTABLE"] = str(EXPECTED_HTTP_TASK_EXECUTABLE)
    env["CODING_TOOLS_HTTP_TASK_ARGUMENTS"] = expected_http_task_arguments()
    return subprocess.run(
        [
            str(powershell_executable()),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            "$ErrorActionPreference='Stop';" + script,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        env=env,
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
    )


def task_state(task_name: str, task_path: str = "\\") -> str:
    result = run_powershell(
        task_name,
        task_path,
        "$task=Get-ScheduledTask -TaskPath $env:CODING_TOOLS_HTTP_TASK_PATH -TaskName $env:CODING_TOOLS_HTTP_TASK_NAME;"
        "[pscustomobject]@{state=[string]$task.State}|ConvertTo-Json -Compress",
    )
    if result.returncode != 0:
        raise RuntimeError("could not query the HTTP MCP task")
    for line in reversed(result.stdout.splitlines()):
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("state"), str):
            return str(payload["state"])
    raise RuntimeError("HTTP MCP task query returned no structured state")


def reregister_task(task_name: str, *, stop_running: bool, task_path: str = "\\") -> None:
    result = run_powershell(
        task_name,
        task_path,
        "$task=Get-ScheduledTask -TaskPath $env:CODING_TOOLS_HTTP_TASK_PATH -TaskName $env:CODING_TOOLS_HTTP_TASK_NAME;"
        "if([string]$task.State -eq 'Disabled'){exit 23};"
        + (
            "if([string]$task.State -eq 'Running'){Stop-ScheduledTask -TaskPath $env:CODING_TOOLS_HTTP_TASK_PATH -TaskName $env:CODING_TOOLS_HTTP_TASK_NAME};"
            "$deadline=(Get-Date).AddSeconds(10);"
            "do{$task=Get-ScheduledTask -TaskPath $env:CODING_TOOLS_HTTP_TASK_PATH -TaskName $env:CODING_TOOLS_HTTP_TASK_NAME;"
            "if([string]$task.State -ne 'Running'){break};Start-Sleep -Milliseconds 250}while((Get-Date)-lt $deadline);"
            "if([string]$task.State -eq 'Running'){exit 32};"
            if stop_running
            else ""
        )
        + "$raw=Export-ScheduledTask -TaskPath $env:CODING_TOOLS_HTTP_TASK_PATH -TaskName $env:CODING_TOOLS_HTTP_TASK_NAME;"
        "[xml]$xml=$raw;$ns=New-Object Xml.XmlNamespaceManager($xml.NameTable);"
        "$ns.AddNamespace('t','http://schemas.microsoft.com/windows/2004/02/mit/task');"
        "if(-not $xml.SelectSingleNode('//t:RegistrationTrigger',$ns)){exit 24};"
        "$actionsNode=$xml.SelectSingleNode('//t:Actions',$ns);"
        "$actionNodes=@($actionsNode.ChildNodes|Where-Object {$_.NodeType -eq [Xml.XmlNodeType]::Element});"
        "if($actionNodes.Count -ne 1 -or $actionNodes[0].LocalName -ne 'Exec'){exit 26};"
        "$identity=[Security.Principal.WindowsIdentity]::GetCurrent();"
        "$user=[string]$xml.SelectSingleNode('//t:Principal/t:UserId',$ns).InnerText;"
        "$short=($identity.Name -split '\\\\')[-1];"
        "if($user -notin @($identity.User.Value,$identity.Name,$short)){exit 27};"
        "$logonType=[string]$xml.SelectSingleNode('//t:Principal/t:LogonType',$ns).InnerText;"
        "if($logonType -ne 'InteractiveToken'){exit 28};"
        "$runLevel=[string]$xml.SelectSingleNode('//t:Principal/t:RunLevel',$ns).InnerText;"
        "if($runLevel -ne 'HighestAvailable'){exit 25};"
        "$multiple=[string]$xml.SelectSingleNode('//t:Settings/t:MultipleInstancesPolicy',$ns).InnerText;"
        "if($multiple -ne 'IgnoreNew'){exit 29};"
        "$command=[string]$xml.SelectSingleNode('//t:Actions/t:Exec/t:Command',$ns).InnerText;"
        "if([IO.Path]::GetFullPath($command) -ne [IO.Path]::GetFullPath($env:CODING_TOOLS_HTTP_TASK_EXECUTABLE)){exit 26};"
        "$arguments=[string]$xml.SelectSingleNode('//t:Actions/t:Exec/t:Arguments',$ns).InnerText;"
        "if($arguments -cne $env:CODING_TOOLS_HTTP_TASK_ARGUMENTS){exit 30};"
        "$working=[string]$xml.SelectSingleNode('//t:Actions/t:Exec/t:WorkingDirectory',$ns).InnerText;"
        "if(-not [string]::IsNullOrWhiteSpace($working)){exit 31};"
        "Register-ScheduledTask -TaskPath $env:CODING_TOOLS_HTTP_TASK_PATH -TaskName $env:CODING_TOOLS_HTTP_TASK_NAME -Xml $raw -Force|Out-Null",
    )
    if result.returncode == 23:
        raise RuntimeError("HTTP MCP task was disabled before re-registration")
    if result.returncode == 24:
        raise RuntimeError("HTTP MCP task has no registration recovery trigger")
    if result.returncode == 25:
        raise RuntimeError("HTTP MCP task is not configured for HighestAvailable")
    if result.returncode == 26:
        raise RuntimeError("HTTP MCP task action changed before re-registration")
    if result.returncode in {27, 28}:
        raise RuntimeError("HTTP MCP task principal changed before re-registration")
    if result.returncode == 29:
        raise RuntimeError("HTTP MCP task single-instance policy changed before re-registration")
    if result.returncode in {30, 31}:
        raise RuntimeError("HTTP MCP task action arguments changed before re-registration")
    if result.returncode == 32:
        raise RuntimeError("HTTP MCP task did not stop before re-registration")
    if result.returncode != 0:
        raise RuntimeError("could not re-register the HTTP MCP task")


def start_task(task_name: str, task_path: str = "\\") -> None:
    reregister_task(task_name, stop_running=False, task_path=task_path)


def restart_task(task_name: str, task_path: str = "\\") -> None:
    reregister_task(task_name, stop_running=True, task_path=task_path)


def run_guarded_lifecycle_action(
    args: argparse.Namespace,
    action: str,
) -> tuple[bool, ControlPlaneLease]:
    state_root = args.control_plane_lease_file.parent
    try:
        with process_lock(state_root):
            lease = inspect_control_plane_lease(
                args.control_plane_lease_file,
                stale_after_seconds=args.control_plane_stale_seconds,
            )
            if lease.action != "inactive":
                return False, lease
            if action == "start":
                start_task(args.task_name, args.task_path)
            elif action == "restart":
                restart_task(args.task_name, args.task_path)
            else:
                raise ValueError(f"unsupported lifecycle action: {action}")
            return True, lease
    except LeaseBusy:
        return False, ControlPlaneLease("hold", "manager_lock_busy")


def probe(url: str, timeout: float) -> bool:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return False
            payload = json.load(response)
    except (OSError, ValueError, urllib.error.URLError):
        return False
    if not isinstance(payload, dict):
        return False
    transport = payload.get("transport")
    return isinstance(transport, dict) and transport.get("endpoint") == "/mcp"


def rotate_event_log(path: Path, *, max_bytes: int, backups: int) -> None:
    try:
        if path.stat().st_size < max_bytes:
            return
    except FileNotFoundError:
        return
    for index in range(backups, 0, -1):
        source = path if index == 1 else path.with_name(f"{path.name}.{index - 1}")
        destination = path.with_name(f"{path.name}.{index}")
        if not source.exists():
            continue
        destination.unlink(missing_ok=True)
        os.replace(source, destination)


def append_event(path: Path, **fields: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rotate_event_log(
        path,
        max_bytes=DEFAULT_EVENT_LOG_MAX_BYTES,
        backups=DEFAULT_EVENT_LOG_BACKUPS,
    )
    event = {"time": datetime.now(timezone.utc).isoformat(), **fields}
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-name", type=non_empty_text, required=True)
    parser.add_argument("--task-path", type=non_empty_text, default="\\")
    parser.add_argument("--health-url", type=non_empty_text, required=True)
    parser.add_argument("--mcp-url", type=non_empty_text, required=True)
    parser.add_argument("--expected-workspace", type=non_empty_text, required=True)
    parser.add_argument("--required-tool", action="append", default=[])
    parser.add_argument("--expected-tool-catalog-sha256", type=sha256_value)
    parser.add_argument("--authorization-header-file", type=non_empty_path, required=True)
    parser.add_argument("--event-log", type=non_empty_text, required=True)
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--failure-threshold", type=int, default=3)
    parser.add_argument("--start-cooldown-seconds", type=float, default=120.0)
    parser.add_argument("--control-plane-stale-seconds", type=float, default=300.0)
    parser.add_argument("--probe-timeout-seconds", type=float, default=3.0)
    parser.add_argument("--startup-grace-seconds", type=float, default=20.0)
    parser.add_argument("--loop-interval-seconds", type=float, default=0.0)
    parser.add_argument(
        "--control-plane-lease-file",
        type=Path,
        default=DEFAULT_CONTROL_PLANE_LEASE,
    )
    return parser


def run_once(args: argparse.Namespace, authorization_header: str | None) -> int:
    if authorization_header is None:
        authorization_header = load_authorization_header(args.authorization_header_file)
    def backend_probe() -> BackendProbe:
        return probe_mcp_backend(
            args.mcp_url,
            expected_workspace=args.expected_workspace,
            required_tools=tuple(args.required_tool),
            timeout=args.probe_timeout_seconds,
            authorization_header=authorization_header,
        )

    event_log = Path(args.event_log)
    state_file = args.state_file or event_log.with_name("watchdog-state.json")
    persisted = load_state(state_file)
    state = task_state(args.task_name, args.task_path)
    backend = backend_probe()
    expected_catalog_sha256 = getattr(args, "expected_tool_catalog_sha256", None)
    actual_catalog_sha256 = tool_catalog_sha256(backend.tools) if backend.tools else None
    catalog_ok = expected_catalog_sha256 is None or actual_catalog_sha256 == expected_catalog_sha256
    health_url_ok = probe(args.health_url, args.probe_timeout_seconds)
    healthy = backend.ok and health_url_ok and catalog_ok
    now_epoch = time.time()
    last_start_epoch = float(persisted.get("last_start_epoch", 0.0))
    start_allowed = (
        last_start_epoch <= 0.0
        or now_epoch - last_start_epoch >= args.start_cooldown_seconds
    )
    lease = inspect_control_plane_lease(
        args.control_plane_lease_file,
        stale_after_seconds=args.control_plane_stale_seconds,
    )
    lease_recovery_attempted = lease.action == "recover"
    lease_recovery_ok: bool | None = None
    if lease_recovery_attempted:
        lease_recovery_ok = recover_control_plane_transaction(args.control_plane_lease_file)
        lease = inspect_control_plane_lease(
            args.control_plane_lease_file,
            stale_after_seconds=args.control_plane_stale_seconds,
        )
        backend = backend_probe()
        actual_catalog_sha256 = tool_catalog_sha256(backend.tools) if backend.tools else None
        catalog_ok = expected_catalog_sha256 is None or actual_catalog_sha256 == expected_catalog_sha256
        health_url_ok = probe(args.health_url, args.probe_timeout_seconds)
        healthy = backend.ok and health_url_ok and catalog_ok
        state = task_state(args.task_name, args.task_path)
        persisted = load_state(state_file)
        now_epoch = time.time()
        last_start_epoch = float(persisted.get("last_start_epoch", 0.0))
        start_allowed = (
            last_start_epoch <= 0.0
            or now_epoch - last_start_epoch >= args.start_cooldown_seconds
        )
    transaction_active = lease.action != "inactive"
    decision = decide_action(
        state,
        healthy,
        int(persisted["consecutive_failures"]),
        args.failure_threshold,
        restartable=backend.retryable or (backend.ok and (not health_url_ok or not catalog_ok)),
        start_allowed=start_allowed,
        transaction_active=transaction_active,
    )
    action = decision.action
    operation_error: str | None = None
    attempted_start = action in {"start", "restart"}
    if attempted_start:
        try:
            performed, guarded_lease = run_guarded_lifecycle_action(args, action)
            lease = guarded_lease
            if not performed:
                attempted_start = False
                transaction_active = True
                action = "transaction_hold"
            else:
                last_start_epoch = now_epoch
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            operation_error = f"{type(exc).__name__}: {exc}"
            action = f"{action}_failed"

    recovered = healthy
    if attempted_start and operation_error is None:
        deadline = time.monotonic() + max(0.0, args.startup_grace_seconds)
        while time.monotonic() < deadline:
            backend = backend_probe()
            actual_catalog_sha256 = tool_catalog_sha256(backend.tools) if backend.tools else None
            catalog_ok = expected_catalog_sha256 is None or actual_catalog_sha256 == expected_catalog_sha256
            health_url_ok = probe(args.health_url, args.probe_timeout_seconds)
            recovered_now = backend.ok and health_url_ok and catalog_ok
            if recovered_now:
                recovered = True
                break
            time.sleep(1)

    next_failures = decision.consecutive_failures
    if attempted_start and operation_error is None and recovered:
        next_failures = 0
    save_state(
        state_file,
        consecutive_failures=next_failures,
        last_action=action,
        last_start_epoch=last_start_epoch,
    )

    append_event(
        event_log,
        action=action,
        task_state=state,
        healthy=healthy,
        health_url_ok=health_url_ok,
        tool_catalog_ok=catalog_ok,
        expected_tool_catalog_sha256=expected_catalog_sha256,
        actual_tool_catalog_sha256=actual_catalog_sha256,
        recovered=recovered,
        backend_category=backend.category,
        backend_phase=backend.phase,
        backend_tool_count=backend.tool_count,
        backend_state=backend.backend_state,
        backend_cause_type=backend.cause_type,
        backend_cause_message=backend.cause_message,
        backend_recovery_hint=backend.recovery_hint,
        backend_retryable=backend.retryable,
        consecutive_failures=next_failures,
        failure_threshold=args.failure_threshold,
        start_cooldown_seconds=args.start_cooldown_seconds,
        start_allowed=start_allowed,
        transaction_active=transaction_active,
        control_plane_lease_action=lease.action,
        control_plane_lease_reason=lease.reason,
        control_plane_operation_id=lease.operation_id,
        control_plane_recovery_attempted=lease_recovery_attempted,
        control_plane_recovery_ok=lease_recovery_ok,
        operation_error=operation_error,
    )
    if action in {"healthy", "degraded", "transaction_hold"}:
        return 0
    if attempted_start and operation_error is None and recovered:
        return 0
    return 1


def record_watchdog_error(args: argparse.Namespace, exc: Exception) -> None:
    try:
        append_event(
            Path(args.event_log),
            action="watchdog_error",
            error_type=type(exc).__name__,
        )
    except OSError:
        pass


def run_loop(
    args: argparse.Namespace,
    authorization_header: str | None,
    *,
    interval_seconds: float,
    max_iterations: int | None = None,
) -> int:
    completed = 0
    last_result = 0
    while True:
        try:
            last_result = run_once(args, authorization_header)
        except Exception as exc:  # noqa: BLE001 - a supervisor must survive one failed probe
            record_watchdog_error(args, exc)
            last_result = 1
        completed += 1
        if max_iterations is not None and completed >= max_iterations:
            return last_result
        time.sleep(interval_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.failure_threshold < 2:
        parser.error("--failure-threshold must be at least 2")
    if args.start_cooldown_seconds < 30:
        parser.error("--start-cooldown-seconds must be at least 30")
    if args.loop_interval_seconds != 0 and args.loop_interval_seconds < 5:
        parser.error("--loop-interval-seconds must be zero or at least 5")
    try:
        load_authorization_header(args.authorization_header_file)
    except ValueError as exc:
        parser.error(str(exc))
    if args.loop_interval_seconds > 0:
        return run_loop(
            args,
            None,
            interval_seconds=args.loop_interval_seconds,
        )
    try:
        return run_once(args, None)
    except Exception as exc:  # noqa: BLE001 - scheduled task must leave durable evidence
        record_watchdog_error(args, exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
