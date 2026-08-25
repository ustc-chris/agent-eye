"""本地和 Docker 模式下的 run 与 ensure 实现。"""

from __future__ import annotations

import fcntl
import hashlib
import os
import posixpath
import re
import shlex
import signal
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .paths import RUNTIME_DIR


TAG_PREFIX = "agent-eye-tag:"
Action = Literal["passed", "started", "skipped", "killed", "failed"]


@dataclass(frozen=True)
class RunRequest:
    """一次命令请求；container 为空时在宿主机执行。"""

    exec_command: str
    container: str | None = None
    tag: str | None = None
    detach: bool = False
    log: str | None = None
    # 为将来的 docker exec 选项预留。
    docker_options: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    action: Action
    pid: int | None = None
    log: str | None = None


def _mode(request: RunRequest) -> str:
    return f"container:{request.container}" if request.container else "local"


def _marker(tag: str) -> str:
    return f"{TAG_PREFIX}{tag}"


def _literal_ere(value: str) -> str:
    """转义 pgrep 使用的 POSIX 扩展正则表达式。"""
    return re.sub(r"([.\\^$*+?{}()|\[\]])", r"\\\1", value)


def _default_log(request: RunRequest) -> str:
    identity = request.tag or request.exec_command
    digest = _tag_digest(identity)
    return f"{RUNTIME_DIR}/{digest}.log"


def _tag_digest(tag: str) -> str:
    return hashlib.sha256(tag.encode("utf-8")).hexdigest()[:16]


@contextmanager
def _ensure_lock(container: str | None, tag: str):
    identity = f"{container or 'local'}\0{tag}"
    directory = Path(RUNTIME_DIR) / "locks"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"{_tag_digest(identity)}.lock"
    descriptor = os.open(
        path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(descriptor)


def _pid_directory(tag: str) -> Path:
    return Path(RUNTIME_DIR) / "pids" / _tag_digest(tag)


def _legacy_pid_path(tag: str) -> Path:
    return Path(RUNTIME_DIR) / "pids" / f"{_tag_digest(tag)}.pid"


def _write_pid(tag: str, pid: int) -> None:
    directory = _pid_directory(tag)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"{pid}.pid"
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="ascii") as stream:
        stream.write(f"{pid}\n")


def _remove_pid(tag: str, pid: int) -> None:
    path = _pid_directory(tag) / f"{pid}.pid"
    try:
        path.unlink()
        path.parent.rmdir()
    except OSError:
        pass


def _find_local_tag(tag: str) -> tuple[int, str | None]:
    paths = list(_pid_directory(tag).glob("*.pid"))
    legacy_path = _legacy_pid_path(tag)
    if legacy_path.exists():
        paths.append(legacy_path)
    live_pids: set[int] = set()
    for path in paths:
        pid = _validated_registered_pid(tag, path)
        if pid is not None:
            live_pids.add(pid)
    if not live_pids:
        return 1, None
    return 0, "\n".join(str(pid) for pid in sorted(live_pids))


def _validated_registered_pid(tag: str, path: Path) -> int | None:
    try:
        pid = int(path.read_text(encoding="ascii").strip())
    except (FileNotFoundError, OSError, ValueError):
        try:
            path.unlink()
        except OSError:
            pass
        return None
    if pid <= 1:
        try:
            path.unlink()
        except OSError:
            pass
        return None

    try:
        os.kill(pid, 0)
    except PermissionError:
        # 进程存在，但属于其他有效用户。
        return pid
    except ProcessLookupError:
        try:
            path.unlink()
        except OSError:
            pass
        return None

    # Linux 上额外检查命令行，防止 PID 被复用后误用旧状态文件。
    command_line = Path(f"/proc/{pid}/cmdline")
    if command_line.exists():
        try:
            if _marker(tag).encode("utf-8") not in command_line.read_bytes():
                path.unlink(missing_ok=True)
                return None
        except OSError:
            pass
    return pid


def _effective_log(request: RunRequest) -> str | None:
    log = request.log
    if request.detach and log is None:
        log = _default_log(request)
    if log is not None and request.container is None:
        return str(Path(log).expanduser().resolve())
    return log


def _tagged_body(exec_command: str, tag: str | None) -> str:
    if tag is None:
        return exec_command
    # 保持外层 Bash 存活，使 tag 在业务命令运行期间始终可被查询。
    return "\n".join(
        (
            "_agent_eye_child=",
            "_agent_eye_forward_signal() {",
            '    if [ -n "${_agent_eye_child}" ]; then',
            '        kill -TERM "${_agent_eye_child}" 2>/dev/null || true',
            '        wait "${_agent_eye_child}" 2>/dev/null || true',
            "    fi",
            "    exit 143",
            "}",
            "trap _agent_eye_forward_signal TERM INT HUP",
            "{",
            exec_command,
            "} &",
            "_agent_eye_child=$!",
            'wait "${_agent_eye_child}"',
            "_agent_eye_status=$?",
            "trap - TERM INT HUP",
            'exit "${_agent_eye_status}"',
        )
    )


def _shell_script(
    request: RunRequest, *, log: str | None, detached_container: bool = False
) -> str:
    body = _tagged_body(request.exec_command, request.tag)
    if log is None:
        return body

    log_directory = posixpath.dirname(log) or "."
    preparation = "\n".join(
        (
            "umask 077",
            f"mkdir -p -- {shlex.quote(log_directory)}",
            f": >> {shlex.quote(log)}",
        )
    )
    if detached_container:
        return "\n".join(
            (preparation, f"{{\n{body}\n}} >> {shlex.quote(log)} 2>&1")
        )
    return "\n".join(
        (
            "set -o pipefail",
            preparation,
            f"{{\n{body}\n}} 2>&1 | tee -a {shlex.quote(log)}",
        )
    )


def _bash_arguments(script: str, tag: str | None) -> list[str]:
    arguments = ["bash", "-c", script]
    if tag is not None:
        # 最后一个参数成为 Bash 的 $0，并持续出现在进程命令行中。
        arguments.append(_marker(tag))
    return arguments


def _docker_arguments(
    request: RunRequest, *, log: str | None, detach: bool
) -> list[str]:
    arguments = ["docker", "exec"]
    if detach:
        arguments.append("-d")
    arguments.extend(request.docker_options)
    arguments.append(request.container or "")
    script = _shell_script(request, log=log, detached_container=detach)
    arguments.extend(_bash_arguments(script, request.tag))
    return arguments


def _completed_result(returncode: int, *, log: str | None) -> CommandResult:
    action: Action = "passed" if returncode == 0 else "failed"
    return CommandResult(returncode=returncode, action=action, log=log)


def _print_result(result: CommandResult, request: RunRequest) -> None:
    fields = [f"mode={_mode(request)}"]
    if request.tag is not None:
        fields.append(f"tag={request.tag}")
    if result.pid is not None:
        fields.append(f"pid={result.pid}")
    if result.log is not None:
        fields.append(f"log={result.log}")
    if result.action in {"passed", "failed"}:
        fields.append(f"exit={result.returncode}")
    label = {
        "passed": "SUCCEEDED",
        "started": "STARTED",
        "skipped": "SKIPPED",
        "killed": "KILLED",
        "failed": "FAILED",
    }[result.action]
    destination = sys.stderr if result.action == "failed" else sys.stdout
    print(f"{label} {' '.join(fields)}", file=destination)


def _run_blocking(request: RunRequest, log: str | None) -> CommandResult:
    if request.container is not None:
        arguments = _docker_arguments(request, log=log, detach=False)
    else:
        arguments = _bash_arguments(_shell_script(request, log=log), request.tag)
    try:
        if request.container is None and request.tag is not None:
            process = subprocess.Popen(arguments)
            try:
                _write_pid(request.tag, process.pid)
            except OSError:
                process.terminate()
                process.wait()
                raise
            try:
                returncode = process.wait()
            finally:
                _remove_pid(request.tag, process.pid)
            return _completed_result(returncode, log=log)
        completed = subprocess.run(arguments, check=False)
    except FileNotFoundError:
        executable = "docker" if request.container else "bash"
        print(f"eye: {executable} was not found in PATH", file=sys.stderr)
        return CommandResult(127, "failed", log=log)
    except OSError as exc:
        print(f"eye: failed to start command: {exc}", file=sys.stderr)
        return CommandResult(126, "failed", log=log)
    return _completed_result(completed.returncode, log=log)


def _run_detached_local(request: RunRequest, log: str) -> CommandResult:
    log_path = Path(log)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            log_path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_APPEND
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "ab", buffering=0) as log_stream:
            process = subprocess.Popen(
                _bash_arguments(_shell_script(request, log=None), request.tag),
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
            if request.tag is not None:
                try:
                    _write_pid(request.tag, process.pid)
                except OSError:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    raise
    except FileNotFoundError:
        print("eye: bash was not found in PATH", file=sys.stderr)
        return CommandResult(127, "failed", log=log)
    except OSError as exc:
        print(f"eye: failed to start detached command: {exc}", file=sys.stderr)
        return CommandResult(126, "failed", log=log)
    return CommandResult(0, "started", pid=process.pid, log=log)


def _run_detached_container(request: RunRequest, log: str) -> CommandResult:
    arguments = _docker_arguments(request, log=log, detach=True)
    try:
        completed = subprocess.run(arguments, check=False)
    except FileNotFoundError:
        print("eye: docker was not found in PATH", file=sys.stderr)
        return CommandResult(127, "failed", log=log)
    except OSError as exc:
        print(f"eye: failed to execute docker: {exc}", file=sys.stderr)
        return CommandResult(126, "failed", log=log)
    if completed.returncode != 0:
        return CommandResult(completed.returncode, "failed", log=log)
    return CommandResult(0, "started", log=log)


def _execute(request: RunRequest) -> CommandResult:
    log = _effective_log(request)
    if request.detach:
        if request.container is None:
            result = _run_detached_local(request, log or _default_log(request))
        else:
            result = _run_detached_container(request, log or _default_log(request))
    else:
        result = _run_blocking(request, log)
    _print_result(result, request)
    return result


def run(request: RunRequest) -> CommandResult:
    """按指定阻塞模式在本地或容器内运行命令。"""
    return _execute(request)


def container_is_running(container: str) -> int:
    arguments = [
        "docker",
        "inspect",
        "--type",
        "container",
        "--format",
        "{{if .State.Running}}true{{else}}false{{end}}",
        container,
    ]
    try:
        completed = subprocess.run(
            arguments, check=False, capture_output=True, text=True
        )
    except FileNotFoundError:
        print("eye: docker was not found in PATH", file=sys.stderr)
        return 127
    except OSError as exc:
        print(f"eye: failed to execute docker: {exc}", file=sys.stderr)
        return 126
    if completed.returncode != 0:
        if completed.stderr:
            print(completed.stderr.rstrip(), file=sys.stderr)
        return completed.returncode
    return 0 if completed.stdout.strip() == "true" else 1


def _find_tag(request: RunRequest) -> tuple[int, str | None]:
    if request.container is None:
        return _find_local_tag(request.tag or "")

    pattern = f"{_literal_ere(_marker(request.tag or ''))}$"
    arguments = [
        "docker",
        "exec",
        request.container,
        "pgrep",
        "-f",
        "--",
        pattern,
    ]
    try:
        completed = subprocess.run(
            list(arguments), check=False, capture_output=True, text=True
        )
    except FileNotFoundError:
        print("eye: docker was not found in PATH", file=sys.stderr)
        return 127, None
    except OSError as exc:
        print(f"eye: failed to check tag: {exc}", file=sys.stderr)
        return 126, None
    return completed.returncode, completed.stdout.strip() or None


def find_tagged_pids(container: str | None, tag: str) -> tuple[int, list[int]]:
    """返回指定字面量 tag 对应的全部存活包装进程 PID。"""
    status, output = _find_tag(
        RunRequest(exec_command="", container=container, tag=tag)
    )
    if status != 0 or output is None:
        return status, []
    try:
        return 0, sorted({int(line) for line in output.splitlines() if line.strip()})
    except ValueError:
        return 2, []


def ensure(request: RunRequest) -> CommandResult:
    """仅在指定字面量 tag 不存在时运行命令。"""
    if request.tag is None:
        result = CommandResult(2, "failed", log=_effective_log(request))
        print("eye: ensure requires a tag", file=sys.stderr)
        _print_result(result, request)
        return result

    try:
        with _ensure_lock(request.container, request.tag) as acquired:
            if not acquired:
                print(
                    "SKIPPED reason=ensure_in_progress "
                    f"mode={_mode(request)} tag={request.tag}"
                )
                return CommandResult(0, "skipped", log=_effective_log(request))

            if request.container is not None:
                inspect_status = container_is_running(request.container)
                if inspect_status != 0:
                    print(
                        "eye: container is missing or not running: "
                        f"{request.container}",
                        file=sys.stderr,
                    )
                    result = CommandResult(
                        inspect_status, "failed", log=_effective_log(request)
                    )
                    _print_result(result, request)
                    return result

            check_status, pids = _find_tag(request)
            if check_status == 0:
                first_pid = int(pids.splitlines()[0]) if pids else None
                result = CommandResult(
                    0, "skipped", pid=first_pid, log=_effective_log(request)
                )
                _print_result(result, request)
                return result
            if check_status != 1:
                print(f"eye: failed to check tag: {request.tag}", file=sys.stderr)
                result = CommandResult(
                    check_status, "failed", log=_effective_log(request)
                )
                _print_result(result, request)
                return result

            return _execute(request)
    except OSError as exc:
        print(f"eye: failed to lock ensure task: {exc}", file=sys.stderr)
        result = CommandResult(126, "failed", log=_effective_log(request))
        _print_result(result, request)
        return result
