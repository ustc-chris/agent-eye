"""加载单任务 JSON 文件，并以精简界面运行完整自检。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, TextIO

from .runner import CommandResult


TaskExecutor = Callable[[Mapping[str, Any]], CommandResult]
TestStatus = Literal["passed", "skipped", "failed"]


@dataclass(frozen=True)
class TestOutcome:
    name: str
    status: TestStatus
    detail: str = ""


class TestReporter:
    """在终端中刷新单行进度，并在最后只展开异常结果。"""

    _COLORS = {
        "reset": "\033[0m",
        "bold": "\033[1m",
        "green": "\033[32m",
        "yellow": "\033[33m",
        "red": "\033[31m",
        "cyan": "\033[36m",
    }

    def __init__(self, total: int, stream: TextIO = sys.stdout) -> None:
        self.total = total
        self.stream = stream
        self.outcomes: list[TestOutcome] = []
        self.interactive = bool(getattr(stream, "isatty", lambda: False)())
        self.color = self.interactive and "NO_COLOR" not in os.environ

    def _paint(self, text: str, color: str, *, bold: bool = False) -> str:
        if not self.color:
            return text
        prefix = self._COLORS["bold"] if bold else ""
        return f"{prefix}{self._COLORS[color]}{text}{self._COLORS['reset']}"

    def start(self, name: str) -> None:
        if not self.interactive:
            return
        current = min(len(self.outcomes) + 1, self.total)
        label = self._paint("●", "cyan")
        self.stream.write(f"\r\033[2K{label} [{current}/{self.total}] {name}")
        self.stream.flush()

    def add(self, outcome: TestOutcome) -> None:
        self.outcomes.append(outcome)

    def finish(self) -> int:
        if self.interactive:
            self.stream.write("\r\033[2K")

        passed = sum(item.status == "passed" for item in self.outcomes)
        skipped = sum(item.status == "skipped" for item in self.outcomes)
        failed = sum(item.status == "failed" for item in self.outcomes)
        summary = "  ".join(
            (
                self._paint(f"✓ {passed} passed", "green", bold=True),
                self._paint(f"○ {skipped} skipped", "yellow", bold=True),
                self._paint(f"✕ {failed} failed", "red", bold=True),
            )
        )
        self.stream.write(f"Agent Eye Test  {summary}\n")

        self._write_group("FAILED", "failed", "red", "✕")
        self._write_group("SKIPPED", "skipped", "yellow", "○")
        self.stream.write("\n")
        self.stream.flush()
        return 1 if failed else 0

    def _write_group(
        self, heading: str, status: TestStatus, color: str, symbol: str
    ) -> None:
        selected = [item for item in self.outcomes if item.status == status]
        if not selected:
            return
        self.stream.write(f"\n{self._paint(heading, color, bold=True)}\n")
        for item in selected:
            self.stream.write(f"  {self._paint(symbol, color)} {item.name}\n")
            if item.detail:
                detail = item.detail.strip().replace("\r", "")
                for line in detail.splitlines():
                    self.stream.write(f"    {line}\n")


class _ReportingTestResult(unittest.TestResult):
    """把 unittest 的每个用例转换为统一测试结果。"""

    def __init__(self, reporter: TestReporter) -> None:
        super().__init__()
        self.reporter = reporter
        self.buffer = True
        self._status: TestStatus = "passed"
        self._detail = ""
        self._active_test: unittest.case.TestCase | None = None
        self._saved_fds: tuple[int, int] | None = None
        self._fd_output: Any = None

    @staticmethod
    def _name(test: unittest.case.TestCase) -> str:
        description = test.shortDescription()
        if description:
            return description
        full_identifier = test.id()
        if full_identifier.startswith("setUpClass (test_docker_integration."):
            return "container · Docker integration"
        identifier = full_identifier.rsplit(".", 1)[-1]
        return f"unit · {identifier.replace('_', ' ')}"

    def startTest(self, test: unittest.case.TestCase) -> None:
        self._status = "passed"
        self._detail = ""
        self._active_test = test
        self.reporter.start(self._name(test))
        super().startTest(test)
        # unittest 的 buffer 只替换 Python 流；同时接管文件描述符以隐藏子进程输出。
        self._fd_output = tempfile.TemporaryFile(mode="w+b")
        self._saved_fds = (os.dup(1), os.dup(2))
        os.dup2(self._fd_output.fileno(), 1)
        os.dup2(self._fd_output.fileno(), 2)

    def _record_external_outcome(
        self, test: unittest.case.TestCase, status: TestStatus, detail: str
    ) -> None:
        """记录 setUpClass 等发生在具体用例之外的结果。"""
        self.reporter.start(self._name(test))
        self.reporter.add(TestOutcome(self._name(test), status, detail))

    def addError(self, test: unittest.case.TestCase, err: tuple[type, BaseException, Any]) -> None:
        super().addError(test, err)
        detail = self.errors[-1][1]
        if self._active_test is test:
            self._status = "failed"
            self._detail = detail
        else:
            self._record_external_outcome(test, "failed", detail)

    def addFailure(
        self, test: unittest.case.TestCase, err: tuple[type, BaseException, Any]
    ) -> None:
        super().addFailure(test, err)
        self._status = "failed"
        self._detail = self.failures[-1][1]

    def addSkip(self, test: unittest.case.TestCase, reason: str) -> None:
        super().addSkip(test, reason)
        if self._active_test is test:
            self._status = "skipped"
            self._detail = reason
        else:
            self._record_external_outcome(test, "skipped", reason)

    def addExpectedFailure(
        self, test: unittest.case.TestCase, err: tuple[type, BaseException, Any]
    ) -> None:
        super().addExpectedFailure(test, err)
        self._status = "skipped"
        self._detail = "expected failure"

    def addUnexpectedSuccess(self, test: unittest.case.TestCase) -> None:
        super().addUnexpectedSuccess(test)
        self._status = "failed"
        self._detail = "unexpected success"

    def stopTest(self, test: unittest.case.TestCase) -> None:
        if self._active_test is not test:
            super().stopTest(test)
            return
        fd_detail = ""
        if self._saved_fds is not None and self._fd_output is not None:
            os.dup2(self._saved_fds[0], 1)
            os.dup2(self._saved_fds[1], 2)
            os.close(self._saved_fds[0])
            os.close(self._saved_fds[1])
            self._fd_output.seek(0)
            fd_detail = self._fd_output.read().decode("utf-8", errors="replace")
            self._fd_output.close()
            self._saved_fds = None
            self._fd_output = None
        super().stopTest(test)
        if self._status != "passed" and fd_detail.strip():
            self._detail = "\n".join(
                part for part in (self._detail.strip(), fd_detail.strip()) if part
            )
        self.reporter.add(TestOutcome(self._name(test), self._status, self._detail))
        self._active_test = None


def load_task(file_path: str | Path) -> Mapping[str, Any]:
    path = Path(file_path).expanduser()
    try:
        with path.open("r", encoding="utf-8") as stream:
            task = json.load(stream)
    except FileNotFoundError as exc:
        raise ValueError(f"task file does not exist: {path}") from exc
    except PermissionError as exc:
        raise ValueError(f"task file is not readable: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"invalid JSON in {path} at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc

    if not isinstance(task, dict):
        raise ValueError(f"task file must contain one JSON object: {path}")
    return task


def run_file(file_path: str | Path, executor: TaskExecutor) -> CommandResult:
    try:
        task = load_task(file_path)
        return executor(task)
    except ValueError as exc:
        print(f"eye: {exc}", file=sys.stderr)
        return CommandResult(2, "failed")


def _captured_json_test(
    path: Path, executor: TaskExecutor, run_id: str, artifacts_dir: Path
) -> TestOutcome:
    name = f"task · {path.name}"
    output = StringIO()
    try:
        task = load_task(path)
    except ValueError as exc:
        return TestOutcome(name, "failed", str(exc))

    if task.get("command") == "npu_status" and shutil.which("npu-smi") is None:
        return TestOutcome(name, "skipped", "npu-smi is unavailable")

    task_path = path
    if str(task.get("tag", "")).startswith("agent-eye-selftest-"):
        isolated_task = dict(task)
        isolated_task["tag"] = f"{task['tag']}-{run_id}"
        if "log" in isolated_task:
            isolated_task["log"] = str(artifacts_dir / f"{path.stem}.log")
        task_path = artifacts_dir / path.name
        task_path.write_text(
            json.dumps(isolated_task, ensure_ascii=False), encoding="utf-8"
        )

    eye = path.parent.parent / "eye"
    if eye.is_file():
        completed = subprocess.run(
            [sys.executable, str(eye), "from_file", str(task_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        detail = "\n".join(
            part.strip()
            for part in (completed.stdout, completed.stderr)
            if part.strip()
        )
        returncode = completed.returncode
    else:  # pragma: no cover，保留给外部调用者的兼容路径
        with redirect_stdout(output), redirect_stderr(output):
            try:
                result = executor(task)
            except Exception as exc:
                return TestOutcome(name, "failed", f"{type(exc).__name__}: {exc}")
        detail = output.getvalue().strip()
        returncode = result.returncode

    if any(line.startswith("SKIPPED ") for line in detail.splitlines()):
        return TestOutcome(name, "skipped", detail)
    if returncode != 0:
        return TestOutcome(name, "failed", detail or f"exit={returncode}")
    return TestOutcome(name, "passed")


def _discover_unit_tests(test_dir: Path) -> unittest.TestSuite:
    return unittest.defaultTestLoader.discover(
        str(test_dir), pattern="test_*.py", top_level_dir=str(test_dir)
    )


def run_test_directory(test_dir: Path, executor: TaskExecutor) -> int:
    files = sorted(test_dir.glob("*.json"))
    suite = _discover_unit_tests(test_dir)
    total = len(files) + suite.countTestCases()
    if total == 0:
        print(f"eye: no tests found in {test_dir}", file=sys.stderr)
        return 2

    reporter = TestReporter(total)
    run_id = uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix="agent-eye-test-") as directory:
        artifacts_dir = Path(directory)
        for path in files:
            reporter.start(f"task · {path.name}")
            reporter.add(
                _captured_json_test(path, executor, run_id, artifacts_dir)
            )

    suite.run(_ReportingTestResult(reporter))
    return reporter.finish()
