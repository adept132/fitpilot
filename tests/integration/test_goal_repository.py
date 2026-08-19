"""Сбор входа автопилота из БД (P0-12, Задача 5).

РАСХОЖДЕНИЯ С ЗАГОТОВКОЙ БРИФА (см. .superpowers/sdd/p0-12-task-5-report.md):
- WorkoutPlanExercise.workow_plan_id не существует — реальная колонка
  называется plan_id (api/services/models.py).
- WorkoutPlan.micro_tag и .meso_tag NOT NULL — заготовка _seed_future_days
  их не задавала, INSERT падал бы NotNullViolation.
Оба места ниже поправлены под настоящую схему.
"""
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from api.services.goal import repository
from api.services.models import (
    Exercise,
    UserCalendarDay,
    UserExerciseProgressionState,
    WorkoutPlan,
    WorkoutPlanExercise,
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)
from app.database import SessionLocal

pytestmark = pytest.mark.asyncio


async def _seed_future_days(user_id: int, exercise_id: int, start: date, count: int) -> None:
    async with SessionLocal() as db:
        plan = WorkoutPlan(
            app_user_id=user_id, name="Push", day_tag="push",
            micro_tag="medium", meso_tag="medium",
        )
        db.add(plan)
        await db.flush()
        db.add(WorkoutPlanExercise(
            plan_id=plan.id, exercise_id=exercise_id,
            target_sets=3, order_index=0,
        ))
        for i in range(count):
            db.add(UserCalendarDay(
                app_user_id=user_id, target_date=start + timedelta(days=i * 3),
                plan_id=plan.id, day_tag="push", micro_tag="medium", meso_tag="medium",
                is_rest_day=False, is_blackout=False, status="planned",
            ))
        await db.commit()


async def test_future_sessions_only_include_days_with_the_lift(test_user, fresh_exercise):
    today = date.today()
    await _seed_future_days(test_user.id, fresh_exercise.id, today + timedelta(days=1), 5)

    async with SessionLocal() as db:
        found = await repository.future_sessions(
            db, test_user.id, fresh_exercise.id, today, today + timedelta(days=30)
        )

    assert len(found) == 5
    assert all(s.date > today for s in found)
    assert all(s.prescription_sets == 3 for s in found)


async def test_future_sessions_are_sorted_by_date(test_user, fresh_exercise):
    today = date.today()
    await _seed_future_days(test_user.id, fresh_exercise.id, today + timedelta(days=1), 4)

    async with SessionLocal() as db:
        found = await repository.future_sessions(
            db, test_user.id, fresh_exercise.id, today, today + timedelta(days=30)
        )

    assert [s.date for s in found] == sorted(s.date for s in found)


async def test_no_calendar_gives_empty_list(test_user, fresh_exercise):
    today = date.today()
    async with SessionLocal() as db:
        found = await repository.future_sessions(
            db, test_user.id, fresh_exercise.id, today, today + timedelta(days=30)
        )
    assert found == []


async def test_exercise_context_reports_muscle_and_scheme(test_user, fresh_exercise):
    async with SessionLocal() as db:
        ctx = await repository.exercise_context(db, test_user.id, fresh_exercise.id)
    assert set(ctx) == {"scheme", "rep_max", "is_heavy_compound", "muscle"}
    assert ctx["rep_max"] >= 1
    assert isinstance(ctx["is_heavy_compound"], bool)


# --- Дополнительные тесты (не из брифа) ---
#
# Бриф не даёт тестов на current_e1rm/lift_stats/scheme_context, хотя они —
# часть контракта Задачи 5 ("Produces"). scheme_context особенно важен:
# именно в нём была найдена и исправлена критическая ошибка заготовки
# (пустая history -> движок никогда не двигает вес, см. докстринг
# repository.scheme_context). Эти тесты проверяют исправление напрямую,
# а не только то, что модуль импортируется.

async def _set_working_e1rm(user_id: int, exercise_id: int, working_e1rm: float) -> None:
    async with SessionLocal() as db:
        db.add(UserExerciseProgressionState(
            app_user_id=user_id, exercise_id=exercise_id, working_e1rm=working_e1rm,
        ))
        await db.commit()


async def _seed_finished_session(user_id: int, exercise_id: int, weight: float, when) -> None:
    async with SessionLocal() as db:
        workout = WorkoutSession(
            app_user_id=user_id, source="free", status="finished", finished_at=when,
        )
        db.add(workout)
        await db.flush()
        se = WorkoutSessionExercise(
            workout_session_id=workout.id, exercise_id=exercise_id, order_index=0,
        )
        db.add(se)
        await db.flush()
        for n in range(1, 4):
            db.add(WorkoutSessionSet(
                workout_session_exercise_id=se.id, set_number=n, set_type="normal",
                weight=weight, reps=8, effort_level="medium", is_completed=True,
            ))
        await db.commit()


async def test_current_e1rm_is_none_without_cached_state(test_user, fresh_exercise):
    async with SessionLocal() as db:
        value = await repository.current_e1rm(db, test_user.id, fresh_exercise.id)
    assert value is None


async def test_current_e1rm_reads_cached_working_e1rm(test_user, fresh_exercise):
    await _set_working_e1rm(test_user.id, fresh_exercise.id, 87.5)
    async with SessionLocal() as db:
        value = await repository.current_e1rm(db, test_user.id, fresh_exercise.id)
    assert value == 87.5


async def test_lift_stats_counts_sessions_and_growth(test_user, fresh_exercise):
    now = datetime.now(timezone.utc)
    await _seed_finished_session(test_user.id, fresh_exercise.id, 40.0, now - timedelta(days=6))
    await _seed_finished_session(test_user.id, fresh_exercise.id, 45.0, now - timedelta(days=3))

    async with SessionLocal() as db:
        count, success_rate = await repository.lift_stats(db, test_user.id, fresh_exercise.id)

    assert count == 2
    assert success_rate == 1.0


async def test_lift_stats_ignores_warmup_sets(test_user, fresh_exercise):
    """set_type='warmup' не должен попадать в максимум рабочего веса сессии:
    разминочные подходы легче рабочих, их наличие не должно портить count/rate."""
    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        workout = WorkoutSession(
            app_user_id=test_user.id, source="free", status="finished", finished_at=now,
        )
        db.add(workout)
        await db.flush()
        se = WorkoutSessionExercise(
            workout_session_id=workout.id, exercise_id=fresh_exercise.id, order_index=0,
        )
        db.add(se)
        await db.flush()
        db.add(WorkoutSessionSet(
            workout_session_exercise_id=se.id, set_number=1, set_type="warmup",
            weight=200.0, reps=5, effort_level="medium", is_completed=True,
        ))
        db.add(WorkoutSessionSet(
            workout_session_exercise_id=se.id, set_number=2, set_type="normal",
            weight=40.0, reps=8, effort_level="medium", is_completed=True,
        ))
        await db.commit()

    async with SessionLocal() as db:
        count, _ = await repository.lift_stats(db, test_user.id, fresh_exercise.id)
    # Одна сессия, одна рабочая точка данных (тёплый подход не считается) —
    # роста измерять не из чего.
    assert count == 1


async def test_scheme_context_is_none_without_working_e1rm(test_user, fresh_exercise):
    async with SessionLocal() as db:
        ctx = await repository.scheme_context(db, test_user.id, fresh_exercise.id, None)
    assert ctx is None


async def test_scheme_context_bootstraps_history_without_completed_sessions(test_user, fresh_exercise):
    """fresh_exercise не имеет ни одной завершённой тренировки — критический
    путь 2: history обязана быть непустой (bootstrap из working_e1rm), иначе
    rebuild_state() внутри движка получит working_e1rm=None и вес не
    сдвинется НИ НА ОДНОЙ будущей сессии прокрутки (см. докстринг
    repository.scheme_context)."""
    await _set_working_e1rm(test_user.id, fresh_exercise.id, 60.0)

    async with SessionLocal() as db:
        ctx = await repository.scheme_context(db, test_user.id, fresh_exercise.id, None)

    assert ctx is not None
    assert len(ctx.history.sessions) > 0
    assert ctx.state.working_e1rm == 60.0


async def test_scheme_context_uses_real_history_when_available(test_user, fresh_exercise):
    """Когда в БД есть настоящая завершённая тренировка, history обязана
    прийти из неё (path 1), а не из bootstrap-заглушки (path 2) — реальная
    история несёт настоящую схему прогрессии, которую bootstrap не знает."""
    await _set_working_e1rm(test_user.id, fresh_exercise.id, 60.0)
    now = datetime.now(timezone.utc)
    await _seed_finished_session(test_user.id, fresh_exercise.id, 55.0, now - timedelta(days=2))

    async with SessionLocal() as db:
        ctx = await repository.scheme_context(db, test_user.id, fresh_exercise.id, None)

    assert ctx is not None
    assert len(ctx.history.sessions) > 0
    # session_id > 0 отличает настоящую сессию (id из БД) от bootstrap-заглушки
    # (session_id=-1, см. repository._bootstrap_history).
    assert ctx.history.sessions[0].session_id > 0


# --- Ревью Задачи 5 ---
#
# Important 1 (регрессия): main_muscle_group иногда уже хранит EN системный
# ключ ("chest"), а не русское отображаемое имя, потому что
# api/routers/exercises.py пишет пользовательский ввод verbatim.
# key_for_muscle такую строку не резолвил вовсе; to_system_key обязана.
# (headroom_sets/headroom_for_window, которые этот же муscle-ключ раньше
# кормили по пути MRV-вето рычага LEVER_SETS, удалены вместе с рычагом —
# P0-12, обрезка лестницы: ни одна схема прогрессии не читает число
# подходов, эффект был всегда нулевым.)

async def test_exercise_context_resolves_en_system_key_muscle(test_user):
    """Регрессия ревью Задачи 5, Important 1: main_muscle_group иногда уже
    хранит EN системный ключ, а не русское отображаемое имя — не только
    легаси-данные, пользователь пишет его verbatim через
    api/routers/exercises.py при создании своего упражнения. key_for_muscle
    такую строку не резолвил вовсе (RU_TO_KEY.get("chest") -> None) —
    ctx["muscle"] был бы None на старом коде."""
    async with SessionLocal() as db:
        ex = Exercise(
            name=f"EN-ключ мышцы {uuid.uuid4().hex[:8]}",
            category="base",
            main_muscle_group="chest",
            difficulty="beginner",
            equipment_needed=[],
            source="custom",
            app_user_id=test_user.id,
        )
        db.add(ex)
        await db.commit()
        ex_id = ex.id

    async with SessionLocal() as db:
        ctx = await repository.exercise_context(db, test_user.id, ex_id)
    assert ctx["muscle"] == "chest"
