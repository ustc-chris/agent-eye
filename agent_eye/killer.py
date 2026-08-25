"""经过安全审查后终止唯一 tag 对应的任务。"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from .runner import CommandResult, container_is_running, find_tagged_pids


# 该名单采用保守策略，并且会在发送任何信号前完成检查。
PROTECTED_PROCESS_NAMES = frozenset(
    {
        "containerd",
        "containerd-shim",
        "cron",
        "crond",
        "dbus-daemon",
        "dockerd",
        "init",
        "kernel_task",
        "kthreadd",
        "kubelet",
        "launchd",
        "networkmanager",
        "polkitd",
        "rsyslogd",
        "sshd",
        "syslogd",
        "systemd",
        "systemd-networkd",
        "systemd-resolved",
        "systemd-timesyncd",
        "systemd-journald",
        "systemd-logind",
        "systemd-udevd",
    }
)


@dataclass(frozen=True)
class KillRequest:
    tag: str
    container: str | None = None


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    ppid: int
    uid: int | None
    name: str
    command: str


def _mode(request: KillRequest) -> str:
    return f"container:{request.container}" if request.container else "local"


def _normal_process_name(name: str) -> str:
    return Path(name.strip().strip("[]")).name.lower()


def _protected_process(processes: list[ProcessInfo]) -> ProcessInfo | None:
    for process in processes:
        if process.pid <= 1:
            return process
        name = _normal_process_name(process.name)
        if name in PROTECTED_PROCESS_NAMES:
            return process
        # Linux 的 comm 最长为 15 字节，因此也保护名单中被截断的名称。
        if len(name) == 15 and any(
            protected.startswith(name) for protected in PROTECTED_PROCESS_NAMES
        ):
            return process
    return None


def _linux_process(pid: int) -> tuple[ProcessInfo, list[int]] | None:
    directory = Path(f"/proc/{pid}")
    try:
        name = (directory / "comm").read_text(encoding="utf-8").strip()
        status_lines = (directory / "status").read_text(encoding="utf-8").splitlines()
        status = {
            key: value.strip()
            for key, value in (line.split(":", 1) for line in status_lines if ":" in line)
        }
        ppid = int(status["PPid"].split()[0])
        uid = int(status["Uid"].split()[0])
        command = (directory / "cmdline").read_bytes().replace(b"\0", b" ").decode(
            "utf-8", errors="replace"
        ).strip()
        children_text = (
            directory / "task" / str(pid) / "children"
        ).read_text(encoding="ascii")
        children = [int(value) for value in children_text.split()]
    except (FileNotFoundError, KeyError, OSError, ValueError):
        return None
    return ProcessInfo(pid, ppid, uid, name, command), children


def _local_process_tree_linux(root_pid: int) -> list[ProcessInfo] | None:
    processes: list[ProcessInfo] = []
    pending = deque([root_pid])
    seen: set[int] = set()
    while pending:
        pid = pending.popleft()
        if pid in seen:
            continue
        seen.add(pid)
        inspected = _linux_process(pid)
        if inspected is None:
            # 任一已发现进程无法审查时都拒绝继续，避免遗漏仍存活的子进程。
            return None
        process, children = inspected
        processes.append(process)
        pending.extend(children)
    return processes


def _local_process_tree(root_pid: int) -> list[ProcessInfo] | None:
    # 不使用可能截断命令行的 ps。缺少 /proc 时采用 fail-closed 策略。
    if not Path("/proc").is_dir():
        return None
    return _local_process_tree_linux(root_pid)


def _container_process_tree(
    container: str, root_pid: int
) -> list[ProcessInfo] | None:
    script = r'''
set -eu
[ -d /proc ] || exit 4
pending="$1"
seen=" "
while [ -n "$pending" ]; do
    set -- $pending
    current="$1"
    shift
    pending="$*"
    case "$seen" in *" $current "*) continue ;; esac
    seen="$seen$current "
    [ -r "/proc/$current/status" ] || exit 4
    [ -r "/proc/$current/comm" ] || exit 4
    [ -r "/proc/$current/cmdline" ] || exit 4
    [ -r "/proc/$current/task/$current/children" ] || exit 4
    name=$(cat "/proc/$current/comm")
    ppid=$(awk '/^PPid:/ {print $2}' "/proc/$current/status")
    uid=$(awk '/^Uid:/ {print $2}' "/proc/$current/status")
    command=$(tr '\000' ' ' < "/proc/$current/cmdline")
    printf '%s\t%s\t%s\t%s\t%s\n' "$current" "$ppid" "$uid" "$name" "$command"
    children=$(cat "/proc/$current/task/$current/children")
    pending="$pending $children"
done
'''
    try:
        completed = subprocess.run(
            [
                "docker",
                "exec",
                container,
                "sh",
                "-c",
                script,
                "eye-kill",
                str(root_pid),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None

    processes: list[ProcessInfo] = []
    for line in completed.stdout.splitlines():
        fields = line.split("\t", 4)
        if len(fields) != 5:
            return None
        try:
            pid, ppid, uid = (int(value) for value in fields[:3])
        except ValueError:
            return None
        processes.append(ProcessInfo(pid, ppid, uid, fields[3], fields[4]))
    if not processes or processes[0].pid != root_pid:
        return None
    return processes


def _kill_local(processes: list[ProcessInfo]) -> int:
    effective_uid = os.geteuid()
    if any(process.uid not in {None, effective_uid} for process in processes):
        return 4
    try:
        for process in reversed(processes):
            try:
                os.kill(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
    except PermissionError:
        return 4
    except OSError:
        return 5
    return 0


def _kill_container(container: str, processes: list[ProcessInfo]) -> int:
    pids = [str(process.pid) for process in reversed(processes)]
    try:
        permission_check = subprocess.run(
            [
                "docker",
                "exec",
                container,
                "sh",
                "-c",
                'kill -0 "$@"',
                "eye-kill",
                *pids,
            ],
            check=False,
        )
        if permission_check.returncode != 0:
            return 4
        completed = subprocess.run(
            [
                "docker",
                "exec",
                container,
                "sh",
                "-c",
                'kill -TERM "$@"',
                "eye-kill",
                *pids,
            ],
            check=False,
        )
    except FileNotFoundError:
        return 127
    except OSError:
        return 126
    return completed.returncode


def _print_failure(reason: str, request: KillRequest, **fields: object) -> None:
    details = [f"mode={_mode(request)}"]
    details.append(f"tag={request.tag}")
    details.extend(f"{key}={value}" for key, value in fields.items())
    print(f"FAILED reason={reason} {' '.join(details)}", file=sys.stderr)


def kill(request: KillRequest) -> CommandResult:
    """审查进程树后，终止恰好一个带指定 tag 的包装进程。"""
    if request.container is not None:
        container_status = container_is_running(request.container)
        if container_status != 0:
            _print_failure("container_unavailable", request)
            return CommandResult(container_status, "failed")

    query_status, pids = find_tagged_pids(request.container, request.tag)
    if query_status == 1 or not pids:
        print(
            "SKIPPED reason=tag_not_found "
            f"mode={_mode(request)} "
            f"tag={request.tag}"
        )
        return CommandResult(0, "skipped")
    if query_status != 0:
        _print_failure("tag_query_failed", request, exit=query_status)
        return CommandResult(query_status, "failed")
    if len(pids) != 1:
        _print_failure("tag_not_unique", request, matches=len(pids))
        return CommandResult(4, "failed")

    root_pid = pids[0]
    if request.container is None:
        processes = _local_process_tree(root_pid)
    else:
        processes = _container_process_tree(request.container, root_pid)
    if processes is None:
        _print_failure("safety_review_failed", request, pid=root_pid)
        return CommandResult(4, "failed")
    expected_marker = f"agent-eye-tag:{request.tag}"
    if expected_marker not in processes[0].command:
        _print_failure("tag_identity_mismatch", request, pid=root_pid)
        return CommandResult(4, "failed")

    protected = _protected_process(processes)
    if protected is not None:
        _print_failure(
            "protected_process",
            request,
            pid=protected.pid,
            process=_normal_process_name(protected.name),
        )
        return CommandResult(4, "failed")

    if request.container is None:
        status = _kill_local(processes)
    else:
        status = _kill_container(request.container, processes)
    if status != 0:
        _print_failure("signal_failed", request, pid=root_pid, exit=status)
        return CommandResult(status, "failed")

    print(
        f"KILLED mode={_mode(request)} "
        f"tag={request.tag} pid={root_pid} processes={len(processes)}"
    )
    return CommandResult(0, "killed", pid=root_pid)
