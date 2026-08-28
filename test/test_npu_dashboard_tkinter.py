from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import npu_dashboard
import npu_dashboard_tkinter as gui


class DashboardConfigTests(unittest.TestCase):
    def test_config_round_trip_preserves_all_adjustable_values(self) -> None:
        config = gui.DashboardConfig.from_mapping(
            {
                "query_refresh_seconds": "12.5",
                "display_columns": "3",
                "machines": [
                    {"name": "node-1", "ip": "root@10.0.0.1", "alias": "推理 A"}
                ],
                "remote_eye_command": "/opt/bin/eye npu_status",
                "ssh_timeout_seconds": "8",
                "always_on_top": True,
                "theme_mode": "dark",
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            self.assertEqual(gui.save_config(config, path), path)
            self.assertEqual(gui.load_config(path), config)
            self.assertEqual(json.loads(path.read_text())["display_columns"], 3)
            self.assertTrue(json.loads(path.read_text())["always_on_top"])
            self.assertEqual(json.loads(path.read_text())["theme_mode"], "dark")

    def test_legacy_interface_refresh_setting_is_ignored_and_removed(self) -> None:
        values = gui.DashboardConfig.defaults().to_dict()
        values["interface_refresh_seconds"] = 99
        config = gui.DashboardConfig.from_mapping(values)
        self.assertNotIn("interface_refresh_seconds", config.to_dict())

    def test_legacy_config_defaults_to_not_always_on_top(self) -> None:
        values = gui.DashboardConfig.defaults().to_dict()
        del values["always_on_top"]
        self.assertFalse(gui.DashboardConfig.from_mapping(values).always_on_top)

    def test_legacy_config_defaults_to_automatic_theme(self) -> None:
        values = gui.DashboardConfig.defaults().to_dict()
        del values["theme_mode"]
        self.assertEqual(gui.DashboardConfig.from_mapping(values).theme_mode, "auto")

    def test_theme_button_cycles_auto_dark_light(self) -> None:
        self.assertEqual(gui.next_theme_mode("auto"), "dark")
        self.assertEqual(gui.next_theme_mode("dark"), "light")
        self.assertEqual(gui.next_theme_mode("light"), "auto")

    def test_invalid_theme_is_rejected(self) -> None:
        values = gui.DashboardConfig.defaults().to_dict()
        values["theme_mode"] = "blue"
        with self.assertRaisesRegex(ValueError, "主题"):
            gui.DashboardConfig.from_mapping(values)

    @patch("npu_dashboard_tkinter.platform.system", return_value="Darwin")
    @patch("npu_dashboard_tkinter.subprocess.run")
    def test_automatic_theme_reads_macos_dark_mode(
        self, mocked_run, _mocked_system
    ) -> None:
        mocked_run.return_value = subprocess.CompletedProcess([], 0, "Dark\n", "")
        self.assertTrue(gui.system_prefers_dark())
        self.assertEqual(gui.resolved_theme("auto"), "dark")

    def test_missing_config_uses_terminal_dashboard_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = gui.load_config(Path(directory) / "missing.json")
        self.assertEqual(config.query_refresh_seconds, npu_dashboard.QUERY_REFRESH_SECONDS)
        self.assertEqual(config.display_columns, npu_dashboard.DISPLAY_COLUMNS)

    def test_invalid_config_is_rejected(self) -> None:
        values = gui.DashboardConfig.defaults().to_dict()
        values["ssh_timeout_seconds"] = 0
        with self.assertRaisesRegex(ValueError, "SSH"):
            gui.DashboardConfig.from_mapping(values)

    @patch("npu_dashboard_tkinter.subprocess.run")
    def test_query_uses_saved_command_and_timeout(self, mocked_run) -> None:
        mocked_run.return_value = subprocess.CompletedProcess(
            [], 0, "0,FREE,null,null\n", ""
        )
        values = gui.DashboardConfig.defaults().to_dict()
        values.update(
            {
                "remote_eye_command": "/opt/eye npu_status",
                "ssh_timeout_seconds": 7.5,
                "machines": [{"name": "node", "ip": "user@host", "alias": ""}],
            }
        )
        config = gui.DashboardConfig.from_mapping(values)
        result = gui.query_machine(config.machines[0], config)
        arguments = mocked_run.call_args.args[0]
        self.assertEqual(arguments[-2:], ["user@host", "/opt/eye npu_status"])
        self.assertEqual(mocked_run.call_args.kwargs["timeout"], 7.5)
        self.assertIsNone(result.error)


if __name__ == "__main__":
    unittest.main()
