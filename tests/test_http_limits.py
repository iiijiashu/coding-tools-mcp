from __future__ import annotations

import http.client
import json
import socket
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from coding_tools_mcp.server import MCPHandler, Runtime, RuntimeHTTPServer


class QuietHandler(MCPHandler):
    def log_message(self, *args):
        pass


@contextmanager
def running_server(*, handler=QuietHandler, timeout=0.15, slots=2):
    with TemporaryDirectory() as temporary:
        runtime = Runtime(Path(temporary), auth_token="local-test", transport="http")
        server = RuntimeHTTPServer(
            ("127.0.0.1", 0),
            handler,
            runtime,
            max_active_requests=slots,
            request_read_timeout=timeout,
        )
        thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.02), daemon=True
        )
        thread.start()
        try:
            yield server.server_address
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


class HttpLimitTests(unittest.TestCase):
    def test_partial_headers_and_bodies_expire(self) -> None:
        requests = [
            b"POST /mcp HTTP/1.1\r\nHost: localhost\r\n",
            b"POST /mcp HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer local-test\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{",
        ]
        for request in requests:
            with (
                running_server() as address,
                socket.create_connection(address, timeout=2) as conn,
            ):
                conn.sendall(request)
                self.assertEqual(conn.recv(1024), b"")

    def test_slow_body_trickle_does_not_reset_absolute_deadline(self) -> None:
        with (
            running_server(timeout=0.2) as address,
            socket.create_connection(address, timeout=2) as conn,
        ):
            conn.sendall(
                b"POST /mcp HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer local-test\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{"
            )
            for _ in range(3):
                time.sleep(0.05)
                conn.sendall(b" ")
            self.assertEqual(conn.recv(1024), b"")

    def test_saturation_returns_503_and_releases_slot(self) -> None:
        entered = threading.Event()

        class Handler(QuietHandler):
            def do_POST(self):
                entered.set()
                super().do_POST()

        with running_server(handler=Handler, timeout=1, slots=1) as address:
            with socket.create_connection(address, timeout=2) as first:
                first.sendall(
                    b"POST /mcp HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer local-test\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{"
                )
                self.assertTrue(entered.wait(1))
                with socket.create_connection(address, timeout=2) as second:
                    second.sendall(b"GET /healthz HTTP/1.0\r\n\r\n")
                    self.assertIn(b"503", second.recv(1024))
            deadline = time.monotonic() + 2
            while True:
                client = http.client.HTTPConnection(*address, timeout=2)
                client.request("GET", "/healthz")
                response = client.getresponse()
                status = response.status
                response.read()
                client.close()
                if status == 200 or time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
            self.assertEqual(status, 200)

    def test_complete_body_cancels_read_deadline_before_tool_execution(self) -> None:
        class Handler(QuietHandler):
            def handle_rpc(self, *args, **kwargs):
                time.sleep(0.25)
                return super().handle_rpc(*args, **kwargs)

        with running_server(handler=Handler, timeout=0.1) as address:
            client = http.client.HTTPConnection(*address, timeout=2)
            try:
                client.request(
                    "POST",
                    "/mcp",
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                    {
                        "Authorization": "Bearer local-test",
                        "Content-Type": "application/json",
                    },
                )
                response = client.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())["result"], {})
            finally:
                client.close()


if __name__ == "__main__":
    unittest.main()
