from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp.processes import CommandRun, terminate_process_group
from coding_tools_mcp.server import Runtime
from coding_tools_mcp.stdin_writer import MAX_STDIN_WRITE_BYTES
from coding_tools_mcp.textutils import output_page_bytes


class RuntimeReliabilityTests(unittest.TestCase):
    def test_oversized_initial_input_is_rejected_before_spawn(self) -> None:
        with TemporaryDirectory() as temporary:
            runtime = Runtime(Path(temporary), permission_mode="dangerous")
            try:
                with patch("coding_tools_mcp.server.spawn_process") as spawn:
                    with self.assertRaises(ToolFailure) as raised:
                        runtime.exec_command(
                            {
                                "cmd": "echo started",
                                "stdin": "x" * (MAX_STDIN_WRITE_BYTES + 1),
                            }
                        )
                self.assertEqual(raised.exception.code, "STDIN_TOO_LARGE")
                spawn.assert_not_called()
            finally:
                runtime.close()

    def test_evicted_utf8_prefix_does_not_corrupt_remaining_pages(self) -> None:
        data = "中中文😀测试".encode()[1:]
        offset = 0
        pages = []
        while offset < len(data):
            page, encoding = output_page_bytes(data, offset, 1, final=True)
            self.assertTrue(page)
            pages.append(page.decode(encoding, errors="replace"))
            offset += len(page)
        self.assertEqual("".join(pages), "��中文😀测试")

    def test_output_waits_for_an_incomplete_utf8_character(self) -> None:
        data = "中文".encode()
        first, encoding = output_page_bytes(data[:4], 0, 4, final=False)
        self.assertEqual(first.decode(encoding), "中")
        self.assertEqual(output_page_bytes(data[:4], 3, 4, final=False)[0], b"")
        self.assertEqual(output_page_bytes(data, 3, 4, final=True)[0].decode(), "文")

    def test_windows_legacy_encoding_keeps_page_boundaries(self) -> None:
        data = "中文ABC测试".encode("gbk")
        with (
            patch("coding_tools_mcp.textutils.os.name", "nt"),
            patch(
                "coding_tools_mcp.textutils.locale.getpreferredencoding",
                return_value="gbk",
            ),
        ):
            offset = 0
            text = ""
            while offset < len(data):
                page, encoding = output_page_bytes(data, offset, 1, final=True)
                self.assertTrue(page)
                text += page.decode(encoding)
                offset += len(page)
        self.assertEqual(text, "中文ABC测试")

    def test_stdin_deadline_stops_unconsumed_delivery(self) -> None:
        with subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(20)"], stdin=subprocess.PIPE
        ) as process:
            command = CommandRun("input-timeout", process)
            command._stdin_writer.delivery_timeout = 0.15
            try:
                command.write_input(b"x" * 262144)
                process.wait(timeout=3)
                deadline = time.monotonic() + 1
                while (
                    command._stdin_writer.snapshot()["status"] == "pending"
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                receipt = command._stdin_writer.snapshot()
                self.assertEqual(receipt["status"], "failed")
                self.assertEqual(receipt["error"], "delivery_timeout")
            finally:
                if process.poll() is None:
                    process.kill()
                command.close_stdin()

    def test_blocked_stdin_returns_pending_and_rejects_duplicate_admission(
        self,
    ) -> None:
        with subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(20)"],
            stdin=subprocess.PIPE,
        ) as process:
            command = CommandRun("input-test", process)
            finished = threading.Event()
            results: list[object] = []

            def write() -> None:
                try:
                    results.append(command.write_input(b"x" * 262144))
                except ToolFailure as exc:
                    results.append(exc)
                finally:
                    finished.set()

            writer = threading.Thread(target=write, daemon=True)
            writer.start()
            try:
                self.assertTrue(finished.wait(1), "stdin admission blocked the caller")
                receipt = results[0]
                self.assertIsInstance(receipt, dict)
                self.assertEqual(receipt["status"], "pending")
                self.assertEqual(receipt["accepted_bytes"], 262144)
                with self.assertRaises(ToolFailure) as raised:
                    command.write_input(b"duplicate")
                self.assertEqual(raised.exception.code, "STDIN_BUSY")
                begin = time.monotonic()
                command.close_stdin()
                self.assertLess(time.monotonic() - begin, 0.5)
            finally:
                process.kill()
                process.wait(timeout=3)
                writer.join(3)

    def test_stdin_delivery_and_deferred_eof_preserve_exact_bytes(self) -> None:
        data = ("中文😀\n" * 20000).encode()
        with subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        ) as process:
            command = CommandRun("input-eof", process)
            try:
                command.write_input(data)
                command.close_stdin()
                self.assertEqual(process.stdout.read(), data)
                process.wait(timeout=3)
            finally:
                if process.poll() is None:
                    process.kill()

    def test_utf8_output_pages_round_trip_small_limits(self) -> None:
        with TemporaryDirectory() as temporary:
            runtime = Runtime(Path(temporary), permission_mode="dangerous")
            code = "import sys; sys.stdout.buffer.write('中文😀测试'.encode('utf-8'))"
            argv = [sys.executable, "-c", code]
            cmd = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
            try:
                result = runtime.exec_command(
                    {"cmd": cmd, "yield_time_ms": 1000, "verbosity": "summary"}
                )
                for limit in (1, 2, 4, 7):
                    offset = 0
                    pages = []
                    while offset is not None:
                        page = runtime.read_output(
                            {
                                "output_ref": result["output_refs"]["stdout"],
                                "offset": offset,
                                "limit": limit,
                            }
                        )
                        pages.append(page["content"])
                        following = page["next_offset"]
                        self.assertTrue(following is None or following > offset)
                        offset = following
                    self.assertEqual("".join(pages), "中文😀测试")
            finally:
                runtime.close()

    def test_failed_receipt_storage_is_explicit_for_successful_mutation(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".coding-tools-mcp").write_text("blocks receipt directory")
            runtime = Runtime(root, permission_mode="dangerous")
            try:
                result = runtime.call_tool(
                    "apply_patch",
                    {
                        "patch": "*** Begin Patch\n*** Add File: result.txt\n+done\n*** End Patch"
                    },
                )
                payload = result["structuredContent"]
                self.assertTrue(payload["ok"])
                self.assertTrue((root / "result.txt").exists())
                self.assertFalse(payload["mutation_receipt"]["persisted"])
                self.assertIn("storage_error", payload["mutation_receipt"])
                self.assertEqual(
                    runtime.server_info_payload()["mutation_receipt_health"]["status"],
                    "degraded",
                )
            finally:
                runtime.close()

    @unittest.skipUnless(
        os.name == "posix" and Path("/proc").exists(), "Linux process-group integration"
    )
    def test_termination_cleans_child_when_parent_exits_first(self) -> None:
        child = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(20)"
        parent = (
            "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c',"
            + repr(child)
            + "],stdout=subprocess.PIPE,text=True); p.stdout.readline(); print(p.pid,flush=True); time.sleep(20)"
        )
        with subprocess.Popen(
            [sys.executable, "-c", parent],
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        ) as process:
            child_pid = int(process.stdout.readline())
            try:
                terminate_process_group(process, signal.SIGTERM)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    path = Path(f"/proc/{child_pid}/stat")
                    if not path.exists() or path.read_text().split()[2] == "Z":
                        break
                    time.sleep(0.02)
                else:
                    self.fail("child still running after process-group termination")
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
