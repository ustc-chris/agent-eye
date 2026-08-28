from __future__ import annotations

import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

from agent_eye.config import npu_status


NPU_SMI_OUTPUT = """\
| NPU ID | Name        | Health | Power(W) |
| 0      | Ascend950PR | OK     | 276.0    |
| 1      | Ascend950PR | OK     | 280.3    |
| 2      | Ascend950PR | OK     | 282.3    |
| NPU ID | Process id | Process name  | Process memory(MB) |
| 0      | 1648703    | VLLMWorker    | 316                |
| 1      | 1649911    | VLLMWorker_TP | 119745             |
| No running processes found in NPU 2                  |
"""


class NpuStatusTests(unittest.TestCase):
    def test_real_table_shape_is_parsed_into_devices(self) -> None:
        devices = npu_status.parse_npu_smi(NPU_SMI_OUTPUT)
        self.assertEqual([item.npu_id for item in devices], [0, 1, 2])
        self.assertEqual(devices[0].processes[0].pid, 1648703)
        self.assertEqual(devices[1].processes[0].name, "VLLMWorker_TP")
        self.assertEqual(devices[2].processes, ())

    def test_pasted_escaped_table_borders_are_accepted(self) -> None:
        output = """\
\\| 0 | Ascend950PR | OK |
\\| NPU ID | Process id | Process name | Process memory(MB) |
\\| 0 | 123 | VLLMWorker\\_TP | 316 |
"""
        devices = npu_status.parse_npu_smi(output)
        self.assertEqual(
            devices[0].processes,
            (npu_status.NpuProcess(123, "VLLMWorker_TP"),),
        )

    @patch("agent_eye.config.npu_status.find_owner_id", return_value=None)
    def test_non_vllm_process_keeps_its_real_name(self, _mocked_owner) -> None:
        statuses = npu_status.build_statuses(
            [
                npu_status.NpuInfo(
                    7,
                    (npu_status.NpuProcess(3537849, "msmodelslim"),),
                )
            ]
        )
        self.assertEqual(
            statuses[0].format(),
            "7,PROCESSING,msmodelslim,null",
        )

    @patch("agent_eye.config.npu_status._query_working_directory", return_value=None)
    @patch("agent_eye.config.npu_status.subprocess.run")
    def test_run_formats_free_owned_and_unowned_npus(
        self, mocked_run, _mocked_pwdx
    ) -> None:
        responses = {
            1648703: (0, "100 /usr/bin/python worker.py\n"),
            100: (0, "1 bash /srv/z50064016/start.sh\n"),
            1649911: (0, "200 /usr/bin/python worker.py\n"),
            200: (0, "0 /usr/bin/launcher\n"),
        }

        def execute(arguments, **_kwargs):
            if arguments == ["npu-smi", "info"]:
                return subprocess.CompletedProcess(arguments, 0, NPU_SMI_OUTPUT, "")
            pid = int(arguments[-1])
            returncode, stdout = responses[pid]
            return subprocess.CompletedProcess(arguments, returncode, stdout, "")

        mocked_run.side_effect = execute
        output = StringIO()
        with redirect_stdout(output):
            status = npu_status.run()

        self.assertEqual(status, 0)
        self.assertEqual(
            output.getvalue().splitlines(),
            [
                "0,PROCESSING,VLLMWorker,z50064016",
                "1,PROCESSING,VLLMWorker_TP,null",
                "2,FREE,null,null",
            ],
        )

    @patch("agent_eye.config.npu_status._query_process")
    @patch("agent_eye.config.npu_status._query_working_directory", return_value=None)
    def test_owner_pattern_requires_exact_slash_boundaries(
        self, _mocked_pwdx, mocked_query
    ) -> None:
        mocked_query.side_effect = [
            (20, "python /srv/zz50064016/job.py"),
            (30, "bash /srv/z5006401/job.sh"),
            (0, "bash /srv/z12345678/job.sh"),
        ]
        self.assertEqual(npu_status.find_owner_id(10), "z12345678")

    @patch("agent_eye.config.npu_status._query_process")
    @patch(
        "agent_eye.config.npu_status._query_working_directory",
        return_value="/home/z50064016",
    )
    def test_owner_at_working_directory_end_is_accepted(
        self, _mocked_pwdx, mocked_process
    ) -> None:
        self.assertEqual(npu_status.find_owner_id(123), "z50064016")
        mocked_process.assert_not_called()

    @patch("agent_eye.config.npu_status._query_process")
    @patch("agent_eye.config.npu_status._query_working_directory", return_value=None)
    def test_owner_at_command_end_is_accepted(
        self, _mocked_pwdx, mocked_query
    ) -> None:
        mocked_query.return_value = (0, "python --workspace /home/z50064016")
        self.assertEqual(npu_status.find_owner_id(123), "z50064016")

    @patch("agent_eye.config.npu_status._query_process")
    @patch(
        "agent_eye.config.npu_status._query_working_directory",
        return_value="/home/root/z1242837164512934712594",
    )
    def test_long_identifier_at_end_is_not_partially_matched(
        self, _mocked_pwdx, mocked_query
    ) -> None:
        mocked_query.return_value = (0, "/home/root/z1242837164512934712594")
        self.assertIsNone(npu_status.find_owner_id(123))

    def test_complete_component_takes_priority_over_end_match(self) -> None:
        self.assertEqual(
            npu_status._extract_owner_id(
                "/home/a12345678/project --fallback /home/b87654321"
            ),
            "a12345678",
        )

    def test_owner_prefix_must_be_lowercase(self) -> None:
        self.assertIsNone(npu_status._extract_owner_id("/home/Z50064016"))

    @patch("agent_eye.config.npu_status._query_process")
    @patch("agent_eye.config.npu_status._query_working_directory", return_value=None)
    def test_parent_cycle_returns_null(self, _mocked_pwdx, mocked_query) -> None:
        mocked_query.side_effect = [(20, "worker"), (10, "launcher")]
        self.assertIsNone(npu_status.find_owner_id(10))
        self.assertEqual(mocked_query.call_count, 2)

    @patch("agent_eye.config.npu_status._query_process")
    @patch(
        "agent_eye.config.npu_status._query_working_directory",
        return_value="/data/z50064016/project",
    )
    def test_pwdx_owner_wins_without_parent_query(
        self, _mocked_pwdx, mocked_process
    ) -> None:
        self.assertEqual(npu_status.find_owner_id(123), "z50064016")
        mocked_process.assert_not_called()

    @patch("agent_eye.config.npu_status.subprocess.run")
    def test_pwdx_output_is_validated(self, mocked_run) -> None:
        mocked_run.return_value = subprocess.CompletedProcess(
            [], 0, "123: /data/z50064016/project\n", ""
        )
        self.assertEqual(
            npu_status._query_working_directory(123),
            "/data/z50064016/project",
        )
        mocked_run.return_value = subprocess.CompletedProcess(
            [], 0, "999: /data/z99999999/project\n", ""
        )
        self.assertIsNone(npu_status._query_working_directory(123))

    @patch("agent_eye.config.npu_status.subprocess.run")
    def test_unparseable_success_is_an_error(self, mocked_run) -> None:
        mocked_run.return_value = subprocess.CompletedProcess([], 0, "unexpected", "")
        error = StringIO()
        with redirect_stderr(error):
            status = npu_status.run()
        self.assertEqual(status, 2)
        self.assertIn("contained no NPU records", error.getvalue())


if __name__ == "__main__":
    unittest.main()
