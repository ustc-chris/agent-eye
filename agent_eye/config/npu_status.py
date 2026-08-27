"""Parse and summarize NPU occupancy for agents."""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass


_TABLE_BORDER = r"\\?\|"
_DEVICE_ROW = re.compile(rf"^\s*{_TABLE_BORDER}\s*(\d+)\s*{_TABLE_BORDER}")
_PROCESS_HEADER = re.compile(r"NPU\s+ID.*Process\s+id", re.IGNORECASE)
_PROCESS_ROW = re.compile(
    rf"^\s*{_TABLE_BORDER}\s*(\d+)\s*{_TABLE_BORDER}"
    rf"\s*(\d+)\s*{_TABLE_BORDER}\s*([^|]+?)\s*{_TABLE_BORDER}"
)
_PS_ROW = re.compile(r"^\s*(\d+)\s+(.*?)\s*$")
_PWDX_ROW = re.compile(r"^\s*(\d+):\s*(.*?)\s*$")
_OWNER_ID = re.compile(r"/([A-Za-z]\d{8})/")
_MAX_PARENT_DEPTH = 256


@dataclass(frozen=True)
class NpuProcess:
    pid: int
    name: str


@dataclass(frozen=True)
class NpuInfo:
    npu_id: int
    processes: tuple[NpuProcess, ...]


@dataclass(frozen=True)
class NpuStatus:
    npu_id: int
    status: str
    process_type: str | None
    owner_id: str | None

    def format(self) -> str:
        return ",".join(
            (
                str(self.npu_id),
                self.status,
                self.process_type or "null",
                self.owner_id or "null",
            )
        )


def parse_npu_smi(output: str) -> list[NpuInfo]:
    """Parse device and process rows from ``npu-smi info`` output."""
    devices: dict[int, list[NpuProcess]] = {}
    in_process_table = False

    for line in output.splitlines():
        if _PROCESS_HEADER.search(line):
            in_process_table = True
            continue

        if not in_process_table:
            match = _DEVICE_ROW.match(line)
            if match:
                devices.setdefault(int(match.group(1)), [])
            continue

        match = _PROCESS_ROW.match(line)
        if not match:
            continue
        npu_id = int(match.group(1))
        process = NpuProcess(
            pid=int(match.group(2)),
            name=match.group(3).strip().replace("\\_", "_"),
        )
        devices.setdefault(npu_id, []).append(process)

    return [
        NpuInfo(npu_id=npu_id, processes=tuple(devices[npu_id]))
        for npu_id in sorted(devices)
    ]


def _query_process(pid: int) -> tuple[int, str] | None:
    try:
        completed = subprocess.run(
            ["ps", "-ww", "-o", "ppid=", "-o", "command=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, OSError):
        return None
    if completed.returncode != 0:
        return None
    match = _PS_ROW.match(completed.stdout)
    if not match:
        return None
    return int(match.group(1)), match.group(2)


def _query_working_directory(pid: int) -> str | None:
    try:
        completed = subprocess.run(
            ["pwdx", str(pid)],
            check=False,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, OSError):
        return None
    if completed.returncode != 0:
        return None
    match = _PWDX_ROW.match(completed.stdout)
    if not match or int(match.group(1)) != pid:
        return None
    return match.group(2)


def find_owner_id(pid: int) -> str | None:
    """Try the PID's cwd, then walk parents for the first path-scoped owner ID."""
    working_directory = _query_working_directory(pid)
    if working_directory is not None:
        owner = _OWNER_ID.search(working_directory)
        if owner:
            return owner.group(1)

    current = pid
    visited: set[int] = set()

    for _ in range(_MAX_PARENT_DEPTH):
        if current <= 0 or current in visited:
            return None
        visited.add(current)

        process = _query_process(current)
        if process is None:
            return None
        parent_pid, command = process
        owner = _OWNER_ID.search(command)
        if owner:
            return owner.group(1)
        if parent_pid == 0:
            return None
        current = parent_pid
    return None


def _normalize_process_name(name: str) -> str:
    normalized = re.sub(r"\s+", "_", name.strip()).replace(",", "_")
    return normalized or "UNKNOWN"


def build_statuses(devices: list[NpuInfo]) -> list[NpuStatus]:
    statuses: list[NpuStatus] = []
    for device in devices:
        if not device.processes:
            statuses.append(NpuStatus(device.npu_id, "FREE", None, None))
            continue

        process_types = {_normalize_process_name(item.name) for item in device.processes}
        process_type = next(iter(process_types)) if len(process_types) == 1 else "MIXED"
        owner_ids = {
            owner
            for item in device.processes
            if (owner := find_owner_id(item.pid)) is not None
        }
        owner_id = next(iter(owner_ids)) if len(owner_ids) == 1 else None
        statuses.append(
            NpuStatus(device.npu_id, "PROCESSING", process_type, owner_id)
        )
    return statuses


def run() -> int:
    """Query NPU state and print one stable, four-column row per NPU."""
    try:
        completed = subprocess.run(
            ["npu-smi", "info"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        print("eye: npu-smi was not found in PATH", file=sys.stderr)
        return 127
    except OSError as exc:
        print(f"eye: failed to execute npu-smi: {exc}", file=sys.stderr)
        return 126

    if completed.returncode != 0:
        detail = completed.stderr.strip()
        print(
            f"eye: npu-smi info failed with exit {completed.returncode}"
            + (f": {detail}" if detail else ""),
            file=sys.stderr,
        )
        return completed.returncode

    devices = parse_npu_smi(completed.stdout)
    if not devices:
        print("eye: npu-smi output contained no NPU records", file=sys.stderr)
        return 2

    for status in build_statuses(devices):
        print(status.format())
    return 0
