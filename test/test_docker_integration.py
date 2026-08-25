from __future__ import annotations

import os
import shutil
import subprocess
import time
import unittest
import uuid

from agent_eye.killer import KillRequest, kill
from agent_eye.runner import RunRequest, ensure, find_tagged_pids, run


CONTAINER_NAME = "eye_test_worker"


def _docker(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *arguments], check=False, capture_output=True, text=True
    )


class DockerIntegrationTests(unittest.TestCase):
    created_container = False

    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("docker") is None:
            raise unittest.SkipTest("docker command is unavailable")
        if _docker("info").returncode != 0:
            raise unittest.SkipTest("docker daemon is unavailable")

        inspected = _docker("inspect", "--type", "container", CONTAINER_NAME)
        if inspected.returncode != 0:
            image = os.environ.get("AGENT_EYE_TEST_IMAGE", "ubuntu:latest")
            if _docker("image", "inspect", image).returncode != 0:
                raise unittest.SkipTest(
                    f"container is absent and local test image is unavailable: {image}"
                )
            created = _docker(
                "run", "-d", "--name", CONTAINER_NAME, image, "sleep", "infinity"
            )
            if created.returncode != 0:
                raise unittest.SkipTest(f"cannot create test container: {created.stderr}")
            cls.created_container = True
            cls.addClassCleanup(cls._remove_created_container)

        running = _docker(
            "inspect", "--format", "{{.State.Running}}", CONTAINER_NAME
        )
        if running.returncode != 0 or running.stdout.strip() != "true":
            cls._remove_created_container()
            raise unittest.SkipTest("eye_test_worker exists but is not running")

        tools = _docker(
            "exec",
            CONTAINER_NAME,
            "sh",
            "-c",
            "command -v bash && command -v pgrep && command -v tee && command -v awk",
        )
        if tools.returncode != 0:
            cls._remove_created_container()
            raise unittest.SkipTest("test container needs bash, pgrep, tee, and awk")

    @classmethod
    def _remove_created_container(cls) -> None:
        if cls.created_container:
            _docker("rm", "-f", CONTAINER_NAME)
            cls.created_container = False

    def test_run_and_ensure(self) -> None:
        run_result = run(
            RunRequest(exec_command="echo hello world", container=CONTAINER_NAME)
        )
        self.assertEqual(run_result.action, "passed")

        log_path = f"/tmp/agent-eye-test-{uuid.uuid4().hex}.log"
        try:
            logged = run(
                RunRequest(
                    exec_command="printf container-log",
                    container=CONTAINER_NAME,
                    log=log_path,
                )
            )
            self.assertEqual(logged.action, "passed")
            content = _docker("exec", CONTAINER_NAME, "cat", log_path)
            self.assertEqual(content.returncode, 0)
            self.assertEqual(content.stdout, "container-log")
        finally:
            _docker("exec", CONTAINER_NAME, "rm", "-f", log_path)

        tag = f"eye-integration-{uuid.uuid4().hex}"
        first = ensure(
            RunRequest(
                exec_command="sleep 5",
                container=CONTAINER_NAME,
                tag=tag,
                detach=True,
            )
        )
        self.assertEqual(first.action, "started")

        # 等待带 tag 的 Bash 可见，避免依赖固定启动耗时。
        for _ in range(20):
            query_status, pids = find_tagged_pids(CONTAINER_NAME, tag)
            if query_status == 0 and len(pids) == 1:
                break
            time.sleep(0.1)
        else:
            self.fail("带 tag 的容器进程未在 2 秒内进入可查询状态")
        second = ensure(
            RunRequest(
                exec_command="sleep 5",
                container=CONTAINER_NAME,
                tag=tag,
                detach=True,
            )
        )
        self.assertEqual(second.action, "skipped")

        killed = kill(KillRequest(tag=tag, container=CONTAINER_NAME))
        self.assertEqual(killed.action, "killed")


if __name__ == "__main__":
    unittest.main()
