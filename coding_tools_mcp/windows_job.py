"""Small native ownership handle; no helper processes or runtime dependencies."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Any


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_longlong),
        ("job_time", ctypes.c_longlong),
        ("flags", wintypes.DWORD),
        ("minimum_working_set", ctypes.c_size_t),
        ("maximum_working_set", ctypes.c_size_t),
        ("active_process_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority_class", wintypes.DWORD),
        ("scheduling_class", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in (
            "read_operations",
            "write_operations",
            "other_operations",
            "read_bytes",
            "write_bytes",
            "other_bytes",
        )
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits),
        ("io", _IoCounters),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


class _ThreadEntry(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD),
        ("usage", wintypes.DWORD),
        ("thread_id", wintypes.DWORD),
        ("owner_pid", wintypes.DWORD),
        ("base_priority", wintypes.LONG),
        ("delta_priority", wintypes.LONG),
        ("flags", wintypes.DWORD),
    ]


class WindowsJob:
    def __init__(self, process: Any, *, persistent: bool) -> None:
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        for name, arguments, result in (
            ("CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            (
                "SetInformationJobObject",
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
                wintypes.BOOL,
            ),
            (
                "AssignProcessToJobObject",
                [wintypes.HANDLE, wintypes.HANDLE],
                wintypes.BOOL,
            ),
            ("TerminateJobObject", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
            (
                "CreateToolhelp32Snapshot",
                [wintypes.DWORD, wintypes.DWORD],
                wintypes.HANDLE,
            ),
            (
                "Thread32First",
                [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)],
                wintypes.BOOL,
            ),
            (
                "Thread32Next",
                [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)],
                wintypes.BOOL,
            ),
            (
                "OpenThread",
                [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD],
                wintypes.HANDLE,
            ),
            ("ResumeThread", [wintypes.HANDLE], wintypes.DWORD),
        ):
            function = getattr(self.api, name)
            function.argtypes = arguments
            function.restype = result
        self.handle = self.api.CreateJobObjectW(None, None)
        self._check(self.handle)
        try:
            limits = _ExtendedLimits()
            limits.basic.flags = 0 if persistent else 0x00002000  # KILL_ON_JOB_CLOSE
            self._check(
                self.api.SetInformationJobObject(
                    self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
                )
            )
            self._check(
                self.api.AssignProcessToJobObject(self.handle, int(process._handle))
            )
            self._resume_primary_thread(process.pid)
        except Exception:
            self.api.TerminateJobObject(self.handle, 1)
            self.close()
            raise

    def _resume_primary_thread(self, pid: int) -> None:
        # Popen closes its primary-thread handle. Find that one thread while
        # the process is still suspended, after its job ownership is installed.
        snapshot = self.api.CreateToolhelp32Snapshot(0x00000004, 0)
        self._check(snapshot and snapshot != ctypes.c_void_p(-1).value)
        try:
            entry = _ThreadEntry()
            entry.size = ctypes.sizeof(entry)
            found = self.api.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.owner_pid == pid:
                    thread = self.api.OpenThread(0x0002, False, entry.thread_id)
                    self._check(thread)
                    try:
                        previous = self.api.ResumeThread(thread)
                        self._check(previous != 0xFFFFFFFF)
                        if previous != 1:
                            raise OSError(
                                "new process primary thread was not suspended exactly once"
                            )
                    finally:
                        self.api.CloseHandle(thread)
                    return
                entry.size = ctypes.sizeof(entry)
                found = self.api.Thread32Next(snapshot, ctypes.byref(entry))
            raise OSError("new process primary thread was not found")
        finally:
            self.api.CloseHandle(snapshot)

    @staticmethod
    def _check(ok: Any) -> None:
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]

    def terminate(self) -> None:
        if self.handle:
            self._check(self.api.TerminateJobObject(self.handle, 1))
            self.close()

    def close(self) -> None:
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None
