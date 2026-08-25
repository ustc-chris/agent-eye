"""解析并判断全局 --allow 执行时间窗口。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime


DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_SELECTORS = {
    **{name: (index,) for index, name in enumerate(DAY_NAMES)},
    "workday": (0, 1, 2, 3, 4),
    "weekends": (5, 6),
}
TIME_PATTERN = re.compile(r"^[0-9]{4}$")


class AllowSyntaxError(ValueError):
    """--allow 表达式格式错误。"""


@dataclass(frozen=True)
class AllowSchedule:
    intervals: dict[int, tuple[tuple[int, int], ...]]

    def permits(self, moment: datetime) -> bool:
        minute = moment.hour * 60 + moment.minute
        return any(
            start <= minute <= end
            for start, end in self.intervals.get(moment.weekday(), ())
        )


@dataclass(frozen=True)
class AllowDecision:
    allowed: bool
    current: str
    timezone: str


def _parse_time(value: str) -> int:
    if not TIME_PATTERN.fullmatch(value):
        raise AllowSyntaxError(f"time must use four-digit HHMM: {value!r}")
    hour = int(value[:2])
    minute = int(value[2:])
    if hour > 23 or minute > 59:
        raise AllowSyntaxError(f"time is outside 0000-2359: {value!r}")
    return hour * 60 + minute


def _parse_ranges(value: str) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    for item in value.split(","):
        item = item.strip()
        if not item or item.count("-") != 1:
            raise AllowSyntaxError(f"invalid time range: {item!r}")
        start_text, end_text = (part.strip() for part in item.split("-", 1))
        start = _parse_time(start_text)
        end = _parse_time(end_text)
        if start > end:
            raise AllowSyntaxError(
                f"range crosses midnight; split it into two ranges: {item!r}"
            )
        ranges.append((start, end))
    return tuple(ranges)


def parse_allow(expression: str) -> AllowSchedule:
    """将每日或指定星期的 allow 表达式解析为周计划。"""
    expression = expression.strip()
    if not expression:
        raise AllowSyntaxError("expression is empty")
    if "+" in expression:
        raise AllowSyntaxError("use ',' between ranges and ';' between day rules")

    intervals: dict[int, list[tuple[int, int]]] = {
        index: [] for index in range(7)
    }
    if ":" not in expression:
        if ";" in expression:
            raise AllowSyntaxError("daily ranges must be separated with ','")
        ranges = _parse_ranges(expression)
        for day in intervals:
            intervals[day].extend(ranges)
    else:
        for clause in expression.split(";"):
            clause = clause.strip()
            if clause.count(":") != 1:
                raise AllowSyntaxError(f"invalid day rule: {clause!r}")
            selector_text, ranges_text = (
                part.strip() for part in clause.split(":", 1)
            )
            selector = selector_text.lower()
            days = DAY_SELECTORS.get(selector)
            if days is None:
                expected = ", ".join((*DAY_NAMES, "workday", "weekends"))
                raise AllowSyntaxError(
                    f"unknown day selector {selector_text!r}; expected one of: {expected}"
                )
            ranges = _parse_ranges(ranges_text)
            for day in days:
                intervals[day].extend(ranges)

    return AllowSchedule(
        {
            day: tuple(sorted(day_intervals))
            for day, day_intervals in intervals.items()
            if day_intervals
        }
    )


def evaluate_allow(
    expression: str, *, moment: datetime | None = None
) -> AllowDecision:
    schedule = parse_allow(expression)
    current = moment if moment is not None else datetime.now().astimezone()
    day = DAY_NAMES[current.weekday()]
    timezone = current.tzname() or "local"
    return AllowDecision(
        allowed=schedule.permits(current),
        current=f"{day}:{current:%H%M}",
        timezone=timezone,
    )
