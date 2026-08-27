from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from .run_local_mcp_http import main as run_local_mcp_http
except ImportError:  # direct script execution
    from run_local_mcp_http import main as run_local_mcp_http


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "local-mcp-http-task.json"


def build_argv(config_path: Path = CONFIG) -> list[str]:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) != {"log_file", "server_args"}:
        raise ValueError("local MCP task config must contain exactly log_file and server_args")
    log_file = raw["log_file"]
    server_args = raw["server_args"]
    if not isinstance(log_file, str) or not log_file.strip():
        raise ValueError("log_file must be a non-empty string")
    if not isinstance(server_args, list) or not server_args or any(
        not isinstance(item, str) or not item for item in server_args
    ):
        raise ValueError("server_args must be a non-empty list of strings")
    return ["--log-file", log_file, "--", *server_args]


def main() -> int:
    parser = argparse.ArgumentParser(description="Start Coding Tools MCP from a short task config path.")
    parser.add_argument("--config", default=str(CONFIG))
    args = parser.parse_args()
    return run_local_mcp_http(build_argv(Path(args.config).resolve()))


if __name__ == "__main__":
    raise SystemExit(main())
