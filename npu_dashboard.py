#!/usr/bin/env python3
"""Query Agent Eye on remote servers and render a live terminal dashboard."""

from __future__ import annotations

# ============================== 配置区 ==============================

# 远端 NPU 查询刷新周期，单位：秒。
QUERY_REFRESH_SECONDS = 20.0

# 终端画面和倒计时刷新周期，单位：秒。
TERMINAL_REFRESH_SECONDS = 0.5

# 期望的最大分列数。终端宽度不足时会自动减少列数。
DISPLAY_COLUMNS = 1

# ip 支持主机名、IP 或 SSH 的 user@host 写法。alias 可以留空。
MACHINES = [
    # {"name": "server-01", "ip": "root@192.168.1.10", "alias": "推理节点 A"},
    # {"name": "server-02", "ip": "root@192.168.1.11", "alias": "推理节点 B"},
]

# 远端命令和单机查询超时。远端需要提前配置 SSH 密钥和 known_hosts。
REMOTE_EYE_COMMAND = "eye npu_status"
SSH_TIMEOUT_SECONDS = 15.0

# ===================================================================

import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Sequence


_STATUS_ROW = re.compile(
    r"^\s*(\d+)\s*,\s*(FREE|PROCESSING)\s*,\s*([^,\r\n]+?)\s*,"
    r"\s*([^,\r\n]+?)\s*$"
)
_RESET = "\033[0m"
_BOLD = "\033[1m"
_CYAN = "\033[36m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_BLUE = "\033[34m"
_DIM = "\033[2m"
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CONTROL_CHARACTER = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


@dataclass(frozen=True)
class Machine:
    name: str
    ip: str
    alias: str = ""

    @property
    def label(self) -> str:
        return f"{self.name} ({self.alias})" if self.alias else self.name


@dataclass(frozen=True)
class NpuRow:
    npu_id: int
    status: str
    process_type: str | None
    owner_id: str | None

    @property
    def display_status(self) -> str:
        if self.status == "FREE":
            return "FREE"
        process = self.process_type or "UNKNOWN"
        owner = self.owner_id or "未知运行者"
        return f"{process}（{owner}）"


@dataclass(frozen=True)
class MachineResult:
    rows: tuple[NpuRow, ...] = ()
    error: str | None = None


def _null_to_none(value: str) -> str | None:
    normalized = value.strip()
    return None if normalized.lower() == "null" else normalized


def parse_eye_output(output: str) -> tuple[NpuRow, ...]:
    """Extract Agent Eye's stable four-column rows, ignoring SSH banners."""
    rows: dict[int, NpuRow] = {}
    for line in output.splitlines():
        match = _STATUS_ROW.match(line)
        if not match:
            continue
        npu_id = int(match.group(1))
        rows[npu_id] = NpuRow(
            npu_id=npu_id,
            status=match.group(2),
            process_type=_null_to_none(match.group(3)),
            owner_id=_null_to_none(match.group(4)),
        )
    return tuple(rows[npu_id] for npu_id in sorted(rows))


def query_machine(machine: Machine) -> MachineResult:
    """Run ``eye npu_status`` on one server over non-interactive SSH."""
    connect_timeout = max(1, int(SSH_TIMEOUT_SECONDS))
    arguments = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={connect_timeout}",
        machine.ip,
        REMOTE_EYE_COMMAND,
    ]
    try:
        completed = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            timeout=SSH_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return MachineResult(error="本机未安装 ssh")
    except subprocess.TimeoutExpired:
        return MachineResult(error=f"查询超时（{SSH_TIMEOUT_SECONDS:g}s）")
    except OSError as exc:
        return MachineResult(error=f"SSH 启动失败：{exc}")

    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()
        message = detail[-1] if detail else f"远端退出码 {completed.returncode}"
        return MachineResult(error=message)

    rows = parse_eye_output(completed.stdout)
    if not rows:
        return MachineResult(error="远端未返回有效的 NPU 状态")
    return MachineResult(rows=rows)


def load_machines(config: Sequence[Mapping[str, object]]) -> tuple[Machine, ...]:
    machines: list[Machine] = []
    for index, item in enumerate(config, start=1):
        name = item.get("name")
        ip = item.get("ip")
        alias = item.get("alias", "")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"MACHINES 第 {index} 项缺少有效 name")
        if not isinstance(ip, str) or not ip.strip():
            raise ValueError(f"MACHINES 第 {index} 项缺少有效 ip")
        if not isinstance(alias, str):
            raise ValueError(f"MACHINES 第 {index} 项的 alias 必须是字符串")
        machines.append(Machine(name.strip(), ip.strip(), alias.strip()))
    return tuple(machines)


def _supports_color() -> bool:
    return (
        sys.stdout.isatty()
        and "NO_COLOR" not in os.environ
        and os.environ.get("TERM", "") != "dumb"
    )


def _paint(text: str, *styles: str) -> str:
    if not _supports_color():
        return text
    return f"{''.join(styles)}{text}{_RESET}"


def _safe_text(text: str) -> str:
    return _CONTROL_CHARACTER.sub("", _ANSI_ESCAPE.sub("", text))


def _character_width(character: str) -> int:
    if unicodedata.combining(character):
        return 0
    return 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1


def _display_width(text: str) -> int:
    return sum(_character_width(character) for character in text)


def _clip(text: str, width: int) -> str:
    text = _safe_text(text)
    if width <= 0:
        return ""
    if _display_width(text) <= width:
        return text
    if width == 1:
        return "…"
    clipped: list[str] = []
    used = 0
    for character in text:
        character_width = _character_width(character)
        if used + character_width > width - 1:
            break
        clipped.append(character)
        used += character_width
    return "".join(clipped) + "…"


def _pad(text: str, width: int, *, center: bool = False) -> str:
    clipped = _clip(text, width)
    padding = max(0, width - _display_width(clipped))
    if not center:
        return clipped + " " * padding
    left = padding // 2
    return " " * left + clipped + " " * (padding - left)


def _cell(text: str, width: int) -> str:
    content_width = max(0, width - 2)
    return " " + _pad(text, content_width) + " "


def _table_style(result: MachineResult | None) -> str:
    if result is None:
        return _CYAN
    if result.error:
        return _RED
    free_count = sum(row.status == "FREE" for row in result.rows)
    if free_count == 0:
        return _RED
    if free_count <= 3:
        return _YELLOW
    return _GREEN


def _availability(result: MachineResult | None) -> str:
    if result is None or result.error:
        return "-/-"
    free_count = sum(row.status == "FREE" for row in result.rows)
    return f"{free_count}/{len(result.rows)}"


def render_machine(
    machine: Machine,
    result: MachineResult | None,
    width: int,
    *,
    querying: bool,
) -> list[str]:
    """Render one machine as a fixed-width ASCII card."""
    width = max(30, width)
    inner = width - 2
    npu_width = min(12, max(8, inner // 3))
    status_width = inner - npu_width - 1
    border = "+" + "#" * inner + "+"
    divider = "+" + "#" * npu_width + "+" + "#" * status_width + "+"
    table_style = _table_style(result)
    title = f"{machine.label}     free: {_availability(result)}"
    lines = [
        _paint(border, table_style),
        _paint(f"|{_pad(title, inner, center=True)}|", table_style),
        _paint(border, table_style),
    ]

    if result is None:
        message = "正在查询..." if querying else "等待查询..."
        lines.extend(
            (
                _paint(f"|{_cell(message, inner)}|", table_style),
                _paint(border, table_style),
            )
        )
        return lines
    if result.error:
        message = f"ERROR: {result.error}"
        lines.extend(
            (
                _paint(f"|{_cell(message, inner)}|", table_style),
                _paint(border, table_style),
            )
        )
        return lines

    lines.append(
        _paint(
            f"|{_cell('NPU', npu_width)}|{_cell('STATUS', status_width)}|",
            table_style,
        )
    )
    lines.append(_paint(divider, table_style))
    for row in result.rows:
        content_style = _BLUE if row.status == "FREE" else _RED
        lines.append(
            _paint("|", table_style)
            + _paint(_cell(f"NPU {row.npu_id}", npu_width), content_style)
            + _paint("|", table_style)
            + _paint(_cell(row.display_status, status_width), content_style)
            + _paint("|", table_style)
        )
    lines.append(_paint(border, table_style))
    return lines


def _layout(
    machines: Sequence[Machine],
    results: Mapping[Machine, MachineResult],
    querying: bool,
    terminal_width: int,
) -> list[str]:
    gap = 2
    requested_columns = max(1, int(DISPLAY_COLUMNS))
    max_columns_by_width = max(1, (terminal_width + gap) // (30 + gap))
    columns = min(requested_columns, max_columns_by_width, max(1, len(machines)))
    card_width = max(30, (terminal_width - gap * (columns - 1)) // columns)

    cards = [
        render_machine(
            machine,
            results.get(machine),
            card_width,
            querying=querying,
        )
        for machine in machines
    ]
    output: list[str] = []
    for start in range(0, len(cards), columns):
        group = cards[start : start + columns]
        height = max(len(card) for card in group)
        for line_index in range(height):
            output.append(
                (" " * gap).join(
                    card[line_index] if line_index < len(card) else " " * card_width
                    for card in group
                ).rstrip()
            )
        if start + columns < len(cards):
            output.append("")
    return output


def _format_time(moment: datetime | None) -> str:
    if moment is None:
        return "等待首次查询"
    return (
        f"{moment.year}-{moment.month}-{moment.day} "
        f"{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}"
    )


def render_dashboard(
    machines: Sequence[Machine],
    results: Mapping[Machine, MachineResult],
    *,
    last_refresh: datetime | None,
    seconds_to_refresh: float,
    querying: bool,
    terminal_width: int | None = None,
) -> str:
    width = terminal_width or shutil.get_terminal_size((100, 30)).columns
    width = max(30, width)
    lines = [_paint("Agent Eye · NPU Dashboard", _BOLD, _CYAN), "", "机器列表："]
    for index, machine in enumerate(machines, start=1):
        lines.append(_clip(f"  {index}. {machine.label}  [{machine.ip}]", width))
    lines.extend(("", *_layout(machines, results, querying, width), ""))

    refresh_text = (
        "正在查询" if querying else f"{max(0.0, seconds_to_refresh):.1f}s"
    )
    footer = (
        f"Last refresh: {_format_time(last_refresh)}  |  "
        f"Next refresh: {refresh_text}/{QUERY_REFRESH_SECONDS:g}s"
    )
    lines.append(_paint(_clip(footer, width), _DIM))
    return "\n".join(lines)


def main() -> int:
    try:
        machines = load_machines(MACHINES)
    except ValueError as exc:
        print(f"dashboard: {exc}", file=sys.stderr)
        return 2
    if not machines:
        print("dashboard: 请先在 npu_dashboard.py 顶部的 MACHINES 中配置服务器", file=sys.stderr)
        return 2
    if QUERY_REFRESH_SECONDS <= 0 or TERMINAL_REFRESH_SECONDS <= 0:
        print("dashboard: 刷新时间必须大于 0", file=sys.stderr)
        return 2

    results: dict[Machine, MachineResult] = {}
    active: dict[Machine, Future[MachineResult]] | None = None
    last_refresh: datetime | None = None
    next_refresh = time.monotonic()
    executor = ThreadPoolExecutor(max_workers=len(machines))
    interactive = sys.stdout.isatty()

    if interactive:
        sys.stdout.write("\033[?25l")
        sys.stdout.flush()
    try:
        while True:
            now = time.monotonic()
            if active is None and now >= next_refresh:
                active = {
                    machine: executor.submit(query_machine, machine)
                    for machine in machines
                }

            if active is not None:
                for machine, future in tuple(active.items()):
                    if not future.done():
                        continue
                    try:
                        results[machine] = future.result()
                    except Exception as exc:  # Defensive isolation between servers.
                        results[machine] = MachineResult(error=f"内部查询错误：{exc}")
                    del active[machine]
                if not active:
                    active = None
                    last_refresh = datetime.now()
                    next_refresh = time.monotonic() + QUERY_REFRESH_SECONDS

            remaining = 0.0 if active is not None else next_refresh - time.monotonic()
            screen = render_dashboard(
                machines,
                results,
                last_refresh=last_refresh,
                seconds_to_refresh=remaining,
                querying=active is not None,
            )
            if interactive:
                sys.stdout.write("\033[H\033[J")
            sys.stdout.write(screen + "\n")
            sys.stdout.flush()
            time.sleep(TERMINAL_REFRESH_SECONDS)
    except KeyboardInterrupt:
        return 0
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
        if interactive:
            sys.stdout.write("\033[?25h\n")
            sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
