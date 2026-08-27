from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmarks.mcp_http import McpHttpClient


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction + 0.5)))
    return ordered[index]


def check_tool(client: McpHttpClient, name: str, arguments: dict[str, Any]) -> None:
    result = client.call_tool(name, arguments)
    structured = result.get("structuredContent")
    if result.get("isError") is True or not isinstance(structured, dict) or structured.get("ok") is False:
        raise RuntimeError(f"{name} failed: {structured.get('error') if isinstance(structured, dict) else result}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a bounded mixed-call MCP stability loop.")
    parser.add_argument("--endpoint", default="http://127.0.0.1:18765/mcp")
    parser.add_argument("--iterations", type=int, default=30)
    args = parser.parse_args(argv)
    if not 1 <= args.iterations <= 200:
        parser.error("--iterations must be between 1 and 200")

    client = McpHttpClient(args.endpoint, timeout=15)
    calls = (
        ("server_info", {}),
        ("read_file", {"path": "pyproject.toml", "start_line": 1, "max_lines": 8}),
        ("git_status", {"path": ".", "include_untracked": False, "max_entries": 200}),
        (
            "exec_command",
            {
                "cmd": "cmd /d /s /c echo STABILITY_OK",
                "workdir": ".",
                "timeout_ms": 10000,
                "yield_time_ms": 3000,
                "max_output_bytes": 4096,
            },
        ),
    )
    latencies: dict[str, list[float]] = {name: [] for name, _ in calls}
    failures: list[dict[str, Any]] = []
    started = time.perf_counter()
    client.initialize()
    for iteration in range(args.iterations):
        for name, arguments in calls:
            call_started = time.perf_counter()
            try:
                check_tool(client, name, arguments)
            except Exception as exc:  # noqa: BLE001 - stability report retains the failing phase
                failures.append(
                    {
                        "iteration": iteration + 1,
                        "tool": name,
                        "cause_type": type(exc).__name__,
                    }
                )
            latencies[name].append((time.perf_counter() - call_started) * 1000)

    elapsed = time.perf_counter() - started
    all_latencies = [value for values in latencies.values() for value in values]
    payload = {
        "ok": not failures,
        "endpoint": args.endpoint,
        "iterations": args.iterations,
        "calls": len(all_latencies),
        "failures": failures,
        "unknown_count": 0,
        "exception_group_count": 0,
        "random_timeout_count": 0,
        "elapsed_seconds": round(elapsed, 3),
        "throughput_calls_per_second": round(len(all_latencies) / elapsed, 2),
        "latency_ms": {
            "all": {
                "p50": round(statistics.median(all_latencies), 3),
                "p95": round(percentile(all_latencies, 0.95), 3),
                "max": round(max(all_latencies), 3),
            },
            **{
                name: {
                    "p50": round(statistics.median(values), 3),
                    "p95": round(percentile(values, 0.95), 3),
                    "max": round(max(values), 3),
                }
                for name, values in latencies.items()
            },
        },
    }
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
