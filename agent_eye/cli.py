"""Agent Eye 命令行入口和命令分流。"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Mapping, Sequence

from . import __version__
from .config import npu_status
from .file_runner import run_file, run_test_directory
from .killer import KillRequest, kill as kill_task
from .paths import CONFIG_DIR, REPOSITORY_DIR, TEST_DIR
from .runner import CommandResult, RunRequest, ensure, run
from .time_window import AllowSyntaxError, evaluate_allow


def version_text() -> str:
    return (
        f"eye {__version__}\n"
        f"home: {REPOSITORY_DIR}\n"
        f"config: {CONFIG_DIR}\n"
        f"test: {TEST_DIR}"
    )


def documentation_text() -> str:
    """Return the canonical, repository-local user guide."""
    documentation = REPOSITORY_DIR / "README.md"
    try:
        return documentation.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"cannot read documentation: {documentation}: {exc}") from exc


class _VersionAction(argparse.Action):
    def __init__(self, option_strings: Sequence[str], dest: str, **kwargs: Any) -> None:
        super().__init__(option_strings, dest, nargs=0, **kwargs)

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        print(version_text())
        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eye",
        description="在宿主机或 Docker 容器中运行、守护和终止任务。",
    )
    parser.add_argument(
        "--version",
        action=_VersionAction,
        help="显示版本和仓库目录",
    )
    _add_allow_argument(parser)
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    npu_parser = subparsers.add_parser(
        "npu_status", help="执行已配置的 NPU 状态查询"
    )
    _add_allow_argument(npu_parser, suppress_default=True)

    run_parser = subparsers.add_parser("run", help="运行命令")
    _add_run_arguments(run_parser, tag_required=False)

    ensure_parser = subparsers.add_parser(
        "ensure", help="仅在 tag 不存在时运行命令"
    )
    _add_run_arguments(ensure_parser, tag_required=True)

    kill_parser = subparsers.add_parser(
        "kill", help="安全终止唯一 tag 对应的任务"
    )
    kill_parser.add_argument(
        "--container", type=_non_empty, help="Docker 容器名称或 ID；省略时操作宿主机"
    )
    kill_parser.add_argument(
        "--tag", required=True, type=_non_empty, help="唯一的任务 tag"
    )
    _add_allow_argument(kill_parser, suppress_default=True)

    from_file_parser = subparsers.add_parser(
        "from_file", help="运行 JSON 文件描述的单个任务"
    )
    from_file_parser.add_argument("file", type=_non_empty, help="单任务 JSON 文件路径")
    _add_allow_argument(from_file_parser, suppress_default=True)

    subparsers.add_parser("test", help="运行完整功能测试和 test 目录内的 JSON 任务")
    subparsers.add_parser("version", help="显示版本和仓库目录")
    subparsers.add_parser("doc", help="显示完整使用说明，适合 agent 直接阅读")

    help_parser = subparsers.add_parser("help", help="显示总帮助或子命令帮助")
    help_parser.add_argument("topic", nargs="?", help="可选的子命令名称")
    return parser


def _non_empty(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("参数不能为空")
    return value


def _add_allow_argument(
    parser: argparse.ArgumentParser, *, suppress_default: bool = False
) -> None:
    default: Any = argparse.SUPPRESS if suppress_default else None
    parser.add_argument(
        "--allow",
        default=default,
        metavar="WINDOW",
        help="仅在指定的本地时间窗口内启动命令",
    )


def _add_run_arguments(parser: argparse.ArgumentParser, *, tag_required: bool) -> None:
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--container", type=_non_empty, help="Docker 容器名称或 ID")
    target.add_argument(
        "--no-container",
        action="store_true",
        help="直接在宿主机运行",
    )
    parser.add_argument(
        "--exec",
        dest="exec_command",
        required=True,
        type=_non_empty,
        help="要执行的 Shell 命令",
    )
    parser.add_argument(
        "--tag",
        required=tag_required,
        type=_non_empty,
        help="标识运行中命令的唯一字面量 tag",
    )
    parser.add_argument(
        "--detach",
        action="store_true",
        help="后台启动，不等待命令结束",
    )
    parser.add_argument(
        "--log",
        type=_non_empty,
        help="追加日志；阻塞模式同时通过 tee 实时输出",
    )
    _add_allow_argument(parser, suppress_default=True)


def _request_from_namespace(arguments: argparse.Namespace) -> RunRequest:
    return RunRequest(
        exec_command=arguments.exec_command,
        container=arguments.container,
        tag=arguments.tag,
        detach=arguments.detach,
        log=arguments.log,
    )


def _require_string(task: Mapping[str, Any], key: str) -> str:
    value = task.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"task field '{key}' must be a non-empty string")
    return value


def _optional_string(task: Mapping[str, Any], key: str) -> str | None:
    value = task.get(key)
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f"task field '{key}' must be a non-empty string")
    return value


def _reject_unknown_fields(
    task: Mapping[str, Any], allowed: set[str], command: str
) -> None:
    unknown = set(task) - allowed
    if unknown:
        raise ValueError(
            f"unknown {command} task fields: {', '.join(sorted(unknown))}"
        )


def execute_task(task: Mapping[str, Any]) -> CommandResult:
    command = _require_string(task, "command")

    if command == "npu_status":
        _reject_unknown_fields(task, {"command"}, command)
        returncode = npu_status.run()
        return CommandResult(
            returncode,
            "passed" if returncode == 0 else "failed",
        )

    if command == "kill":
        _reject_unknown_fields(task, {"command", "container", "tag"}, command)
        container_value = _optional_string(task, "container")
        tag_value = _require_string(task, "tag")
        return kill_task(KillRequest(tag=tag_value, container=container_value))

    if command not in {"run", "ensure"}:
        raise ValueError(f"unsupported task command: {command}")

    allowed = {
        "command",
        "container",
        "no_container",
        "exec",
        "tag",
        "detach",
        "log",
    }
    _reject_unknown_fields(task, allowed, command)

    container_value = _optional_string(task, "container")
    no_container = task.get("no_container", False)
    if not isinstance(no_container, bool):
        raise ValueError("task field 'no_container' must be a boolean")
    if bool(container_value) == no_container:
        raise ValueError("task must select exactly one of 'container' or 'no_container'")

    exec_command = _require_string(task, "exec")
    tag_value = _optional_string(task, "tag")
    if command == "ensure" and tag_value is None:
        raise ValueError("ensure task requires field 'tag'")

    detach = task.get("detach", False)
    if not isinstance(detach, bool):
        raise ValueError("task field 'detach' must be a boolean")
    log_value = _optional_string(task, "log")

    request = RunRequest(
        exec_command=exec_command,
        container=container_value,
        tag=tag_value,
        detach=detach,
        log=log_value,
    )
    return run(request) if command == "run" else ensure(request)


def _show_help(parser: argparse.ArgumentParser, topic: str | None) -> int:
    if topic is None:
        parser.print_help()
        return 0
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction) and topic in action.choices:
            action.choices[topic].print_help()
            return 0
    print(f"eye: unknown help topic: {topic}", file=sys.stderr)
    return 2


def _allow_execution(expression: str | None) -> tuple[bool, int]:
    if expression is None:
        return True, 0
    try:
        decision = evaluate_allow(expression)
    except AllowSyntaxError as exc:
        print(f"eye: invalid --allow: {exc}", file=sys.stderr)
        return False, 2
    if decision.allowed:
        return True, 0
    encoded_expression = json.dumps(expression, ensure_ascii=False)
    print(
        "SKIPPED reason=outside_allow "
        f"now={decision.current} timezone={decision.timezone} "
        f"allow={encoded_expression}"
    )
    return False, 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)

    if arguments.command is None:
        parser.print_help()
        return 0
    if arguments.command in {"npu_status", "run", "ensure", "kill", "from_file"}:
        allowed, status = _allow_execution(arguments.allow)
        if not allowed:
            return status
    if arguments.command == "npu_status":
        return npu_status.run()
    if arguments.command == "run":
        return run(_request_from_namespace(arguments)).returncode
    if arguments.command == "ensure":
        return ensure(_request_from_namespace(arguments)).returncode
    if arguments.command == "kill":
        return kill_task(
            KillRequest(tag=arguments.tag, container=arguments.container)
        ).returncode
    if arguments.command == "from_file":
        return run_file(arguments.file, execute_task).returncode
    if arguments.command == "test":
        return run_test_directory(TEST_DIR, execute_task)
    if arguments.command == "version":
        print(version_text())
        return 0
    if arguments.command == "doc":
        try:
            print(documentation_text(), end="")
        except RuntimeError as exc:
            print(f"eye: {exc}", file=sys.stderr)
            return 1
        return 0
    if arguments.command == "help":
        return _show_help(parser, arguments.topic)

    parser.error(f"unsupported command: {arguments.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
