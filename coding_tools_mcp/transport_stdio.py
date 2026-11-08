from __future__ import annotations

import io
import json
import sys
from typing import Any, BinaryIO, Protocol, TextIO

from .protocol import (
    RequestContext,
    dispatch_rpc,
    invalid_request_response,
    jsonrpc_error,
    response_id,
)
from .telemetry import SessionTelemetry


class StdioRuntime(Protocol):
    telemetry: SessionTelemetry

    def initialize(
        self,
        client_info: dict[str, Any] | None = None,
        protocol_version: str = ...,
    ) -> dict[str, Any]: ...

    def initialize_result(self, protocol_version: str = ...) -> dict[str, Any]: ...

    def discover_payload(self) -> dict[str, Any]: ...

    def server_identity(self) -> dict[str, Any]: ...

    def list_tools(self) -> dict[str, Any]: ...

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        context: RequestContext | None = None,
    ) -> dict[str, Any]: ...

    def close(self) -> None: ...


def serve_stdio(
    runtime: StdioRuntime,
    *,
    input_stream: TextIO | BinaryIO | None = None,
    output_stream: TextIO | BinaryIO | None = None,
) -> int:
    # MCP stdio is UTF-8 regardless of the Windows console code page.  Use the
    # underlying binary streams by default so Python's locale-selected text
    # wrappers (commonly cp936/GBK) cannot corrupt or reject JSON-RPC frames.
    source: TextIO | BinaryIO = input_stream if input_stream is not None else sys.stdin.buffer
    sink: TextIO | BinaryIO = output_stream if output_stream is not None else sys.stdout.buffer
    binary_sink = not isinstance(sink, io.TextIOBase)
    try:
        for raw_line in source:
            if isinstance(raw_line, bytes):
                try:
                    line = raw_line.decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    response = jsonrpc_error(None, -32700, "Parse error")
                    payload = json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n"
                    sink.write(payload.encode("utf-8") if binary_sink else payload)  # type: ignore[arg-type]
                    sink.flush()
                    continue
            else:
                line = raw_line
            if not line.strip():
                continue
            try:
                request = json.loads(line)
            except (json.JSONDecodeError, RecursionError):
                # RecursionError included: a deeply nested document is a
                # document this server cannot parse, not a reason to end the
                # session.
                response = jsonrpc_error(None, -32700, "Parse error")
            else:
                try:
                    response = (
                        dispatch_rpc(runtime, request)
                        if isinstance(request, dict)
                        else invalid_request_response()
                    )
                except Exception as exc:  # noqa: BLE001 - keep the stdio server alive
                    if isinstance(request, dict) and "id" not in request:
                        # A notification is answered with nothing, however
                        # badly its handling went.
                        continue
                    response = jsonrpc_error(
                        response_id(request) if isinstance(request, dict) else None,
                        -32603,
                        str(exc),
                    )
            if response is not None:
                payload = json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n"
                sink.write(payload.encode("utf-8") if binary_sink else payload)  # type: ignore[arg-type]
                sink.flush()
    finally:
        runtime.close()
    return 0
