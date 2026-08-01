"""Calendar date calculations shared by discovery and backtests."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timezone


@dataclass(frozen=True, order=True)
class DateWindow:
    start: date
    end: date
    kind: str = "benchmark"

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError("date window end precedes start")

    @property
    def key(self) -> str:
        return f"{self.start.isoformat()}_{self.end.isoformat()}"


def parse_iso_date(value: str, *, label: str = "date") -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must use YYYY-MM-DD format") from exc


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def subtract_calendar_months(value: date, months: int) -> date:
    if months < 0:
        raise ValueError("months must be non-negative")
    month_index = value.year * 12 + value.month - 1 - months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def add_calendar_months(value: date, months: int) -> date:
    if months < 0:
        return subtract_calendar_months(value, -months)
    month_index = value.year * 12 + value.month - 1 + months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def resolve_range(
    explicit_from: str | None,
    explicit_until: str | None,
    *,
    months: int = 6,
    today: str | None = None,
) -> tuple[date, date, date]:
    resolved_today = parse_iso_date(today, label="today") if today else utc_today()
    if bool(explicit_from) != bool(explicit_until):
        raise ValueError("--from and --until must be supplied together")
    if explicit_from and explicit_until:
        start = parse_iso_date(explicit_from, label="from")
        end = parse_iso_date(explicit_until, label="until")
    else:
        if months <= 0:
            raise ValueError("months must be greater than zero")
        end = resolved_today
        start = subtract_calendar_months(end, months)
    if end < start:
        raise ValueError("until must be on or after from")
    return start, end, resolved_today


def benchmark_windows(
    start: date,
    as_of: date,
    *,
    window_months: int = 6,
    step_months: int = 6,
) -> list[DateWindow]:
    if window_months <= 0 or step_months <= 0:
        raise ValueError("window and step months must be greater than zero")
    windows: list[DateWindow] = []
    cursor = start
    while True:
        next_start = add_calendar_months(cursor, window_months)
        window_end = date.fromordinal(next_start.toordinal() - 1)
        if window_end > as_of:
            break
        windows.append(DateWindow(cursor, window_end, "completed"))
        cursor = add_calendar_months(cursor, step_months)
    if step_months == window_months and windows:
        boundary = windows[-1].end
        if boundary != as_of:
            rolling = DateWindow(subtract_calendar_months(as_of, window_months), as_of, "current")
            if rolling not in windows:
                windows.append(rolling)
    return windows
