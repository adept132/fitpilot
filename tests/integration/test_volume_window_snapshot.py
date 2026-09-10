"""Закрытое окно замораживается снимком и не пересоздаётся повторно."""
import asyncio
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from api.services.models import TrainingBlock, UserCalendarDay, VolumeWindow
from api.services.volume.repository import (
    close_window,
    closed_windows,
    current_window,
    recompute_stale_window,
    utc_today,
)
from app.database import SessionLocal

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


async def test_close_window_scales_landmarks_by_block_microcycle_length(
    db, test_user, seeded_plan
):
    """P0-09 I2 (Important): landmarks._TABLE — за 7 ДНЕЙ. На блоке с
    microcycle_length=10 замороженные в снимке границы обязаны быть
    смасштабированы (cycle_multiplier=10/7), а не взяты сырыми — иначе
    decide() судит десятидневный факт семидневным потолком.

    Chest/intermediate raw (mev=6, mav=12, mrv=18). Масштаб 10/7:
    floor(6*10/7)=8, floor(12*10/7)=17, floor(18*10/7)=25.
    """
    block = TrainingBlock(
        phase_snapshot_trusted=True,
        app_user_id=test_user.id, block_index=1,
        phases=[{"phase_number": 1, "name": "medium", "effort_tier": "medium", "length_days": 10}],
        microcycle_length=10,
        start_date=utc_today() - timedelta(days=10),
        planned_end_date=utc_today() + timedelta(days=20),
        status="active",
    )
    db.add(block)
    await db.flush()

    start = utc_today() - timedelta(days=9)
    await _seed_microcycle(db, test_user.id, block.id, start, seeded_plan.id, days=10)

    window = await current_window(db, test_user.id, start)
    row = await close_window(
        db, test_user.id, window, {"chest": 24.0}, level="intermediate"
    )
    await db.commit()

    assert row is not None
    assert row.landmarks["chest"]["mev"] == 8
    assert row.landmarks["chest"]["mav"] == 17
    assert row.landmarks["chest"]["mrv"] == 25


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


async def test_close_window_race_does_not_leave_duplicate_or_raise(
    db, test_user, active_block, seeded_plan
):
    """P0-09 I6 (Important): чтение `existing` и вставка снимка в close_window
    неатомарны — /workout-center/context и /periodization/context дёргаются
    с клиента одновременно на старте приложения. Без уникального индекса
    гонка создаёт ВТОРУЮ строку на то же (app_user_id, block_id,
    window_index); после этого КАЖДЫЙ следующий close_window падает
    MultipleResultsFound на `existing = ...scalar_one_or_none()`, guarded()
    глотает исключение — контур объёма молча умирает НАВСЕГДА для
    пользователя, попавшего в гонку один раз.

    Гонка воспроизводится ДЕТЕРМИНИРОВАННО, а не понадеявшись на удачное
    чередование корутин: отдельная сессия вставляет и держит НЕЗАКОММИЧЕННУЮ
    строку-конкурента с тем же ключом (app_user_id, block_id, window_index).
    Наш `close_window` её не видит на своей проверке `existing` (read
    committed) и доходит до собственной вставки — Postgres на уровне
    уникального индекса блокирует эту вставку, ожидая исхода
    конкурирующей транзакции, и после её коммита детерминированно
    возвращает конфликт. Именно так и выглядит настоящая гонка, просто
    воспроизведённая без угадывания тайминга asyncio.
    """
    start = utc_today() - timedelta(days=6)
    await _seed_microcycle(db, test_user.id, active_block.id, start, seeded_plan.id)
    window = await current_window(db, test_user.id, start)

    concurrent_session = SessionLocal()
    try:
        concurrent_row = VolumeWindow(
            app_user_id=test_user.id, block_id=window.block_id,
            window_index=window.window_index, phase_number=window.phase_number,
            start_date=window.start_date, end_date=window.end_date,
            muscles={}, adherence={
                "planned_days": 0, "completed_days": 0, "missed_days": 0,
            },
            landmarks={},
        )
        concurrent_session.add(concurrent_row)
        # flush(), не commit(): строка уже держит блокировку уникального
        # индекса на уровне БД, но ещё не видна другим транзакциям через
        # обычное чтение — ровно то состояние, в котором наш `existing`
        # выше её не находит.
        await concurrent_session.flush()

        async def _commit_concurrent_after_delay():
            # Задержка заведомо больше времени, за которое close_window
            # успевает дойти до своей вставки на этом маленьком наборе
            # данных, — к моменту коммита наша вставка уже блокируется на
            # конфликте и ждёт именно этот коммит.
            await asyncio.sleep(0.2)
            await concurrent_session.commit()

        result, _ = await asyncio.gather(
            close_window(db, test_user.id, window, {}, level="intermediate"),
            _commit_concurrent_after_delay(),
        )
    finally:
        await concurrent_session.close()

    await db.commit()

    assert result is not None, (
        "close_window обязана вернуть строку конкурента, а не упасть"
    )
    total = (await db.execute(
        select(func.count(VolumeWindow.id)).where(
            VolumeWindow.app_user_id == test_user.id,
            VolumeWindow.block_id == active_block.id,
        )
    )).scalar_one()
    assert total == 1

    # Повторное обращение (эквивалент следующего захода на
    # /workout-center/context) обязано отработать без MultipleResultsFound —
    # до фикса именно ТУТ проявлялся перманентный отказ.
    window_again = await current_window(db, test_user.id, start)
    again = await close_window(
        db, test_user.id, window_again, {}, level="intermediate"
    )
    assert again is not None


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
