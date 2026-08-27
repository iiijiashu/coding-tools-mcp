from __future__ import annotations

import json
import io
import socket
import threading
import unittest
import urllib.request
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from coding_tools_mcp.server import (
    HTTPInstanceConflict,
    HTTPInstanceLease,
    MCPHandler,
    Runtime,
    RuntimeHTTPServer,
    build_parser,
    run_http,
)


class HttpRuntimeIdentityTests(unittest.TestCase):
    def test_occupied_port_is_classified_and_releases_the_instance_lease(self) -> None:
        with TemporaryDirectory() as temporary, socket.socket() as blocker:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            port = int(blocker.getsockname()[1])
            args = build_parser().parse_args(
                ["--workspace", temporary, "--host", "127.0.0.1", "--port", str(port)]
            )
            errors = io.StringIO()
            state_root = Path(temporary) / "state"
            real_lease = HTTPInstanceLease(
                "127.0.0.1", port, Path(temporary), state_root=state_root
            )
            with (
                patch("coding_tools_mcp.server.HTTPInstanceLease", return_value=real_lease),
                redirect_stderr(errors),
            ):
                self.assertEqual(run_http(args), 98)
            event = json.loads(errors.getvalue().splitlines()[-1])
            self.assertEqual(event["category"], "port_in_use")
            self.assertEqual(event["phase"], "bind")
            self.assertIs(event["retryable"], False)

            lease = HTTPInstanceLease("127.0.0.1", port, Path(temporary), state_root=state_root)
            lease.acquire()
            lease.release()

    def test_instance_lease_is_single_owner_and_stale_safe(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = HTTPInstanceLease("127.0.0.1", 18765, root, state_root=root / "state")
            second = HTTPInstanceLease("127.0.0.1", 18765, root, state_root=root / "state")
            first.acquire()
            try:
                with self.assertRaises(HTTPInstanceConflict) as raised:
                    second.acquire()
                self.assertEqual(raised.exception.owner["pid"], first.pid)
                self.assertEqual(raised.exception.owner["workspace"], str(root.resolve()))
                self.assertEqual(raised.exception.owner["generation"], first.generation)
            finally:
                first.release()

            # A lock file may persist as an inode, but no stale kernel lock may
            # block the next generation after the owner releases it.
            second.acquire()
            second.release()

    def test_health_and_readiness_expose_the_live_instance_without_secrets(self) -> None:
        with TemporaryDirectory() as temporary:
            runtime = Runtime(Path(temporary), enable_computer_use=True, permission_mode="trusted")
            server = RuntimeHTTPServer(("127.0.0.1", 0), MCPHandler, runtime)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = int(server.server_address[1])
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
                    health = json.load(response)
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=2) as response:
                    ready = json.load(response)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

            self.assertEqual(health["state"], "live")
            self.assertEqual(health["server_instance_id"], runtime.server_instance_id)
            self.assertEqual(ready["state"], "ready")
            self.assertEqual(ready["workspace"], str(runtime.workspace.root))
            self.assertEqual(ready["catalog_version"], runtime.catalog_version)
            self.assertEqual(ready["tool_count"], 22)
            self.assertNotIn("auth_token", json.dumps({"health": health, "ready": ready}))


if __name__ == "__main__":
    unittest.main()
