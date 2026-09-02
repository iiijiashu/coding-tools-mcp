#!/usr/bin/env python3
"""Repair a Windows Secure MCP Tunnel task when it exits or becomes unhealthy.

The primary scheduled task continues to own ``tunnel-client.exe``.  The
watchdog can run once or remain alive for the duration of an interactive
session, avoiding repeated Task Scheduler launches that require a fresh
interactive token.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .mcp_backend_probe import (  # noqa: F401
        BackendProbe,
        load_authorization_header,
        probe_mcp_backend,
        tool_catalog_sha256,
    )
except ImportError:  # direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from mcp_backend_probe import (  # noqa: F401
        BackendProbe,
        load_authorization_header,
        probe_mcp_backend,
        tool_catalog_sha256,
    )


RUNNING = "Running"
READY = "Ready"
DISABLED = "Disabled"
HEALTHY = "healthy"
LOCAL_UNHEALTHY = "local_unhealthy"
CONTROL_PLANE_DEGRADED = "control_plane_degraded"
DEADLINE_DEGRADED = "deadline_degraded"
COMMAND_DEADLINE_OBSERVED = "command_deadline_observed"
DEADLINE_MESSAGE = "command response deadline reached; dropping without posting a response"
DEFAULT_RECOVERY_BACKOFF_SECONDS = (60.0, 120.0, 300.0, 600.0, 1800.0)
TASK_REQUEST_REFUSED = 0x800710E0


@dataclass(frozen=True)
class Decision:
    action: str
    consecutive_failures: int


@dataclass(frozen=True)
class Config:
    main_task_name: str
    tunnel_client: Path
    tunnel_client_sha256: str
    health_url_file: Path
    pid_file: Path
    state_file: Path
    event_log: Path
    powershell: Path
    failure_threshold: int
    startup_grace_seconds: float
    health_timeout_seconds: float
    backend_url: str
    backend_workspace: str
    backend_required_tools: tuple[str, ...]
    main_task_path: str = "\\"
    tunnel_log: Path | None = None
    recovery_backoff_seconds: tuple[float, ...] = DEFAULT_RECOVERY_BACKOFF_SECONDS
    stable_health_seconds: float = 300.0
    event_log_max_bytes: int = 5 * 1024 * 1024
    event_log_backups: int = 3
    backend_authorization_header_file: Path | None = None
    loop_interval_seconds: float = 0.0
    main_task_arguments_sha256: str | None = None
    expected_tool_catalog_sha256: str | None = None


@dataclass(frozen=True)
class TaskStatus:
    state: str
    last_task_result: int | None


@dataclass(frozen=True)
class HealthProbe:
    kind: str
    ok: bool
    local_live: bool
    ready: bool
    control_plane_poll: bool


def decide_backend_action(
    task_state: str,
    backend_ok: bool,
    consecutive_failures: int,
    failure_threshold: int,
) -> str:
    if backend_ok:
        return "proceed"
    if task_state == RUNNING:
        if consecutive_failures + 1 < failure_threshold:
            return "backend_degraded"
        return "stop_backend_unavailable"
    return "hold_backend_unavailable"


def decide_action(
    task_state: str,
    health_kind: str | None,
    consecutive_failures: int,
    failure_threshold: int,
) -> Decision:
    """Return the next repair action without performing external effects."""
    if failure_threshold < 1:
        raise ValueError("failure_threshold must be at least 1")
    if task_state == DISABLED:
        return Decision("disabled", 0)
    if task_state == READY:
        return Decision("start", 0)
    if task_state != RUNNING:
        return Decision("wait", max(0, consecutive_failures))
    if health_kind == HEALTHY:
        return Decision("healthy", 0)
    if health_kind == CONTROL_PLANE_DEGRADED:
        # Restarting a live local process cannot repair an upstream outage.
        return Decision("control_plane_degraded", 0)
    if health_kind in {DEADLINE_DEGRADED, COMMAND_DEADLINE_OBSERVED}:
        # A command can exceed its response SLA while the process, health
        # endpoint, readiness endpoint, and control-plane poll are all healthy.
        # Restarting the transport cannot deliver an already-expired response
        # and needlessly drops unrelated in-flight work.
        return Decision("command_deadline_observed", 0)
    failures = max(0, consecutive_failures) + 1
    if failures >= failure_threshold:
        return Decision("restart", failures)
    return Decision("degraded", failures)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {
            "consecutive_failures": 0,
            "backend_consecutive_failures": 0,
            "recovery_attempts": 0,
            "next_recovery_at": 0.0,
            "last_recovery_at": 0.0,
            "healthy_since_at": 0.0,
            "last_deadline_time": "",
        }
    if not isinstance(payload, dict):
        return {
            "consecutive_failures": 0,
            "backend_consecutive_failures": 0,
            "recovery_attempts": 0,
            "next_recovery_at": 0.0,
            "last_recovery_at": 0.0,
            "healthy_since_at": 0.0,
            "last_deadline_time": "",
        }
    try:
        failures = max(0, int(payload.get("consecutive_failures", 0)))
    except (TypeError, ValueError):
        failures = 0
    try:
        backend_failures = max(0, int(payload.get("backend_consecutive_failures", 0)))
    except (TypeError, ValueError):
        backend_failures = 0
    try:
        attempts = max(0, int(payload.get("recovery_attempts", 0)))
    except (TypeError, ValueError):
        attempts = 0
    try:
        next_recovery_at = max(0.0, float(payload.get("next_recovery_at", 0.0)))
    except (TypeError, ValueError):
        next_recovery_at = 0.0
    try:
        last_recovery_at = max(0.0, float(payload.get("last_recovery_at", 0.0)))
    except (TypeError, ValueError):
        last_recovery_at = 0.0
    try:
        healthy_since_at = max(0.0, float(payload.get("healthy_since_at", 0.0)))
    except (TypeError, ValueError):
        healthy_since_at = 0.0
    return {
        **payload,
        "consecutive_failures": failures,
        "backend_consecutive_failures": backend_failures,
        "recovery_attempts": attempts,
        "next_recovery_at": next_recovery_at,
        "last_recovery_at": last_recovery_at,
        "healthy_since_at": healthy_since_at,
        "last_deadline_time": str(payload.get("last_deadline_time", "") or ""),
    }


def save_state(
    path: Path,
    *,
    consecutive_failures: int,
    backend_consecutive_failures: int = 0,
    last_action: str,
    recovery_attempts: int = 0,
    next_recovery_at: float = 0.0,
    last_recovery_at: float = 0.0,
    healthy_since_at: float = 0.0,
    last_deadline_time: str = "",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "consecutive_failures": max(0, consecutive_failures),
        "backend_consecutive_failures": max(0, backend_consecutive_failures),
        "recovery_attempts": max(0, recovery_attempts),
        "next_recovery_at": max(0.0, next_recovery_at),
        "last_recovery_at": max(0.0, last_recovery_at),
        "healthy_since_at": max(0.0, healthy_since_at),
        "last_deadline_time": last_deadline_time,
        "last_action": last_action,
        "updated_at": utc_now(),
    }
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=True, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def rotate_event_log(path: Path, *, max_bytes: int, backups: int) -> None:
    if max_bytes <= 0 or backups <= 0:
        return
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
        if destination.exists():
            destination.unlink()
        os.replace(source, destination)


def append_event(
    path: Path,
    *,
    max_bytes: int = 5 * 1024 * 1024,
    backups: int = 3,
    **fields: Any,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rotate_event_log(path, max_bytes=max_bytes, backups=backups)
    event = {"time": utc_now(), **fields}
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n")


def creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def run_powershell(config: Config, script: str, *, timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["OPENAI_TUNNEL_TASK_NAME"] = config.main_task_name
    environment["OPENAI_TUNNEL_TASK_PATH"] = config.main_task_path
    environment["OPENAI_TUNNEL_CLIENT"] = str(config.tunnel_client)
    environment["OPENAI_TUNNEL_ARGUMENTS_SHA256"] = config.main_task_arguments_sha256 or ""
    prefix = (
        "$ErrorActionPreference='Stop';"
        "$OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
    )
    return subprocess.run(
        [
            str(config.powershell),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            prefix + script,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=environment,
        creationflags=creation_flags(),
    )


def task_status(config: Config) -> TaskStatus:
    result = run_powershell(
        config,
        "$task=Get-ScheduledTask -TaskPath $env:OPENAI_TUNNEL_TASK_PATH -TaskName $env:OPENAI_TUNNEL_TASK_NAME;"
        "$info=Get-ScheduledTaskInfo -TaskPath $env:OPENAI_TUNNEL_TASK_PATH -TaskName $env:OPENAI_TUNNEL_TASK_NAME;"
        "[pscustomobject]@{State=[string]$task.State;LastTaskResult=[int64]$info.LastTaskResult}"
        "|ConvertTo-Json -Compress",
    )
    if result.returncode != 0:
        raise RuntimeError("could not query the primary tunnel task")
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("primary tunnel task query returned no data")
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise RuntimeError("primary tunnel task query returned invalid JSON") from exc
    last_result = payload.get("LastTaskResult")
    return TaskStatus(
        state=str(payload.get("State", "Unknown")),
        last_task_result=int(last_result) if last_result is not None else None,
    )


def start_task(config: Config) -> None:
    reregister_task(config)


def _require_main_task_arguments_pin(config: Config) -> None:
    if not config.main_task_arguments_sha256:
        raise RuntimeError("primary tunnel task arguments SHA-256 pin is required before lifecycle recovery")


def reregister_task(config: Config) -> None:
    _require_main_task_arguments_pin(config)
    result = run_powershell(
        config,
        "$task=Get-ScheduledTask -TaskPath $env:OPENAI_TUNNEL_TASK_PATH -TaskName $env:OPENAI_TUNNEL_TASK_NAME;"
        "if([string]$task.State -eq 'Disabled'){exit 23};"
        "$raw=Export-ScheduledTask -TaskPath $env:OPENAI_TUNNEL_TASK_PATH -TaskName $env:OPENAI_TUNNEL_TASK_NAME;"
        "[xml]$xml=$raw;$ns=New-Object Xml.XmlNamespaceManager($xml.NameTable);"
        "$ns.AddNamespace('t','http://schemas.microsoft.com/windows/2004/02/mit/task');"
        "if(-not $xml.SelectSingleNode('//t:RegistrationTrigger',$ns)){exit 24};"
        "$actionsNode=$xml.SelectSingleNode('//t:Actions',$ns);"
        "$actionNodes=@($actionsNode.ChildNodes|Where-Object {$_.NodeType -eq [Xml.XmlNodeType]::Element});"
        "if($actionNodes.Count -ne 1 -or $actionNodes[0].LocalName -ne 'Exec'){exit 25};"
        "$identity=[Security.Principal.WindowsIdentity]::GetCurrent();"
        "$user=[string]$xml.SelectSingleNode('//t:Principal/t:UserId',$ns).InnerText;"
        "$short=($identity.Name -split '\\\\')[-1];"
        "if($user -notin @($identity.User.Value,$identity.Name,$short)){exit 27};"
        "$level=[string]$xml.SelectSingleNode('//t:Principal/t:RunLevel',$ns).InnerText;"
        "if($level -and $level -ne 'LeastPrivilege'){exit 28};"
        "$multiple=[string]$xml.SelectSingleNode('//t:Settings/t:MultipleInstancesPolicy',$ns).InnerText;"
        "if($multiple -ne 'IgnoreNew'){exit 29};"
        "$command=[string]$xml.SelectSingleNode('//t:Actions/t:Exec/t:Command',$ns).InnerText;"
        "if([IO.Path]::GetFullPath($command) -ne [IO.Path]::GetFullPath($env:OPENAI_TUNNEL_CLIENT)){exit 25};"
        "$logonType=[string]$xml.SelectSingleNode('//t:Principal/t:LogonType',$ns).InnerText;"
        "if($logonType -ne 'InteractiveToken'){exit 26};"
        "$arguments=[string]$xml.SelectSingleNode('//t:Actions/t:Exec/t:Arguments',$ns).InnerText;"
        "if($env:OPENAI_TUNNEL_ARGUMENTS_SHA256){$sha=[Security.Cryptography.SHA256]::Create();"
        "try{$bytes=[Text.Encoding]::UTF8.GetBytes($arguments);$actual=([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()};"
        "if($actual -cne $env:OPENAI_TUNNEL_ARGUMENTS_SHA256){exit 30}};"
        "$working=[string]$xml.SelectSingleNode('//t:Actions/t:Exec/t:WorkingDirectory',$ns).InnerText;"
        "if(-not [string]::IsNullOrWhiteSpace($working)){exit 31};"
        "Register-ScheduledTask -TaskPath $env:OPENAI_TUNNEL_TASK_PATH "
        "-TaskName $env:OPENAI_TUNNEL_TASK_NAME -Xml $raw -Force|Out-Null",
    )
    if result.returncode == 23:
        raise RuntimeError("primary tunnel task was disabled before re-registration")
    if result.returncode == 24:
        raise RuntimeError("primary tunnel task has no registration recovery trigger")
    if result.returncode == 25:
        raise RuntimeError("primary tunnel task action changed before re-registration")
    if result.returncode == 26:
        raise RuntimeError("primary tunnel task logon type changed before re-registration")
    if result.returncode == 27:
        raise RuntimeError("primary tunnel task principal changed before re-registration")
    if result.returncode == 28:
        raise RuntimeError("primary tunnel task run level changed before re-registration")
    if result.returncode == 29:
        raise RuntimeError("primary tunnel task single-instance policy changed before re-registration")
    if result.returncode == 30:
        raise RuntimeError("primary tunnel task arguments changed before re-registration")
    if result.returncode == 31:
        raise RuntimeError("primary tunnel task working directory changed before re-registration")
    if result.returncode != 0:
        raise RuntimeError("could not re-register the primary tunnel task")


def restart_task(config: Config) -> None:
    _require_main_task_arguments_pin(config)
    result = run_powershell(
        config,
        "$task=Get-ScheduledTask -TaskPath $env:OPENAI_TUNNEL_TASK_PATH -TaskName $env:OPENAI_TUNNEL_TASK_NAME;"
        "if([string]$task.State -eq 'Disabled'){exit 23};"
        "if([string]$task.State -eq 'Running'){Stop-ScheduledTask -TaskPath $env:OPENAI_TUNNEL_TASK_PATH -TaskName $env:OPENAI_TUNNEL_TASK_NAME};"
        "$deadline=(Get-Date).AddSeconds(10);"
        "do{$task=Get-ScheduledTask -TaskPath $env:OPENAI_TUNNEL_TASK_PATH -TaskName $env:OPENAI_TUNNEL_TASK_NAME;"
        "if([string]$task.State -ne 'Running'){break};Start-Sleep -Milliseconds 250}while((Get-Date)-lt $deadline);"
        "if([string]$task.State -eq 'Running'){exit 32}",
        timeout=25.0,
    )
    if result.returncode == 23:
        raise RuntimeError("primary tunnel task was disabled during restart")
    if result.returncode == 32:
        raise RuntimeError("primary tunnel task did not stop before re-registration")
    if result.returncode != 0:
        raise RuntimeError("could not restart the primary tunnel task")
    reregister_task(config)


def stop_task(config: Config) -> None:
    result = run_powershell(
        config,
        "Stop-ScheduledTask -TaskPath $env:OPENAI_TUNNEL_TASK_PATH -TaskName $env:OPENAI_TUNNEL_TASK_NAME",
    )
    if result.returncode != 0:
        raise RuntimeError("could not stop the primary tunnel task")


def current_pid(path: Path) -> int | None:
    try:
        value = int(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return None
    return value if value > 0 else None


def scan_deadline_drops(path: Path | None, after_time: str) -> tuple[str, int]:
    """Track response-deadline drops without rescanning an unbounded log."""
    if path is None or not path.is_file():
        return after_time, 0
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            start = max(0, size - 1024 * 1024)
            stream.seek(start)
            if start:
                stream.readline()
            lines = stream.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return after_time, 0

    newest = after_time
    baseline_only = not after_time
    count = 0
    for line in lines:
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict) or event.get("msg") != DEADLINE_MESSAGE:
            continue
        event_time = str(event.get("time", ""))
        if not event_time:
            continue
        if event_time > newest:
            newest = event_time
        if not baseline_only and event_time > after_time:
            count += 1
    return newest, count


def probe_health(config: Config) -> HealthProbe:
    if not config.health_url_file.is_file() or not config.pid_file.is_file():
        return HealthProbe(LOCAL_UNHEALTHY, False, False, False, False)
    try:
        result = subprocess.run(
            [
                str(config.tunnel_client),
                "health",
                "--url-file",
                str(config.health_url_file),
                "--pid-file",
                str(config.pid_file),
                "--require-control-plane-poll",
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=config.health_timeout_seconds,
            creationflags=creation_flags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return HealthProbe(LOCAL_UNHEALTHY, False, False, False, False)
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return HealthProbe(LOCAL_UNHEALTHY, False, False, False, False)
    process = payload.get("process") if isinstance(payload, dict) else None
    healthz = payload.get("healthz") if isinstance(payload, dict) else None
    readyz = payload.get("readyz") if isinstance(payload, dict) else None
    poll = payload.get("control_plane_poll") if isinstance(payload, dict) else None
    local_live = bool(
        isinstance(process, dict)
        and process.get("running") is True
        and isinstance(healthz, dict)
        and healthz.get("ok") is True
    )
    ready = bool(isinstance(readyz, dict) and readyz.get("ok") is True)
    control_plane_poll = bool(isinstance(poll, dict) and poll.get("ok") is True)
    if result.returncode == 0 and local_live and ready and control_plane_poll:
        return HealthProbe(HEALTHY, True, True, True, True)
    if local_live and not control_plane_poll:
        return HealthProbe(CONTROL_PLANE_DEGRADED, False, True, ready, False)
    return HealthProbe(LOCAL_UNHEALTHY, False, local_live, ready, control_plane_poll)


def wait_for_health(config: Config) -> HealthProbe:
    deadline = time.monotonic() + max(0.0, config.startup_grace_seconds)
    while True:
        probe = probe_health(config)
        if probe.ok:
            return probe
        if time.monotonic() >= deadline:
            return probe
        time.sleep(min(2.0, max(0.1, deadline - time.monotonic())))


def record(
    config: Config,
    *,
    action: str,
    status: TaskStatus,
    failures: int,
    backend_failures: int,
    recovery_attempts: int,
    next_recovery_at: float,
    last_recovery_at: float,
    healthy_since_at: float,
    probe: HealthProbe | None,
    last_deadline_time: str,
    new_deadline_drops: int = 0,
) -> None:
    append_event(
        config.event_log,
        max_bytes=config.event_log_max_bytes,
        backups=config.event_log_backups,
        action=action,
        task_state=status.state,
        last_task_result=status.last_task_result,
        consecutive_failures=failures,
        backend_consecutive_failures=backend_failures,
        healthy=probe.ok if probe is not None else None,
        health_kind=probe.kind if probe is not None else None,
        pid=current_pid(config.pid_file) if status.state == RUNNING else None,
        recovery_attempts=recovery_attempts,
        next_recovery_at=next_recovery_at or None,
        healthy_since_at=healthy_since_at or None,
        new_deadline_drops=new_deadline_drops,
    )
    save_state(
        config.state_file,
        consecutive_failures=failures,
        backend_consecutive_failures=backend_failures,
        last_action=action,
        recovery_attempts=recovery_attempts,
        next_recovery_at=next_recovery_at,
        last_recovery_at=last_recovery_at,
        healthy_since_at=healthy_since_at,
        last_deadline_time=last_deadline_time,
    )


def recovery_delay(config: Config, attempt: int) -> float:
    index = min(max(1, attempt), len(config.recovery_backoff_seconds)) - 1
    return config.recovery_backoff_seconds[index]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_once(config: Config) -> int:
    if os.name != "nt":
        raise RuntimeError("the Secure MCP Tunnel watchdog requires Windows")
    for required in (config.tunnel_client, config.powershell):
        if not required.is_file():
            raise RuntimeError(f"required executable is missing: {required}")
    if file_sha256(config.tunnel_client) != config.tunnel_client_sha256:
        raise RuntimeError("tunnel client fingerprint does not match the runtime contract")
    state = load_state(config.state_file)
    failures = int(state.get("consecutive_failures", 0))
    backend_failures = int(state.get("backend_consecutive_failures", 0))
    recovery_attempts = int(state.get("recovery_attempts", 0))
    next_recovery_at = float(state.get("next_recovery_at", 0.0))
    last_recovery_at = float(state.get("last_recovery_at", 0.0))
    healthy_since_at = float(state.get("healthy_since_at", 0.0))
    last_deadline_time = str(state.get("last_deadline_time", "") or "")
    status = task_status(config)
    authorization_header = load_authorization_header(config.backend_authorization_header_file)
    backend = probe_mcp_backend(
        config.backend_url,
        expected_workspace=config.backend_workspace,
        required_tools=config.backend_required_tools,
        timeout=config.health_timeout_seconds,
        authorization_header=authorization_header,
    )
    actual_catalog_sha256 = tool_catalog_sha256(backend.tools) if backend.tools else None
    catalog_ok = (
        config.expected_tool_catalog_sha256 is None
        or actual_catalog_sha256 == config.expected_tool_catalog_sha256
    )
    backend_action = decide_backend_action(
        status.state,
        backend.ok and catalog_ok,
        backend_failures,
        config.failure_threshold,
    )
    if backend_action != "proceed":
        if status.state == RUNNING:
            backend_failures = min(backend_failures + 1, config.failure_threshold)
        if backend_action == "stop_backend_unavailable":
            stop_task(config)
        append_event(
            config.event_log,
            max_bytes=config.event_log_max_bytes,
            backups=config.event_log_backups,
            action=backend_action,
            task_state=status.state,
            backend_ok=backend.ok,
            backend_category=backend.category,
            backend_phase=backend.phase,
            backend_retryable=backend.retryable,
            backend_message=backend.message,
            backend_state=backend.backend_state,
            backend_cause_type=backend.cause_type,
            backend_cause_message=backend.cause_message,
            backend_recovery_hint=backend.recovery_hint,
            tool_catalog_ok=catalog_ok,
            expected_tool_catalog_sha256=config.expected_tool_catalog_sha256,
            actual_tool_catalog_sha256=actual_catalog_sha256,
        )
        save_state(
            config.state_file,
            consecutive_failures=failures,
            backend_consecutive_failures=backend_failures,
            last_action=backend_action,
            recovery_attempts=recovery_attempts,
            next_recovery_at=next_recovery_at,
            last_recovery_at=last_recovery_at,
            healthy_since_at=healthy_since_at,
            last_deadline_time=last_deadline_time,
        )
        return 1
    backend_failures = 0
    probe = probe_health(config) if status.state == RUNNING else None
    new_deadline_drops = 0
    if status.state == RUNNING:
        last_deadline_time, new_deadline_drops = scan_deadline_drops(
            config.tunnel_log, last_deadline_time
        )
        if probe is not None and probe.kind == HEALTHY and new_deadline_drops:
            probe = HealthProbe(
                COMMAND_DEADLINE_OBSERVED,
                True,
                probe.local_live,
                probe.ready,
                probe.control_plane_poll,
            )
    decision = decide_action(
        status.state,
        probe.kind if probe is not None else None,
        failures,
        config.failure_threshold,
    )

    if decision.action in {
        "disabled",
        "wait",
        "healthy",
        "degraded",
        "control_plane_degraded",
        "command_deadline_observed",
    }:
        action = decision.action
        if decision.action == "disabled":
            recovery_attempts = 0
            next_recovery_at = 0.0
            last_recovery_at = 0.0
            healthy_since_at = 0.0
        elif decision.action in {"healthy", "command_deadline_observed"}:
            now = time.time()
            if recovery_attempts == 0:
                next_recovery_at = 0.0
                last_recovery_at = 0.0
                healthy_since_at = 0.0
            else:
                if healthy_since_at <= 0.0:
                    healthy_since_at = now
                if now - healthy_since_at >= config.stable_health_seconds:
                    action = "healthy_stable"
                    recovery_attempts = 0
                    next_recovery_at = 0.0
                    last_recovery_at = 0.0
                    healthy_since_at = 0.0
        else:
            healthy_since_at = 0.0
        record(
            config,
            action=action,
            status=status,
            failures=decision.consecutive_failures,
            backend_failures=backend_failures,
            recovery_attempts=recovery_attempts,
            next_recovery_at=next_recovery_at,
            last_recovery_at=last_recovery_at,
            healthy_since_at=healthy_since_at,
            probe=probe,
            last_deadline_time=last_deadline_time,
            new_deadline_drops=new_deadline_drops,
        )
        return 0

    if decision.action in {"start", "restart"}:
        now = time.time()
        if now < next_recovery_at:
            record(
                config,
                action=f"{decision.action}_cooldown",
                status=status,
                failures=decision.consecutive_failures,
                backend_failures=backend_failures,
                recovery_attempts=recovery_attempts,
                next_recovery_at=next_recovery_at,
                last_recovery_at=last_recovery_at,
                healthy_since_at=0.0,
                probe=probe,
                last_deadline_time=last_deadline_time,
                new_deadline_drops=new_deadline_drops,
            )
            return 0

        # Persist the recovery lease before touching Task Scheduler.  If the
        # process is killed mid-attempt, the next run still observes backoff.
        recovery_attempts += 1
        last_recovery_at = now
        next_recovery_at = now + recovery_delay(config, recovery_attempts)
        healthy_since_at = 0.0
        record(
            config,
            action=f"{decision.action}_attempt",
            status=status,
            failures=0,
            backend_failures=backend_failures,
            recovery_attempts=recovery_attempts,
            next_recovery_at=next_recovery_at,
            last_recovery_at=last_recovery_at,
            healthy_since_at=healthy_since_at,
            probe=probe,
            last_deadline_time=last_deadline_time,
            new_deadline_drops=new_deadline_drops,
        )

        if decision.action == "start":
            start_task(config)
        else:
            restart_task(config)
        recovered_probe = wait_for_health(config)
        refreshed = task_status(config)
        completed_action = f"{decision.action}ed"
        if (
            not recovered_probe.ok
            and refreshed.state == READY
            and refreshed.last_task_result == TASK_REQUEST_REFUSED
        ):
            reregister_task(config)
            recovered_probe = wait_for_health(config)
            refreshed = task_status(config)
            completed_action = f"{decision.action}_reregistered"
        if recovered_probe.ok:
            # A process that becomes ready and immediately crashes is still a
            # failure loop.  Keep its backoff lease until it stays healthy for
            # the configured stability window.
            healthy_since_at = time.time()
        record(
            config,
            action=f"{completed_action}_healthy" if recovered_probe.ok else f"{completed_action}_unready",
            status=refreshed,
            failures=0,
            backend_failures=backend_failures,
            recovery_attempts=recovery_attempts,
            next_recovery_at=next_recovery_at,
            last_recovery_at=last_recovery_at,
            healthy_since_at=healthy_since_at,
            probe=recovered_probe,
            last_deadline_time=last_deadline_time,
            new_deadline_drops=new_deadline_drops,
        )
        return 0

    raise RuntimeError(f"unsupported watchdog action: {decision.action}")


def default_powershell() -> Path:
    resolved = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    return Path(resolved) if resolved else Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")


def record_watchdog_error(config: Config, exc: Exception) -> None:
    try:
        append_event(
            config.event_log,
            max_bytes=config.event_log_max_bytes,
            backups=config.event_log_backups,
            action="watchdog_error",
            error_type=type(exc).__name__,
        )
    except OSError:
        pass
    if sys.stderr is not None:
        print(f"Secure MCP Tunnel watchdog failed: {exc}", file=sys.stderr)


def run_loop(
    config: Config,
    *,
    interval_seconds: float,
    max_iterations: int | None = None,
) -> int:
    completed = 0
    last_result = 0
    while True:
        try:
            last_result = run_once(config)
        except Exception as exc:  # noqa: BLE001 - a supervisor must survive one failed probe
            record_watchdog_error(config, exc)
            last_result = 1
        completed += 1
        if max_iterations is not None and completed >= max_iterations:
            return last_result
        time.sleep(interval_seconds)


def parse_backoff(value: str) -> tuple[float, ...]:
    try:
        parsed = tuple(float(part.strip()) for part in value.split(",") if part.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("backoff values must be numbers") from exc
    if not parsed or any(item <= 0 for item in parsed):
        raise argparse.ArgumentTypeError("backoff values must be positive")
    return parsed


def normalize_task_path(value: str) -> str:
    normalized = value.strip().replace("/", "\\")
    if not normalized.startswith("\\"):
        normalized = "\\" + normalized
    if not normalized.endswith("\\"):
        normalized += "\\"
    if any(character in normalized for character in "*?[]"):
        raise argparse.ArgumentTypeError("task path must not contain wildcards")
    return normalized


def sha256_value(value: str) -> str:
    if re.fullmatch(r"[A-Fa-f0-9]{64}", value) is None:
        raise argparse.ArgumentTypeError("SHA-256 must contain 64 hexadecimal characters")
    return value.lower()


def parse_args(argv: list[str] | None = None) -> Config:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--main-task-name", required=True)
    parser.add_argument("--main-task-path", type=normalize_task_path, default="\\")
    parser.add_argument("--tunnel-client", type=Path, required=True)
    parser.add_argument("--tunnel-client-sha256", type=sha256_value, required=True)
    parser.add_argument("--health-url-file", type=Path, required=True)
    parser.add_argument("--pid-file", type=Path, required=True)
    parser.add_argument("--tunnel-log", type=Path)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--event-log", type=Path, required=True)
    parser.add_argument("--powershell", type=Path, default=default_powershell())
    parser.add_argument("--failure-threshold", type=int, default=2)
    parser.add_argument("--startup-grace-seconds", type=float, default=45.0)
    parser.add_argument("--health-timeout-seconds", type=float, default=10.0)
    parser.add_argument("--backend-url", required=True)
    parser.add_argument("--backend-workspace", required=True)
    parser.add_argument("--backend-required-tool", action="append", default=[])
    parser.add_argument("--expected-tool-catalog-sha256", type=sha256_value)
    parser.add_argument("--backend-authorization-header-file", type=Path)
    parser.add_argument("--main-task-arguments-sha256", type=sha256_value)
    parser.add_argument(
        "--recovery-backoff-seconds",
        type=parse_backoff,
        default=DEFAULT_RECOVERY_BACKOFF_SECONDS,
        help="comma-separated delays; the final value is the retry cap",
    )
    parser.add_argument("--event-log-max-bytes", type=int, default=5 * 1024 * 1024)
    parser.add_argument("--event-log-backups", type=int, default=3)
    parser.add_argument("--stable-health-seconds", type=float, default=300.0)
    parser.add_argument("--loop-interval-seconds", type=float, default=0.0)
    args = parser.parse_args(argv)
    if args.failure_threshold < 1:
        parser.error("--failure-threshold must be at least 1")
    if args.loop_interval_seconds != 0 and args.loop_interval_seconds < 5:
        parser.error("--loop-interval-seconds must be zero or at least 5")
    return Config(
        main_task_name=args.main_task_name,
        tunnel_client=args.tunnel_client.resolve(),
        tunnel_client_sha256=args.tunnel_client_sha256,
        health_url_file=args.health_url_file.resolve(),
        pid_file=args.pid_file.resolve(),
        tunnel_log=args.tunnel_log.resolve() if args.tunnel_log is not None else None,
        state_file=args.state_file.resolve(),
        event_log=args.event_log.resolve(),
        powershell=args.powershell.resolve(),
        failure_threshold=args.failure_threshold,
        startup_grace_seconds=max(0.0, args.startup_grace_seconds),
        health_timeout_seconds=max(0.1, args.health_timeout_seconds),
        backend_url=args.backend_url,
        backend_workspace=args.backend_workspace,
        backend_required_tools=tuple(args.backend_required_tool),
        backend_authorization_header_file=(
            args.backend_authorization_header_file.resolve()
            if args.backend_authorization_header_file is not None
            else None
        ),
        main_task_path=args.main_task_path,
        recovery_backoff_seconds=args.recovery_backoff_seconds,
        stable_health_seconds=max(0.0, args.stable_health_seconds),
        event_log_max_bytes=max(1024, args.event_log_max_bytes),
        event_log_backups=max(1, args.event_log_backups),
        loop_interval_seconds=max(0.0, args.loop_interval_seconds),
        main_task_arguments_sha256=args.main_task_arguments_sha256,
        expected_tool_catalog_sha256=args.expected_tool_catalog_sha256,
    )


def main(argv: list[str] | None = None) -> int:
    config = parse_args(argv)
    if config.loop_interval_seconds > 0:
        return run_loop(config, interval_seconds=config.loop_interval_seconds)
    try:
        return run_once(config)
    except Exception as exc:  # noqa: BLE001 - scheduled task must leave durable evidence
        record_watchdog_error(config, exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
