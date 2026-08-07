"""Закрытое окно замораживается снимком и не пересоздаётся повторно."""
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from api.services.models import UserCalendarDay, VolumeWindow
from api.services.volume.repository import (
    close_window,
    closed_windows,
    current_window,
    recompute_stale_window,
    utc_today,
)

pytestmark = pytest.mark.asyncio


async def _seed_microcycle(db, user_id, block_id, start, plan_id, days=4):
    """Дни микроцикла. `plan_id` обязателен и проставляется на КАЖДЫЙ день:
    предохранитель close_window считает долю рабочих дней окна с
    предписанием (UserCalendarDay.plan_id IS NOT NULL) и отсекает окно, если
    она ниже половины (MIN_PRESCRIBED_DAY_RATIO) — без plan_id на днях этот
    предохранитель срабатывал бы всегда и ни одно окно не закрывалось."""
    for offset in range(days):
        db.add(UserCalendarDay(
            app_user_id=user_id, target_date=start + timedelta(days=offset),
            block_id=block_id, day_tag="push", micro_tag="medium",
            meso_tag="medium", microcycle_day_number=offset + 1,
            is_rest_day=False, is_blackout=False,
            status="completed" if offset < 3 else "missed",
            plan_id=plan_id,
        ))
    await db.commit()


async def test_close_window_writes_snapshot(db, test_user, active_block, seeded_plan):
    start = utc_today() - timedelta(days=6)
    await _seed_microcycle(db, test_user.id, active_block.id, start, seeded_plan.id)

    window = await current_window(db, test_user.id, start)
    row = await close_window(
        db, test_user.id, window, {"chest": 12.0}, level="intermediate"
    )
    await db.commit()

    assert row.window_index == window.window_index
    assert row.start_date == window.start_date
    assert row.adherence["planned_days"] == 4
    assert row.adherence["completed_days"] == 3
    assert row.adherence["missed_days"] == 1
    assert row.muscles["chest"]["target"] == 12.0
    # Снимок landmarks обязателен: таблица калибруемая, и решение,
    # принятое по старым границам, должно остаться объяснимым.
    assert row.landmarks["chest"]["mrv"] == 18


async def test_close_window_is_idempotent(db, test_user, active_block, seeded_plan):
    start = utc_today() - timedelta(days=6)
    await _seed_microcycle(db, test_user.id, active_block.id, start, seeded_plan.id)
    window = await current_window(db, test_user.id, start)

    first = await close_window(db, test_user.id, window, {}, level="intermediate")
    await db.commit()
    second = await close_window(db, test_user.id, window, {}, level="intermediate")
    await db.commit()

    assert first.id == second.id
    total = (await db.execute(
        select(func.count(VolumeWindow.id)).where(
            VolumeWindow.app_user_id == test_user.id
        )
    )).scalar_one()
    assert total == 1


async def test_window_with_too_few_prescribed_days_is_not_closed(
    db, test_user, active_block
):
    # Обрывок после смены сплита — не окно.
    start = utc_today() - timedelta(days=6)
    for offset in range(4):
        db.add(UserCalendarDay(
            app_user_id=test_user.id, target_date=start + timedelta(days=offset),
            block_id=active_block.id, day_tag="push", micro_tag="medium",
            meso_tag="medium", microcycle_day_number=offset + 1,
            is_rest_day=False, is_blackout=False, status="planned",
            plan_id=None,
        ))
    await db.commit()

    window = await current_window(db, test_user.id, start)
    row = await close_window(db, test_user.id, window, {}, level="intermediate")
    assert row is None


async def test_blackout_days_do_not_reject_the_window(
    db, test_user, active_block, seeded_plan
):
    """Ревью Task 9: blackout-дни не несут предписания по построению.
    Считать их днями без плана значит отвергнуть окно за отпуск."""
    start = utc_today() - timedelta(days=6)

    # Два рабочих дня с планом и два blackout — по старому знаменателю
    # доля была бы 2/4 и окно бы уцелело только на грани; делаем
    # blackout-дней больше, чтобы старая логика гарантированно отвергла.
    for offset in range(2):
        db.add(UserCalendarDay(
            app_user_id=test_user.id, target_date=start + timedelta(days=offset),
            block_id=active_block.id, plan_id=seeded_plan.id, day_tag="push",
            micro_tag="medium", meso_tag="medium",
            microcycle_day_number=offset + 1,
            is_rest_day=False, is_blackout=False, status="completed",
        ))
    for offset in range(2, 5):
        db.add(UserCalendarDay(
            app_user_id=test_user.id, target_date=start + timedelta(days=offset),
            block_id=active_block.id, plan_id=None, day_tag="push",
            micro_tag="medium", meso_tag="medium",
            microcycle_day_number=offset + 1,
            is_rest_day=False, is_blackout=True, status="planned",
        ))
    await db.commit()

    window = await current_window(db, test_user.id, start)
    row = await close_window(db, test_user.id, window, {}, level="intermediate")
    await db.commit()

    assert row is not None, "окно отвергнуто из-за blackout-дней"


async def test_closed_windows_returns_newest_first(db, test_user, active_block, seeded_plan):
    start = utc_today() - timedelta(days=12)
    await _seed_microcycle(db, test_user.id, active_block.id, start, seeded_plan.id)
    await _seed_microcycle(
        db, test_user.id, active_block.id, start + timedelta(days=4), seeded_plan.id
    )

    first = await current_window(db, test_user.id, start)
    second = await current_window(db, test_user.id, start + timedelta(days=4))
    await close_window(db, test_user.id, first, {}, level="intermediate")
    await close_window(db, test_user.id, second, {}, level="intermediate")
    await db.commit()

    rows = await closed_windows(db, test_user.id, limit=5)
    assert [r.window_index for r in rows] == [2, 1]


async def _seed_finished_session(db, user_id, exercise_id, started_on, sets_count):
    """Завершённая сессия внутри окна. Нужна, чтобы у окна вообще был факт:
    без сессий recompute_stale_window нечего пересчитывать и он вернёт None."""
    from datetime import datetime, time, timezone

    from api.services.models import (
        WorkoutSession,
        WorkoutSessionExercise,
        WorkoutSessionSet,
    )

    session_row = WorkoutSession(
        app_user_id=user_id,
        source="free",
        status="finished",
        started_at=datetime.combine(started_on, time(12, 0), tzinfo=timezone.utc),
        finished_at=datetime.combine(started_on, time(13, 0), tzinfo=timezone.utc),
    )
    db.add(session_row)
    await db.flush()

    session_exercise = WorkoutSessionExercise(
        workout_session_id=session_row.id, exercise_id=exercise_id, order_index=0
    )
    db.add(session_exercise)
    await db.flush()

    for i in range(sets_count):
        db.add(WorkoutSessionSet(
            workout_session_exercise_id=session_exercise.id,
            set_number=i + 1,
            weight=50.0, reps=8, is_completed=True,
            set_type="normal", is_anomalous=False,
        ))
    await db.commit()
    return session_row


async def test_recompute_returns_none_when_window_was_not_touched(
    db, test_user, active_block, seeded_history, seeded_plan
):
    start = utc_today() - timedelta(days=6)
    await _seed_microcycle(db, test_user.id, active_block.id, start, seeded_plan.id)
    await _seed_finished_session(db, test_user.id, seeded_history.id, start, 3)

    window = await current_window(db, test_user.id, start)
    snapshot = await close_window(db, test_user.id, window, {}, level="intermediate")
    await db.commit()

    # Снимок сделан ПОСЛЕ последней правки подхода — пересчитывать нечего.
    assert await recompute_stale_window(db, test_user.id, snapshot) is None


async def test_snapshot_is_recomputed_after_backdated_set_edit(
    db, test_user, active_block, seeded_history, seeded_plan
):
    from datetime import datetime, timezone
    from api.services.muscle_keys import to_system_key

    start = utc_today() - timedelta(days=6)
    await _seed_microcycle(db, test_user.id, active_block.id, start, seeded_plan.id)
    await _seed_finished_session(db, test_user.id, seeded_history.id, start, 3)

    window = await current_window(db, test_user.id, start)
    snapshot = await close_window(db, test_user.id, window, {}, level="intermediate")
    await db.commit()

    muscle = to_system_key(seeded_history.main_muscle_group)
    assert snapshot.muscles[muscle]["performed_direct"] == 3.0

    # Правка задним числом: добавили подход в уже закрытом окне. Снимок
    # старше правки — updated_at подхода проставляется сервером на INSERT.
    snapshot.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    await _seed_finished_session(db, test_user.id, seeded_history.id, start, 2)

    refreshed = await recompute_stale_window(db, test_user.id, snapshot)
    await db.commit()

    assert refreshed is not None
    assert refreshed.id == snapshot.id
    assert refreshed.muscles[muscle]["performed_direct"] == 5.0
