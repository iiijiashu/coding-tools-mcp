from __future__ import annotations

import subprocess
import sys
import time


CREATE_NO_WINDOW = 0x08000000


def run_schtasks(*args: str) -> int:
    completed = subprocess.run(
        ["schtasks", *args],
        creationflags=CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return int(completed.returncode)


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].startswith("\\"):
        return 2
    task_name = sys.argv[1]
    # Give the spawning MCP call time to return before ending its own task.
    time.sleep(2.0)
    end_code = run_schtasks("/end", "/tn", task_name)
    time.sleep(2.0)
    run_code = run_schtasks("/run", "/tn", task_name)
    return 0 if end_code == 0 and run_code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
