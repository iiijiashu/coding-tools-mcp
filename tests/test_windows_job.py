from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from coding_tools_mcp.processes import CommandRun, HARD_KILL_SIGNAL, spawn_process
from coding_tools_mcp.server import process_group_popen_kwargs
from coding_tools_mcp.windows_job import WindowsJob


class WindowsJobPolicyTests(unittest.TestCase):
    def test_assigns_before_resuming_and_distinguishes_persistence(self) -> None:
        for persistent in (False, True):
            api = Mock()
            api.CreateJobObjectW.return_value = 100
            api.SetInformationJobObject.return_value = 1
            api.AssignProcessToJobObject.return_value = 1
            api.CreateToolhelp32Snapshot.return_value = 200
            api.OpenThread.return_value = 300
            api.ResumeThread.return_value = 1

            def first(_snapshot, pointer):
                pointer._obj.owner_pid = 42
                pointer._obj.thread_id = 43
                return 1

            api.Thread32First.side_effect = first
            seen_flags = []
            api.SetInformationJobObject.side_effect = lambda _h, _kind, pointer, _size: (
                seen_flags.append(pointer._obj.basic.flags) or 1
            )
            with patch(
                "coding_tools_mcp.windows_job.ctypes.WinDLL",
                return_value=api,
                create=True,
            ):
                job = WindowsJob(Mock(pid=42, _handle=44), persistent=persistent)
            calls = [call[0] for call in api.mock_calls]
            self.assertLess(
                calls.index("AssignProcessToJobObject"), calls.index("ResumeThread")
            )
            self.assertEqual(seen_flags, [0 if persistent else 0x2000])
            job.terminate()
            api.TerminateJobObject.assert_called_once_with(100, 1)

    @unittest.skipUnless(os.name == "nt", "native Windows Job Object acceptance")
    def test_native_job_cleans_descendant_after_parent_exit(self) -> None:
        with TemporaryDirectory() as temporary:
            heartbeat = Path(temporary) / "heartbeat"
            child = f"import time; p={str(heartbeat)!r}\nfor _ in range(400):\n with open(p,'a') as f: f.write('alive\\n')\n time.sleep(.02)"
            parent = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{child!r}])"
            process, _ = spawn_process(
                [sys.executable, "-c", parent],
                cwd=temporary,
                shell=False,
                env=dict(os.environ),
                tty=False,
                popen_kwargs=process_group_popen_kwargs(),
            )
            command = CommandRun("job-test", process)
            try:
                process.wait(timeout=3)
                deadline = time.monotonic() + 3
                while not heartbeat.exists():
                    if time.monotonic() > deadline:
                        self.fail("descendant failed to start")
                    time.sleep(0.02)
                time.sleep(0.05)
                before = heartbeat.stat().st_size
                time.sleep(0.1)
                self.assertGreater(heartbeat.stat().st_size, before)
                command.refresh_status()
                stopped = heartbeat.stat().st_size
                time.sleep(0.12)
                self.assertEqual(heartbeat.stat().st_size, stopped)
            finally:
                command.terminate(HARD_KILL_SIGNAL, force=True)
                command.close_stdin()
                for stream in (process.stdout, process.stderr):
                    stream.close()


if __name__ == "__main__":
    unittest.main()
