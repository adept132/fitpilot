"""P0-08 §8.1: перегенерация будущего не имеет права стирать факт.

До P0-09 правило «удаляем всё после сегодня» было безопасно только потому,
что UserCalendarDay не хранил ни статуса выполнения, ни ссылки на сессию.
"""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from api.services.models import UserCalendarDay
from api.services.periodization.service import _wipe_future_calendar
from api.services.volume.repository import utc_today

pytestmark = pytest.mark.asyncio


async def _day(db, user_id, block_id, target_date, **kw):
    # Дефолты через словарь — см. пояснение в Task 5: именованные аргументы
    # рядом с **kw дают TypeError о дублирующемся ключе.
    fields = {
        "app_user_id": user_id, "target_date": target_date, "block_id": block_id,
        "day_tag": "push", "micro_tag": "medium", "meso_tag": "medium",
        "microcycle_day_number": 1, "is_rest_day": False, "is_blackout": False,
        "status": "planned",
    }
    fields.update(kw)
    day = UserCalendarDay(**fields)
    db.add(day)
    await db.commit()
    await db.refresh(day)
    return day


async def _surviving_dates(db, user_id):
    rows = (await db.execute(
        select(UserCalendarDay.target_date).where(
            UserCalendarDay.app_user_id == user_id
        )
    )).scalars().all()
    return set(rows)


async def test_wipe_keeps_completed_day(db, test_user, active_block):
    today = utc_today()
    done = await _day(db, test_user.id, active_block.id, today, status="completed")
    plain = await _day(db, test_user.id, active_block.id, today + timedelta(days=1))

    await _wipe_future_calendar(db, test_user.id, active_block, today)
    await db.commit()

    survived = await _surviving_dates(db, test_user.id)
    assert done.target_date in survived
    assert plain.target_date not in survived


async def test_wipe_keeps_missed_day(db, test_user, active_block):
    today = utc_today()
    missed = await _day(db, test_user.id, active_block.id, today, status="missed")

    await _wipe_future_calendar(db, test_user.id, active_block, today)
    await db.commit()

    assert missed.target_date in await _surviving_dates(db, test_user.id)


async def test_wipe_keeps_day_with_accepted_adjustments(db, test_user, active_block):
    # Принятая пользователем правка предписания живёт на дне. Стереть её
    # значит отменить решение, которое пользователь уже принял.
    today = utc_today()
    tweaked = await _day(
        db, test_user.id, active_block.id, today + timedelta(days=2),
        volume_adjustments=[{"exercise_id": 1, "delta_sets": 2, "proposal_id": 7}],
    )

    await _wipe_future_calendar(db, test_user.id, active_block, today)
    await db.commit()

    assert tweaked.target_date in await _surviving_dates(db, test_user.id)


async def test_wipe_still_removes_untouched_future(db, test_user, active_block):
    today = utc_today()
    for offset in range(1, 5):
        await _day(db, test_user.id, active_block.id, today + timedelta(days=offset))

    await _wipe_future_calendar(db, test_user.id, active_block, today)
    await db.commit()

    assert await _surviving_dates(db, test_user.id) == set()
