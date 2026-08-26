from __future__ import annotations

import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from agent_eye import cli, runner
from agent_eye.runner import CommandResult


class CliTests(unittest.TestCase):
    def test_all_public_commands_are_registered(self) -> None:
        parser = cli.build_parser()
        subparsers = next(
            action
            for action in parser._actions
            if isinstance(action, cli.argparse._SubParsersAction)
        )
        self.assertEqual(
            set(subparsers.choices),
            {
                "npu_status",
                "run",
                "ensure",
                "kill",
                "from_file",
                "test",
                "version",
                "doc",
                "help",
            },
        )

    def test_version_command_contains_config_directory(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            status = cli.main(["version"])
        self.assertEqual(status, 0)
        self.assertIn("eye 0.5.0", output.getvalue())
        self.assertIn("config:", output.getvalue())

    def test_version_option_is_supported(self) -> None:
        output = StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            cli.main(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("eye 0.5.0", output.getvalue())

    def test_help_command_supports_topics(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            status = cli.main(["help", "ensure"])
        self.assertEqual(status, 0)
        self.assertIn("--tag", output.getvalue())

    def test_help_option_is_supported(self) -> None:
        output = StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            cli.main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("npu_status", output.getvalue())

    def test_doc_returns_detailed_agent_guide(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            status = cli.main(["doc"])
        self.assertEqual(status, 0)
        rendered = output.getvalue()
        self.assertIn("# Agent Eye", rendered)
        self.assertIn("## 给新 Agent 的快速入口", rendered)
        self.assertIn("eye ensure", rendered)
        self.assertIn("SUCCEEDED", rendered)

    @patch("agent_eye.cli.npu_status.run", return_value=0)
    def test_npu_status_command_routes_to_config(self, mocked_status) -> None:
        self.assertEqual(cli.main(["npu_status"]), 0)
        mocked_status.assert_called_once_with()

    @patch("agent_eye.cli.run_test_directory", return_value=0)
    def test_test_command_routes_to_repository_suite(self, mocked_test) -> None:
        self.assertEqual(cli.main(["test"]), 0)
        mocked_test.assert_called_once()

    @patch("agent_eye.cli.run_file", return_value=CommandResult(0, "passed"))
    def test_from_file_command_routes_one_file(self, mocked_file) -> None:
        self.assertEqual(cli.main(["from_file", "task.json"]), 0)
        self.assertEqual(mocked_file.call_args.args[0], "task.json")

    @patch("agent_eye.cli.run", return_value=CommandResult(0, "passed"))
    def test_run_command_routes_local_request(self, mocked_run) -> None:
        self.assertEqual(
            cli.main(["run", "--no-container", "--exec", "printf hello"]), 0
        )
        request = mocked_run.call_args.args[0]
        self.assertIsNone(request.container)
        self.assertEqual(request.exec_command, "printf hello")

    @patch("agent_eye.cli.ensure", return_value=CommandResult(0, "started"))
    def test_ensure_command_routes_detached_request(self, mocked_ensure) -> None:
        self.assertEqual(
            cli.main(
                [
                    "ensure",
                    "--no-container",
                    "--tag",
                    "worker",
                    "--exec",
                    "sleep 5",
                    "--detach",
                ]
            ),
            0,
        )
        request = mocked_ensure.call_args.args[0]
        self.assertEqual(request.tag, "worker")
        self.assertTrue(request.detach)

    @patch("agent_eye.cli.kill_task", return_value=CommandResult(0, "killed"))
    def test_kill_command_routes_container_request(self, mocked_kill) -> None:
        self.assertEqual(
            cli.main(["kill", "--container", "worker", "--tag", "task"]), 0
        )
        request = mocked_kill.call_args.args[0]
        self.assertEqual(request.container, "worker")
        self.assertEqual(request.tag, "task")

    def test_ensure_file_requires_tag(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires field 'tag'"):
            cli.execute_task(
                {
                    "command": "ensure",
                    "no_container": True,
                    "exec": "sleep 60",
                }
            )

    def test_task_requires_exactly_one_execution_target(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly one"):
            cli.execute_task(
                {
                    "command": "run",
                    "container": "worker",
                    "no_container": True,
                    "exec": "true",
                }
            )

    @patch("agent_eye.cli.kill_task", return_value=CommandResult(0, "killed"))
    def test_kill_task_file_defaults_to_local_mode(self, mocked_kill) -> None:
        result = cli.execute_task({"command": "kill", "tag": "worker"})
        self.assertEqual(result.action, "killed")
        request = mocked_kill.call_args.args[0]
        self.assertEqual(request.tag, "worker")
        self.assertIsNone(request.container)


class LocalRunnerTests(unittest.TestCase):
    def test_same_tag_keeps_separate_pid_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "agent_eye.runner.RUNTIME_DIR", directory
        ):
            runner._write_pid("duplicate", 100)
            runner._write_pid("duplicate", 101)
            records = sorted(
                path.name for path in runner._pid_directory("duplicate").glob("*.pid")
            )
        self.assertEqual(records, ["100.pid", "101.pid"])

    def test_invalid_registered_pid_is_removed_without_querying_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "agent_eye.runner.RUNTIME_DIR", directory
        ):
            runner._write_pid("invalid", 0)
            status, pids = runner._find_local_tag("invalid")
            remaining = list(runner._pid_directory("invalid").glob("*.pid"))
        self.assertEqual((status, pids), (1, None))
        self.assertEqual(remaining, [])

    def test_blocking_run_tees_output_to_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "nested" / "task.log"
            result = runner.run(
                runner.RunRequest(exec_command="printf hello", log=str(log))
            )
            self.assertEqual(result.action, "passed")
            self.assertEqual(log.read_text(encoding="utf-8"), "hello")

    def test_tagged_wrapper_preserves_command_exit_code(self) -> None:
        tag = "exit-code-test"
        result = runner.run(runner.RunRequest(exec_command="exit 7", tag=tag))
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.action, "failed")
        status, pids = runner._find_local_tag(tag)
        self.assertEqual((status, pids), (1, None))

    @patch("agent_eye.runner.subprocess.Popen")
    def test_detached_local_run_uses_new_session(self, mocked_popen) -> None:
        mocked_popen.return_value.pid = 4321
        with tempfile.TemporaryDirectory() as directory:
            result = runner.run(
                runner.RunRequest(
                    exec_command="sleep 5",
                    detach=True,
                    log=str(Path(directory) / "task.log"),
                )
            )
        self.assertEqual(result.action, "started")
        self.assertEqual(result.pid, 4321)
        self.assertTrue(mocked_popen.call_args.kwargs["start_new_session"])
        self.assertTrue(mocked_popen.call_args.kwargs["close_fds"])

    @patch("agent_eye.runner._write_pid", side_effect=PermissionError("denied"))
    @patch("agent_eye.runner.subprocess.Popen")
    def test_blocking_process_is_stopped_when_pid_registration_fails(
        self, mocked_popen, _mocked_write_pid
    ) -> None:
        mocked_popen.return_value.pid = 4322
        result = runner.run(
            runner.RunRequest(exec_command="sleep 5", tag="registration-failure")
        )
        self.assertEqual(result.action, "failed")
        mocked_popen.return_value.terminate.assert_called_once_with()
        mocked_popen.return_value.wait.assert_called_once_with()

    @patch("agent_eye.runner.os.killpg")
    @patch("agent_eye.runner._write_pid", side_effect=PermissionError("denied"))
    @patch("agent_eye.runner.subprocess.Popen")
    def test_detached_process_group_is_stopped_when_pid_registration_fails(
        self, mocked_popen, _mocked_write_pid, mocked_killpg
    ) -> None:
        mocked_popen.return_value.pid = 4323
        with tempfile.TemporaryDirectory() as directory:
            result = runner.run(
                runner.RunRequest(
                    exec_command="sleep 5",
                    tag="registration-failure",
                    detach=True,
                    log=str(Path(directory) / "task.log"),
                )
            )
        self.assertEqual(result.action, "failed")
        mocked_killpg.assert_called_once_with(4323, 15)
        mocked_popen.return_value.wait.assert_called_once_with()

    @patch("agent_eye.runner._find_tag", return_value=(1, None))
    @patch("agent_eye.runner.subprocess.Popen")
    def test_ensure_is_blocking_without_detach(
        self, mocked_popen, _mocked_find_tag
    ) -> None:
        mocked_popen.return_value.pid = 5432
        mocked_popen.return_value.wait.return_value = 0
        result = runner.ensure(
            runner.RunRequest(exec_command="sleep 5", tag="blocking-test")
        )
        self.assertEqual(result.action, "passed")
        mocked_popen.return_value.wait.assert_called_once_with()

    @patch("agent_eye.runner._find_tag")
    def test_concurrent_ensure_skips_before_query(self, mocked_find_tag) -> None:
        output = StringIO()
        with tempfile.TemporaryDirectory() as directory, patch(
            "agent_eye.runner.RUNTIME_DIR", directory
        ):
            with runner._ensure_lock(None, "same-task") as acquired:
                self.assertTrue(acquired)
                with redirect_stdout(output):
                    result = runner.ensure(
                        runner.RunRequest(exec_command="sleep 5", tag="same-task")
                    )
        self.assertEqual(result.action, "skipped")
        self.assertIn("reason=ensure_in_progress", output.getvalue())
        mocked_find_tag.assert_not_called()


class DockerRunnerTests(unittest.TestCase):
    @patch("agent_eye.runner.subprocess.run")
    def test_container_run_passes_command_without_host_shell(self, mocked_run) -> None:
        mocked_run.return_value = subprocess.CompletedProcess([], 0)
        result = runner.run(
            runner.RunRequest(exec_command="echo '$HOME'", container="worker")
        )
        self.assertEqual(result.action, "passed")
        arguments = mocked_run.call_args.args[0]
        self.assertEqual(arguments[:4], ["docker", "exec", "worker", "bash"])
        self.assertFalse(mocked_run.call_args.kwargs.get("shell", False))

    @patch("agent_eye.runner.subprocess.run")
    def test_ensure_skips_an_existing_container_tag(self, mocked_run) -> None:
        mocked_run.side_effect = [
            subprocess.CompletedProcess([], 0, stdout="true\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="23\n", stderr=""),
        ]
        result = runner.ensure(
            runner.RunRequest(
                exec_command="sleep 60", container="worker", tag="job[1]"
            )
        )
        self.assertEqual(result.action, "skipped")
        self.assertEqual(result.pid, 23)
        pattern = mocked_run.call_args_list[1].args[0][-1]
        self.assertEqual(pattern, r"agent-eye-tag:job\[1\]$")

    @patch("agent_eye.runner.subprocess.run")
    def test_detached_container_run_uses_docker_detach(self, mocked_run) -> None:
        mocked_run.return_value = subprocess.CompletedProcess([], 0)
        result = runner.run(
            runner.RunRequest(
                exec_command="sleep 60",
                container="worker",
                tag="job-1",
                detach=True,
            )
        )
        self.assertEqual(result.action, "started")
        arguments = mocked_run.call_args.args[0]
        self.assertEqual(arguments[:4], ["docker", "exec", "-d", "worker"])


if __name__ == "__main__":
    unittest.main()
