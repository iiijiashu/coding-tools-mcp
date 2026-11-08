from __future__ import annotations

import argparse
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable


CREATE_NO_WINDOW = 0x08000000


def schtasks(*args: str) -> int:
    completed = subprocess.run(
        ["schtasks", *args],
        creationflags=CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return int(completed.returncode)


def wait_for_url(url: str, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        try:
            request = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=min(2.0, max(0.1, timeout_seconds))) as response:
                if response.status == 200:
                    return True
        except (OSError, urllib.error.URLError):
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.5, max(0.05, deadline - time.monotonic())))


def switch_tasks(
    *,
    from_task: str,
    to_task: str,
    enable_to: bool,
    disable_from: bool,
    run_task_command: Callable[..., int] = schtasks,
    wait_until_ready: Callable[[], bool] = lambda: True,
) -> int:
    """Start and verify the target before touching the source task."""
    if enable_to and run_task_command("/change", "/tn", to_task, "/enable") != 0:
        return 1
    if run_task_command("/run", "/tn", to_task) != 0:
        return 1
    if not wait_until_ready():
        run_task_command("/end", "/tn", to_task)
        return 1
    if disable_from:
        if run_task_command("/end", "/tn", from_task) != 0:
            return 1
        if run_task_command("/change", "/tn", from_task, "/disable") != 0:
            return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-task", required=True)
    parser.add_argument("--to-task", required=True)
    parser.add_argument("--disable-from", action="store_true")
    parser.add_argument("--enable-to", action="store_true")
    parser.add_argument("--delay-seconds", type=float, default=1.5)
    parser.add_argument("--readiness-url")
    parser.add_argument("--readiness-timeout-seconds", type=float, default=20.0)
    args = parser.parse_args()
    if not args.from_task.startswith("\\") or not args.to_task.startswith("\\"):
        return 2
    time.sleep(max(0.0, min(args.delay_seconds, 10.0)))
    readiness = (
        (lambda: wait_for_url(args.readiness_url, args.readiness_timeout_seconds))
        if args.readiness_url
        else (lambda: True)
    )
    return switch_tasks(
        from_task=args.from_task,
        to_task=args.to_task,
        enable_to=args.enable_to,
        disable_from=args.disable_from,
        wait_until_ready=readiness,
    )


if __name__ == "__main__":
    raise SystemExit(main())
