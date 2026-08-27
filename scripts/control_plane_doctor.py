from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .mcp_backend_probe import probe_mcp_backend
except ImportError:  # direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from mcp_backend_probe import probe_mcp_backend


HTTP_TASK = "Coding Tools MCP HTTP - Local D Drive"
HTTP_WATCHDOG_TASK = "Coding Tools MCP HTTP Watchdog - Local D Drive"
TUNNEL_TASK = "OpenAI Secure MCP Tunnel - Local D Drive Coding Tools"
TUNNEL_WATCHDOG_TASK = "OpenAI Secure MCP Tunnel Watchdog - Local D Drive Coding Tools"
REQUIRED_TOOLS = (
    "server_info",
    "computer_screenshot",
    "computer_mouse",
    "computer_keyboard",
    "computer_launch",
)


def _creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _powershell() -> str:
    return shutil.which("powershell.exe") or "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"


def scheduled_tasks() -> dict[str, dict[str, Any]]:
    names = (HTTP_TASK, HTTP_WATCHDOG_TASK, TUNNEL_TASK, TUNNEL_WATCHDOG_TASK)
    environment = os.environ.copy()
    environment["CODING_TOOLS_DOCTOR_TASK_NAMES"] = json.dumps(names)
    script = (
        "$ErrorActionPreference='Stop';"
        "$names=ConvertFrom-Json $env:CODING_TOOLS_DOCTOR_TASK_NAMES;"
        "$items=@();foreach($name in $names){"
        "$task=Get-ScheduledTask -TaskName $name;"
        "$info=Get-ScheduledTaskInfo -TaskName $name;"
        "$items+=[pscustomobject]@{Name=$name;State=[string]$task.State;"
        "LastResult=[int64]$info.LastTaskResult;LastRun=[string]$info.LastRunTime}};"
        "$items|ConvertTo-Json -Compress"
    )
    completed = subprocess.run(
        [_powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        env=environment,
        creationflags=_creation_flags(),
    )
    if completed.returncode != 0:
        return {"_error": {"category": "task_query", "cause_type": "PowerShellExit"}}
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"_error": {"category": "task_query", "cause_type": "InvalidJson"}}
    items = payload if isinstance(payload, list) else [payload]
    return {
        str(item.get("Name")): {
            "state": item.get("State"),
            "last_result": item.get("LastResult"),
            "last_run": item.get("LastRun"),
        }
        for item in items
        if isinstance(item, dict) and item.get("Name")
    }


def http_json(url: str, timeout: float = 3.0) -> dict[str, Any]:
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return {
            "ok": False,
            "category": "transport",
            "phase": "http_probe",
            "retryable": True,
            "cause_type": type(exc).__name__,
        }
    return payload if isinstance(payload, dict) else {"ok": False, "category": "protocol"}


def port_owner(port: int) -> dict[str, Any] | None:
    try:
        import psutil
    except ImportError:
        return {"category": "dependency", "cause_type": "PsutilUnavailable"}
    for connection in psutil.net_connections(kind="tcp"):
        if connection.status != psutil.CONN_LISTEN or not connection.laddr:
            continue
        if int(connection.laddr.port) != port:
            continue
        process = psutil.Process(connection.pid) if connection.pid else None
        try:
            return {
                "pid": connection.pid,
                "name": process.name() if process is not None else None,
                "executable": process.exe() if process is not None else None,
                "parent_pid": process.ppid() if process is not None else None,
                "created_at": (
                    datetime.fromtimestamp(process.create_time(), timezone.utc).isoformat()
                    if process is not None
                    else None
                ),
            }
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            return {"pid": connection.pid, "details": "process metadata unavailable"}
    return None


def tunnel_health() -> dict[str, Any]:
    task = subprocess.run(
        [
            _powershell(),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "(Get-ScheduledTask -TaskName 'OpenAI Secure MCP Tunnel - Local D Drive Coding Tools').Actions[0].Execute",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        creationflags=_creation_flags(),
    )
    executable = Path(task.stdout.strip()) if task.returncode == 0 and task.stdout.strip() else None
    state_root = Path.home() / ".local" / "state" / "tunnel-client"
    if executable is None or not executable.is_file():
        return {"ok": False, "category": "tunnel_process", "cause_type": "ExecutableUnavailable"}
    completed = subprocess.run(
        [
            str(executable),
            "health",
            "--url-file",
            str(state_root / "health" / "local-d-drive-coding-tools.url"),
            "--pid-file",
            str(state_root / "pids" / "local-d-drive-coding-tools.pid"),
            "--require-control-plane-poll",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        creationflags=_creation_flags(),
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "category": "tunnel_health", "cause_type": "InvalidJson"}
    process = payload.get("process") if isinstance(payload, dict) else None
    healthz = payload.get("healthz") if isinstance(payload, dict) else None
    readyz = payload.get("readyz") if isinstance(payload, dict) else None
    poll = payload.get("control_plane_poll") if isinstance(payload, dict) else None
    return {
        "ok": completed.returncode == 0 and payload.get("result") == "ok",
        "pid": process.get("pid") if isinstance(process, dict) else None,
        "process_running": process.get("running") if isinstance(process, dict) else False,
        "healthz": healthz.get("ok") if isinstance(healthz, dict) else False,
        "readyz": readyz.get("ok") if isinstance(readyz, dict) else False,
        "control_plane_poll": poll.get("ok") if isinstance(poll, dict) else False,
    }


def recent_errors(paths: tuple[Path, ...], limit: int = 20) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
        except OSError:
            continue
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            action = str(event.get("action", ""))
            if action in {"healthy", "healthy_stable", "started_healthy"}:
                continue
            errors.append(
                {
                    "source": path.name,
                    "time": event.get("time"),
                    "action": action,
                    "category": event.get("backend_category") or event.get("health_kind"),
                }
            )
    return errors[-limit:]


def overall_status(
    backend_ok: bool,
    ready_ok: bool,
    tunnel_ok: bool,
    tasks: dict[str, dict[str, Any]],
) -> str:
    if (
        backend_ok
        and ready_ok
        and tunnel_ok
        and tasks.get(HTTP_TASK, {}).get("state") == "Running"
        and tasks.get(TUNNEL_TASK, {}).get("state") == "Running"
    ):
        return "READY"
    if backend_ok or ready_ok or tunnel_ok:
        return "DEGRADED"
    return "FAILED"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Emit one machine-readable Coding MCP control-plane snapshot.")
    parser.add_argument("--endpoint", default="http://127.0.0.1:18765/mcp")
    parser.add_argument("--workspace", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--port", type=int, default=18765)
    args = parser.parse_args(argv)

    backend = probe_mcp_backend(
        args.endpoint,
        expected_workspace=args.workspace,
        required_tools=REQUIRED_TOOLS,
        timeout=5,
    )
    ready = http_json(args.endpoint.removesuffix("/mcp") + "/readyz")
    tasks = scheduled_tasks()
    tunnel = tunnel_health()
    state_root = Path.home() / ".local" / "state" / "tunnel-client" / "watchdogs"
    errors = recent_errors(
        (
            state_root / "local-d-drive-coding-tools-http" / "watchdog-events.jsonl",
            state_root / "local-d-drive-coding-tools" / "watchdog-events.jsonl",
        )
    )
    result = {
        "time": datetime.now(timezone.utc).isoformat(),
        "status": overall_status(backend.ok, ready.get("ok") is True, tunnel.get("ok") is True, tasks),
        "endpoint": args.endpoint,
        "workspace": args.workspace,
        "port": args.port,
        "port_owner": port_owner(args.port),
        "local_mcp": {**backend.__dict__},
        "health": ready,
        "tool_catalog": {
            "count": backend.tool_count,
            "required": list(REQUIRED_TOOLS),
            "missing": sorted(set(REQUIRED_TOOLS) - set(backend.tools)),
        },
        "scheduled_tasks": tasks,
        "tunnel": tunnel,
        "recent_errors": errors,
    }
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result["status"] == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
