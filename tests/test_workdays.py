"""Working-day calendar tests for briefing scheduling windows."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from dailycast.core.workdays import (
    ChinaWorkdayCalendar,
    WeekdayWorkdayCalendar,
    collection_days_back,
    load_workday_calendar,
    previous_working_day,
)


def test_china_calendar_treats_makeup_sunday_as_a_workday() -> None:
    """2026-09-20 is a Mid-Autumn makeup shift and must be a working day."""
    calendar = ChinaWorkdayCalendar(
        holidays=frozenset({date(2026, 9, 25), date(2026, 9, 26), date(2026, 9, 27)}),
        makeup_workdays=frozenset({date(2026, 9, 20)}),
    )

    assert calendar.is_working_day(date(2026, 9, 20)) is True
    assert calendar.is_working_day(date(2026, 9, 19)) is False
    assert calendar.is_working_day(date(2026, 9, 25)) is False
    assert calendar.is_working_day(date(2026, 9, 21)) is True


def test_collection_window_covers_days_since_previous_working_day() -> None:
    """A makeup Sunday looks back through Friday and Saturday."""
    calendar = ChinaWorkdayCalendar(
        holidays=frozenset(),
        makeup_workdays=frozenset({date(2026, 9, 20)}),
    )

    assert previous_working_day(date(2026, 9, 20), calendar.is_working_day) == date(2026, 9, 18)
    assert collection_days_back(date(2026, 9, 20), calendar.is_working_day) == 2
    # Ordinary Monday after a non-working weekend still covers Fri-Sun.
    assert collection_days_back(date(2026, 8, 24), WeekdayWorkdayCalendar().is_working_day) == 3
    # Ordinary Tuesday covers only Monday.
    assert collection_days_back(date(2026, 8, 25), WeekdayWorkdayCalendar().is_working_day) == 1


def test_load_workday_calendar_reads_project_config() -> None:
    """The committed project calendar marks 2026-09-20 as a makeup workday."""
    path = Path("config/china_workdays.yaml")
    calendar = load_workday_calendar(path)

    assert isinstance(calendar, ChinaWorkdayCalendar)
    assert calendar.is_working_day(date(2026, 9, 20)) is True
    assert calendar.is_working_day(date(2026, 9, 25)) is False
    assert calendar.is_working_day(date(2026, 10, 10)) is True


def test_missing_calendar_file_falls_back_to_weekdays() -> None:
    """A missing calendar path must not disable delivery entirely."""
    calendar = load_workday_calendar(Path("config/does-not-exist.yaml"))

    assert isinstance(calendar, WeekdayWorkdayCalendar)
    assert calendar.is_working_day(date(2026, 9, 20)) is False
    assert calendar.is_working_day(date(2026, 9, 21)) is True
