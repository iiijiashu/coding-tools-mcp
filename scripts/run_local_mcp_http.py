from __future__ import annotations

import argparse
import contextlib
import json
import traceback
from datetime import datetime, timezone
from pathlib import Path

from coding_tools_mcp.server import main as server_main


SENSITIVE_OPTIONS = frozenset(
    {
        "--auth-token",
        "--oauth-password",
        "--oauth-token-secret",
        "--client-secret",
    }
)


def redact_server_args(args: list[str]) -> list[str]:
    redacted: list[str] = []
    hide_next = False
    for value in args:
        if hide_next:
            redacted.append("<redacted>")
            hide_next = False
            continue
        name, separator, _ = value.partition("=")
        if name.lower() in SENSITIVE_OPTIONS:
            if separator:
                redacted.append(f"{name}=<redacted>")
            else:
                redacted.append(value)
                hide_next = True
            continue
        redacted.append(value)
    return redacted


def _leaf_exceptions(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        leaves: list[BaseException] = []
        for child in exc.exceptions:
            leaves.extend(_leaf_exceptions(child))
        return leaves
    return [exc]


def classify_exception(exc: BaseException) -> dict[str, object]:
    leaves = _leaf_exceptions(exc)
    cause_types = sorted({type(item).__name__ for item in leaves})
    port_conflict = any(
        isinstance(item, OSError)
        and (
            getattr(item, "winerror", None) == 10048
            or getattr(item, "errno", None) in {98, 10048}
            or "address already in use" in str(item).lower()
        )
        for item in leaves
    )
    if port_conflict:
        category, phase, retryable = "port_in_use", "bind", False
    elif any(isinstance(item, PermissionError) for item in leaves):
        category, phase, retryable = "permission", "startup", False
    elif any(isinstance(item, TimeoutError) for item in leaves):
        category, phase, retryable = "timeout", "startup", True
    elif isinstance(exc, BaseExceptionGroup):
        category, phase, retryable = "exception_group", "startup", False
    else:
        category, phase, retryable = "runtime", "startup", False
    return {
        "category": category,
        "phase": phase,
        "retryable": retryable,
        "cause_types": cause_types,
    }


def _event(name: str, **details: object) -> str:
    return json.dumps(
        {"time": datetime.now(timezone.utc).isoformat(), "event": name, **details},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local HTTP MCP server with a persistent UTF-8 log.")
    parser.add_argument("--log-file", required=True)
    args, server_args = parser.parse_known_args(argv)
    if server_args[:1] == ["--"]:
        server_args = server_args[1:]

    log_path = Path(args.log_file).expanduser().resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", buffering=1) as log:
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            print(_event("server_start", argv=redact_server_args(server_args)), flush=True)
            try:
                exit_code = server_main(server_args)
            except BaseException as exc:
                print(
                    _event(
                        "server_exception",
                        type=type(exc).__name__,
                        **classify_exception(exc),
                    ),
                    flush=True,
                )
                log.writelines(traceback.format_tb(exc.__traceback__))
                return 1
            print(_event("server_exit", exit_code=exit_code), flush=True)
            return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
