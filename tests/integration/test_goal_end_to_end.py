"""Сквозной сценарий автопилота: предложение -> применение -> откат (P0-12, Задача 15).

Существующие тесты уже покрывают отдельные звенья цепочки по отдельности, и
этот файл сознательно их не повторяет:
  - test_goal_apply_structural.py доказывает, что структурный рычаг щадит
    дни с фактом, но вызывает apply_goal_decision НАПРЯМУЮ, минуя диспетчер.
  - test_goal_undo.py::test_dispatcher_applies_structural_lever_and_allows_undo
    гоняет структурный рычаг через диспетчер (apply_decision) туда и обратно,
    но без посеянного календаря и без дня с фактом — сравнивать откату
    буквально нечего, проверяются только поля самого proposal.
Ни один из них не создаёт настоящую UserGoal и не сравнивает календарь
целиком до и после связки apply -> undo. Именно эта связка — цель с
дедлайном -> предложение -> применение через диспетчер (apply_decision, а не
apply_goal_decision/undo_goal_decision напрямую) -> день с фактом не тронут
-> откат -> координаты дней в точности совпадают с тем, что было до
применения — и есть предмет Задачи 15.
"""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from api.services.models import PeriodizationProposal, UserCalendarDay, UserGoal
from api.services.periodization import params as periodization_params
from api.services.periodization.service import apply_decision
from app.database import SessionLocal

pytestmark = pytest.mark.asyncio


def _coords(day: UserCalendarDay) -> tuple:
    """Координаты дня, которые undo_goal_decision обязана вернуть как были
    (см. её шаг 5 «Дни календаря»), плюс факт (status/
    actual_workout_session_id) — регенерация не имеет права его переписать
    вовсе, поэтому он обязан остаться прежним и без всякого отката."""
    return (
        day.day_tag, day.micro_tag, day.meso_tag, day.mesocycle_phase_number,
        day.is_rest_day, day.plan_id, day.status, day.actual_workout_session_id,
    )


async def _fetch(db, app_user_id: int, dates: list[date]) -> dict[date, tuple]:
    rows = (await db.execute(
        select(UserCalendarDay).where(
            UserCalendarDay.app_user_id == app_user_id,
            UserCalendarDay.target_date.in_(dates),
        )
    )).scalars().all()
    return {row.target_date: _coords(row) for row in rows}


async def test_apply_then_undo_restores_calendar(test_user, fresh_exercise, active_block):
    dates = [date.today() + timedelta(days=i) for i in range(1, 6)]
    fact_date = dates[2]  # третий будущий день уже отработан — несёт факт

    async with SessionLocal() as db:
        goal = UserGoal(
            app_user_id=test_user.id, goal_type="strength", target_value=140.0,
            exercise_id=fresh_exercise.id, target_reps=3,
            deadline=date.today() + timedelta(days=60), is_primary=True,
        )
        db.add(goal)
        await db.flush()  # нужен goal.id для payload["goal_id"] ниже

        for d in dates:
            db.add(UserCalendarDay(
                app_user_id=test_user.id, target_date=d,
                block_id=active_block.id, day_tag="push", micro_tag="medium",
                meso_tag="medium", is_rest_day=False, is_blackout=False,
                status="completed" if d == fact_date else "planned",
                actual_workout_session_id=777 if d == fact_date else None,
            ))

        # lift_frequency — структурный рычаг (params.STRUCTURAL_LEVERS):
        # только он реально трогает UserCalendarDay через _apply_structural,
        # поэтому только на нём осмысленно проверять «дни с фактом не
        # тронуты, календарь после отката совпадает с исходным». Мягкие
        # рычаги (ensure_present и т.п.) календаря вообще не касаются.
        proposal = PeriodizationProposal(
            app_user_id=test_user.id, block_id=active_block.id,
            kind=periodization_params.KIND_GOAL_PLAN, reason_code="pace_behind",
            payload={
                "goal_id": goal.id, "exercise_id": fresh_exercise.id,
                "levers": [{"index": 0, "kind": "lift_frequency",
                            "reason_code": "pace_behind", "effect_slope": 0.2,
                            "effect_days": 12, "detail": {"delta_sessions": 1}}],
                "applied_snapshot": None,
            },
            status=periodization_params.STATUS_PENDING,
        )
        db.add(proposal)
        await db.commit()
        await db.refresh(proposal)
        pid = proposal.id

        before = await _fetch(db, test_user.id, dates)
    assert len(before) == 5

    async with SessionLocal() as db:
        applied = await apply_decision(
            db, test_user.id, pid, periodization_params.ACTION_APPLY_GOAL,
            client_uuid="uuid-apply", options={"accepted": [0]},
        )
    assert applied["status"] == "applied"
    assert applied["applied"] == [0]

    async with SessionLocal() as db:
        after_apply = await _fetch(db, test_user.id, dates)
        row = await db.get(PeriodizationProposal, pid)

    # День с фактом остался нетронутым буквально — та же координата, тот же
    # статус, та же ссылка на тренировку.
    assert after_apply[fact_date] == before[fact_date]

    # Остальные дни регенерация обязана была реально перезаписать — иначе
    # тест доказывал бы совпадение на пустой выборке (рычаг ничего не
    # сделал), а не настоящую регенерацию. day_tag "Push"/"Pull" (с большой
    # буквы) может прийти только из настоящего SchedulingEngine.
    # generate_block_days по сплиту active_block — посеянные дни несли
    # day_tag="push" (нижний регистр).
    touched_dates = {d for d in dates if d != fact_date}
    for d in touched_dates:
        assert after_apply[d] != before[d]
        assert after_apply[d][0] in ("Push", "Pull")

    snapshot_days = row.payload["applied_snapshot"]["days"]
    snapshot_touched = {
        date.fromisoformat(entry["target_date"])
        for entry in snapshot_days if entry["touched"]
    }
    assert snapshot_touched == touched_dates

    async with SessionLocal() as db:
        undone = await apply_decision(
            db, test_user.id, pid, periodization_params.ACTION_UNDO_GOAL,
            client_uuid="uuid-undo", options={},
        )
    assert undone["status"] == "undone"

    async with SessionLocal() as db:
        after_undo = await _fetch(db, test_user.id, dates)
        row = await db.get(PeriodizationProposal, pid)

    # Календарь на этих пяти датах — в точности то, что было ДО применения.
    assert after_undo == before
    assert row.status == periodization_params.STATUS_UNDONE
