from __future__ import annotations

import os
import signal
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import call, patch

from agent_eye.killer import KillRequest, ProcessInfo, _local_process_tree, kill
from agent_eye import runner


class KillSafetyTests(unittest.TestCase):
    @patch("agent_eye.killer._local_process_tree_linux")
    @patch("agent_eye.killer.Path.is_dir", return_value=False)
    def test_local_kill_refuses_when_proc_is_unavailable(
        self, _mocked_is_dir, mocked_linux_tree
    ) -> None:
        self.assertIsNone(_local_process_tree(123))
        mocked_linux_tree.assert_not_called()

    @patch("agent_eye.killer.find_tagged_pids", return_value=(1, []))
    def test_missing_tag_is_a_successful_skip(self, _mocked_find) -> None:
        output = StringIO()
        with redirect_stdout(output):
            result = kill(KillRequest(tag="missing"))
        self.assertEqual(result.action, "skipped")
        self.assertEqual(result.returncode, 0)
        self.assertIn("reason=tag_not_found", output.getvalue())

    @patch("agent_eye.killer._local_process_tree")
    @patch("agent_eye.killer.find_tagged_pids", return_value=(0, [12, 13]))
    def test_non_unique_tag_is_refused(self, _mocked_find, mocked_tree) -> None:
        with redirect_stderr(StringIO()):
            result = kill(KillRequest(tag="duplicate"))
        self.assertEqual(result.action, "failed")
        self.assertEqual(result.returncode, 4)
        mocked_tree.assert_not_called()

    @patch("agent_eye.killer._kill_local")
    @patch("agent_eye.killer._local_process_tree")
    @patch("agent_eye.killer.find_tagged_pids", return_value=(0, [20]))
    def test_protected_descendant_is_refused(
        self, _mocked_find, mocked_tree, mocked_kill
    ) -> None:
        mocked_tree.return_value = [
            ProcessInfo(20, 10, 1000, "bash", "agent-eye-tag:critical"),
            ProcessInfo(21, 20, 1000, "sshd", "/usr/sbin/sshd"),
        ]
        with redirect_stderr(StringIO()):
            result = kill(KillRequest(tag="critical"))
        self.assertEqual(result.action, "failed")
        self.assertEqual(result.returncode, 4)
        mocked_kill.assert_not_called()

    @patch("agent_eye.killer._kill_local")
    @patch("agent_eye.killer._local_process_tree")
    @patch("agent_eye.killer.find_tagged_pids", return_value=(0, [22]))
    def test_truncated_linux_process_name_is_still_protected(
        self, _mocked_find, mocked_tree, mocked_kill
    ) -> None:
        mocked_tree.return_value = [
            ProcessInfo(22, 10, 1000, "bash", "agent-eye-tag:critical"),
            ProcessInfo(23, 22, 1000, "systemd-timesyn", "systemd-timesyncd"),
        ]
        with redirect_stderr(StringIO()):
            result = kill(KillRequest(tag="critical"))
        self.assertEqual(result.returncode, 4)
        mocked_kill.assert_not_called()

    @patch("agent_eye.killer._kill_local")
    @patch("agent_eye.killer._local_process_tree")
    @patch("agent_eye.killer.find_tagged_pids", return_value=(0, [25]))
    def test_pid_without_exact_tag_marker_is_refused(
        self, _mocked_find, mocked_tree, mocked_kill
    ) -> None:
        mocked_tree.return_value = [
            ProcessInfo(25, 10, 1000, "bash", "unrelated command")
        ]
        with redirect_stderr(StringIO()):
            result = kill(KillRequest(tag="expected"))
        self.assertEqual(result.returncode, 4)
        mocked_kill.assert_not_called()

    @patch("agent_eye.killer._kill_local")
    @patch("agent_eye.killer._local_process_tree", return_value=None)
    @patch("agent_eye.killer.find_tagged_pids", return_value=(0, [26]))
    def test_incomplete_proc_review_never_sends_a_signal(
        self, _mocked_find, _mocked_tree, mocked_kill
    ) -> None:
        with redirect_stderr(StringIO()) as output:
            result = kill(KillRequest(tag="worker"))
        self.assertEqual(result.returncode, 4)
        self.assertIn("reason=safety_review_failed", output.getvalue())
        mocked_kill.assert_not_called()

    @patch("agent_eye.killer.os.geteuid", return_value=1000)
    @patch("agent_eye.killer.os.kill")
    @patch("agent_eye.killer._local_process_tree")
    @patch("agent_eye.killer.find_tagged_pids", return_value=(0, [30]))
    def test_local_tree_is_terminated_children_first(
        self, _mocked_find, mocked_tree, mocked_kill, _mocked_uid
    ) -> None:
        mocked_tree.return_value = [
            ProcessInfo(30, 10, 1000, "bash", "agent-eye-tag:worker"),
            ProcessInfo(31, 30, 1000, "sleep", "sleep 60"),
        ]
        with redirect_stdout(StringIO()):
            result = kill(KillRequest(tag="worker"))
        self.assertEqual(result.action, "killed")
        self.assertEqual(
            mocked_kill.call_args_list,
            [call(31, 15), call(30, 15)],
        )

    @patch("agent_eye.killer._kill_container", return_value=0)
    @patch("agent_eye.killer._container_process_tree")
    @patch("agent_eye.killer.find_tagged_pids", return_value=(0, [40]))
    @patch("agent_eye.killer.container_is_running", return_value=0)
    def test_container_mode_uses_container_process_tree(
        self, _mocked_running, _mocked_find, mocked_tree, mocked_kill
    ) -> None:
        processes = [ProcessInfo(40, 1, 0, "bash", "agent-eye-tag:worker")]
        mocked_tree.return_value = processes
        with redirect_stdout(StringIO()):
            result = kill(KillRequest(tag="worker", container="eye_test_worker"))
        self.assertEqual(result.action, "killed")
        mocked_kill.assert_called_once_with("eye_test_worker", processes)


class LocalKillIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(os.path.isdir("/proc"), "local kill requires /proc")
    def test_real_tagged_process_can_be_killed(self) -> None:
        """local · safe kill integration"""
        tag = f"agent-eye-kill-test-{uuid.uuid4().hex}"
        pid: int | None = None
        with tempfile.TemporaryDirectory() as directory, patch(
            "agent_eye.runner.RUNTIME_DIR", directory
        ):
            started = runner.run(
                runner.RunRequest(
                    exec_command="sleep 30",
                    tag=tag,
                    detach=True,
                    log=f"{directory}/task.log",
                )
            )
            pid = started.pid
            self.assertEqual(started.action, "started")
            self.assertIsNotNone(pid)
            try:
                with redirect_stdout(StringIO()):
                    result = kill(KillRequest(tag=tag))
                self.assertEqual(result.action, "killed")
            finally:
                if pid is not None:
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    unittest.main()
