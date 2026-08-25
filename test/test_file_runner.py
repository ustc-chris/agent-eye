from __future__ import annotations

import os
import unittest
from io import StringIO
from unittest.mock import patch

from agent_eye.file_runner import TestOutcome, TestReporter


class _TerminalBuffer(StringIO):
    def isatty(self) -> bool:
        return True


class TestReporterTests(unittest.TestCase):
    def test_interactive_output_refreshes_and_colors_only_final_issues(self) -> None:
        output = _TerminalBuffer()
        with patch.dict(os.environ, {}, clear=True):
            reporter = TestReporter(3, output)
            reporter.start("successful item")
            reporter.add(TestOutcome("successful item", "passed"))
            reporter.add(TestOutcome("skipped item", "skipped", "not available"))
            reporter.add(TestOutcome("failed item", "failed", "exit=1"))
            status = reporter.finish()

        rendered = output.getvalue()
        self.assertEqual(status, 1)
        self.assertIn("\r\033[2K", rendered)
        self.assertIn("\033[31m", rendered)
        self.assertNotIn("  ✓ successful item", rendered)
        self.assertIn("skipped item", rendered)
        self.assertIn("failed item", rendered)

    def test_redirected_output_has_no_color_or_progress_noise(self) -> None:
        output = StringIO()
        reporter = TestReporter(1, output)
        reporter.start("successful item")
        reporter.add(TestOutcome("successful item", "passed"))
        status = reporter.finish()

        rendered = output.getvalue()
        self.assertEqual(status, 0)
        self.assertNotIn("\033[", rendered)
        self.assertNotIn("successful item", rendered)
        self.assertTrue(rendered.endswith("\n\n"))
