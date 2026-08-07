"""Границы окна и агрегаты цель/предписание/факт."""
from datetime import date, timedelta

import pytest

from api.services.muscle_keys import to_system_key
from api.services.volume.measure import MuscleContribution
from api.services.volume.repository import (
    build_rows,
    current_window,
    prescribed_for,
    utc_today,
    window_after,
)

pytestmark = pytest.mark.asyncio


async def _day(db, user_id, block_id, target_date, micro_day, **kw):
    from api.services.models import UserCalendarDay

    day = UserCalendarDay(
        app_user_id=user_id, target_date=target_date, block_id=block_id,
        day_tag="push", micro_tag="medium", meso_tag="medium",
        microcycle_day_number=micro_day, is_rest_day=False, is_blackout=False,
        status="planned", **kw,
    )
    db.add(day)
    return day


async def test_window_starts_where_microcycle_day_resets_to_one(
    db, test_user, active_block
):
    start = utc_today() - timedelta(days=8)
    # Два микроцикла по 4 дня подряд.
    for offset in range(8):
        await _day(
            db, test_user.id, active_block.id,
            start + timedelta(days=offset), (offset % 4) + 1,
        )
    await db.commit()

    window = await current_window(db, test_user.id, start + timedelta(days=6))
    assert window is not None
    assert window.start_date == start + timedelta(days=4)
    assert window.end_date == start + timedelta(days=7)
    assert window.window_index == 2


async def test_window_after_returns_the_following_microcycle(
    db, test_user, active_block
):
    start = utc_today() - timedelta(days=8)
    for offset in range(12):
        await _day(
            db, test_user.id, active_block.id,
            start + timedelta(days=offset), (offset % 4) + 1,
        )
    await db.commit()

    window = await current_window(db, test_user.id, start + timedelta(days=6))
    nxt = await window_after(db, test_user.id, window)
    assert nxt is not None
    assert nxt.start_date == start + timedelta(days=8)
    assert nxt.window_index == 3


async def test_no_calendar_means_no_window(db, test_user):
    assert await current_window(db, test_user.id, utc_today()) is None


async def test_prescribed_counts_plan_sets_and_adjustments(
    db, test_user, active_block, seeded_plan, seeded_history
):
    today = utc_today()
    await _day(
        db, test_user.id, active_block.id, today, 1,
        plan_id=seeded_plan.id,
        volume_adjustments=[{"exercise_id": seeded_history.id, "delta_sets": 2}],
    )
    await db.commit()

    window = await current_window(db, test_user.id, today)
    prescribed = await prescribed_for(db, test_user.id, window)

    key = seeded_history.main_muscle_group
    assert prescribed[to_system_key(key)].direct > 0


def test_build_rows_merges_three_sources():
    target = {"chest": 12.0, "lats": 10.0}
    prescribed = {"chest": MuscleContribution(direct=9.0, indirect=2.0)}
    performed = {"chest": MuscleContribution(direct=5.0, indirect=4.0)}

    rows = build_rows(target, prescribed, performed)

    assert rows["chest"].target == 12.0
    assert rows["chest"].prescribed == pytest.approx(10.0)
    assert rows["chest"].performed_direct == 5.0
    assert rows["chest"].performed_indirect == 4.0
    assert rows["chest"].performed_effective == pytest.approx(7.0)
    # Мышца из бюджета без предписания и факта всё равно в рядах: недобор
    # виден только если строка существует.
    assert rows["lats"].target == 10.0
    assert rows["lats"].performed_effective == 0.0
