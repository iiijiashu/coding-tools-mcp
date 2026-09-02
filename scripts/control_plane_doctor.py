from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

try:
    from coding_tools_mcp.control_plane_runtime_contract import (
        DEFAULT_RUNTIME_CONTRACT_PATH,
        RuntimeContract,
        RuntimeContractError,
        load_runtime_contract,
    )
    from .mcp_backend_probe import (
        BackendProbe,
        load_authorization_header,
        probe_mcp_backend,
        tool_catalog_sha256,
    )
    from .local_http_mcp_watchdog import DEFAULT_CONTROL_PLANE_LEASE, inspect_control_plane_lease
except ImportError:  # direct script execution
    sys.path.insert(0, str(ROOT))
    from coding_tools_mcp.control_plane_runtime_contract import (
        DEFAULT_RUNTIME_CONTRACT_PATH,
        RuntimeContract,
        RuntimeContractError,
        load_runtime_contract,
    )
    from mcp_backend_probe import (
        BackendProbe,
        load_authorization_header,
        probe_mcp_backend,
        tool_catalog_sha256,
    )
    from local_http_mcp_watchdog import DEFAULT_CONTROL_PLANE_LEASE, inspect_control_plane_lease


DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "local-mcp-http-task.json"


def _creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _powershell() -> str:
    return shutil.which("powershell.exe") or "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"


def scheduled_tasks(
    runtime_contract: RuntimeContract | None = None,
) -> dict[str, dict[str, Any]]:
    runtime_contract = runtime_contract or load_runtime_contract()
    names = tuple(task.name for task in runtime_contract.tasks)
    environment = os.environ.copy()
    environment["CODING_TOOLS_DOCTOR_TASK_NAMES"] = json.dumps(names)
    environment["CODING_TOOLS_DOCTOR_TASK_PATH"] = runtime_contract.task_path
    script = (
        "$ErrorActionPreference='Stop';"
        "$names=ConvertFrom-Json $env:CODING_TOOLS_DOCTOR_TASK_NAMES;"
        "$taskPath=$env:CODING_TOOLS_DOCTOR_TASK_PATH;"
        "$items=@();foreach($name in $names){"
        "$task=Get-ScheduledTask -TaskPath $taskPath -TaskName $name;"
        "$info=Get-ScheduledTaskInfo -TaskPath $taskPath -TaskName $name;"
        "$raw=Export-ScheduledTask -TaskPath $taskPath -TaskName $name;[xml]$xml=$raw;"
        "$ns=New-Object Xml.XmlNamespaceManager($xml.NameTable);"
        "$ns.AddNamespace('t','http://schemas.microsoft.com/windows/2004/02/mit/task');"
        "$actionsNode=$xml.SelectSingleNode('//t:Actions',$ns);"
        "$actionNodes=@($actionsNode.ChildNodes|Where-Object {$_.NodeType -eq [Xml.XmlNodeType]::Element});"
        "$items+=[pscustomobject]@{Name=$name;State=[string]$task.State;"
        "Enabled=[bool]$task.Settings.Enabled;"
        "RunLevel=[string]$task.Principal.RunLevel;"
        "LogonType=[string]$task.Principal.LogonType;UserId=[string]$task.Principal.UserId;"
        "TaskPath=[string]$task.TaskPath;MultipleInstances=[string]$task.Settings.MultipleInstances;"
        "ActionCount=[int]$actionNodes.Count;ActionType=if($actionNodes.Count -eq 1){[string]$actionNodes[0].LocalName}else{$null};"
        "Execute=[string]$task.Actions[0].Execute;WorkingDirectory=[string]$task.Actions[0].WorkingDirectory;"
        "RegistrationTrigger=[bool]($null-ne $xml.SelectSingleNode('//t:RegistrationTrigger',$ns));"
        "LogonTrigger=[bool]($null-ne $xml.SelectSingleNode('//t:LogonTrigger',$ns));"
        "SessionTriggerCount=[int]@($xml.SelectNodes('//t:SessionStateChangeTrigger',$ns)).Count;"
        "TriggerTypes=@($xml.SelectSingleNode('//t:Triggers',$ns).ChildNodes|Where-Object {$_.NodeType -eq [Xml.XmlNodeType]::Element}|ForEach-Object {$_.LocalName});"
        "TimeTrigger=[bool]($null-ne $xml.SelectSingleNode('//t:TimeTrigger',$ns));"
        "RestartOnFailure=[bool]($null-ne $xml.SelectSingleNode('//t:RestartOnFailure',$ns));"
        "Arguments=[string]$task.Actions[0].Arguments;"
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
            "enabled": item.get("Enabled"),
            "run_level": item.get("RunLevel"),
            "logon_type": item.get("LogonType"),
            "user_id": item.get("UserId"),
            "task_path": item.get("TaskPath"),
            "multiple_instances": item.get("MultipleInstances"),
            "action_count": item.get("ActionCount"),
            "action_type": item.get("ActionType"),
            "execute": item.get("Execute"),
            "execute_sha256": _file_sha256(item.get("Execute")),
            "working_directory": item.get("WorkingDirectory"),
            "registration_trigger": item.get("RegistrationTrigger"),
            "logon_trigger": item.get("LogonTrigger"),
            "session_trigger_count": item.get("SessionTriggerCount"),
            "trigger_types": (
                list(item.get("TriggerTypes"))
                if isinstance(item.get("TriggerTypes"), list)
                else ([item.get("TriggerTypes")] if item.get("TriggerTypes") else [])
            ),
            "time_trigger": item.get("TimeTrigger"),
            "restart_on_failure": item.get("RestartOnFailure"),
            "arguments": item.get("Arguments"),
            "arguments_sha256": hashlib.sha256(
                str(item.get("Arguments") or "").encode("utf-8")
            ).hexdigest(),
            "last_result": item.get("LastResult"),
            "last_run": item.get("LastRun"),
        }
        for item in items
        if isinstance(item, dict) and item.get("Name")
    }


def http_json(
    url: str,
    timeout: float = 3.0,
    authorization_header: str | None = None,
) -> dict[str, Any]:
    try:
        headers = {"Accept": "application/json"}
        if authorization_header:
            headers["Authorization"] = authorization_header
        request = urllib.request.Request(url, headers=headers)
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


def tunnel_health(runtime_contract: RuntimeContract | None = None) -> dict[str, Any]:
    runtime_contract = runtime_contract or load_runtime_contract()
    environment = os.environ.copy()
    environment["CODING_TOOLS_DOCTOR_TUNNEL_TASK"] = runtime_contract.task("tunnel").name
    environment["CODING_TOOLS_DOCTOR_TASK_PATH"] = runtime_contract.task_path
    task = subprocess.run(
        [
            _powershell(),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "(Get-ScheduledTask -TaskPath $env:CODING_TOOLS_DOCTOR_TASK_PATH "
            "-TaskName $env:CODING_TOOLS_DOCTOR_TUNNEL_TASK).Actions[0].Execute",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        env=environment,
        creationflags=_creation_flags(),
    )
    executable = Path(task.stdout.strip()) if task.returncode == 0 and task.stdout.strip() else None
    state_root = Path.home() / ".local" / "state" / "tunnel-client"
    if executable is None or not executable.is_file():
        return {"ok": False, "category": "tunnel_process", "cause_type": "ExecutableUnavailable"}
    if _file_sha256(executable) != runtime_contract.tunnel.executable_sha256:
        return {
            "ok": False,
            "category": "tunnel_process",
            "cause_type": "ExecutableFingerprintMismatch",
        }
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
    process_running = process.get("running") if isinstance(process, dict) else False
    healthz_ok = healthz.get("ok") if isinstance(healthz, dict) else False
    readyz_ok = readyz.get("ok") if isinstance(readyz, dict) else False
    poll_ok = poll.get("ok") if isinstance(poll, dict) else False
    return {
        "ok": (
            completed.returncode == 0
            and payload.get("result") == "ok"
            and process_running is True
            and healthz_ok is True
            and readyz_ok is True
            and poll_ok is True
        ),
        "pid": process.get("pid") if isinstance(process, dict) else None,
        "process_running": process_running,
        "healthz": healthz_ok,
        "readyz": readyz_ok,
        "control_plane_poll": poll_ok,
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
            if action in {
                "healthy",
                "healthy_stable",
                "started_healthy",
                "command_deadline_observed",
            }:
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
    contract_ok: bool,
    *,
    task_contract_ok: bool = True,
    control_plane_ok: bool = True,
    runtime_contract: RuntimeContract | None = None,
) -> str:
    runtime_contract = runtime_contract or load_runtime_contract()
    http_task = runtime_contract.task("http")
    http_watchdog = runtime_contract.task("http_watchdog")
    tunnel_task = runtime_contract.task("tunnel")
    tunnel_watchdog = runtime_contract.task("tunnel_watchdog")
    manager = runtime_contract.task("manager")
    if (
        backend_ok
        and ready_ok
        and tunnel_ok
        and tasks.get(http_task.name, {}).get("state") == "Running"
        and tasks.get(http_task.name, {}).get("run_level") == http_task.run_level
        and tasks.get(http_watchdog.name, {}).get("enabled") is True
        and tasks.get(tunnel_task.name, {}).get("state") == "Running"
        and tasks.get(tunnel_watchdog.name, {}).get("enabled") is True
        and tasks.get(manager.name, {}).get("enabled") is True
        and tasks.get(manager.name, {}).get("run_level") == manager.run_level
        and contract_ok
        and task_contract_ok
        and control_plane_ok
    ):
        return "READY"
    if backend_ok or ready_ok or tunnel_ok:
        return "DEGRADED"
    return "FAILED"


def _same_windows_path(actual: object, expected: Path) -> bool:
    try:
        return str(Path(str(actual)).resolve()).casefold() == str(expected.resolve()).casefold()
    except (OSError, ValueError):
        return False


def _file_sha256(value: object) -> str | None:
    try:
        path = Path(str(value))
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, ValueError):
        return None


def _windows_argv(arguments: str) -> list[str]:
    if not arguments.strip():
        return []
    if os.name != "nt":
        return [item.strip('"') for item in shlex.split(arguments, posix=False)]

    import ctypes

    argc = ctypes.c_int()
    command_line_to_argv = ctypes.windll.shell32.CommandLineToArgvW
    command_line_to_argv.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    command_line_to_argv.restype = ctypes.POINTER(ctypes.c_wchar_p)
    argv = command_line_to_argv(arguments, ctypes.byref(argc))
    if not argv:
        return []
    try:
        return [argv[index] for index in range(argc.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(argv)


def _option_values(arguments: str, option: str) -> list[str]:
    pattern = re.compile(
        rf"(?<!\S){re.escape(option)}(?:\s+|=)(?:\"([^\"]+)\"|(\S+))",
        re.IGNORECASE,
    )
    return [quoted or bare for quoted, bare in pattern.findall(arguments or "")]


def evaluate_task_contracts(
    tasks: dict[str, dict[str, Any]],
    expected_tool_catalog_sha256: str | None = None,
    *,
    runtime_contract: RuntimeContract | None = None,
) -> dict[str, Any]:
    runtime_contract = runtime_contract or load_runtime_contract()
    violations: list[str] = []
    tunnel_task = tasks.get(runtime_contract.task("tunnel").name, {})
    tunnel_executable = tunnel_task.get("execute") if isinstance(tunnel_task, dict) else None
    manager_state_root = (
        Path.home()
        / ".local"
        / "state"
        / "tunnel-client"
        / "control-plane"
        / "local-d-drive-coding-tools"
    )
    for spec in runtime_contract.tasks:
        name = spec.name
        prefix = spec.key
        task = tasks.get(name)
        if not isinstance(task, dict):
            violations.append(f"task_missing:{name}")
            continue
        if task.get("enabled") is not True:
            violations.append(f"{prefix}_disabled")
        if task.get("multiple_instances") != "IgnoreNew":
            violations.append(f"{prefix}_multiple_instances")
        if task.get("logon_type") != "Interactive":
            violations.append(f"{prefix}_logon_type")
        for trigger in spec.required_triggers:
            observed = (
                int(task.get("session_trigger_count") or 0) > 0
                if trigger == "session"
                else task.get(f"{trigger}_trigger") is True
            )
            if not observed:
                violations.append(f"{prefix}_{trigger}_trigger")
        if task.get("task_path") != runtime_contract.task_path:
            violations.append(f"{prefix}_task_path")
        if task.get("action_count") != 1 or task.get("action_type") != "Exec":
            violations.append(f"{prefix}_action_contract")
        if task.get("restart_on_failure") is not spec.restart_on_failure:
            violations.append(f"{prefix}_restart_on_failure")
        if task.get("time_trigger") is True and not spec.allow_time_trigger:
            violations.append(f"{prefix}_unexpected_time_trigger")
        allowed_trigger_types = {
            "RegistrationTrigger",
            "LogonTrigger",
            "SessionStateChangeTrigger",
        }
        if spec.allow_time_trigger:
            allowed_trigger_types.add("TimeTrigger")
        observed_trigger_types = {
            str(value) for value in task.get("trigger_types", []) if str(value)
        }
        if observed_trigger_types - allowed_trigger_types:
            violations.append(f"{prefix}_unexpected_trigger_type")

        state = task.get("state")
        state_ok = state == "Running" if spec.must_be_running else state in {"Ready", "Running"}
        if not state_ok or task.get("run_level") != spec.run_level:
            violations.append(f"{prefix}_runtime_owner")

        arguments = str(task.get("arguments", ""))
        if spec.script_relative is None:
            if not str(task.get("execute", "")).casefold().endswith("tunnel-client.exe"):
                violations.append(f"{prefix}_executable")
            if task.get("execute_sha256") != runtime_contract.tunnel.executable_sha256:
                violations.append(f"{prefix}_executable_fingerprint")
            if task.get("arguments_sha256") != runtime_contract.tunnel.main_arguments_sha256:
                violations.append(f"{prefix}_arguments_fingerprint")
        else:
            expected_pythonw = ROOT / Path(runtime_contract.pythonw_relative)
            expected_script = ROOT / Path(spec.script_relative)
            if not _same_windows_path(task.get("execute"), expected_pythonw):
                violations.append(f"{prefix}_executable")
            argv = _windows_argv(arguments)
            if not argv or not _same_windows_path(argv[0], expected_script):
                violations.append(f"{prefix}_script")
            if prefix == "tunnel_watchdog":
                tunnel_client_values = _option_values(arguments, "--tunnel-client")
                tunnel_client_sha256_values = _option_values(
                    arguments, "--tunnel-client-sha256"
                )
                main_task_values = _option_values(arguments, "--main-task-name")
                main_path_values = _option_values(arguments, "--main-task-path")
                if (
                    len(tunnel_client_values) != 1
                    or tunnel_executable is None
                    or not _same_windows_path(tunnel_client_values[0], Path(str(tunnel_executable)))
                ):
                    violations.append("tunnel_watchdog_tunnel_client")
                if tunnel_client_sha256_values != [
                    runtime_contract.tunnel.executable_sha256
                ]:
                    violations.append("tunnel_watchdog_tunnel_client_fingerprint")
                if main_task_values != [runtime_contract.task("tunnel").name]:
                    violations.append("tunnel_watchdog_main_task")
                if main_path_values != [runtime_contract.task_path]:
                    violations.append("tunnel_watchdog_main_task_path")
            elif prefix == "manager":
                manager_argv_ok = (
                    len(argv) == 6
                    and argv[1] == "execute"
                    and argv[2] == "--state-root"
                    and _same_windows_path(argv[3], manager_state_root)
                    and argv[4] == "--loop-interval-seconds"
                    and argv[5] == str(spec.loop_interval_seconds)
                )
                if not manager_argv_ok:
                    violations.append("manager_arguments")

        loop_values = _option_values(arguments, "--loop-interval-seconds")
        if spec.loop_interval_seconds > 0:
            if loop_values != [str(spec.loop_interval_seconds)]:
                violations.append(f"{prefix}_loop_interval")
        elif loop_values:
            violations.append(f"{prefix}_unexpected_loop_interval")

        catalog_values = _option_values(arguments, "--expected-tool-catalog-sha256")
        if spec.pin_tool_catalog:
            if expected_tool_catalog_sha256 is None:
                catalog_ok = (
                    len(catalog_values) == 1
                    and re.fullmatch(r"[0-9a-f]{64}", catalog_values[0]) is not None
                )
            else:
                catalog_ok = catalog_values == [expected_tool_catalog_sha256]
            if not catalog_ok:
                violations.append(f"{prefix}_tool_catalog_fingerprint")
        elif catalog_values:
            violations.append(f"{prefix}_unexpected_tool_catalog_fingerprint")

        main_arguments_values = _option_values(arguments, "--main-task-arguments-sha256")
        if spec.pin_main_arguments:
            if main_arguments_values != [runtime_contract.tunnel.main_arguments_sha256]:
                violations.append(f"{prefix}_main_task_fingerprint")
        elif main_arguments_values:
            violations.append(f"{prefix}_unexpected_main_task_fingerprint")

    return {
        "ok": not violations,
        "violations": violations,
        "live_tool_catalog_sha256": expected_tool_catalog_sha256,
    }


def _option_value(args: list[str], option: str) -> str | None:
    try:
        index = args.index(option)
    except ValueError:
        return None
    return args[index + 1] if index + 1 < len(args) else None


def evaluate_runtime_contract(
    config_path: Path,
    task_arguments: str,
    backend: BackendProbe,
) -> dict[str, Any]:
    violations: list[str] = []
    try:
        raw_bytes = config_path.read_bytes()
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"ok": False, "violations": ["config_unreadable"]}
    args = raw.get("server_args") if isinstance(raw, dict) else None
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        return {"ok": False, "violations": ["config_server_args"]}

    actual_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    match = re.search(
        r"(?:^|\s)--expect-config-sha256(?:=|\s+)[\"']?([A-Fa-f0-9]{64})",
        task_arguments or "",
    )
    expected_sha256 = match.group(1).lower() if match else None
    if expected_sha256 is None:
        violations.append("task_config_hash_pin_missing")
    elif expected_sha256 != actual_sha256:
        violations.append("task_config_hash_mismatch")
    if raw.get("require_windows_administrator") is not True:
        violations.append("config_administrator_requirement")
    if raw.get("require_config_sha256_pin") is not True:
        violations.append("config_hash_pin_requirement")
    if not isinstance(raw.get("auth_header_file"), str) or not raw["auth_header_file"].strip():
        violations.append("config_auth_header_file")
    if _option_value(args, "--permission-mode") != "dangerous":
        violations.append("config_permission_mode")
    for option, violation in (
        ("--dangerously-allow-any-local-path", "config_filesystem_scope"),
        ("--enable-computer-use", "config_computer_use"),
    ):
        if option not in args:
            violations.append(violation)
    if backend.ok:
        if backend.permission_mode != "dangerous":
            violations.append("runtime_permission_mode")
        if backend.filesystem_scope != "all_local_drives":
            violations.append("runtime_filesystem_scope")
        if backend.dangerously_skip_all_permissions is not True:
            violations.append("runtime_permission_gates")
        if backend.dangerously_allow_any_local_path is not True:
            violations.append("runtime_unrestricted_path")
        if backend.annotation_override is not None:
            violations.append("runtime_annotation_override")
        if backend.computer_use_enabled is not True:
            violations.append("runtime_computer_use")
    return {
        "ok": not violations,
        "actual_config_sha256": actual_sha256,
        "expected_config_sha256": expected_sha256,
        "violations": violations,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Emit one machine-readable Coding MCP control-plane snapshot.")
    parser.add_argument("--endpoint", default="http://127.0.0.1:18765/mcp")
    parser.add_argument("--workspace", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--runtime-contract-manifest",
        type=Path,
        default=DEFAULT_RUNTIME_CONTRACT_PATH,
    )
    parser.add_argument(
        "--catalog-snapshot-only",
        action="store_true",
        help="Emit only the authenticated live tool-catalog deployment snapshot.",
    )
    args = parser.parse_args(argv)

    try:
        runtime_contract = load_runtime_contract(args.runtime_contract_manifest)
    except RuntimeContractError as exc:
        print(
            json.dumps(
                {
                    "time": datetime.now(timezone.utc).isoformat(),
                    "status": "FAILED",
                    "runtime_manifest": {
                        "ok": False,
                        "path": str(args.runtime_contract_manifest),
                        "cause_type": type(exc).__name__,
                    },
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return 1

    try:
        task_config = json.loads(args.config.read_text(encoding="utf-8"))
        header_path = task_config.get("auth_header_file") if isinstance(task_config, dict) else None
        if not isinstance(header_path, str) or not header_path.strip():
            raise ValueError("auth_header_file is required for control-plane doctor")
        authorization_header = load_authorization_header(Path(header_path) if header_path else None)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        authorization_header = None
        backend = BackendProbe(
            False,
            "configuration",
            "authorization",
            False,
            args.endpoint,
            "local MCP authorization configuration is invalid",
            cause_type=type(exc).__name__,
        )
    else:
        backend = probe_mcp_backend(
            args.endpoint,
            expected_workspace=args.workspace,
            required_tools=runtime_contract.tool_catalog.required_tools,
            timeout=5,
            authorization_header=authorization_header,
        )
    if args.catalog_snapshot_only:
        catalog_sha256 = tool_catalog_sha256(backend.tools) if backend.ok else None
        result = {
            "time": datetime.now(timezone.utc).isoformat(),
            "status": "READY" if backend.ok and catalog_sha256 is not None else "FAILED",
            "endpoint": args.endpoint,
            "workspace": args.workspace,
            "tool_catalog": {
                "count": backend.tool_count,
                "sha256": catalog_sha256,
                "required": list(runtime_contract.tool_catalog.required_tools),
                "missing": sorted(
                    set(runtime_contract.tool_catalog.required_tools) - set(backend.tools)
                ),
            },
            "local_mcp": {**backend.__dict__},
            "runtime_manifest": {
                "ok": True,
                "path": str(args.runtime_contract_manifest),
                "schema_version": runtime_contract.schema_version,
            },
        }
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0 if result["status"] == "READY" else 1
    ready = http_json(
        args.endpoint.removesuffix("/mcp") + "/readyz",
        authorization_header=authorization_header,
    )
    tasks = scheduled_tasks(runtime_contract)
    contract = evaluate_runtime_contract(
        args.config,
        str(tasks.get(runtime_contract.task("http").name, {}).get("arguments") or ""),
        backend,
    )
    expected_catalog_sha256 = tool_catalog_sha256(backend.tools) if backend.ok else None
    task_contract = evaluate_task_contracts(
        tasks,
        expected_catalog_sha256,
        runtime_contract=runtime_contract,
    )
    control_plane_lease = inspect_control_plane_lease(DEFAULT_CONTROL_PLANE_LEASE)
    control_plane_ok = control_plane_lease.action == "inactive" or (
        control_plane_lease.action == "hold"
        and control_plane_lease.reason in {"transaction_starting", "manager_active"}
    )
    tunnel = tunnel_health(runtime_contract)
    state_root = Path.home() / ".local" / "state" / "tunnel-client" / "watchdogs"
    errors = recent_errors(
        (
            state_root / "local-d-drive-coding-tools-http" / "watchdog-events.jsonl",
            state_root / "local-d-drive-coding-tools" / "watchdog-events.jsonl",
        )
    )
    result = {
        "time": datetime.now(timezone.utc).isoformat(),
        "status": overall_status(
            backend.ok,
            ready.get("ok") is True,
            tunnel.get("ok") is True,
            tasks,
            contract.get("ok") is True,
            task_contract_ok=task_contract.get("ok") is True,
            control_plane_ok=control_plane_ok,
            runtime_contract=runtime_contract,
        ),
        "endpoint": args.endpoint,
        "workspace": args.workspace,
        "port": args.port,
        "port_owner": port_owner(args.port),
        "local_mcp": {**backend.__dict__},
        "health": ready,
        "tool_catalog": {
            "count": backend.tool_count,
            "sha256": expected_catalog_sha256,
            "required": list(runtime_contract.tool_catalog.required_tools),
            "missing": sorted(set(runtime_contract.tool_catalog.required_tools) - set(backend.tools)),
        },
        "runtime_manifest": {
            "ok": True,
            "path": str(args.runtime_contract_manifest),
            "schema_version": runtime_contract.schema_version,
            "tunnel_main_arguments_sha256": runtime_contract.tunnel.main_arguments_sha256,
        },
        "scheduled_tasks": tasks,
        "runtime_contract": contract,
        "task_contract": task_contract,
        "control_plane": {
            "ok": control_plane_ok,
            "lease_action": control_plane_lease.action,
            "lease_reason": control_plane_lease.reason,
            "operation_id": control_plane_lease.operation_id,
            "age_seconds": control_plane_lease.age_seconds,
        },
        "tunnel": tunnel,
        "recent_errors": errors,
    }
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result["status"] == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
