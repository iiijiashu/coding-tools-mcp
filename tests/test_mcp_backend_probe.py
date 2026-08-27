from __future__ import annotations

import io
import unittest
import urllib.error
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts import mcp_backend_probe as probe_module


class McpBackendProbeTests(unittest.TestCase):
    def probe(self) -> probe_module.BackendProbe:
        return probe_module.probe_mcp_backend(
            "http://127.0.0.1:18765/mcp",
            expected_workspace="D:/coding-tools-mcp",
            required_tools=("server_info",),
            timeout=1,
        )

    def test_connection_refused_is_structured_and_retryable(self) -> None:
        failure = urllib.error.URLError(ConnectionRefusedError(10061, "refused"))
        with patch.object(probe_module.urllib.request, "urlopen", side_effect=failure):
            result = self.probe()
        self.assertEqual(result.category, "transport")
        self.assertEqual(result.phase, "connect")
        self.assertEqual(result.cause_type, "ConnectionRefusedError")
        self.assertEqual(result.cause_message, "connection refused")
        self.assertIs(result.retryable, True)
        self.assertNotIn("refused", result.recovery_hint or "")

    def test_authentication_failure_is_not_flattened_to_unknown(self) -> None:
        failure = urllib.error.HTTPError(
            "http://127.0.0.1:18765/mcp", 401, "Unauthorized", {}, io.BytesIO(b"{}")
        )
        with patch.object(probe_module.urllib.request, "urlopen", side_effect=failure):
            result = self.probe()
        self.assertEqual(result.category, "authentication")
        self.assertEqual(result.cause_message, "HTTP 401")
        self.assertIs(result.retryable, False)

    def test_502_is_retryable_upstream_http_failure(self) -> None:
        failure = urllib.error.HTTPError(
            "http://127.0.0.1:18765/mcp", 502, "Bad Gateway", {}, io.BytesIO(b"{}")
        )
        with patch.object(probe_module.urllib.request, "urlopen", side_effect=failure):
            result = self.probe()
        self.assertEqual(result.category, "upstream_http")
        self.assertEqual(result.backend_state, "upstream_error")
        self.assertIs(result.retryable, True)

    def test_timeout_is_transport_failure_with_phase(self) -> None:
        with patch.object(probe_module.urllib.request, "urlopen", side_effect=TimeoutError()):
            result = self.probe()
        self.assertEqual(result.category, "transport")
        self.assertEqual(result.cause_type, "TimeoutError")
        self.assertEqual(result.cause_message, "connection timed out")

    def test_wrong_workspace_is_explicit_identity_failure(self) -> None:
        with TemporaryDirectory() as temporary:
            expected = Path(temporary) / "expected"
            wrong = Path(temporary) / "wrong"
            expected.mkdir()
            wrong.mkdir()
            replies = [
                {"protocolVersion": "2025-11-25"},
                {"tools": [{"name": "server_info"}]},
                {"structuredContent": {"workspace": str(wrong)}},
            ]
            with (
                patch.object(probe_module, "_request", side_effect=replies),
                patch.object(probe_module, "_post", return_value=None),
            ):
                result = probe_module.probe_mcp_backend(
                    "http://127.0.0.1:18765/mcp",
                    expected_workspace=str(expected),
                    required_tools=("server_info",),
                    timeout=1,
                )
        self.assertEqual(result.category, "identity")
        self.assertEqual(result.backend_state, "wrong_workspace")
        self.assertIs(result.retryable, False)


if __name__ == "__main__":
    unittest.main()
