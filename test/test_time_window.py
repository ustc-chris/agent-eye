from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from unittest.mock import patch

from agent_eye import cli
from agent_eye.time_window import (
    AllowDecision,
    AllowSyntaxError,
    evaluate_allow,
    parse_allow,
)


class AllowParserTests(unittest.TestCase):
    def test_daily_ranges_apply_to_every_day(self) -> None:
        expression = "2100-2359,0000-0600"
        self.assertTrue(
            evaluate_allow(
                expression,
                moment=datetime(2026, 8, 24, 22, 30, tzinfo=timezone.utc),
            ).allowed
        )
        self.assertTrue(
            evaluate_allow(
                expression,
                moment=datetime(2026, 8, 25, 5, 59, tzinfo=timezone.utc),
            ).allowed
        )
        self.assertFalse(
            evaluate_allow(
                expression,
                moment=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
            ).allowed
        )

    def test_workday_and_weekends_are_fixed_weekday_groups(self) -> None:
        schedule = parse_allow("workday:0900-1800;weekends:1000-1200")
        monday = datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc)
        saturday = datetime(2026, 8, 29, 10, 30, tzinfo=timezone.utc)
        self.assertTrue(schedule.permits(monday))
        self.assertTrue(schedule.permits(saturday))
        self.assertFalse(schedule.permits(saturday.replace(hour=9)))

    def test_overlapping_day_rules_form_a_union(self) -> None:
        schedule = parse_allow("workday:0900-1800;mon:2000-2200")
        monday_evening = datetime(2026, 8, 24, 21, 0, tzinfo=timezone.utc)
        tuesday_evening = datetime(2026, 8, 25, 21, 0, tzinfo=timezone.utc)
        self.assertTrue(schedule.permits(monday_evening))
        self.assertFalse(schedule.permits(tuesday_evening))

    def test_range_boundaries_are_inclusive(self) -> None:
        schedule = parse_allow("mon:1500-2100")
        self.assertTrue(
            schedule.permits(datetime(2026, 8, 24, 15, 0, tzinfo=timezone.utc))
        )
        self.assertTrue(
            schedule.permits(datetime(2026, 8, 24, 21, 0, tzinfo=timezone.utc))
        )

    def test_cross_midnight_and_old_plus_syntax_are_rejected(self) -> None:
        with self.assertRaisesRegex(AllowSyntaxError, "crosses midnight"):
            parse_allow("2200-0600")
        with self.assertRaisesRegex(AllowSyntaxError, "use ','"):
            parse_allow("mon:0900-1200+1300-1800")


class AllowCliTests(unittest.TestCase):
    @patch("agent_eye.cli.npu_status.run")
    @patch("agent_eye.cli.evaluate_allow")
    def test_denied_query_does_not_call_npu_status(
        self, mocked_evaluate, mocked_npu_status
    ) -> None:
        mocked_evaluate.return_value = AllowDecision(False, "mon:0800", "UTC")
        output = StringIO()
        with redirect_stdout(output):
            status = cli.main(["npu_status", "--allow", "mon:0900-1000"])
        self.assertEqual(status, 0)
        self.assertIn("SKIPPED reason=outside_allow", output.getvalue())
        mocked_npu_status.assert_not_called()

    @patch("agent_eye.cli.run_file")
    @patch("agent_eye.cli.evaluate_allow")
    def test_denied_from_file_is_not_opened(
        self, mocked_evaluate, mocked_run_file
    ) -> None:
        mocked_evaluate.return_value = AllowDecision(False, "sun:0800", "UTC")
        status = cli.main(
            ["--allow", "workday:0900-1800", "from_file", "missing.json"]
        )
        self.assertEqual(status, 0)
        mocked_run_file.assert_not_called()

    @patch("agent_eye.cli.run_test_directory", return_value=0)
    def test_test_command_ignores_allow_even_when_invalid(self, mocked_test) -> None:
        status = cli.main(["--allow", "not-a-window", "test"])
        self.assertEqual(status, 0)
        mocked_test.assert_called_once()

    @patch("agent_eye.cli.npu_status.run")
    def test_invalid_allow_fails_before_query(self, mocked_npu_status) -> None:
        status = cli.main(["npu_status", "--allow", "mon:2200-0600"])
        self.assertEqual(status, 2)
        mocked_npu_status.assert_not_called()

    @patch("agent_eye.cli.kill_task")
    @patch("agent_eye.cli.evaluate_allow")
    def test_denied_kill_does_not_query_or_signal(
        self, mocked_evaluate, mocked_kill
    ) -> None:
        mocked_evaluate.return_value = AllowDecision(False, "mon:0800", "UTC")
        status = cli.main(["kill", "--tag", "worker", "--allow", "mon:0900-1000"])
        self.assertEqual(status, 0)
        mocked_kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
