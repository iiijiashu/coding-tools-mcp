from __future__ import annotations

import unittest

from scripts.local_http_mcp_watchdog import decide_action


class LocalHttpMcpWatchdogTests(unittest.TestCase):
    def test_decision_matrix(self) -> None:
        cases = (
            ("Disabled", False, "enable_start"),
            ("Ready", False, "start"),
            ("Running", True, "healthy"),
            ("Running", False, "restart"),
            ("Queued", False, "wait"),
        )
        for state, healthy, expected in cases:
            with self.subTest(state=state, healthy=healthy):
                self.assertEqual(decide_action(state, healthy), expected)


if __name__ == "__main__":
    unittest.main()
