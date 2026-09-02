"""Strict loader for the local control-plane deployment contract."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_CONTRACT_PATH = ROOT / "config" / "control-plane-runtime-contract-v1.json"

_TOP_LEVEL_KEYS = frozenset(
    {"schema_version", "task_path", "pythonw_relative", "tool_catalog", "tunnel", "tasks"}
)
_TOOL_CATALOG_KEYS = frozenset({"required_tools"})
_TUNNEL_KEYS = frozenset({"main_arguments_sha256", "executable_sha256"})
_TASK_KEYS = frozenset(
    {
        "name",
        "script_relative",
        "run_level",
        "must_be_running",
        "required_triggers",
        "restart_on_failure",
        "allow_time_trigger",
        "loop_interval_seconds",
        "pin_tool_catalog",
        "pin_main_arguments",
    }
)
_TASK_ORDER = ("http", "http_watchdog", "tunnel", "tunnel_watchdog", "manager")
_RUN_LEVELS = frozenset({"Highest", "Limited"})
_TRIGGERS = frozenset({"registration", "logon", "session"})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

CANONICAL_PYTHONW_RELATIVE = ".venv/Scripts/pythonw.exe"
CRITICAL_REQUIRED_TOOLS = frozenset(
    {
        "server_info",
        "computer_screenshot",
        "computer_mouse",
        "computer_keyboard",
        "computer_launch",
    }
)
CANONICAL_TASK_INVARIANTS: dict[str, dict[str, object]] = {
    "http": {
        "name": "Coding Tools MCP HTTP - Local D Drive",
        "script_relative": "scripts/run_local_mcp_http_task.py",
        "run_level": "Highest",
        "must_be_running": True,
        "required_triggers": ("registration",),
        "restart_on_failure": False,
        "allow_time_trigger": False,
        "loop_interval_seconds": 0,
        "pin_tool_catalog": False,
        "pin_main_arguments": False,
    },
    "http_watchdog": {
        "name": "Coding Tools MCP HTTP Watchdog - Local D Drive",
        "script_relative": "scripts/local_http_mcp_watchdog.py",
        "run_level": "Highest",
        "must_be_running": True,
        "required_triggers": ("registration", "logon"),
        "restart_on_failure": True,
        "allow_time_trigger": False,
        "loop_interval_seconds": 60,
        "pin_tool_catalog": True,
        "pin_main_arguments": False,
    },
    "tunnel": {
        "name": "OpenAI Secure MCP Tunnel - Local D Drive Coding Tools",
        "script_relative": None,
        "run_level": "Limited",
        "must_be_running": True,
        "required_triggers": ("registration",),
        "restart_on_failure": False,
        "allow_time_trigger": False,
        "loop_interval_seconds": 0,
        "pin_tool_catalog": False,
        "pin_main_arguments": False,
    },
    "tunnel_watchdog": {
        "name": "OpenAI Secure MCP Tunnel Watchdog - Local D Drive Coding Tools",
        "script_relative": "scripts/secure_tunnel_watchdog.py",
        "run_level": "Limited",
        "must_be_running": True,
        "required_triggers": ("registration", "logon"),
        "restart_on_failure": True,
        "allow_time_trigger": False,
        "loop_interval_seconds": 15,
        "pin_tool_catalog": True,
        "pin_main_arguments": True,
    },
    "manager": {
        "name": "Coding Tools MCP Control Plane Manager - Local D Drive",
        "script_relative": "scripts/control_plane_transaction.py",
        "run_level": "Highest",
        "must_be_running": False,
        "required_triggers": ("registration", "logon"),
        "restart_on_failure": True,
        "allow_time_trigger": False,
        "loop_interval_seconds": 60,
        "pin_tool_catalog": False,
        "pin_main_arguments": False,
    },
}


class RuntimeContractError(ValueError):
    """The runtime contract is unreadable, ambiguous, or outside schema v1."""


@dataclass(frozen=True, slots=True)
class ToolCatalogContract:
    required_tools: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TunnelContract:
    main_arguments_sha256: str
    executable_sha256: str


@dataclass(frozen=True, slots=True)
class TaskContract:
    key: str
    name: str
    script_relative: str | None
    run_level: str
    must_be_running: bool
    required_triggers: tuple[str, ...]
    restart_on_failure: bool
    allow_time_trigger: bool
    loop_interval_seconds: int
    pin_tool_catalog: bool
    pin_main_arguments: bool


@dataclass(frozen=True, slots=True)
class RuntimeContract:
    schema_version: int
    task_path: str
    pythonw_relative: str
    tool_catalog: ToolCatalogContract
    tunnel: TunnelContract
    tasks: tuple[TaskContract, ...]

    def task(self, key: str) -> TaskContract:
        for task in self.tasks:
            if task.key == key:
                return task
        raise KeyError(key)


def _reject_duplicate_object_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeContractError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _object(value: Any, location: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise RuntimeContractError(f"{location} must be an object")
    return value


def _exact_keys(value: dict[str, Any], expected: frozenset[str], location: str) -> None:
    actual = frozenset(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing or unknown:
        raise RuntimeContractError(f"{location} keys invalid: missing={missing}, unknown={unknown}")


def _string(value: Any, location: str) -> str:
    if type(value) is not str or not value:
        raise RuntimeContractError(f"{location} must be a non-empty string")
    return value


def _boolean(value: Any, location: str) -> bool:
    if type(value) is not bool:
        raise RuntimeContractError(f"{location} must be a boolean")
    return value


def _integer(value: Any, location: str) -> int:
    if type(value) is not int:
        raise RuntimeContractError(f"{location} must be an integer")
    return value


def _sha256(value: Any, location: str) -> str:
    text = _string(value, location)
    if _SHA256.fullmatch(text) is None:
        raise RuntimeContractError(f"{location} must be a canonical lowercase SHA-256")
    return text


def _relative_path(value: Any, location: str) -> str:
    text = _string(value, location)
    windows = PureWindowsPath(text)
    posix = PurePosixPath(text)
    if windows.is_absolute() or posix.is_absolute() or windows.drive or windows.root:
        raise RuntimeContractError(f"{location} must be repository-relative")
    if ".." in windows.parts or ".." in posix.parts or text in {".", "./", ".\\"}:
        raise RuntimeContractError(f"{location} cannot traverse parent directories")
    return text


def _string_tuple(value: Any, location: str, *, allowed: frozenset[str] | None = None) -> tuple[str, ...]:
    if type(value) is not list:
        raise RuntimeContractError(f"{location} must be an array")
    items = tuple(_string(item, f"{location}[{index}]") for index, item in enumerate(value))
    if len(set(items)) != len(items):
        raise RuntimeContractError(f"{location} cannot contain duplicates")
    if allowed is not None and any(item not in allowed for item in items):
        raise RuntimeContractError(f"{location} contains an unsupported value")
    return items


def _load_task(key: str, raw: Any) -> TaskContract:
    location = f"tasks.{key}"
    value = _object(raw, location)
    _exact_keys(value, _TASK_KEYS, location)
    script_value = value["script_relative"]
    if script_value is None:
        script_relative = None
    else:
        script_relative = _relative_path(script_value, f"{location}.script_relative")
    if (key == "tunnel") != (script_relative is None):
        raise RuntimeContractError("only tasks.tunnel may have a null script_relative")
    run_level = _string(value["run_level"], f"{location}.run_level")
    if run_level not in _RUN_LEVELS:
        raise RuntimeContractError(f"{location}.run_level is unsupported")
    loop_interval = _integer(value["loop_interval_seconds"], f"{location}.loop_interval_seconds")
    if loop_interval < 0:
        raise RuntimeContractError(f"{location}.loop_interval_seconds cannot be negative")
    triggers = _string_tuple(
        value["required_triggers"],
        f"{location}.required_triggers",
        allowed=_TRIGGERS,
    )
    if "registration" not in triggers:
        raise RuntimeContractError(f"{location}.required_triggers must include registration")
    task = TaskContract(
        key=key,
        name=_string(value["name"], f"{location}.name"),
        script_relative=script_relative,
        run_level=run_level,
        must_be_running=_boolean(value["must_be_running"], f"{location}.must_be_running"),
        required_triggers=triggers,
        restart_on_failure=_boolean(value["restart_on_failure"], f"{location}.restart_on_failure"),
        allow_time_trigger=_boolean(value["allow_time_trigger"], f"{location}.allow_time_trigger"),
        loop_interval_seconds=loop_interval,
        pin_tool_catalog=_boolean(value["pin_tool_catalog"], f"{location}.pin_tool_catalog"),
        pin_main_arguments=_boolean(value["pin_main_arguments"], f"{location}.pin_main_arguments"),
    )
    expected = CANONICAL_TASK_INVARIANTS[key]
    for field, expected_value in expected.items():
        if getattr(task, field) != expected_value:
            raise RuntimeContractError(f"{location}.{field} violates the schema v1 role invariant")
    return task


def load_runtime_contract(path: Path | None = None) -> RuntimeContract:
    contract_path = path or DEFAULT_RUNTIME_CONTRACT_PATH
    try:
        text = contract_path.read_text(encoding="utf-8")
        raw = json.loads(text, object_pairs_hook=_reject_duplicate_object_keys)
    except RuntimeContractError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeContractError(f"runtime contract is unreadable: {contract_path}") from exc

    value = _object(raw, "contract")
    _exact_keys(value, _TOP_LEVEL_KEYS, "contract")
    schema_version = _integer(value["schema_version"], "schema_version")
    if schema_version != 1:
        raise RuntimeContractError("schema_version must be 1")
    task_path = _string(value["task_path"], "task_path")
    if task_path != "\\":
        raise RuntimeContractError("task_path must be the canonical root task path")
    pythonw_relative = _relative_path(value["pythonw_relative"], "pythonw_relative")
    if pythonw_relative != CANONICAL_PYTHONW_RELATIVE:
        raise RuntimeContractError("pythonw_relative violates the schema v1 runtime invariant")

    catalog_raw = _object(value["tool_catalog"], "tool_catalog")
    _exact_keys(catalog_raw, _TOOL_CATALOG_KEYS, "tool_catalog")
    required_tools = _string_tuple(catalog_raw["required_tools"], "tool_catalog.required_tools")
    missing_critical_tools = sorted(CRITICAL_REQUIRED_TOOLS - set(required_tools))
    if missing_critical_tools:
        raise RuntimeContractError(
            f"tool_catalog.required_tools is missing critical tools: {missing_critical_tools}"
        )
    tool_catalog = ToolCatalogContract(
        required_tools=required_tools,
    )

    tunnel_raw = _object(value["tunnel"], "tunnel")
    _exact_keys(tunnel_raw, _TUNNEL_KEYS, "tunnel")
    tunnel = TunnelContract(
        main_arguments_sha256=_sha256(
            tunnel_raw["main_arguments_sha256"],
            "tunnel.main_arguments_sha256",
        ),
        executable_sha256=_sha256(
            tunnel_raw["executable_sha256"],
            "tunnel.executable_sha256",
        ),
    )

    tasks_raw = _object(value["tasks"], "tasks")
    if frozenset(tasks_raw) != frozenset(_TASK_ORDER):
        raise RuntimeContractError(
            f"tasks keys invalid: expected={list(_TASK_ORDER)}, actual={sorted(tasks_raw)}"
        )
    tasks = tuple(_load_task(key, tasks_raw[key]) for key in _TASK_ORDER)
    names = tuple(task.name for task in tasks)
    if len(set(names)) != len(names):
        raise RuntimeContractError("task names must be unique")

    return RuntimeContract(
        schema_version=schema_version,
        task_path=task_path,
        pythonw_relative=pythonw_relative,
        tool_catalog=tool_catalog,
        tunnel=tunnel,
        tasks=tasks,
    )


def runtime_contract_payload(contract: RuntimeContract) -> dict[str, Any]:
    payload = asdict(contract)
    payload["tasks"] = {task.key: asdict(task) for task in contract.tasks}
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate and normalize a control-plane contract.")
    parser.add_argument("--contract", type=Path, default=DEFAULT_RUNTIME_CONTRACT_PATH)
    args = parser.parse_args(argv)
    try:
        contract = load_runtime_contract(args.contract)
    except RuntimeContractError as exc:
        parser.error(str(exc))
    print(json.dumps(runtime_contract_payload(contract), ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
