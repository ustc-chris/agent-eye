"""加载单任务 JSON 文件并执行测试目录。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

from .runner import CommandResult


TaskExecutor = Callable[[Mapping[str, Any]], CommandResult]


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


def run_test_directory(test_dir: Path, executor: TaskExecutor) -> int:
    files = sorted(test_dir.glob("*.json"))
    if not files:
        print(f"eye: no JSON task files found in {test_dir}", file=sys.stderr)
        return 2

    passed = skipped = failed = 0
    for path in files:
        print(f"==> {path.name}", flush=True)
        result = run_file(path, executor)
        if result.action == "skipped":
            skipped += 1
            print(f"SKIP {path.name}")
        elif result.returncode == 0:
            passed += 1
            print(f"PASS {path.name}")
        else:
            failed += 1
            print(f"FAIL {path.name} (exit={result.returncode})")

    print(
        f"{len(files)} tasks: {passed} passed, {skipped} skipped, {failed} failed"
    )
    return 1 if failed else 0
