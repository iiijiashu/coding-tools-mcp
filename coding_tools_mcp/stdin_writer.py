"""Bounded stdin admission with observable, non-replayable delivery receipts."""

from __future__ import annotations

import os
import secrets
import subprocess
import threading
from typing import Any
from collections.abc import Callable

from .errors import ToolFailure

MAX_STDIN_WRITE_BYTES = 1_048_576
STDIN_SYNC_WAIT_SECONDS = 0.05
STDIN_DELIVERY_TIMEOUT_SECONDS = 5.0
_WRITER_SLOTS = threading.BoundedSemaphore(32)


class StdinWriter:
    def __init__(
        self,
        process: subprocess.Popen[bytes],
        pty_fd: int | None,
        abort: Callable[[], None],
    ) -> None:
        self.process = process
        self.pty_fd = pty_fd
        self.lock = threading.Lock()
        self.closed = False
        self.receipt: dict[str, Any] | None = None
        self.abort = abort
        self.delivery_timeout = STDIN_DELIVERY_TIMEOUT_SECONDS

    def snapshot(self) -> dict[str, Any] | None:
        with self.lock:
            return dict(self.receipt) if self.receipt else None

    def write(self, data: bytes) -> dict[str, Any]:
        if len(data) > MAX_STDIN_WRITE_BYTES:
            raise ToolFailure(
                "STDIN_TOO_LARGE",
                "Input exceeds the per-write byte limit.",
                category="validation",
                details={"max_bytes": MAX_STDIN_WRITE_BYTES},
            )
        done = threading.Event()
        with self.lock:
            if self.closed:
                raise ToolFailure(
                    "COMMAND_CLOSED", "Command stdin is closed.", category="runtime"
                )
            if self.receipt and self.receipt["status"] == "pending":
                raise ToolFailure(
                    "STDIN_BUSY",
                    "Previous input is still being delivered; poll the command before submitting more input.",
                    category="runtime",
                    details=dict(self.receipt),
                )
            stream = getattr(self.process, "stdin", None)
            if self.pty_fd is None and (stream is None or stream.closed):
                raise ToolFailure(
                    "COMMAND_CLOSED", "Command stdin is closed.", category="runtime"
                )
            fd = self.pty_fd if self.pty_fd is not None else stream.fileno()
            if not _WRITER_SLOTS.acquire(blocking=False):
                raise ToolFailure(
                    "STDIN_BUSY",
                    "The bounded stdin delivery pool is full; no input was accepted.",
                    category="runtime",
                )
            receipt = {
                "write_id": secrets.token_urlsafe(12),
                "status": "pending",
                "accepted_bytes": len(data),
                "written_bytes": 0,
            }
            self.receipt = receipt
            thread = threading.Thread(
                target=self._deliver, args=(fd, data, receipt, done), daemon=True
            )
            try:
                thread.start()
            except RuntimeError:
                self.receipt = None
                _WRITER_SLOTS.release()
                raise
        done.wait(STDIN_SYNC_WAIT_SECONDS)
        with self.lock:
            receipt = dict(receipt)
        if receipt["status"] == "failed":
            raise ToolFailure(
                "COMMAND_CLOSED",
                "Command stdin closed during delivery; do not replay accepted input.",
                category="runtime",
                details=receipt,
            )
        return receipt

    def _deliver(
        self, fd: int, data: bytes, receipt: dict[str, Any], done: threading.Event
    ) -> None:
        offset = 0
        error: str | None = None

        def expire() -> None:
            with self.lock:
                if receipt["status"] != "pending":
                    return
                receipt["error"] = "delivery_timeout"
            self.abort()

        timer = threading.Timer(self.delivery_timeout, expire)
        timer.daemon = True
        try:
            timer.start()
            while offset < len(data):
                count = os.write(fd, data[offset : offset + 4096])
                if count <= 0:
                    raise OSError("stdin write made no progress")
                offset += count
                with self.lock:
                    receipt["written_bytes"] = offset
        except (OSError, ValueError, RuntimeError) as exc:
            error = type(exc).__name__
        finally:
            timer.cancel()
            with self.lock:
                error = receipt.get("error") or error
                receipt["status"] = "failed" if error else "written"
                if error:
                    receipt["error"] = error
                if self.closed:
                    self._close_pipe()
            _WRITER_SLOTS.release()
            done.set()

    def _close_pipe(self) -> None:
        stream = getattr(self.process, "stdin", None)
        if self.pty_fd is None and stream is not None:
            try:
                stream.close()
            except OSError:
                pass

    def close(self) -> None:
        with self.lock:
            self.closed = True
            if not self.receipt or self.receipt["status"] != "pending":
                self._close_pipe()
