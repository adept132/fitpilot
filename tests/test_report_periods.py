from datetime import date

import pytest

from api.services.reports.periods import (
    CATCHUP_DEPTH,
    closed_periods,
    period_bounds,
)


def test_week_is_iso_monday_to_sunday():
    # 2026-08-14 — пятница.
    assert period_bounds("week", date(2026, 8, 14)) == (date(2026, 8, 10), date(2026, 8, 16))


def test_month_and_year_are_calendar():
    assert period_bounds("month", date(2026, 8, 14)) == (date(2026, 8, 1), date(2026, 8, 31))
    assert period_bounds("year", date(2026, 8, 14)) == (date(2026, 1, 1), date(2026, 12, 31))


def test_february_of_leap_year_ends_on_29th():
    assert period_bounds("month", date(2028, 2, 3))[1] == date(2028, 2, 29)


def test_unknown_period_type_raises():
    with pytest.raises(ValueError):
        period_bounds("decade", date(2026, 8, 14))


def test_closed_periods_never_include_the_current_one():
    """Понедельник 17 августа: текущая неделя ещё идёт, последняя закрытая —
    10–16 августа."""
    periods = closed_periods("week", date(2026, 8, 17))
    assert periods[0] == (date(2026, 8, 10), date(2026, 8, 16))
    assert all(end < date(2026, 8, 17) for _, end in periods)


def test_closed_periods_are_limited_by_catchup_depth():
    assert len(closed_periods("week", date(2026, 8, 17))) == CATCHUP_DEPTH["week"]
    assert len(closed_periods("month", date(2026, 8, 17))) == CATCHUP_DEPTH["month"]
    assert len(closed_periods("year", date(2026, 8, 17))) == CATCHUP_DEPTH["year"]


def test_closed_periods_are_ordered_newest_first():
    periods = closed_periods("week", date(2026, 8, 17))
    assert periods == sorted(periods, reverse=True)
