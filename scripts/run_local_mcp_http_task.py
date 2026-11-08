from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
from pathlib import Path

try:
    from .run_local_mcp_http import main as run_local_mcp_http
except ImportError:  # direct script execution
    from run_local_mcp_http import main as run_local_mcp_http


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "local-mcp-http-task.json"


def parse_config_snapshot(raw_bytes: bytes) -> dict[str, object]:
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("local MCP task config must be valid UTF-8 JSON") from exc
    required_keys = {"log_file", "server_args"}
    allowed_keys = required_keys | {
        "auth_header_file",
        "require_config_sha256_pin",
        "require_windows_administrator",
    }
    if (
        not isinstance(raw, dict)
        or not required_keys.issubset(raw)
        or not set(raw).issubset(allowed_keys)
    ):
        raise ValueError(
            "local MCP task config must contain log_file and server_args, with optional auth_header_file"
        )
    return raw


def load_config_snapshot(config_path: Path = CONFIG) -> tuple[bytes, dict[str, object]]:
    raw_bytes = config_path.read_bytes()
    return raw_bytes, parse_config_snapshot(raw_bytes)


def build_argv_from_config(raw: dict[str, object]) -> list[str]:
    log_file = raw["log_file"]
    server_args = raw["server_args"]
    if not isinstance(log_file, str) or not log_file.strip():
        raise ValueError("log_file must be a non-empty string")
    if not isinstance(server_args, list) or not server_args or any(
        not isinstance(item, str) or not item for item in server_args
    ):
        raise ValueError("server_args must be a non-empty list of strings")
    if "--auth-token" in server_args or any(item.startswith("--auth-token=") for item in server_args):
        raise ValueError("auth tokens must be loaded from auth_header_file, not server_args")
    auth_args: list[str] = []
    auth_header_file = raw.get("auth_header_file")
    if auth_header_file is not None:
        if not isinstance(auth_header_file, str) or not auth_header_file.strip():
            raise ValueError("auth_header_file must be a non-empty path")
        try:
            header_path = Path(auth_header_file).expanduser().resolve(strict=True)
            if not header_path.is_file():
                raise ValueError("auth_header_file must name a file")
            header = header_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError("auth_header_file could not be read") from exc
        match = re.fullmatch(r"Bearer ([A-Za-z0-9._~+/=-]{16,4096})", header)
        if match is None:
            raise ValueError("auth_header_file must contain one Bearer credential")
        auth_args = ["--auth-token", match.group(1)]
    return ["--log-file", log_file, "--", *server_args, *auth_args]


def build_argv(config_path: Path = CONFIG) -> list[str]:
    _raw_bytes, raw = load_config_snapshot(config_path)
    return build_argv_from_config(raw)


def verify_config_snapshot(raw_bytes: bytes, expected_sha256: str | None) -> str:
    actual = hashlib.sha256(raw_bytes).hexdigest()
    if expected_sha256 is None:
        return actual
    if re.fullmatch(r"[A-Fa-f0-9]{64}", expected_sha256) is None:
        raise ValueError("expected config SHA-256 must contain 64 hexadecimal characters")
    if actual.lower() != expected_sha256.lower():
        raise ValueError("local MCP task config hash does not match the scheduled-task pin")
    return actual


def verify_config_hash(config_path: Path, expected_sha256: str | None) -> str:
    return verify_config_snapshot(config_path.read_bytes(), expected_sha256)


def _is_windows_administrator() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def require_config_hash_pin(required: bool, expected_sha256: str | None) -> None:
    if required and expected_sha256 is None:
        raise RuntimeError("local MCP task requires an external config SHA-256 pin")


def require_windows_administrator(
    required: bool,
    *,
    is_administrator: object = _is_windows_administrator,
) -> None:
    if not required or os.name != "nt":
        return
    checker = is_administrator
    if not callable(checker) or not bool(checker()):
        raise RuntimeError("local MCP task requires a Windows administrator token")


def main() -> int:
    parser = argparse.ArgumentParser(description="Start Coding Tools MCP from a short task config path.")
    parser.add_argument("--config", default=str(CONFIG))
    parser.add_argument("--expect-config-sha256")
    args = parser.parse_args()
    config_path = Path(args.config).resolve()
    raw_bytes, raw = load_config_snapshot(config_path)
    require_config_hash_pin(
        raw.get("require_config_sha256_pin") is True,
        args.expect_config_sha256,
    )
    verify_config_snapshot(raw_bytes, args.expect_config_sha256)
    require_windows_administrator(raw.get("require_windows_administrator") is True)
    return run_local_mcp_http(build_argv_from_config(raw))


if __name__ == "__main__":
    raise SystemExit(main())
