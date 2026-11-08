from __future__ import annotations

import io
import json
import unittest
from typing import Any

from coding_tools_mcp.transport_stdio import serve_stdio


class _Telemetry:
    def record_request(self, _era: str, _method: str) -> None:
        pass


class _Runtime:
    telemetry = _Telemetry()

    def __init__(self) -> None:
        self.closed = False

    def initialize(self, _client_info: dict[str, Any] | None = None, _protocol_version: str = "") -> dict[str, Any]:
        return {"capabilities": {}, "serverInfo": {"name": "拾光", "version": "1"}}

    def server_identity(self) -> dict[str, Any]:
        return {"name": "拾光", "version": "1"}

    def close(self) -> None:
        self.closed = True


class StdioUtf8Tests(unittest.TestCase):
    def test_binary_stdio_round_trips_utf8_independently_of_locale(self) -> None:
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "拾光客户端", "version": "1"},
            },
        }
        source = io.BytesIO((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
        sink = io.BytesIO()
        runtime = _Runtime()

        self.assertEqual(serve_stdio(runtime, input_stream=source, output_stream=sink), 0)

        response = json.loads(sink.getvalue().decode("utf-8"))
        self.assertEqual(response["result"]["serverInfo"]["name"], "拾光")
        self.assertTrue(runtime.closed)

    def test_invalid_utf8_frame_does_not_end_following_request(self) -> None:
        valid_request = b'{"jsonrpc":"2.0","id":2,"method":"ping"}\n'
        source = io.BytesIO(b'{"bad":"\xff"}\n' + valid_request)
        sink = io.BytesIO()
        runtime = _Runtime()

        self.assertEqual(serve_stdio(runtime, input_stream=source, output_stream=sink), 0)

        responses = [json.loads(line) for line in sink.getvalue().decode("utf-8").splitlines()]
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertEqual(responses[1], {"jsonrpc": "2.0", "id": 2, "result": {}})
        self.assertTrue(runtime.closed)


if __name__ == "__main__":
    unittest.main()
