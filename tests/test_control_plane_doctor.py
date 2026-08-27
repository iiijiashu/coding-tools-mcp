from __future__ import annotations

import unittest

from scripts.control_plane_doctor import HTTP_TASK, TUNNEL_TASK, overall_status


class ControlPlaneDoctorTests(unittest.TestCase):
    def test_ready_requires_both_runtime_owners_and_all_health_layers(self) -> None:
        tasks = {HTTP_TASK: {"state": "Running"}, TUNNEL_TASK: {"state": "Running"}}
        self.assertEqual(overall_status(True, True, True, tasks), "READY")
        self.assertEqual(overall_status(True, True, False, tasks), "DEGRADED")
        self.assertEqual(overall_status(False, False, False, tasks), "FAILED")


if __name__ == "__main__":
    unittest.main()
