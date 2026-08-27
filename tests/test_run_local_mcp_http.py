from __future__ import annotations

import unittest

from scripts.run_local_mcp_http import classify_exception, redact_server_args


class RunLocalMcpHttpTests(unittest.TestCase):
    def test_sensitive_cli_values_are_never_logged(self) -> None:
        self.assertEqual(
            redact_server_args(
                [
                    "--workspace",
                    "D:/coding-tools-mcp",
                    "--auth-token",
                    "top-secret",
                    "--oauth-token-secret=hex-secret",
                ]
            ),
            [
                "--workspace",
                "D:/coding-tools-mcp",
                "--auth-token",
                "<redacted>",
                "--oauth-token-secret=<redacted>",
            ],
        )

    def test_port_conflict_inside_exception_group_is_classified(self) -> None:
        failure = OSError(10048, "address already in use")
        grouped = ExceptionGroup("startup failed", [failure])
        classified = classify_exception(grouped)
        self.assertEqual(classified["category"], "port_in_use")
        self.assertEqual(classified["phase"], "bind")
        self.assertIs(classified["retryable"], False)
        self.assertEqual(classified["cause_types"], ["OSError"])


if __name__ == "__main__":
    unittest.main()
