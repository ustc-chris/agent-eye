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
                "interface_refresh_seconds": "0.25",
                "display_columns": "3",
                "machines": [
                    {"name": "node-1", "ip": "root@10.0.0.1", "alias": "推理 A"}
                ],
                "remote_eye_command": "/opt/bin/eye npu_status",
                "ssh_timeout_seconds": "8",
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            self.assertEqual(gui.save_config(config, path), path)
            self.assertEqual(gui.load_config(path), config)
            self.assertEqual(json.loads(path.read_text())["display_columns"], 3)

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
