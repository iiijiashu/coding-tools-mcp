from __future__ import annotations

import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from coding_tools_mcp.processes import (
    CommandRun,
    HARD_KILL_SIGNAL,
    start_reader_threads,
)
from coding_tools_mcp.server import Runtime, process_group_popen_kwargs
from tests.compliance import runner


def python_command(code: str) -> str:
    argv = [sys.executable, "-u", "-c", code]
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def wait_file(path: Path) -> None:
    deadline = time.monotonic() + 3
    while not path.exists() or not path.stat().st_size:
        if time.monotonic() >= deadline:
            raise AssertionError("child did not publish heartbeat")
        time.sleep(0.02)


class EfficiencyRegressionTests(unittest.TestCase):
    def test_unowned_process_refresh_never_signals_a_process_group(self) -> None:
        process = Mock()
        process.poll.return_value = 0
        process.pid = 0
        process.stdin = None
        command = CommandRun("unowned", process)
        with patch("coding_tools_mcp.processes.terminate_process_group") as terminate:
            command.refresh_status()
        terminate.assert_not_called()

    def test_progressing_stdin_outlives_its_idle_timeout(self) -> None:
        code = "import os,time; n=0\nwhile True:\n b=os.read(0,4096)\n if not b: break\n n+=len(b); time.sleep(.012)\nprint(n,flush=True)"
        with subprocess.Popen(
            [sys.executable, "-u", "-c", code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            **process_group_popen_kwargs(),
        ) as process:
            command = CommandRun("slow-input", process)
            command._stdin_writer.delivery_timeout = 0.3
            try:
                started = time.monotonic()
                command.write_input(b"x" * 262144)
                command.close_stdin()
                process.wait(timeout=5)
                self.assertEqual(process.returncode, 0)
                self.assertEqual(int(process.stdout.read()), 262144)
                self.assertGreater(time.monotonic() - started, 0.3)
                self.assertEqual(command._stdin_writer.snapshot()["status"], "written")
            finally:
                if process.poll() is None:
                    command.terminate(HARD_KILL_SIGNAL, force=True)

    def test_live_poll_preserves_split_utf8_and_exact_cursor(self) -> None:
        process = Mock()
        process.poll.return_value = None
        process.stdin = None
        command = CommandRun("unicode", process)
        data = "中😀".encode()
        output = []
        for chunk in (data[:2], data[2:5], data[5:]):
            command.append_stdout(chunk)
            output.append(command.snapshot_since_cursor(100)["stdout"])
        self.assertEqual(output, ["", "中", "😀"])
        self.assertEqual(command.stdout_cursor, len(data))

    def test_poll_wakes_after_retention_and_empty_poll_does_not_fsync(self) -> None:
        with TemporaryDirectory() as temporary:
            runtime = Runtime(Path(temporary), permission_mode="dangerous")
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(20)"],
                stdin=subprocess.PIPE,
                **process_group_popen_kwargs(),
            )
            command = runtime._make_command(process)
            command.buffer_limit = 64
            runtime.commands[command.command_id] = command
            command.append_stdout(b"x" * 100)
            command.snapshot_since_cursor(100)
            timer = threading.Timer(0.05, lambda: command.append_stdout(b"fresh\n"))
            try:
                timer.start()
                start = time.monotonic()
                with patch("coding_tools_mcp.server.os.fsync", wraps=os.fsync) as fsync:
                    result = runtime.call_tool(
                        "write_stdin",
                        {
                            "command_id": command.command_id,
                            "chars": "",
                            "yield_time_ms": 1000,
                        },
                    )
                self.assertLess(time.monotonic() - start, 0.8)
                self.assertEqual(result["structuredContent"]["stdout"], "fresh\n")
                fsync.assert_not_called()
                self.assertNotIn("mutation_receipt", result["structuredContent"])
                with patch("coding_tools_mcp.server.os.fsync", wraps=os.fsync) as fsync:
                    result = runtime.call_tool(
                        "write_stdin",
                        {
                            "command_id": command.command_id,
                            "chars": "input",
                            "yield_time_ms": 0,
                        },
                    )
                self.assertEqual(fsync.call_count, 2)
                self.assertTrue(
                    result["structuredContent"]["mutation_receipt"]["persisted"]
                )
            finally:
                timer.join()
                runtime.close()

    @unittest.skipUnless(os.name == "posix", "POSIX group ownership")
    def test_exited_parent_descendants_stop_on_runtime_close(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            heartbeat = root / "heartbeat"
            child = f"import time; p={str(heartbeat)!r}\nfor _ in range(400):\n with open(p,'a') as f: f.write('alive\\n')\n time.sleep(.02)"
            parent = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{child!r}])"
            runtime = Runtime(root, permission_mode="dangerous")
            process = subprocess.Popen(
                [sys.executable, "-c", parent],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            command = runtime._make_command(process)
            command.owns_process_group = True
            runtime.commands[command.command_id] = command
            start_reader_threads(command)
            try:
                process.wait(timeout=3)
                wait_file(heartbeat)
                runtime.close()
                before = heartbeat.stat().st_size
                time.sleep(0.12)
                self.assertEqual(heartbeat.stat().st_size, before)
                self.assertFalse(
                    any(thread.is_alive() for thread in command.reader_threads)
                )
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                runtime.close()

    def test_explicit_background_service_survives_runtime_close(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            heartbeat = root / "service-heartbeat"
            code = f"import time; p={str(heartbeat)!r}; print('service ready',flush=True)\nfor _ in range(400):\n with open(p,'a') as f: f.write('alive\\n')\n time.sleep(.02)"
            runtime = Runtime(root, permission_mode="dangerous")
            command = None
            try:
                payload = runtime.exec_command(
                    {"cmd": python_command(code), "background": True, "timeout_ms": 1}
                )
                command = runtime.commands[payload["command_id"]]
                wait_file(heartbeat)
                polled = runtime.write_stdin(
                    {"command_id": command.command_id, "chars": "", "yield_time_ms": 0}
                )
                self.assertIn("service ready", polled["stdout"])
                self.assertEqual(command.reader_threads, [])
                self.assertIsNone(command.timeout_at)
                manifest_path = (
                    Path(payload["log_paths"]["stdout"]).parent / "manifest.json"
                )
                self.assertEqual(
                    json.loads(manifest_path.read_text())["command_id"],
                    command.command_id,
                )
                runtime.close()
                before = heartbeat.stat().st_size
                time.sleep(0.12)
                self.assertGreater(heartbeat.stat().st_size, before)
                self.assertIsNone(command.process.poll())
            finally:
                if command is not None:
                    command.terminate(HARD_KILL_SIGNAL, force=True)
                    command.process.wait(timeout=3)
                runtime.close()

    def test_background_service_can_be_explicitly_stopped(self) -> None:
        with TemporaryDirectory() as temporary:
            runtime = Runtime(Path(temporary), permission_mode="dangerous")
            command = None
            try:
                result = runtime.call_tool(
                    "exec_command",
                    {
                        "cmd": python_command("import time; time.sleep(20)"),
                        "background": True,
                    },
                )
                payload = result["structuredContent"]
                command = runtime.commands[payload["command_id"]]
                text = "\n".join(
                    item["text"] for item in result["content"] if item["type"] == "text"
                )
                self.assertIn("Survives client disconnect and runtime shutdown", text)
                self.assertIn("kill_command", text)
                self.assertIn(payload["log_paths"]["stdout"], text)
                self.assertEqual(
                    payload["next_action"]["arguments"]["yield_time_ms"], 0
                )
                result = runtime.kill_command(
                    {"command_id": command.command_id, "signal": "KILL"}
                )
                self.assertTrue(result["killed"])
                self.assertIsNotNone(command.process.poll())
            finally:
                if command is not None:
                    command.terminate(HARD_KILL_SIGNAL, force=True)
                runtime.close()

    def test_background_service_survives_backend_process_exit(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            heartbeat = root / "detached-heartbeat"
            service = f"import time; p={str(heartbeat)!r}; print('detached ready',flush=True)\nfor _ in range(1000):\n with open(p,'a') as f: f.write('alive\\n')\n time.sleep(.02)"
            launcher = (
                "import json; from pathlib import Path; from coding_tools_mcp.server import Runtime; "
                f"runtime=Runtime(Path({temporary!r}), permission_mode='dangerous'); "
                f"result=runtime.exec_command({{'cmd': {python_command(service)!r}, 'background': True}}); "
                "result['runtime_dir']=str(runtime.runtime_dir); runtime.close(); print(json.dumps(result))"
            )
            payload = None
            try:
                completed = subprocess.run(
                    [sys.executable, "-c", launcher],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                payload = json.loads(completed.stdout)
                wait_file(heartbeat)
                before = heartbeat.stat().st_size
                time.sleep(0.15)
                self.assertGreater(heartbeat.stat().st_size, before)
                self.assertIn(
                    "detached ready", Path(payload["log_paths"]["stdout"]).read_text()
                )
                self.assertTrue(Path(payload["runtime_dir"]).is_dir())
            finally:
                if payload is not None:
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/PID", str(payload["pid"]), "/T", "/F"],
                            capture_output=True,
                            timeout=5,
                        )
                    else:
                        try:
                            os.killpg(payload["pid"], signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    shutil.rmtree(payload["runtime_dir"], ignore_errors=True)

    def test_skipped_category_and_tools_are_not_reported_passed(self) -> None:
        class Skipped(unittest.TestCase):
            @unittest.skip("optional dependency absent")
            def test_read_file(self):
                pass

        suite = unittest.defaultTestLoader.loadTestsFromTestCase(Skipped)
        result = runner.RecordingRunner(stream=io.StringIO()).run(suite)
        self.assertEqual(runner.category_status(result, __name__), "skipped")
        statuses = runner.required_tool_statuses(
            suite_name="discovery", result=result, passed=True
        )
        self.assertEqual(set(statuses.values()), {"skipped"})


if __name__ == "__main__":
    unittest.main()
