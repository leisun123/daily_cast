"""Chinese statutory working-day calendar used by briefing scheduling."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

logger = logging.getLogger(__name__)

IsWorkingDay = Callable[[date], bool]


@dataclass(frozen=True, slots=True)
class ChinaWorkdayCalendar:
    """Holiday + makeup-workday overrides on top of the Mon-Fri baseline."""

    holidays: frozenset[date]
    makeup_workdays: frozenset[date]

    def is_working_day(self, day: date) -> bool:
        """Return True on statutory workdays, including weekend makeup shifts."""
        if day in self.holidays:
            return False
        if day in self.makeup_workdays:
            return True
        return day.weekday() < 5


@dataclass(frozen=True, slots=True)
class WeekdayWorkdayCalendar:
    """Mon-Fri fallback used when no calendar file is available."""

    def is_working_day(self, day: date) -> bool:
        """Treat Monday-Friday as working days."""
        return day.weekday() < 5


def load_workday_calendar(path: Path | None) -> ChinaWorkdayCalendar | WeekdayWorkdayCalendar:
    """Load the configured China calendar, or fall back to Mon-Fri."""
    if path is None:
        return WeekdayWorkdayCalendar()
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning(
            "workday calendar file missing; falling back to Mon-Fri",
            extra={"path": str(path)},
        )
        return WeekdayWorkdayCalendar()
    except (OSError, yaml.YAMLError) as error:
        logger.warning(
            "workday calendar file unreadable; falling back to Mon-Fri",
            extra={"path": str(path), "error": str(error)},
        )
        return WeekdayWorkdayCalendar()
    if not isinstance(payload, dict):
        logger.warning(
            "workday calendar file is not a mapping; falling back to Mon-Fri",
            extra={"path": str(path)},
        )
        return WeekdayWorkdayCalendar()
    years = payload.get("years")
    if not isinstance(years, dict):
        logger.warning(
            "workday calendar file has no years mapping; falling back to Mon-Fri",
            extra={"path": str(path)},
        )
        return WeekdayWorkdayCalendar()
    holidays: set[date] = set()
    makeup_workdays: set[date] = set()
    for year_entry in years.values():
        if not isinstance(year_entry, dict):
            continue
        holidays.update(_parse_dates(year_entry.get("holidays")))
        makeup_workdays.update(_parse_dates(year_entry.get("makeup_workdays")))
    return ChinaWorkdayCalendar(
        holidays=frozenset(holidays),
        makeup_workdays=frozenset(makeup_workdays),
    )


def resolve_is_working_day(calendar: ChinaWorkdayCalendar | WeekdayWorkdayCalendar) -> IsWorkingDay:
    """Return the calendar's working-day predicate."""
    return calendar.is_working_day


def local_today(timezone: str, now: datetime | None = None) -> date:
    """Return the application-local calendar date for schedule decisions."""
    tz = ZoneInfo(timezone)
    moment = now or datetime.now(tz)
    return moment.astimezone(tz).date()


def previous_working_day(day: date, is_working_day: IsWorkingDay) -> date:
    """Return the most recent working day strictly before ``day``."""
    cursor = day - timedelta(days=1)
    for _ in range(31):
        if is_working_day(cursor):
            return cursor
        cursor -= timedelta(days=1)
    return day - timedelta(days=1)


def collection_days_back(run_date: date, is_working_day: IsWorkingDay) -> int:
    """Cover every calendar day since the previous working day's briefing window."""
    return max(1, (run_date - previous_working_day(run_date, is_working_day)).days)


def _parse_dates(raw: object) -> set[date]:
    if not isinstance(raw, list):
        return set()
    parsed: set[date] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        try:
            parsed.add(date.fromisoformat(item))
        except ValueError:
            continue
    return parsed
