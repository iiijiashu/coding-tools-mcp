from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from .errors import ToolFailure
from .stdin_writer import StdinWriter
from .textutils import DEFAULT_MAX_LINES, TextTruncation, decode_output_bytes, output_page_bytes, truncate_text_tail


COMMAND_BUFFER_BYTES = 524_288
# Fraction of the per-stream budget frozen as the head segment. The head keeps
# the earliest output (command echo, first error) that a tail-only rolling
# buffer would lose first, mirroring the head+tail retention used by other
# agent runtimes.
COMMAND_HEAD_BUFFER_DIVISOR = 8
HARD_KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)


def _own_windows_process(process: subprocess.Popen[bytes], *, persistent: bool) -> None:
    if os.name == "nt":
        from .windows_job import WindowsJob

        try:
            process._coding_mcp_job = WindowsJob(process, persistent=persistent)  # type: ignore[attr-defined]
        except OSError:
            _force_terminate_windows_process_tree(process)
            raise


def hidden_process_popen_kwargs() -> dict[str, Any]:
    """Return Windows flags that keep background helper processes off-screen."""

    if os.name != "nt":
        return {}
    kwargs: dict[str, Any] = {}
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if creation_flags:
        kwargs["creationflags"] = creation_flags
    startupinfo = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
    startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
    startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    kwargs["startupinfo"] = startupinfo
    return kwargs


def _force_terminate_windows_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Terminate one exact Windows process tree, falling back to the parent."""

    taskkill = shutil.which("taskkill.exe") or shutil.which("taskkill")
    if taskkill:
        try:
            completed = subprocess.run(
                [taskkill, "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if completed.returncode == 0:
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                return
        except (OSError, subprocess.SubprocessError):
            pass
    process.kill()
    try:
        process.wait(timeout=1)
    except Exception:
        pass


def terminate_process_group(
    process: subprocess.Popen[bytes],
    signum: signal.Signals,
    *,
    force: bool = False,
) -> None:
    if os.name == "nt":
        if force:
            _force_terminate_windows_process_tree(process)
            return
        if not force:
            event = getattr(signal, "CTRL_BREAK_EVENT", None)
            if event is not None:
                try:
                    process.send_signal(event)
                    process.wait(timeout=1)
                    return
                except Exception:
                    pass
        _force_terminate_windows_process_tree(process)
        return
    if not hasattr(os, "killpg"):
        try:
            if force:
                process.kill()
            else:
                process.terminate()
            process.wait(timeout=1)
        except Exception:
            process.kill()
        return
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        return
    except Exception:
        process.terminate()
    # A reaped parent does not imply that its process group has drained.
    deadline = time.monotonic() + (0 if force else 1)
    while time.monotonic() < deadline:
        process.poll()
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(.02)
    else:
        try:
            os.killpg(process.pid, HARD_KILL_SIGNAL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()


def spawn_process(
    command: Any,
    *,
    cwd: str,
    shell: bool,
    env: dict[str, str],
    tty: bool,
    popen_kwargs: dict[str, Any],
    log_paths: dict[str, Path] | None = None,
) -> tuple[subprocess.Popen[bytes], int | None]:
    """Spawn a pipe-backed or true POSIX PTY-backed process."""

    if os.name == "nt":
        popen_kwargs = dict(popen_kwargs)
        # Assign ownership before the child can create any descendants.
        popen_kwargs["creationflags"] = int(popen_kwargs.get("creationflags", 0)) | 0x00000004
    if log_paths is not None:
        with log_paths["stdout"].open("ab") as stdout, log_paths["stderr"].open("ab") as stderr:
            process = subprocess.Popen(
                command, cwd=cwd, shell=shell, env=env,
                stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, **popen_kwargs,
            )
        _own_windows_process(process, persistent=True)
        process._coding_mcp_owns_group = True  # type: ignore[attr-defined]
        return process, None
    if not tty:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            shell=shell,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            **popen_kwargs,
        )
        _own_windows_process(process, persistent=False)
        process._coding_mcp_owns_group = bool(popen_kwargs.get("start_new_session")) or os.name == "nt"  # type: ignore[attr-defined]
        return process, None
    if os.name == "nt":
        raise ToolFailure(
            "TTY_UNSUPPORTED",
            "tty=true requires ConPTY support, which is not available in this build.",
            category="runtime",
            details={"platform": os.name, "retry_hint": "Run the command without tty=true."},
        )
    try:
        import pty

        master_fd, slave_fd = pty.openpty()
    except (ImportError, OSError) as exc:
        raise ToolFailure(
            "TTY_UNSUPPORTED",
            "A POSIX pseudo-terminal could not be created.",
            category="runtime",
        ) from exc
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            shell=shell,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            **popen_kwargs,
        )
    except Exception:
        os.close(master_fd)
        raise
    finally:
        os.close(slave_fd)
    process._coding_mcp_owns_group = bool(popen_kwargs.get("start_new_session"))  # type: ignore[attr-defined]
    return process, master_fd


@dataclass
class CommandRun:
    command_id: str
    process: subprocess.Popen[bytes]
    timeout_at: float | None = None
    warnings: list[str] = field(default_factory=list)
    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    stdout_head: bytearray = field(default_factory=bytearray)
    stderr_head: bytearray = field(default_factory=bytearray)
    stdout_start_offset: int = 0
    stderr_start_offset: int = 0
    stdout_cursor: int = 0
    stderr_cursor: int = 0
    stdout_total_bytes: int = 0
    stderr_total_bytes: int = 0
    stdout_dropped_bytes: int = 0
    stderr_dropped_bytes: int = 0
    buffer_limit: int = COMMAND_BUFFER_BYTES
    lock: threading.Lock = field(default_factory=threading.Lock)
    reader_threads: list[threading.Thread] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    closed: bool = False
    exit_code: int | None = None
    signal_name: str | None = None
    timed_out: bool = False
    terminating: bool = False
    pty_master_fd: int | None = None
    on_evict: Any = None
    _stdin_closed: bool = False
    _stdin_writer: StdinWriter = field(init=False, repr=False)
    output_changed: threading.Condition = field(init=False, repr=False)
    background: bool = False
    log_paths: dict[str, Path] | None = None
    _group_cleaned: bool = False
    _cleanup_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _windows_job: Any = field(init=False, repr=False)
    owns_process_group: bool = False

    def __post_init__(self) -> None:
        self.output_changed = threading.Condition(self.lock)
        self._windows_job = vars(self.process).get("_coding_mcp_job")
        self.owns_process_group = self.owns_process_group or vars(self.process).get("_coding_mcp_owns_group") is True
        self._stdin_writer = StdinWriter(self.process, self.pty_master_fd, self._abort_stdin_delivery)

    def _abort_stdin_delivery(self) -> None:
        try:
            self.terminate(HARD_KILL_SIGNAL, force=True)
        finally:
            if self.process.poll() is None:
                self.process.kill()

    def terminate(self, signum: signal.Signals, *, force: bool = False) -> None:
        # Own the process group even after its original parent exits. Cleanup
        # happens once, so a retained output handle cannot kill a reused PGID.
        with self._cleanup_lock:
            if self._group_cleaned:
                return
            if self._windows_job is not None:
                if not force and self.process.poll() is None:
                    try:
                        self.process.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signum))
                        self.process.wait(timeout=1)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                self._windows_job.terminate()
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.process.kill()
            elif self.owns_process_group:
                terminate_process_group(self.process, signum, force=force)
            elif self.process.poll() is None:
                if force:
                    self.process.kill()
                else:
                    self.process.terminate()
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.process.kill()
            if self.process.poll() is not None:
                self._group_cleaned = True
        with self.output_changed:
            self.output_changed.notify_all()

    def release_background(self) -> None:
        if self._windows_job is not None:
            self._windows_job.close()
            self._windows_job = None

    @property
    def head_buffer_limit(self) -> int:
        return self.buffer_limit // COMMAND_HEAD_BUFFER_DIVISOR

    @property
    def retained_bytes(self) -> int:
        with self.lock:
            stdout_head_unique = min(len(self.stdout_head), self.stdout_start_offset)
            stderr_head_unique = min(len(self.stderr_head), self.stderr_start_offset)
            return len(self.stdout) + len(self.stderr) + stdout_head_unique + stderr_head_unique

    def append_stdout(self, chunk: bytes) -> None:
        with self.lock:
            head_capacity = self.head_buffer_limit - len(self.stdout_head)
            if head_capacity > 0:
                self.stdout_head.extend(chunk[:head_capacity])
            self.stdout.extend(chunk)
            self.stdout_total_bytes += len(chunk)
            previous_start = self.stdout_start_offset
            dropped = _trim_buffer(
                self.stdout,
                total_bytes=self.stdout_total_bytes,
                start_offset_attr="stdout_start_offset",
                command=self,
            )
            self.stdout_dropped_bytes += dropped
            if dropped:
                self._report_eviction("stdout", previous_start, self.stdout_start_offset, len(self.stdout_head))
            self.output_changed.notify_all()

    def append_stderr(self, chunk: bytes) -> None:
        with self.lock:
            head_capacity = self.head_buffer_limit - len(self.stderr_head)
            if head_capacity > 0:
                self.stderr_head.extend(chunk[:head_capacity])
            self.stderr.extend(chunk)
            self.stderr_total_bytes += len(chunk)
            previous_start = self.stderr_start_offset
            dropped = _trim_buffer(
                self.stderr,
                total_bytes=self.stderr_total_bytes,
                start_offset_attr="stderr_start_offset",
                command=self,
            )
            self.stderr_dropped_bytes += dropped
            if dropped:
                self._report_eviction("stderr", previous_start, self.stderr_start_offset, len(self.stderr_head))
            self.output_changed.notify_all()

    def _report_eviction(self, stream: str, previous_start: int, new_start: int, head_len: int) -> None:
        if self.on_evict is None:
            return
        lost_bytes = max(0, new_start - max(previous_start, head_len))
        if lost_bytes:
            self.on_evict(stream, lost_bytes)

    def write_input(self, data: bytes) -> dict[str, Any]:
        return self._stdin_writer.write(data)

    def close_stdin(self) -> None:
        if self.pty_master_fd is not None or self._stdin_closed:
            return
        self._stdin_closed = True
        self._stdin_writer.close()

    def snapshot_since_cursor(self, max_output_bytes: int) -> dict[str, Any]:
        self.refresh_status()
        with self.lock:
            stdout_omitted = max(0, self.stdout_start_offset - self.stdout_cursor)
            stderr_omitted = max(0, self.stderr_start_offset - self.stderr_cursor)
            stdout_start = max(0, self.stdout_cursor - self.stdout_start_offset)
            stderr_start = max(0, self.stderr_cursor - self.stderr_start_offset)
            stdout_bytes = bytes(self.stdout[stdout_start:])
            stderr_bytes = bytes(self.stderr[stderr_start:])
            stdout_bytes, stdout_encoding = output_page_bytes(stdout_bytes, 0, len(stdout_bytes), final=self.closed)
            stderr_bytes, stderr_encoding = output_page_bytes(stderr_bytes, 0, len(stderr_bytes), final=self.closed)
            self.stdout_cursor = max(self.stdout_cursor, self.stdout_start_offset) + len(stdout_bytes)
            self.stderr_cursor = max(self.stderr_cursor, self.stderr_start_offset) + len(stderr_bytes)
        stdout_text = stdout_bytes.decode(stdout_encoding, errors="replace") if stdout_encoding != "unknown" else decode_output_bytes(stdout_bytes)
        stderr_text = stderr_bytes.decode(stderr_encoding, errors="replace") if stderr_encoding != "unknown" else decode_output_bytes(stderr_bytes)
        stdout_truncation = truncate_text_tail(stdout_text, max_bytes=max_output_bytes)
        stderr_truncation = truncate_text_tail(stderr_text, max_bytes=max_output_bytes)
        if self.timed_out:
            status = "timeout"
        elif self.terminating and self.process.poll() is None:
            status = "running"
        elif self.signal_name is not None:
            status = "terminated"
        else:
            status = "running" if self.process.poll() is None else "exited"
        payload: dict[str, Any] = {
            "command_id": self.command_id,
            "stdin_write": self._stdin_writer.snapshot(),
            "status": status,
            "exit_code": self.exit_code,
            "signal": self.signal_name,
            "timed_out": self.timed_out,
            "stdout": stdout_truncation.content,
            "stderr": stderr_truncation.content,
            "stdout_truncated": stdout_truncation.truncated,
            "stderr_truncated": stderr_truncation.truncated,
            "stdout_truncated_by": stdout_truncation.truncated_by,
            "stderr_truncated_by": stderr_truncation.truncated_by,
            "stdout_output_lines": stdout_truncation.output_lines,
            "stderr_output_lines": stderr_truncation.output_lines,
            "stdout_output_bytes": stdout_truncation.output_bytes,
            "stderr_output_bytes": stderr_truncation.output_bytes,
            "stdout_dropped_bytes": self.stdout_dropped_bytes,
            "stderr_dropped_bytes": self.stderr_dropped_bytes,
            "stdout_omitted_bytes": stdout_omitted,
            "stderr_omitted_bytes": stderr_omitted,
            "truncated": (
                stdout_truncation.truncated
                or stderr_truncation.truncated
                or stdout_omitted > 0
                or stderr_omitted > 0
            ),
            "ok": True,
        }
        if self.background:
            payload["background"] = True
            payload["pid"] = self.process.pid
            payload["log_paths"] = {name: str(path) for name, path in (self.log_paths or {}).items()}
        warnings: list[str] = list(self.warnings)
        if stdout_truncation.truncated:
            warnings.append(f"stdout truncated from tail by {stdout_truncation.truncated_by}")
        if stderr_truncation.truncated:
            warnings.append(f"stderr truncated from tail by {stderr_truncation.truncated_by}")
        if stdout_omitted > 0:
            warnings.append("stdout cursor skipped dropped bytes")
        if stderr_omitted > 0:
            warnings.append("stderr cursor skipped dropped bytes")
        if warnings:
            payload["warnings"] = warnings
        return payload

    def refresh_status(self) -> None:
        self._sync_logs()
        if self.timeout_at is not None and not self.timed_out and self.process.poll() is None and time.time() >= self.timeout_at:
            self.timed_out = True
            self.terminate(signal.SIGTERM)
            self.drain_readers()
        code = self.process.poll()
        if code is None:
            return
        if not self.background and self.owns_process_group and not self._group_cleaned:
            self.terminate(signal.SIGTERM)
        self.drain_readers()
        self.exit_code = code
        self.terminating = False
        if code < 0:
            values = {item.value for item in signal.Signals}
            self.signal_name = signal.Signals(-code).name if -code in values else str(-code)
        self.closed = True
        if self.completed_at is None:
            self.completed_at = time.time()
        with self.output_changed:
            self.output_changed.notify_all()

    def _sync_logs(self) -> None:
        """Read a bounded head/tail on demand; background services need no reader threads."""
        for name, path in (self.log_paths or {}).items():
            try:
                with path.open("rb") as stream:
                    size = os.fstat(stream.fileno()).st_size
                    with self.lock:
                        previous = getattr(self, f"{name}_total_bytes")
                    if size == previous:
                        continue
                    head = stream.read(min(size, self.head_buffer_limit))
                    tail_start = max(0, size - (self.buffer_limit - self.head_buffer_limit))
                    stream.seek(tail_start)
                    tail = stream.read(size - tail_start)
                with self.output_changed:
                    setattr(self, f"{name}_head", bytearray(head))
                    setattr(self, name, bytearray(tail))
                    setattr(self, f"{name}_total_bytes", size)
                    setattr(self, f"{name}_start_offset", tail_start)
                    setattr(self, f"{name}_dropped_bytes", tail_start)
                    self.output_changed.notify_all()
            except FileNotFoundError:
                continue

    def drain_readers(self, timeout: float = 0.2) -> None:
        deadline = time.time() + timeout
        for thread in list(self.reader_threads):
            remaining = max(0.0, deadline - time.time())
            if remaining <= 0:
                break
            thread.join(timeout=remaining)

    def retained_output_bytes(self) -> bytes:
        with self.lock:
            stdout = bytes(self.stdout)
            stderr = bytes(self.stderr)
        sections: list[bytes] = []
        if stdout:
            sections.extend([b"--- stdout ---\n", stdout])
        if stderr:
            if sections:
                sections.append(b"\n")
            sections.extend([b"--- stderr ---\n", stderr])
        return b"".join(sections)

    def retained_stream_segments(self, stream: str) -> tuple[bytes, bytes, int, int, int]:
        """Return (head, tail, tail_start_offset, total_bytes, tail_dropped_bytes).

        The retained set for a stream is the frozen head segment covering
        absolute offsets [0, len(head)) plus the rolling tail window covering
        [tail_start_offset, total_bytes). While the tail has not dropped
        anything the head is a duplicate prefix of the tail; once the tail
        rolls past the head, offsets between the two segments are evicted.
        """
        with self.lock:
            if stream == "stdout":
                return (
                    bytes(self.stdout_head),
                    bytes(self.stdout),
                    self.stdout_start_offset,
                    self.stdout_total_bytes,
                    self.stdout_dropped_bytes,
                )
            if stream == "stderr":
                return (
                    bytes(self.stderr_head),
                    bytes(self.stderr),
                    self.stderr_start_offset,
                    self.stderr_total_bytes,
                    self.stderr_dropped_bytes,
                )
        raise ValueError(f"Unknown output stream: {stream}")


def start_reader_threads(command: CommandRun) -> None:
    if command.background:
        return
    def reader(stream: BinaryIO, append: Any) -> None:
        try:
            while True:
                chunk = os.read(stream.fileno(), 4096)
                if not chunk:
                    break
                append(chunk)
        except (OSError, ValueError):
            return
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def pty_reader(fd: int) -> None:
        try:
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                command.append_stdout(chunk)
        except OSError:
            return
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            if command.pty_master_fd == fd:
                command.pty_master_fd = None

    if command.pty_master_fd is not None:
        thread = threading.Thread(target=pty_reader, args=(command.pty_master_fd,), daemon=True)
        command.reader_threads.append(thread)
        thread.start()
        return
    if command.process.stdout is not None:
        thread = threading.Thread(target=reader, args=(command.process.stdout, command.append_stdout), daemon=True)
        command.reader_threads.append(thread)
        thread.start()
    if command.process.stderr is not None:
        thread = threading.Thread(target=reader, args=(command.process.stderr, command.append_stderr), daemon=True)
        command.reader_threads.append(thread)
        thread.start()


def start_command_watchdog(command: CommandRun) -> None:
    if command.timeout_at is None:
        return

    def watchdog() -> None:
        delay = max(0.0, command.timeout_at - time.time()) if command.timeout_at is not None else 0.0
        try:
            command.process.wait(timeout=delay)
        except subprocess.TimeoutExpired:
            pass
        else:
            command.refresh_status()
            return
        if command.process.poll() is not None or command.timed_out:
            return
        command.timed_out = True
        command.terminate(signal.SIGTERM)
        command.refresh_status()

    threading.Thread(
        target=watchdog,
        name=f"coding-tools-watchdog-{command.command_id}",
        daemon=True,
    ).start()


def _trim_buffer(
    buffer: bytearray,
    *,
    total_bytes: int,
    start_offset_attr: str,
    command: CommandRun,
) -> int:
    tail_limit = command.buffer_limit - command.head_buffer_limit
    overflow = len(buffer) - tail_limit
    if overflow <= 0:
        return 0
    del buffer[:overflow]
    setattr(command, start_offset_attr, total_bytes - len(buffer))
    return overflow


def truncate_output_bytes_tail(data: bytes, max_bytes: int, max_lines: int = DEFAULT_MAX_LINES) -> TextTruncation:
    return truncate_text_tail(
        decode_output_bytes(data),
        max_lines=max_lines,
        max_bytes=max_bytes,
    )
