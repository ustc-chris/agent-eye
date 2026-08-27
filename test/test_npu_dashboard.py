from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

import npu_dashboard


class NpuDashboardTests(unittest.TestCase):
    def test_eye_output_ignores_banner_and_sorts_npus(self) -> None:
        output = """\
Welcome to server
2,PROCESSING,VLLM,null
0,FREE,null,null
1,PROCESSING,VLLM,z50064016
"""
        rows = npu_dashboard.parse_eye_output(output)
        self.assertEqual([row.npu_id for row in rows], [0, 1, 2])
        self.assertEqual(rows[0].display_status, "FREE")
        self.assertEqual(rows[1].display_status, "VLLM（z50064016）")
        self.assertEqual(rows[2].display_status, "VLLM（未知运行者）")

    @patch("npu_dashboard.subprocess.run")
    def test_query_machine_uses_noninteractive_ssh(self, mocked_run) -> None:
        mocked_run.return_value = subprocess.CompletedProcess(
            [], 0, "0,FREE,null,null\n", ""
        )
        machine = npu_dashboard.Machine("node-1", "root@10.0.0.1", "worker")
        result = npu_dashboard.query_machine(machine)
        arguments = mocked_run.call_args.args[0]
        self.assertEqual(arguments[0], "ssh")
        self.assertIn("BatchMode=yes", arguments)
        self.assertEqual(arguments[-2:], ["root@10.0.0.1", "eye npu_status"])
        self.assertIsNone(result.error)
        self.assertEqual(result.rows[0].status, "FREE")

    def test_dashboard_renders_machine_list_cards_and_refresh_footer(self) -> None:
        first = npu_dashboard.Machine("server-a", "10.0.0.1", "推理 A")
        second = npu_dashboard.Machine("server-b", "10.0.0.2", "推理 B")
        results = {
            first: npu_dashboard.MachineResult(
                rows=(npu_dashboard.NpuRow(0, "FREE", None, None),)
            ),
            second: npu_dashboard.MachineResult(error="连接失败"),
        }
        rendered = npu_dashboard.render_dashboard(
            (first, second),
            results,
            last_refresh=None,
            seconds_to_refresh=5,
            querying=False,
            terminal_width=100,
        )
        self.assertIn("server-a (推理 A)  [10.0.0.1]", rendered)
        self.assertIn("server-a (推理 A)     free: 1/1", rendered)
        self.assertIn("NPU 0", rendered)
        self.assertIn("FREE", rendered)
        self.assertIn("ERROR: 连接失败", rendered)
        self.assertIn("Last refresh: 等待首次查询", rendered)
        self.assertIn("Next refresh: 5.0s/20s", rendered)

    def test_narrow_terminal_reduces_configured_columns(self) -> None:
        machines = tuple(
            npu_dashboard.Machine(f"server-{index}", f"10.0.0.{index}")
            for index in range(3)
        )
        with patch.object(npu_dashboard, "DISPLAY_COLUMNS", 3):
            rendered = npu_dashboard.render_dashboard(
                machines,
                {},
                last_refresh=None,
                seconds_to_refresh=0,
                querying=True,
                terminal_width=40,
            )
        title_lines = [line for line in rendered.splitlines() if "server-" in line]
        self.assertGreaterEqual(len(title_lines), 3)

    def test_chinese_alias_keeps_card_borders_aligned(self) -> None:
        machine = npu_dashboard.Machine("server", "10.0.0.1", "中文节点")
        lines = npu_dashboard.render_machine(machine, None, 36, querying=True)
        self.assertTrue(all(npu_dashboard._display_width(line) == 36 for line in lines))

    def test_table_and_npu_rows_follow_availability_colors(self) -> None:
        machine = npu_dashboard.Machine("server", "10.0.0.1")
        cases = (
            (0, npu_dashboard._RED),
            (2, npu_dashboard._YELLOW),
            (4, npu_dashboard._GREEN),
        )
        for free_count, expected_table_color in cases:
            rows = tuple(
                npu_dashboard.NpuRow(index, "FREE", None, None)
                for index in range(free_count)
            ) + (npu_dashboard.NpuRow(7, "PROCESSING", "VLLM", None),)
            with patch("npu_dashboard._supports_color", return_value=True):
                rendered = "\n".join(
                    npu_dashboard.render_machine(
                        machine,
                        npu_dashboard.MachineResult(rows=rows),
                        44,
                        querying=False,
                    )
                )
            self.assertIn(expected_table_color, rendered)
            self.assertIn(npu_dashboard._RED, rendered)
            if free_count:
                self.assertIn(npu_dashboard._BLUE, rendered)


if __name__ == "__main__":
    unittest.main()
