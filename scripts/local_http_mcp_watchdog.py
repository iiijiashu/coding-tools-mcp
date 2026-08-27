#!/usr/bin/env python3
"""Keep one exact Windows scheduled HTTP MCP task reachable on loopback."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    from .mcp_backend_probe import BackendProbe, probe_mcp_backend
except ImportError:  # direct script execution
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from mcp_backend_probe import BackendProbe, probe_mcp_backend


RUNNING = "Running"
READY = "Ready"
DISABLED = "Disabled"


def decide_action(task_state: str, health_ok: bool) -> str:
    if task_state == DISABLED:
        return "enable_start"
    if task_state == READY:
        return "start"
    if task_state == RUNNING and health_ok:
        return "healthy"
    if task_state == RUNNING:
        return "restart"
    return "wait"


def powershell_executable() -> Path:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    candidate = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if candidate.is_file():
        return candidate
    raise RuntimeError("Windows PowerShell is unavailable")


def run_powershell(task_name: str, script: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["CODING_TOOLS_HTTP_TASK_NAME"] = task_name
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


def task_state(task_name: str) -> str:
    result = run_powershell(
        task_name,
        "[string](Get-ScheduledTask -TaskName $env:CODING_TOOLS_HTTP_TASK_NAME).State",
    )
    if result.returncode != 0:
        raise RuntimeError("could not query the HTTP MCP task")
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("HTTP MCP task query returned no state")
    return lines[-1]


def start_task(task_name: str) -> None:
    result = run_powershell(
        task_name,
        "Start-ScheduledTask -TaskName $env:CODING_TOOLS_HTTP_TASK_NAME",
    )
    if result.returncode != 0:
        raise RuntimeError("could not start the HTTP MCP task")


def enable_and_start_task(task_name: str) -> None:
    result = run_powershell(
        task_name,
        "Enable-ScheduledTask -TaskName $env:CODING_TOOLS_HTTP_TASK_NAME|Out-Null;"
        "Start-ScheduledTask -TaskName $env:CODING_TOOLS_HTTP_TASK_NAME",
    )
    if result.returncode != 0:
        raise RuntimeError("could not enable and start the HTTP MCP task")


def restart_task(task_name: str) -> None:
    result = run_powershell(
        task_name,
        "$task=Get-ScheduledTask -TaskName $env:CODING_TOOLS_HTTP_TASK_NAME;"
        "if([string]$task.State -eq 'Running'){Stop-ScheduledTask -TaskName $env:CODING_TOOLS_HTTP_TASK_NAME};"
        "Start-ScheduledTask -TaskName $env:CODING_TOOLS_HTTP_TASK_NAME",
    )
    if result.returncode != 0:
        raise RuntimeError("could not restart the HTTP MCP task")


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


def append_event(path: Path, **fields: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {"time": datetime.now(timezone.utc).isoformat(), **fields}
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-name", required=True)
    parser.add_argument("--health-url", required=True)
    parser.add_argument("--mcp-url")
    parser.add_argument("--expected-workspace")
    parser.add_argument("--required-tool", action="append", default=[])
    parser.add_argument("--event-log", required=True)
    parser.add_argument("--probe-timeout-seconds", type=float, default=3.0)
    parser.add_argument("--startup-grace-seconds", type=float, default=20.0)
    args = parser.parse_args(argv)

    if bool(args.mcp_url) != bool(args.expected_workspace):
        parser.error("--mcp-url and --expected-workspace must be provided together")

    def backend_probe() -> BackendProbe | None:
        if not args.mcp_url:
            return None
        return probe_mcp_backend(
            args.mcp_url,
            expected_workspace=args.expected_workspace,
            required_tools=tuple(args.required_tool),
            timeout=args.probe_timeout_seconds,
        )

    state = task_state(args.task_name)
    backend = backend_probe() if state == RUNNING else None
    healthy = backend.ok if backend is not None else (
        probe(args.health_url, args.probe_timeout_seconds) if state == RUNNING else False
    )
    action = decide_action(state, healthy)
    if action == "enable_start":
        enable_and_start_task(args.task_name)
    elif action == "start":
        start_task(args.task_name)
    elif action == "restart":
        restart_task(args.task_name)

    recovered = healthy
    if action in {"enable_start", "start", "restart"}:
        deadline = time.monotonic() + max(0.0, args.startup_grace_seconds)
        while time.monotonic() < deadline:
            backend = backend_probe()
            recovered_now = backend.ok if backend is not None else probe(
                args.health_url, args.probe_timeout_seconds
            )
            if recovered_now:
                recovered = True
                break
            time.sleep(1)

    append_event(
        Path(args.event_log),
        action=action,
        task_state=state,
        healthy=healthy,
        recovered=recovered,
        backend_category=backend.category if backend is not None else None,
        backend_phase=backend.phase if backend is not None else None,
        backend_tool_count=backend.tool_count if backend is not None else None,
        backend_state=backend.backend_state if backend is not None else None,
        backend_cause_type=backend.cause_type if backend is not None else None,
        backend_cause_message=backend.cause_message if backend is not None else None,
        backend_recovery_hint=backend.recovery_hint if backend is not None else None,
    )
    return 0 if action in {"healthy", "wait"} or recovered else 1


if __name__ == "__main__":
    raise SystemExit(main())
