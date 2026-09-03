from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from coding_tools_mcp.control_plane_runtime_contract import (  # noqa: E402
    CANONICAL_PYTHONW_RELATIVE,
    CANONICAL_TASK_INVARIANTS,
)

try:
    from .run_local_mcp_http_task import parse_config_snapshot
except ImportError:  # direct script execution
    from scripts.run_local_mcp_http_task import parse_config_snapshot


CONFIG = ROOT / "config" / "local-mcp-http-task.json"
DOCTOR = ROOT / "scripts" / "control_plane_doctor.py"
MAIN_SCRIPT = ROOT / Path(str(CANONICAL_TASK_INVARIANTS["http"]["script_relative"]))
MAIN_EXECUTABLE = ROOT / Path(CANONICAL_PYTHONW_RELATIVE)
MAIN_TASK = str(CANONICAL_TASK_INVARIANTS["http"]["name"])
MANAGER_TASK = str(CANONICAL_TASK_INVARIANTS["manager"]["name"])
DEFAULT_STATE_ROOT = (
    Path.home() / ".local" / "state" / "tunnel-client" / "control-plane" / "local-d-drive-coding-tools"
)
SHA256_RE = re.compile(r"^[A-Fa-f0-9]{64}$")
TASK_PIN_RE = re.compile(r"(?i)(--expect-config-sha256(?:=|\s+))([A-Fa-f0-9]{64})")
TERMINAL_STATES = {"succeeded", "failed", "rejected", "rolled_back"}
MANAGER_LOOP_INTERVAL_SECONDS = int(
    CANONICAL_TASK_INVARIANTS["manager"]["loop_interval_seconds"]
)


class LeaseBusy(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _validated_sha256(value: str, *, field: str = "SHA-256") -> str:
    if SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must contain 64 hexadecimal characters")
    return value.lower()


def build_request(
    operation: str,
    expected_sha256: str,
    *,
    candidate: Path | None = None,
    candidate_sha256: str | None = None,
) -> dict[str, object]:
    if operation not in {"restart", "update"}:
        raise ValueError("operation must be restart or update")
    request: dict[str, object] = {
        "schema_version": 1,
        "operation_id": uuid.uuid4().hex,
        "operation": operation,
        "expected_sha256": _validated_sha256(expected_sha256),
        "created_at": utc_now(),
    }
    if operation == "update":
        if candidate is None or candidate_sha256 is None:
            raise ValueError("update requires a candidate path and candidate SHA-256")
        request["candidate"] = str(candidate.resolve(strict=True))
        request["candidate_sha256"] = _validated_sha256(
            candidate_sha256,
            field="candidate SHA-256",
        )
    return request


def _json_bytes(payload: dict[str, object]) -> bytes:
    return (json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode("utf-8")


def write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(_json_bytes(payload))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_exclusive(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_json_bytes(payload))
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def install_request(request: dict[str, object], state_root: Path = DEFAULT_STATE_ROOT) -> None:
    operation_id = str(request.get("operation_id", ""))
    if re.fullmatch(r"[a-f0-9]{32}", operation_id) is None:
        raise ValueError("operation_id is invalid")
    request_path = state_root / "requests" / f"{operation_id}.json"
    active_path = state_root / "active.json"
    with process_lock(state_root):
        _write_exclusive(request_path, request)
        try:
            _write_exclusive(
                active_path,
                {
                    "schema_version": 1,
                    "operation_id": operation_id,
                    "request": str(request_path),
                    "created_at": request["created_at"],
                },
            )
        except FileExistsError as exc:
            request_path.unlink(missing_ok=True)
            raise LeaseBusy("another control-plane transaction owns the active lease") from exc


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path.name} must contain a JSON object")
    return payload


@contextlib.contextmanager
def process_lock(state_root: Path) -> Iterator[None]:
    state_root.mkdir(parents=True, exist_ok=True)
    lock_path = state_root / "manager.lock"
    stream = lock_path.open("a+b")
    stream.seek(0, os.SEEK_END)
    if stream.tell() == 0:
        stream.write(b"0")
        stream.flush()
    stream.seek(0)
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise LeaseBusy("the control-plane manager is already running") from exc
            acquired = True
        else:
            import fcntl

            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise LeaseBusy("the control-plane manager is already running") from exc
            acquired = True
        yield
    finally:
        try:
            if acquired:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


def _powershell() -> str:
    return shutil.which("powershell.exe") or "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"


def _creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _run_powershell(script: str, environment: dict[str, str], timeout: float = 30) -> str:
    env = os.environ.copy()
    env.update(environment)
    completed = subprocess.run(
        [_powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout,
        creationflags=_creation_flags(),
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic output"
        raise RuntimeError(
            f"PowerShell control-plane operation failed with exit {completed.returncode}: {detail[:1200]}"
        )
    return completed.stdout.strip()


def validate_state_root_security_payload(payload: dict[str, Any]) -> None:
    if payload.get("owner_sid") != "S-1-5-32-544":
        raise RuntimeError("control-plane state root owner must be BUILTIN\\Administrators")
    if payload.get("protected") is not True:
        raise RuntimeError("control-plane state root must disable inherited access rules")
    if payload.get("high_integrity") is not True:
        raise RuntimeError("control-plane state root must carry a High mandatory integrity label")
    if payload.get("has_reparse_point") is not False:
        raise RuntimeError("control-plane state root must not contain reparse points")
    rules = payload.get("rules")
    if not isinstance(rules, list):
        raise RuntimeError("control-plane state root access rules are invalid")
    privileged_writers: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict):
            raise RuntimeError("control-plane state root access rule is invalid")
        if rule.get("allow") is not True or rule.get("writable") is not True:
            continue
        sid = str(rule.get("sid", ""))
        if sid not in {"S-1-5-18", "S-1-5-32-544"}:
            raise RuntimeError(f"control-plane state root is writable by an unprivileged identity: {sid}")
        privileged_writers.add(sid)
    if privileged_writers != {"S-1-5-18", "S-1-5-32-544"}:
        raise RuntimeError("control-plane state root must grant SYSTEM and Administrators write access")


def validate_state_root_security(state_root: Path) -> dict[str, Any]:
    output = _run_powershell(
        "$ErrorActionPreference='Stop';$item=Get-Item -LiteralPath $env:CTM_STATE_ROOT -Force;"
        "$acl=[IO.Directory]::GetAccessControl($env:CTM_STATE_ROOT,[Security.AccessControl.AccessControlSections]::All);"
        "$owner=(New-Object Security.Principal.NTAccount($acl.Owner)).Translate([Security.Principal.SecurityIdentifier]).Value;"
        "$writeMask=[int64]([Security.AccessControl.FileSystemRights]::Write -bor "
        "[Security.AccessControl.FileSystemRights]::Delete -bor "
        "[Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor "
        "[Security.AccessControl.FileSystemRights]::ChangePermissions -bor "
        "[Security.AccessControl.FileSystemRights]::TakeOwnership);"
        "$rules=@();foreach($rule in $acl.Access){"
        "try{$sid=$rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value}catch{$sid=[string]$rule.IdentityReference};"
        "$mask=[int64]$rule.FileSystemRights;$rules+=[pscustomobject]@{sid=$sid;"
        "allow=([string]$rule.AccessControlType -eq 'Allow');writable=(($mask -band $writeMask)-ne 0)}};"
        "$reparse=[bool]($item.Attributes -band [IO.FileAttributes]::ReparsePoint);"
        "if(-not $reparse){$reparse=[bool](Get-ChildItem -LiteralPath $env:CTM_STATE_ROOT -Force -Recurse -ErrorAction Stop|"
        "Where-Object {$_.Attributes -band [IO.FileAttributes]::ReparsePoint}|Select-Object -First 1)};"
        "$integrity=(& \"$env:SystemRoot\\System32\\icacls.exe\" $env:CTM_STATE_ROOT|Out-String);"
        "[pscustomobject]@{owner_sid=$owner;protected=[bool]$acl.AreAccessRulesProtected;"
        "high_integrity=[bool]($integrity -match 'High Mandatory Level' -and $integrity -match '\\(OI\\)\\(CI\\)\\(NW\\)');"
        "has_reparse_point=$reparse;rules=$rules}|ConvertTo-Json -Depth 5 -Compress",
        {"CTM_STATE_ROOT": str(state_root.absolute())},
    )
    payload = json.loads(output)
    if not isinstance(payload, dict):
        raise RuntimeError("control-plane state root security query returned invalid data")
    validate_state_root_security_payload(payload)
    return payload


def task_snapshot() -> dict[str, Any]:
    output = _run_powershell(
        "$ErrorActionPreference='Stop';$task=Get-ScheduledTask -TaskPath '\\' -TaskName $env:CTM_TASK;"
        "$raw=Export-ScheduledTask -TaskPath '\\' -TaskName $env:CTM_TASK;[xml]$xml=$raw;"
        "$ns=New-Object Xml.XmlNamespaceManager($xml.NameTable);"
        "$ns.AddNamespace('t','http://schemas.microsoft.com/windows/2004/02/mit/task');"
        "$actionsNode=$xml.SelectSingleNode('//t:Actions',$ns);"
        "$actionNodes=@($actionsNode.ChildNodes|Where-Object {$_.NodeType -eq [Xml.XmlNodeType]::Element});"
        "$identity=[Security.Principal.WindowsIdentity]::GetCurrent();"
        "$user=[string]$task.Principal.UserId;$short=($identity.Name -split '\\\\')[-1];"
        "[pscustomobject]@{State=[string]$task.State;Enabled=[bool]$task.Settings.Enabled;"
        "RunLevel=[string]$task.Principal.RunLevel;LogonType=[string]$task.Principal.LogonType;"
        "UserId=[string]$task.Principal.UserId;MultipleInstances=[string]$task.Settings.MultipleInstances;"
        "TaskPath=[string]$task.TaskPath;IdentityMatch=[bool]($user -in @($identity.User.Value,$identity.Name,$short));"
        "ActionCount=[int]$actionNodes.Count;ActionType=if($actionNodes.Count -eq 1){[string]$actionNodes[0].LocalName}else{$null};"
        "Execute=[string]$task.Actions[0].Execute;"
        "Arguments=[string]$task.Actions[0].Arguments;"
        "WorkingDirectory=[string]$task.Actions[0].WorkingDirectory}|ConvertTo-Json -Compress",
        {"CTM_TASK": MAIN_TASK},
    )
    payload = json.loads(output)
    if not isinstance(payload, dict):
        raise RuntimeError("scheduled task query returned an invalid payload")
    return payload


def expected_main_task_arguments(expected_sha256: str) -> str:
    return (
        f'"{MAIN_SCRIPT}" --config "{CONFIG}" '
        f"--expect-config-sha256 {_validated_sha256(expected_sha256)}"
    )


def expected_manager_arguments(state_root: Path) -> str:
    return subprocess.list2cmdline(
        [
            str(Path(__file__).resolve()),
            "execute",
            "--state-root",
            str(state_root.resolve()),
            "--loop-interval-seconds",
            str(MANAGER_LOOP_INTERVAL_SECONDS),
        ]
    )


def _same_windows_path(actual: object, expected: Path) -> bool:
    try:
        return str(Path(str(actual)).resolve()).casefold() == str(expected.resolve()).casefold()
    except (OSError, ValueError):
        return False


def validate_main_task_contract(task: dict[str, Any], expected_sha256: str) -> None:
    if task.get("Enabled") is not True or task.get("RunLevel") != "Highest":
        raise RuntimeError("main task must be enabled at Highest run level")
    if task.get("LogonType") != "Interactive":
        raise RuntimeError("main task must use an Interactive token")
    if task.get("IdentityMatch") is not True:
        raise RuntimeError("main task principal does not match the current identity")
    if task.get("TaskPath") != "\\":
        raise RuntimeError("main task TaskPath must be the root task path")
    if task.get("MultipleInstances") != "IgnoreNew":
        raise RuntimeError("main task must use IgnoreNew single-instance policy")
    if task.get("ActionCount") != 1 or task.get("ActionType") != "Exec":
        raise RuntimeError("main task must contain exactly one Exec action")
    if not _same_windows_path(task.get("Execute"), MAIN_EXECUTABLE):
        raise RuntimeError("main task executable changed before the transaction")
    expected_arguments = expected_main_task_arguments(expected_sha256)
    if str(task.get("Arguments", "")) != expected_arguments:
        raise RuntimeError("main task arguments changed before the transaction")
    if str(task.get("WorkingDirectory") or "").strip():
        raise RuntimeError("main task working directory changed before the transaction")
    validate_generation(CONFIG, str(task.get("Arguments", "")), expected_sha256)


def validate_generation(config_path: Path, task_arguments: str, expected_sha256: str) -> str:
    expected = _validated_sha256(expected_sha256)
    actual = hashlib.sha256(config_path.read_bytes()).hexdigest()
    if actual != expected:
        raise RuntimeError("config generation changed before the transaction acquired the lease")
    matches = TASK_PIN_RE.findall(task_arguments)
    if len(matches) != 1 or matches[0][1].lower() != expected:
        raise RuntimeError("scheduled task pin does not match the expected config generation")
    return actual


def replace_expected_pin(arguments: str, old_sha256: str, new_sha256: str) -> str:
    old = _validated_sha256(old_sha256)
    new = _validated_sha256(new_sha256)
    matches = list(TASK_PIN_RE.finditer(arguments))
    if len(matches) != 1 or matches[0].group(2).lower() != old:
        raise RuntimeError("scheduled task pin changed during the transaction")
    match = matches[0]
    return arguments[: match.start(2)] + new + arguments[match.end(2) :]


def _set_task_arguments(
    arguments: str,
    *,
    expected_existing_arguments: tuple[str, ...],
) -> None:
    if not expected_existing_arguments:
        raise ValueError("at least one expected existing argument string is required")
    _run_powershell(
        "$ErrorActionPreference='Stop';$task=Get-ScheduledTask -TaskPath '\\' -TaskName $env:CTM_TASK;"
        "$raw=Export-ScheduledTask -TaskPath '\\' -TaskName $env:CTM_TASK;[xml]$xml=$raw;"
        "$ns=New-Object Xml.XmlNamespaceManager($xml.NameTable);"
        "$ns.AddNamespace('t','http://schemas.microsoft.com/windows/2004/02/mit/task');"
        "$actionsNode=$xml.SelectSingleNode('//t:Actions',$ns);"
        "$actionNodes=@($actionsNode.ChildNodes|Where-Object {$_.NodeType -eq [Xml.XmlNodeType]::Element});"
        "if($actionNodes.Count -ne 1 -or $actionNodes[0].LocalName -ne 'Exec'){throw 'main task must contain exactly one Exec action'};"
        "$identity=[Security.Principal.WindowsIdentity]::GetCurrent();$short=($identity.Name -split '\\\\')[-1];"
        "$user=[string]$task.Principal.UserId;if($user -notin @($identity.User.Value,$identity.Name,$short)){throw 'main task principal changed'};"
        "if([string]$task.Principal.LogonType -ne 'Interactive'){throw 'main task logon type changed'};"
        "if([string]$task.Principal.RunLevel -ne 'Highest'){throw 'main task run level changed'};"
        "if([string]$task.Settings.MultipleInstances -ne 'IgnoreNew'){throw 'main task instance policy changed'};"
        "$old=$task.Actions[0];if([IO.Path]::GetFullPath([string]$old.Execute) -ne [IO.Path]::GetFullPath($env:CTM_EXECUTE)){throw 'main task executable changed'};"
        "if(-not [string]::IsNullOrWhiteSpace([string]$old.WorkingDirectory)){throw 'main task working directory changed'};"
        "$decodedAllowed=ConvertFrom-Json $env:CTM_ALLOWED_ARGUMENTS;"
        "$allowed=@($decodedAllowed);"
        "if([string]$old.Arguments -cnotin $allowed){throw 'main task arguments changed'};"
        "$action=New-ScheduledTaskAction -Execute $env:CTM_EXECUTE -Argument $env:CTM_ARGUMENTS;"
        "Set-ScheduledTask -TaskPath '\\' -TaskName $env:CTM_TASK -Action $action | Out-Null;"
        "$updated=Get-ScheduledTask -TaskPath '\\' -TaskName $env:CTM_TASK;"
        "$updatedRaw=Export-ScheduledTask -TaskPath '\\' -TaskName $env:CTM_TASK;[xml]$updatedXml=$updatedRaw;"
        "$updatedNs=New-Object Xml.XmlNamespaceManager($updatedXml.NameTable);"
        "$updatedNs.AddNamespace('t','http://schemas.microsoft.com/windows/2004/02/mit/task');"
        "$updatedActions=$updatedXml.SelectSingleNode('//t:Actions',$updatedNs);"
        "$updatedActionNodes=@($updatedActions.ChildNodes|Where-Object {$_.NodeType -eq [Xml.XmlNodeType]::Element});"
        "if($updatedActionNodes.Count -ne 1 -or $updatedActionNodes[0].LocalName -ne 'Exec'){throw 'main task action write added an unexpected action'};"
        "$actual=$updated.Actions[0];"
        "if([IO.Path]::GetFullPath([string]$actual.Execute) -ne [IO.Path]::GetFullPath($env:CTM_EXECUTE) -or "
        "[string]$actual.Arguments -cne $env:CTM_ARGUMENTS -or -not [string]::IsNullOrWhiteSpace([string]$actual.WorkingDirectory)){throw 'main task action write verification failed'}",
        {
            "CTM_TASK": MAIN_TASK,
            "CTM_EXECUTE": str(MAIN_EXECUTABLE),
            "CTM_ARGUMENTS": arguments,
            "CTM_ALLOWED_ARGUMENTS": json.dumps(list(expected_existing_arguments)),
        },
    )


def _restart_main_task() -> None:
    digest = hashlib.sha256(CONFIG.read_bytes()).hexdigest()
    _run_powershell(
        "$ErrorActionPreference='Stop';"
        "$task=Get-ScheduledTask -TaskPath '\\' -TaskName $env:CTM_TASK;"
        "if([string]$task.State -eq 'Disabled'){throw 'main task is disabled'};"
        "Stop-ScheduledTask -TaskPath '\\' -TaskName $env:CTM_TASK -ErrorAction SilentlyContinue;"
        "$deadline=(Get-Date).AddSeconds(20);do{$state=[string](Get-ScheduledTask -TaskPath '\\' -TaskName $env:CTM_TASK).State;"
        "if($state -ne 'Running'){break};Start-Sleep -Milliseconds 250}while((Get-Date)-lt $deadline);"
        "if($state -eq 'Running'){throw 'main task did not stop'};"
        "$raw=Export-ScheduledTask -TaskPath '\\' -TaskName $env:CTM_TASK;"
        "[xml]$xml=$raw;$ns=New-Object Xml.XmlNamespaceManager($xml.NameTable);"
        "$ns.AddNamespace('t','http://schemas.microsoft.com/windows/2004/02/mit/task');"
        "if(-not $xml.SelectSingleNode('//t:RegistrationTrigger',$ns)){throw 'main task has no registration trigger'};"
        "$identity=[Security.Principal.WindowsIdentity]::GetCurrent();"
        "$user=[string]$xml.SelectSingleNode('//t:Principal/t:UserId',$ns).InnerText;"
        "$short=($identity.Name -split '\\\\')[-1];"
        "if($user -notin @($identity.User.Value,$identity.Name,$short)){throw 'main task user changed'};"
        "$logon=[string]$xml.SelectSingleNode('//t:Principal/t:LogonType',$ns).InnerText;"
        "if($logon -ne 'InteractiveToken'){throw 'main task logon type changed'};"
        "$level=[string]$xml.SelectSingleNode('//t:Principal/t:RunLevel',$ns).InnerText;"
        "if($level -ne 'HighestAvailable'){throw 'main task run level changed'};"
        "$multiple=[string]$xml.SelectSingleNode('//t:Settings/t:MultipleInstancesPolicy',$ns).InnerText;"
        "if($multiple -ne 'IgnoreNew'){throw 'main task instance policy changed'};"
        "$actionsNode=$xml.SelectSingleNode('//t:Actions',$ns);"
        "$actionNodes=@($actionsNode.ChildNodes|Where-Object {$_.NodeType -eq [Xml.XmlNodeType]::Element});"
        "if($actionNodes.Count -ne 1 -or $actionNodes[0].LocalName -ne 'Exec'){throw 'main task must contain exactly one Exec action'};"
        "$execute=[string]$xml.SelectSingleNode('//t:Actions/t:Exec/t:Command',$ns).InnerText;"
        "if([IO.Path]::GetFullPath($execute) -ne [IO.Path]::GetFullPath($env:CTM_EXECUTE)){throw 'main task executable changed'};"
        "$arguments=[string]$xml.SelectSingleNode('//t:Actions/t:Exec/t:Arguments',$ns).InnerText;"
        "if($arguments -cne $env:CTM_ARGUMENTS){throw 'main task arguments changed'};"
        "Register-ScheduledTask -TaskPath '\\' -TaskName $env:CTM_TASK -Xml $raw -Force|Out-Null",
        {
            "CTM_TASK": MAIN_TASK,
            "CTM_EXECUTE": str(MAIN_EXECUTABLE),
            "CTM_ARGUMENTS": expected_main_task_arguments(digest),
        },
        timeout=30,
    )


def _doctor_snapshot(timeout: float = 75) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {"status": "UNAVAILABLE"}
    while time.monotonic() < deadline:
        completed = subprocess.run(
            [sys.executable, str(DOCTOR)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
            creationflags=_creation_flags(),
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            payload = {"status": "UNAVAILABLE"}
        if isinstance(payload, dict):
            last = payload
        if completed.returncode == 0 and last.get("status") == "READY":
            return last
        time.sleep(2)
    raise RuntimeError(f"control-plane doctor did not become READY (last status: {last.get('status')})")


def _receipt_summary(snapshot: dict[str, Any]) -> dict[str, object]:
    contract = snapshot.get("runtime_contract")
    local_mcp = snapshot.get("local_mcp")
    tunnel = snapshot.get("tunnel")
    owner = snapshot.get("port_owner")
    return {
        "doctor_status": snapshot.get("status"),
        "config_sha256": contract.get("actual_config_sha256") if isinstance(contract, dict) else None,
        "permission_mode": local_mcp.get("permission_mode") if isinstance(local_mcp, dict) else None,
        "backend_pid": owner.get("pid") if isinstance(owner, dict) else None,
        "tunnel_ok": tunnel.get("ok") if isinstance(tunnel, dict) else None,
    }


def _listener_pid(port: int = 18765) -> int | None:
    try:
        import psutil
    except ImportError:
        return None
    for connection in psutil.net_connections(kind="tcp"):
        if (
            connection.status == psutil.CONN_LISTEN
            and connection.laddr
            and int(connection.laddr.port) == port
        ):
            return int(connection.pid) if connection.pid else None
    return None


def _validate_candidate(path: Path, expected_sha256: str) -> bytes:
    resolved = path.resolve(strict=True)
    if str(resolved).casefold() == str(CONFIG.resolve(strict=True)).casefold():
        raise RuntimeError("the update candidate must not be the production config path")
    raw_bytes = resolved.read_bytes()
    actual = hashlib.sha256(raw_bytes).hexdigest()
    if actual != _validated_sha256(expected_sha256, field="candidate SHA-256"):
        raise RuntimeError("candidate config hash changed after submission")
    payload = parse_config_snapshot(raw_bytes)
    args = payload.get("server_args")
    if not isinstance(args, list):
        raise RuntimeError("candidate server_args are invalid")
    required_options = {"--dangerously-allow-any-local-path", "--enable-computer-use"}
    if payload.get("require_config_sha256_pin") is not True:
        raise RuntimeError("candidate must require a config SHA-256 pin")
    if payload.get("require_windows_administrator") is not True:
        raise RuntimeError("candidate must require a Windows administrator token")
    if "--permission-mode" not in args or args[args.index("--permission-mode") + 1] != "dangerous":
        raise RuntimeError("candidate must preserve dangerous permission mode")
    if not required_options.issubset(args):
        raise RuntimeError("candidate must preserve the privileged runtime contract")
    return raw_bytes


def _replace_file_atomic(path: Path, raw_bytes: bytes) -> None:
    descriptor, raw_temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_receipt(state_root: Path, operation_id: str, **fields: object) -> None:
    write_json_atomic(
        state_root / "receipts" / f"{operation_id}.json",
        {"schema_version": 1, "operation_id": operation_id, "updated_at": utc_now(), **fields},
    )


def _clear_active(state_root: Path, operation_id: str) -> None:
    active_path = state_root / "active.json"
    try:
        active = _load_json(active_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return
    if active.get("operation_id") == operation_id:
        active_path.unlink(missing_ok=True)


def _recover_incomplete_update(state_root: Path, operation_id: str, journal: dict[str, Any]) -> None:
    _write_receipt(
        state_root,
        operation_id,
        state="running",
        operation="update",
        recovered=True,
        phase="rollback",
    )
    backup_path = Path(str(journal["backup_path"]))
    old_arguments = str(journal["old_arguments"])
    _replace_file_atomic(CONFIG, backup_path.read_bytes())
    allowed_arguments = [old_arguments]
    with contextlib.suppress(ValueError, RuntimeError):
        allowed_arguments.append(
            replace_expected_pin(
                old_arguments,
                str(journal["old_sha256"]),
                str(journal["new_sha256"]),
            )
        )
    _set_task_arguments(
        old_arguments,
        expected_existing_arguments=tuple(dict.fromkeys(allowed_arguments)),
    )
    _restart_main_task()
    snapshot = _doctor_snapshot()
    _write_receipt(
        state_root,
        operation_id,
        state="rolled_back",
        reason="recovered_incomplete_update",
        **_receipt_summary(snapshot),
    )


def _recover_journal(state_root: Path, operation_id: str, journal_path: Path) -> int:
    journal = _load_json(journal_path)
    operation = journal.get("operation")
    phase = journal.get("phase")
    if operation == "update" and phase == "verified":
        task = task_snapshot()
        validate_generation(CONFIG, str(task.get("Arguments", "")), str(journal.get("new_sha256", "")))
        snapshot = _doctor_snapshot()
        _write_receipt(
            state_root,
            operation_id,
            state="succeeded",
            operation="update",
            recovered=True,
            **_receipt_summary(snapshot),
        )
        journal_path.unlink(missing_ok=True)
        return 0
    if operation == "update":
        _recover_incomplete_update(state_root, operation_id, journal)
        journal_path.unlink(missing_ok=True)
        return 1
    if operation == "restart":
        previous_failure: dict[str, object] | None = None
        receipt_path = state_root / "receipts" / f"{operation_id}.json"
        if receipt_path.exists():
            previous_receipt = _load_json(receipt_path)
            if (
                previous_receipt.get("schema_version") != 1
                or previous_receipt.get("operation_id") != operation_id
            ):
                raise RuntimeError("restart recovery receipt identity is invalid")
            preserved_failure = previous_receipt.get("recovery_previous_failure")
            if isinstance(preserved_failure, dict):
                previous_failure = {
                    key: preserved_failure[key]
                    for key in ("state", "error_type", "error")
                    if key in preserved_failure
                }
            elif previous_receipt.get("state") == "failed":
                previous_failure = {
                    key: previous_receipt[key]
                    for key in ("state", "error_type", "error")
                    if key in previous_receipt
                }
        recovery_fields: dict[str, object] = {
            "state": "running",
            "operation": "restart",
            "recovered": True,
            "phase": "verification",
        }
        if previous_failure:
            recovery_fields["recovery_previous_failure"] = previous_failure
        _write_receipt(
            state_root,
            operation_id,
            **recovery_fields,
        )
        old_pid = journal.get("old_backend_pid")
        current_pid = _listener_pid()
        if (
            not isinstance(old_pid, int)
            or not isinstance(current_pid, int)
            or current_pid == old_pid
        ):
            _restart_main_task()
        snapshot = _doctor_snapshot()
        new_pid = _receipt_summary(snapshot).get("backend_pid")
        if isinstance(old_pid, int) and new_pid == old_pid:
            raise RuntimeError("recovered restart did not replace the backend process")
        recovery_fields.update(
            state="succeeded",
            **_receipt_summary(snapshot),
        )
        recovery_fields.pop("phase", None)
        _write_receipt(state_root, operation_id, **recovery_fields)
        journal_path.unlink(missing_ok=True)
        return 0
    raise RuntimeError("transaction journal operation is invalid")


def execute_active(state_root: Path = DEFAULT_STATE_ROOT) -> int:
    validate_state_root_security(state_root)
    with process_lock(state_root):
        active_path = state_root / "active.json"
        if not active_path.exists():
            return 0
        active = _load_json(active_path)
        operation_id = str(active.get("operation_id", ""))
        if re.fullmatch(r"[a-f0-9]{32}", operation_id) is None:
            raise RuntimeError("active lease contains an invalid operation_id")
        journal_path = state_root / "journals" / f"{operation_id}.json"
        receipt_path = state_root / "receipts" / f"{operation_id}.json"
        if receipt_path.exists() and not journal_path.exists():
            receipt = _load_json(receipt_path)
            if receipt.get("operation_id") != operation_id or receipt.get("schema_version") != 1:
                raise RuntimeError("terminal receipt identity is invalid")
            receipt_state = str(receipt.get("state", ""))
            if receipt_state in TERMINAL_STATES:
                _clear_active(state_root, operation_id)
                return 0 if receipt_state == "succeeded" else 1
        request = _load_json(state_root / "requests" / f"{operation_id}.json")
        if request.get("operation_id") != operation_id or request.get("schema_version") != 1:
            raise RuntimeError("active request identity is invalid")
        release_lease = True
        try:
            if journal_path.exists():
                return _recover_journal(state_root, operation_id, journal_path)

            task = task_snapshot()
            expected = str(request.get("expected_sha256", ""))
            validate_main_task_contract(task, expected)
            operation = request.get("operation")
            _write_receipt(state_root, operation_id, state="running", operation=operation)

            if operation == "restart":
                old_pid = _listener_pid()
                write_json_atomic(
                    journal_path,
                    {
                        "schema_version": 1,
                        "operation_id": operation_id,
                        "operation": "restart",
                        "phase": "prepared",
                        "old_backend_pid": old_pid,
                    },
                )
                _restart_main_task()
                snapshot = _doctor_snapshot()
                new_pid = _receipt_summary(snapshot).get("backend_pid")
                if isinstance(old_pid, int) and new_pid == old_pid:
                    raise RuntimeError("restart did not replace the backend process")
                restart_journal = _load_json(journal_path)
                restart_journal["phase"] = "verified"
                write_json_atomic(journal_path, restart_journal)
            elif operation == "update":
                candidate = Path(str(request.get("candidate", "")))
                new_digest = str(request.get("candidate_sha256", ""))
                candidate_bytes = _validate_candidate(candidate, new_digest)
                backup_path = state_root / "backups" / f"{operation_id}-config.json"
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                _replace_file_atomic(backup_path, CONFIG.read_bytes())
                old_arguments = str(task.get("Arguments", ""))
                new_arguments = replace_expected_pin(old_arguments, expected, new_digest)
                write_json_atomic(
                    journal_path,
                    {
                        "schema_version": 1,
                        "operation_id": operation_id,
                        "operation": "update",
                        "phase": "prepared",
                        "backup_path": str(backup_path),
                        "old_arguments": old_arguments,
                        "old_sha256": expected,
                        "new_sha256": new_digest,
                    },
                )
                try:
                    _replace_file_atomic(CONFIG, candidate_bytes)
                    journal = _load_json(journal_path)
                    journal["phase"] = "config_replaced"
                    write_json_atomic(journal_path, journal)
                    _set_task_arguments(
                        new_arguments,
                        expected_existing_arguments=(old_arguments,),
                    )
                    journal["phase"] = "task_pin_updated"
                    write_json_atomic(journal_path, journal)
                    _restart_main_task()
                    snapshot = _doctor_snapshot()
                    journal = _load_json(journal_path)
                    journal["phase"] = "verified"
                    write_json_atomic(journal_path, journal)
                except BaseException:
                    _recover_incomplete_update(state_root, operation_id, _load_json(journal_path))
                    journal_path.unlink(missing_ok=True)
                    raise
            else:
                raise RuntimeError("request operation is invalid")

            _write_receipt(
                state_root,
                operation_id,
                state="succeeded",
                operation=operation,
                **_receipt_summary(snapshot),
            )
            journal_path.unlink(missing_ok=True)
            return 0
        except BaseException as exc:
            if journal_path.exists():
                release_lease = False
            receipt_path = state_root / "receipts" / f"{operation_id}.json"
            previous: dict[str, Any] = {}
            if receipt_path.exists():
                with contextlib.suppress(OSError, ValueError, json.JSONDecodeError):
                    previous = _load_json(receipt_path)
            if previous.get("state") != "rolled_back":
                failure_fields: dict[str, object] = {
                    "state": "failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
                if isinstance(previous.get("recovery_previous_failure"), dict):
                    failure_fields["recovery_previous_failure"] = previous[
                        "recovery_previous_failure"
                    ]
                _write_receipt(
                    state_root,
                    operation_id,
                    **failure_fields,
                )
            return 1
        finally:
            if release_lease:
                _clear_active(state_root, operation_id)


def run_manager_loop(
    state_root: Path,
    *,
    interval_seconds: float = MANAGER_LOOP_INTERVAL_SECONDS,
    max_iterations: int | None = None,
) -> int:
    completed = 0
    result = 0
    while True:
        try:
            result = execute_active(state_root)
        except Exception as exc:  # noqa: BLE001 - the supervisor must survive one bad iteration
            result = 1
            try:
                append_manager_event(
                    state_root / "manager-events.jsonl",
                    action="manager_iteration_error",
                    error_type=type(exc).__name__,
                    error=str(exc)[:1000],
                )
            except OSError:
                pass
        completed += 1
        if max_iterations is not None and completed >= max_iterations:
            return result
        time.sleep(interval_seconds)


def append_manager_event(
    path: Path,
    *,
    max_bytes: int = 5 * 1024 * 1024,
    backups: int = 3,
    **fields: object,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        rotate = path.stat().st_size >= max_bytes
    except FileNotFoundError:
        rotate = False
    if rotate:
        for index in range(backups, 0, -1):
            source = path if index == 1 else path.with_name(f"{path.name}.{index - 1}")
            destination = path.with_name(f"{path.name}.{index}")
            if not source.exists():
                continue
            destination.unlink(missing_ok=True)
            os.replace(source, destination)
    payload = {"time": utc_now(), **fields}
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def register_manager(state_root: Path = DEFAULT_STATE_ROOT, pythonw: Path | None = None) -> dict[str, object]:
    if pythonw is not None:
        executable = pythonw.resolve(strict=True)
    else:
        candidates = [
            ROOT / ".venv" / "Scripts" / "pythonw.exe",
            Path(sys.executable).with_name("pythonw.exe"),
        ]
        executable = next((candidate.resolve() for candidate in candidates if candidate.is_file()), None)
        if executable is None:
            raise FileNotFoundError(
                "No usable pythonw.exe was found for the control-plane manager."
            )
    arguments = expected_manager_arguments(state_root)
    output = _run_powershell(
        "$ErrorActionPreference='Stop';$identity=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name;"
        "$action=New-ScheduledTaskAction -Execute $env:CTM_EXECUTE -Argument $env:CTM_ARGUMENTS;"
        "$principal=New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Highest;"
        "$settings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) "
        "-RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 1) "
        "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries;"
        "$logon=New-ScheduledTaskTrigger -AtLogOn -User $identity;"
        "Register-ScheduledTask -TaskName $env:CTM_MANAGER -Action $action -Principal $principal "
        "-Settings $settings -Trigger $logon -Force | Out-Null;"
        "$raw=Export-ScheduledTask -TaskPath '\\' -TaskName $env:CTM_MANAGER;[xml]$xml=$raw;"
        "$ns=New-Object Xml.XmlNamespaceManager($xml.NameTable);"
        "$ns.AddNamespace('t','http://schemas.microsoft.com/windows/2004/02/mit/task');"
        "$triggers=$xml.SelectSingleNode('//t:Triggers',$ns);"
        "$registration=$xml.CreateElement('RegistrationTrigger','http://schemas.microsoft.com/windows/2004/02/mit/task');"
        "$delay=$xml.CreateElement('Delay','http://schemas.microsoft.com/windows/2004/02/mit/task');$delay.InnerText='PT1S';"
        "$null=$registration.AppendChild($delay);$null=$triggers.AppendChild($registration);"
        "Register-ScheduledTask -TaskPath '\\' -TaskName $env:CTM_MANAGER -Xml $xml.OuterXml -Force|Out-Null;"
        "$task=Get-ScheduledTask -TaskPath '\\' -TaskName $env:CTM_MANAGER;"
        "$verifiedRaw=Export-ScheduledTask -TaskPath '\\' -TaskName $env:CTM_MANAGER;[xml]$verified=$verifiedRaw;"
        "$verifiedNs=New-Object Xml.XmlNamespaceManager($verified.NameTable);"
        "$verifiedNs.AddNamespace('t','http://schemas.microsoft.com/windows/2004/02/mit/task');"
        "$actionsNode=$verified.SelectSingleNode('//t:Actions',$verifiedNs);"
        "$actionNodes=@($actionsNode.ChildNodes|Where-Object {$_.NodeType -eq [Xml.XmlNodeType]::Element});"
        "[pscustomobject]@{Name=$task.TaskName;State=[string]$task.State;Enabled=[bool]$task.Settings.Enabled;"
        "RunLevel=[string]$task.Principal.RunLevel;LogonType=[string]$task.Principal.LogonType;"
        "MultipleInstances=[string]$task.Settings.MultipleInstances;UserId=[string]$task.Principal.UserId;"
        "ActionCount=[int]$actionNodes.Count;ActionType=if($actionNodes.Count -eq 1){[string]$actionNodes[0].LocalName}else{$null};"
        "RegistrationTrigger=[bool]($null-ne $verified.SelectSingleNode('//t:RegistrationTrigger',$verifiedNs));"
        "LogonTrigger=[bool]($null-ne $verified.SelectSingleNode('//t:LogonTrigger',$verifiedNs));"
        "TimeTrigger=[bool]($null-ne $verified.SelectSingleNode('//t:TimeTrigger',$verifiedNs));"
        "RestartOnFailure=[bool]($null-ne $verified.SelectSingleNode('//t:RestartOnFailure',$verifiedNs))}|ConvertTo-Json -Compress",
        {
            "CTM_EXECUTE": str(executable),
            "CTM_ARGUMENTS": arguments,
            "CTM_MANAGER": MANAGER_TASK,
        },
    )
    payload = json.loads(output)
    if (
        not isinstance(payload, dict)
        or payload.get("RunLevel") != "Highest"
        or payload.get("LogonType") != "Interactive"
        or payload.get("MultipleInstances") != "IgnoreNew"
        or payload.get("Enabled") is not True
        or payload.get("ActionCount") != 1
        or payload.get("ActionType") != "Exec"
        or payload.get("RegistrationTrigger") is not True
        or payload.get("LogonTrigger") is not True
        or payload.get("TimeTrigger") is not False
        or payload.get("RestartOnFailure") is not True
    ):
        raise RuntimeError("control-plane manager registration did not satisfy the Highest contract")
    return payload


def start_manager(state_root: Path = DEFAULT_STATE_ROOT) -> None:
    executable = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    arguments = expected_manager_arguments(state_root)
    _run_powershell(
        "$ErrorActionPreference='Stop';"
        "$raw=Export-ScheduledTask -TaskPath '\\' -TaskName $env:CTM_MANAGER;[xml]$xml=$raw;"
        "$ns=New-Object Xml.XmlNamespaceManager($xml.NameTable);"
        "$ns.AddNamespace('t','http://schemas.microsoft.com/windows/2004/02/mit/task');"
        "if(-not $xml.SelectSingleNode('//t:RegistrationTrigger',$ns)){throw 'manager has no registration trigger'};"
        "if(-not $xml.SelectSingleNode('//t:LogonTrigger',$ns)){throw 'manager has no logon trigger'};"
        "if($xml.SelectSingleNode('//t:TimeTrigger',$ns)){throw 'manager must not use a repeating time trigger'};"
        "if(-not $xml.SelectSingleNode('//t:RestartOnFailure',$ns)){throw 'manager has no restart-on-failure policy'};"
        "$task=Get-ScheduledTask -TaskPath '\\' -TaskName $env:CTM_MANAGER;"
        "if(-not [bool]$task.Settings.Enabled){throw 'manager is disabled'};"
        "$identity=[Security.Principal.WindowsIdentity]::GetCurrent();$short=($identity.Name -split '\\\\')[-1];"
        "$user=[string]$xml.SelectSingleNode('//t:Principal/t:UserId',$ns).InnerText;"
        "if($user -notin @($identity.User.Value,$identity.Name,$short)){throw 'manager principal changed'};"
        "$logon=[string]$xml.SelectSingleNode('//t:Principal/t:LogonType',$ns).InnerText;"
        "if($logon -ne 'InteractiveToken'){throw 'manager logon type changed'};"
        "$level=[string]$xml.SelectSingleNode('//t:Principal/t:RunLevel',$ns).InnerText;"
        "if($level -ne 'HighestAvailable'){throw 'manager run level changed'};"
        "$multiple=[string]$xml.SelectSingleNode('//t:Settings/t:MultipleInstancesPolicy',$ns).InnerText;"
        "if($multiple -ne 'IgnoreNew'){throw 'manager instance policy changed'};"
        "$actionsNode=$xml.SelectSingleNode('//t:Actions',$ns);"
        "$actionNodes=@($actionsNode.ChildNodes|Where-Object {$_.NodeType -eq [Xml.XmlNodeType]::Element});"
        "if($actionNodes.Count -ne 1 -or $actionNodes[0].LocalName -ne 'Exec'){throw 'manager must contain exactly one Exec action'};"
        "$execute=[string]$xml.SelectSingleNode('//t:Actions/t:Exec/t:Command',$ns).InnerText;"
        "if([IO.Path]::GetFullPath($execute) -ne [IO.Path]::GetFullPath($env:CTM_EXECUTE)){throw 'manager executable changed'};"
        "$actual=[string]$xml.SelectSingleNode('//t:Actions/t:Exec/t:Arguments',$ns).InnerText;"
        "if($actual -cne $env:CTM_ARGUMENTS){throw 'manager arguments changed'};"
        "$working=[string]$xml.SelectSingleNode('//t:Actions/t:Exec/t:WorkingDirectory',$ns).InnerText;"
        "if(-not [string]::IsNullOrWhiteSpace($working)){throw 'manager working directory changed'};"
        "Register-ScheduledTask -TaskPath '\\' -TaskName $env:CTM_MANAGER -Xml $raw -Force|Out-Null",
        {
            "CTM_MANAGER": MANAGER_TASK,
            "CTM_EXECUTE": str(executable),
            "CTM_ARGUMENTS": arguments,
        },
    )


def wait_for_receipt(state_root: Path, operation_id: str, timeout: float) -> dict[str, Any]:
    receipt_path = state_root / "receipts" / f"{operation_id}.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if receipt_path.exists():
            receipt = _load_json(receipt_path)
            if receipt.get("state") in TERMINAL_STATES:
                return receipt
        time.sleep(0.5)
    raise TimeoutError(f"control-plane transaction {operation_id} did not finish within {timeout:g}s")


def submit_request(request: dict[str, object], state_root: Path, wait_seconds: float) -> int:
    validate_state_root_security(state_root)
    install_request(request, state_root)
    operation_id = str(request["operation_id"])
    try:
        start_manager(state_root)
    except BaseException:
        _clear_active(state_root, operation_id)
        raise
    if wait_seconds <= 0:
        print(json.dumps({"operation_id": operation_id, "state": "submitted"}, sort_keys=True))
        return 0
    receipt = wait_for_receipt(state_root, operation_id, wait_seconds)
    print(json.dumps(receipt, ensure_ascii=True, sort_keys=True))
    return 0 if receipt.get("state") == "succeeded" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lease/CAS manager for the shared local Coding MCP control plane.")
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("register")
    execute = subparsers.add_parser("execute")
    execute.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    execute.add_argument("--loop-interval-seconds", type=float, default=0.0)
    restart = subparsers.add_parser("submit-restart")
    restart.add_argument("--expected-sha256", required=True)
    restart.add_argument("--wait", type=float, default=120)
    update = subparsers.add_parser("submit-update")
    update.add_argument("--expected-sha256", required=True)
    update.add_argument("--candidate", type=Path, required=True)
    update.add_argument("--candidate-sha256", required=True)
    update.add_argument("--wait", type=float, default=120)
    args = parser.parse_args(argv)
    state_root = args.state_root.resolve()

    if args.command == "register":
        print(json.dumps(register_manager(state_root), ensure_ascii=True, sort_keys=True))
        return 0
    if args.command == "execute":
        if args.loop_interval_seconds:
            if args.loop_interval_seconds < 5:
                parser.error("--loop-interval-seconds must be zero or at least 5")
            return run_manager_loop(
                state_root,
                interval_seconds=args.loop_interval_seconds,
            )
        return execute_active(state_root)
    if args.command == "submit-restart":
        return submit_request(build_request("restart", args.expected_sha256), state_root, args.wait)
    if args.command == "submit-update":
        return submit_request(
            build_request(
                "update",
                args.expected_sha256,
                candidate=args.candidate,
                candidate_sha256=args.candidate_sha256,
            ),
            state_root,
            args.wait,
        )
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
