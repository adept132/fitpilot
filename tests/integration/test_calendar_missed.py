"""Пропущенные дни проставляются лениво; adherence считает только рабочие дни."""
from datetime import date, timedelta

import pytest

from api.services.models import UserCalendarDay
from api.services.volume.repository import adherence_for_range, mark_missed_days, utc_today

pytestmark = pytest.mark.asyncio


async def _day(db, user_id, target_date, **kw):
    # Дефолты через словарь, а не именованными аргументами рядом с **kw:
    # иначе _day(..., is_rest_day=True) даёт TypeError о дублирующемся
    # ключе, потому что тот же ключ уже передан явно.
    fields = {
        "app_user_id": user_id, "target_date": target_date, "day_tag": "push",
        "micro_tag": "medium", "meso_tag": "medium", "microcycle_day_number": 1,
        "is_rest_day": False, "is_blackout": False, "status": "planned",
    }
    fields.update(kw)
    day = UserCalendarDay(**fields)
    db.add(day)
    await db.commit()
    await db.refresh(day)
    return day


async def test_past_planned_day_becomes_missed(db, test_user):
    day = await _day(db, test_user.id, utc_today() - timedelta(days=2))
    marked = await mark_missed_days(db, test_user.id, utc_today())
    await db.commit()
    await db.refresh(day)
    assert marked == 1
    assert day.status == "missed"


async def test_today_is_not_missed_yet(db, test_user):
    day = await _day(db, test_user.id, utc_today())
    await mark_missed_days(db, test_user.id, utc_today())
    await db.commit()
    await db.refresh(day)
    assert day.status == "planned"


async def test_future_day_is_not_missed(db, test_user):
    day = await _day(db, test_user.id, utc_today() + timedelta(days=3))
    await mark_missed_days(db, test_user.id, utc_today())
    await db.commit()
    await db.refresh(day)
    assert day.status == "planned"


async def test_rest_and_blackout_days_are_never_missed(db, test_user):
    rest = await _day(db, test_user.id, utc_today() - timedelta(days=2), is_rest_day=True)
    black = await _day(db, test_user.id, utc_today() - timedelta(days=3), is_blackout=True)
    await mark_missed_days(db, test_user.id, utc_today())
    await db.commit()
    await db.refresh(rest)
    await db.refresh(black)
    assert rest.status == "planned"
    assert black.status == "planned"


async def test_completed_day_is_not_overwritten(db, test_user):
    day = await _day(db, test_user.id, utc_today() - timedelta(days=2), status="completed")
    await mark_missed_days(db, test_user.id, utc_today())
    await db.commit()
    await db.refresh(day)
    assert day.status == "completed"


async def test_adherence_counts_only_working_days(db, test_user):
    start = utc_today() - timedelta(days=5)
    await _day(db, test_user.id, start, status="completed")
    await _day(db, test_user.id, start + timedelta(days=1), status="missed")
    await _day(db, test_user.id, start + timedelta(days=2), is_rest_day=True)
    await _day(db, test_user.id, start + timedelta(days=3), is_blackout=True)
    await _day(db, test_user.id, start + timedelta(days=4), status="completed")

    result = await adherence_for_range(db, test_user.id, start, start + timedelta(days=4))
    assert result.planned_days == 3
    assert result.completed_days == 2
    assert result.missed_days == 1
