from __future__ import annotations

import unittest

from scripts.switch_scheduled_tasks_once import switch_tasks


class SwitchScheduledTasksOnceTests(unittest.TestCase):
    def test_target_is_ready_before_source_is_stopped_or_disabled(self) -> None:
        events: list[tuple[str, ...] | str] = []

        def run_task_command(*args: str) -> int:
            events.append(args)
            return 0

        def wait_until_ready() -> bool:
            events.append("target_ready")
            return True

        result = switch_tasks(
            from_task="\\control",
            to_task="\\project",
            enable_to=True,
            disable_from=True,
            run_task_command=run_task_command,
            wait_until_ready=wait_until_ready,
        )

        self.assertEqual(result, 0)
        self.assertEqual(
            events,
            [
                ("/change", "/tn", "\\project", "/enable"),
                ("/run", "/tn", "\\project"),
                "target_ready",
                ("/end", "/tn", "\\control"),
                ("/change", "/tn", "\\control", "/disable"),
            ],
        )

    def test_failed_target_start_never_touches_source(self) -> None:
        events: list[tuple[str, ...]] = []

        def run_task_command(*args: str) -> int:
            events.append(args)
            return 1 if args[:2] == ("/run", "/tn") else 0

        result = switch_tasks(
            from_task="\\control",
            to_task="\\project",
            enable_to=True,
            disable_from=True,
            run_task_command=run_task_command,
            wait_until_ready=lambda: True,
        )

        self.assertEqual(result, 1)
        self.assertNotIn(("/end", "/tn", "\\control"), events)
        self.assertNotIn(("/change", "/tn", "\\control", "/disable"), events)

    def test_unready_target_is_ended_but_source_remains_untouched(self) -> None:
        events: list[tuple[str, ...]] = []

        def run_task_command(*args: str) -> int:
            events.append(args)
            return 0

        result = switch_tasks(
            from_task="\\control",
            to_task="\\project",
            enable_to=True,
            disable_from=True,
            run_task_command=run_task_command,
            wait_until_ready=lambda: False,
        )

        self.assertEqual(result, 1)
        self.assertIn(("/end", "/tn", "\\project"), events)
        self.assertNotIn(("/end", "/tn", "\\control"), events)
        self.assertNotIn(("/change", "/tn", "\\control", "/disable"), events)


if __name__ == "__main__":
    unittest.main()
