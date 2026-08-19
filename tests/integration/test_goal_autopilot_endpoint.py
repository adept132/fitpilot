"""Контекст экрана автопилота (P0-12, Задача 11)."""
from datetime import date, datetime, timedelta, timezone

import pytest

from api.services.goal.service import (
    _history_weekly_points,
    _timeline_milestones,
    build_context,
)
from api.services.goal.types import Milestone
from api.services.models import (
    PeriodizationProposal,
    UserCalendarDay,
    UserExerciseProgressionState,
    UserGoal,
)
from api.services.periodization import params as periodization_params
from app.database import SessionLocal

pytestmark = pytest.mark.asyncio


_NO_DEADLINE_GIVEN = object()


async def _goal(
    user_id: int, exercise_id: int, primary: bool = True,
    deadline=_NO_DEADLINE_GIVEN, goal_type: str = "strength",
) -> int:
    if deadline is _NO_DEADLINE_GIVEN:
        deadline = date.today() + timedelta(days=60)
    async with SessionLocal() as db:
        goal = UserGoal(
            app_user_id=user_id, goal_type=goal_type, target_value=120.0,
            exercise_id=exercise_id, target_reps=3,
            deadline=deadline,
            is_primary=primary,
        )
        db.add(goal)
        await db.commit()
        await db.refresh(goal)
        return goal.id


async def _set_working_e1rm(user_id: int, exercise_id: int, working_e1rm: float) -> None:
    async with SessionLocal() as db:
        db.add(UserExerciseProgressionState(
            app_user_id=user_id, exercise_id=exercise_id, working_e1rm=working_e1rm,
        ))
        await db.commit()


async def test_returns_shape_even_without_history(client, auth_headers, test_user, fresh_exercise):
    goal_id = await _goal(test_user.id, fresh_exercise.id)
    r = await client.get(f"/goals/{goal_id}/autopilot", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) >= {"eta", "rates", "milestones", "plan_ahead", "proposal",
                         "last_applied", "available", "unavailable_reason"}
    assert body["available"] is False
    assert body["unavailable_reason"]


async def test_foreign_goal_is_not_found(client, auth_headers, test_user, fresh_exercise):
    r = await client.get("/goals/999999/autopilot", headers=auth_headers)
    assert r.status_code == 404


async def test_not_strength_goal_gives_specific_reason(client, auth_headers, test_user):
    goal_id = await _goal(test_user.id, exercise_id=None, primary=False, goal_type="body_fat")
    r = await client.get(f"/goals/{goal_id}/autopilot", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is False
    assert "питани" in body["unavailable_reason"].lower()


async def test_no_deadline_gives_specific_reason(client, auth_headers, test_user, fresh_exercise):
    goal_id = await _goal(test_user.id, fresh_exercise.id, primary=False, deadline=None)
    r = await client.get(f"/goals/{goal_id}/autopilot", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is False
    assert "срок" in body["unavailable_reason"].lower()


async def test_no_active_block_gives_specific_reason(
    client, auth_headers, test_user, seeded_history
):
    """evaluate() успешен (рабочий e1RM есть), но активного блока нет —
    молчание с отдельной причиной, а не общее "нет истории"."""
    await _set_working_e1rm(test_user.id, seeded_history.id, 100.0)
    goal_id = await _goal(test_user.id, seeded_history.id)
    r = await client.get(f"/goals/{goal_id}/autopilot", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is False
    assert "блок" in body["unavailable_reason"].lower()


async def test_returns_full_shape_when_available(
    client, auth_headers, test_user, seeded_history, active_block
):
    """Обязательный случай сверх пары «доступно/нет» из брифа (см. Global
    Constraints задачи): тест формы обязан покрывать и путь, где автопилот
    реально доступен, а не только те, что гасят его на раннем return."""
    await _set_working_e1rm(test_user.id, seeded_history.id, 100.0)
    goal_id = await _goal(test_user.id, seeded_history.id)

    r = await client.get(f"/goals/{goal_id}/autopilot", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["available"] is True
    assert body["unavailable_reason"] is None
    assert isinstance(body["rates"]["required"], (int, float))
    assert isinstance(body["rates"]["plan"], (int, float))
    assert isinstance(body["rates"]["ceiling"], (int, float))
    assert body["rates"]["ceiling"] > 0
    assert isinstance(body["milestones"], list)
    assert isinstance(body["plan_ahead"]["target_lift_sessions"], int)
    # Без сгенерированного плана в календаре — 0 будущих сессий лифта, но
    # поле обязано быть реально посчитано, а не заглушкой.
    assert body["plan_ahead"]["target_lift_sessions"] == 0
    assert body["proposal"] is None
    assert body["last_applied"] is None


async def test_payload_carries_past_points_for_lift_with_history(
    client, auth_headers, test_user, seeded_history, active_block
):
    """P0-12, Задача 19/20-фикс, §6.2: seeded_history несёт завершённую
    тренировку "только что" (finished_at ~30 минут назад) — её e1RM обязан
    попасть в milestones как точка факта текущей недели, а не потеряться в
    веках, которые раньше видели только будущее."""
    await _set_working_e1rm(test_user.id, seeded_history.id, 100.0)
    goal_id = await _goal(test_user.id, seeded_history.id)

    r = await client.get(f"/goals/{goal_id}/autopilot", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["available"] is True
    past = [m for m in body["milestones"] if m["actual_e1rm"] is not None]
    assert len(past) >= 1
    assert all(m["expected_e1rm"] is None for m in past)


async def test_payload_carries_no_past_points_for_lift_without_history(
    client, auth_headers, test_user, fresh_exercise, active_block
):
    """Тот же экран для лифта БЕЗ единой завершённой тренировки (только
    вручную выставленный рабочий e1RM, bootstrap-путь scheme_context) не
    имеет права нарисовать точку факта, которого не было."""
    await _set_working_e1rm(test_user.id, fresh_exercise.id, 100.0)
    goal_id = await _goal(test_user.id, fresh_exercise.id)

    r = await client.get(f"/goals/{goal_id}/autopilot", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["available"] is True
    assert all(m["actual_e1rm"] is None for m in body["milestones"])


# --- can_undo обязан совпадать с воротами undo_goal_decision (Задача 10) ---
#
# _undo_blocked_reason смотрит ТОЛЬКО на дни снимка с touched=True — то же
# множество, что и реальный откат (см. её докстринг). Наивная реализация по
# ВСЕМ дням снимка запретила бы отмену там, где /undo её реально разрешает.

async def test_can_undo_true_when_only_untouched_day_has_fact(
    test_user, seeded_history, active_block
):
    await _set_working_e1rm(test_user.id, seeded_history.id, 100.0)
    goal_id = await _goal(test_user.id, seeded_history.id)

    touched_date = date.today() + timedelta(days=1)
    untouched_date = date.today() + timedelta(days=2)
    async with SessionLocal() as db:
        # Нетронутый регенерацией день несёт факт — он не входит в touched
        # и потому не имеет права блокировать откат.
        db.add(UserCalendarDay(
            app_user_id=test_user.id, target_date=untouched_date,
            block_id=active_block.id, day_tag="pull", micro_tag="medium",
            meso_tag="medium", is_rest_day=False, is_blackout=False,
            status="completed", actual_workout_session_id=1,
        ))
        proposal = PeriodizationProposal(
            app_user_id=test_user.id, block_id=active_block.id,
            kind=periodization_params.KIND_GOAL_PLAN, reason_code="pace_behind",
            payload={
                "goal_id": goal_id, "exercise_id": seeded_history.id,
                "levers": [],
                "applied_snapshot": {
                    "days": [
                        {"target_date": touched_date.isoformat(), "plan_id": None,
                         "day_tag": "push", "micro_tag": "medium", "meso_tag": "medium",
                         "mesocycle_phase_number": None, "is_rest_day": False, "touched": True},
                        {"target_date": untouched_date.isoformat(), "plan_id": None,
                         "day_tag": "pull", "micro_tag": "medium", "meso_tag": "medium",
                         "mesocycle_phase_number": None, "is_rest_day": False, "touched": False},
                    ],
                    "created_plan_ids": [],
                },
            },
            status=periodization_params.STATUS_ACCEPTED,
            decided_at=datetime.now(timezone.utc),
        )
        db.add(proposal)
        await db.commit()
        goal = await db.get(UserGoal, goal_id)
        ctx = await build_context(db, test_user.id, goal, date.today())

    assert ctx["last_applied"]["can_undo"] is True
    assert ctx["last_applied"]["undo_blocked_reason"] is None


async def test_can_undo_false_when_touched_day_has_fact(
    test_user, seeded_history, active_block
):
    await _set_working_e1rm(test_user.id, seeded_history.id, 100.0)
    goal_id = await _goal(test_user.id, seeded_history.id)

    touched_date = date.today() + timedelta(days=1)
    async with SessionLocal() as db:
        db.add(UserCalendarDay(
            app_user_id=test_user.id, target_date=touched_date,
            block_id=active_block.id, day_tag="push", micro_tag="medium",
            meso_tag="medium", is_rest_day=False, is_blackout=False,
            status="completed", actual_workout_session_id=1,
        ))
        proposal = PeriodizationProposal(
            app_user_id=test_user.id, block_id=active_block.id,
            kind=periodization_params.KIND_GOAL_PLAN, reason_code="pace_behind",
            payload={
                "goal_id": goal_id, "exercise_id": seeded_history.id,
                "levers": [],
                "applied_snapshot": {
                    "days": [
                        {"target_date": touched_date.isoformat(), "plan_id": None,
                         "day_tag": "push", "micro_tag": "medium", "meso_tag": "medium",
                         "mesocycle_phase_number": None, "is_rest_day": False, "touched": True},
                    ],
                    "created_plan_ids": [],
                },
            },
            status=periodization_params.STATUS_ACCEPTED,
            decided_at=datetime.now(timezone.utc),
        )
        db.add(proposal)
        await db.commit()
        goal = await db.get(UserGoal, goal_id)
        ctx = await build_context(db, test_user.id, goal, date.today())

    assert ctx["last_applied"]["can_undo"] is False
    assert ctx["last_applied"]["undo_blocked_reason"]


# --- ревью Задачи 11, Critical 1: предложение одной цели не должно
# утекать на экран другой цели того же пользователя ---
#
# Обе выборки в build_context (pending и applied) раньше фильтровались
# только по app_user_id + kind + status, без payload["goal_id"] — при двух
# strength-целях одного пользователя самое свежее goal_plan-предложение
# ПОЛЬЗОВАТЕЛЯ (принадлежащее первой цели) утекало на экран второй.

async def test_pending_proposal_of_other_goal_does_not_leak(
    test_user, seeded_history, active_block
):
    await _set_working_e1rm(test_user.id, seeded_history.id, 100.0)
    goal_a = await _goal(test_user.id, seeded_history.id, primary=False)
    goal_b = await _goal(test_user.id, seeded_history.id, primary=False)

    async with SessionLocal() as db:
        db.add(PeriodizationProposal(
            app_user_id=test_user.id, block_id=active_block.id,
            kind=periodization_params.KIND_GOAL_PLAN, reason_code="pace_behind",
            payload={"goal_id": goal_a, "exercise_id": seeded_history.id, "levers": []},
            status=periodization_params.STATUS_PENDING,
        ))
        await db.commit()

        goal = await db.get(UserGoal, goal_b)
        ctx_b = await build_context(db, test_user.id, goal, date.today())

    assert ctx_b["proposal"] is None

    async with SessionLocal() as db:
        goal = await db.get(UserGoal, goal_a)
        ctx_a = await build_context(db, test_user.id, goal, date.today())

    assert ctx_a["proposal"] is not None
    assert ctx_a["proposal"]["id"] is not None


async def test_applied_proposal_of_other_goal_does_not_leak(
    test_user, seeded_history, active_block
):
    """Хуже, чем просто «показал не то»: если бы этот путь утёк, экран
    второй цели показал бы last_applied.proposal_id первой, и «Отменить»
    там откатывала бы изменения, принадлежащие чужой цели."""
    await _set_working_e1rm(test_user.id, seeded_history.id, 100.0)
    goal_a = await _goal(test_user.id, seeded_history.id, primary=False)
    goal_b = await _goal(test_user.id, seeded_history.id, primary=False)

    async with SessionLocal() as db:
        db.add(PeriodizationProposal(
            app_user_id=test_user.id, block_id=active_block.id,
            kind=periodization_params.KIND_GOAL_PLAN, reason_code="pace_behind",
            payload={
                "goal_id": goal_a, "exercise_id": seeded_history.id, "levers": [],
                "applied_snapshot": {"days": [], "created_plan_ids": []},
            },
            status=periodization_params.STATUS_ACCEPTED,
            decided_at=datetime.now(timezone.utc),
        ))
        await db.commit()

        goal = await db.get(UserGoal, goal_b)
        ctx_b = await build_context(db, test_user.id, goal, date.today())

    assert ctx_b["last_applied"] is None

    async with SessionLocal() as db:
        goal = await db.get(UserGoal, goal_a)
        ctx_a = await build_context(db, test_user.id, goal, date.today())

    assert ctx_a["last_applied"] is not None


# --- P0-12, Задача 19/20-фикс, §6.2: вехи симуляции начинаются только с
# первой БУДУЩЕЙ сессии — факт по определению существует лишь в прошлом, и
# потому со старой by-week-matching-логикой actual_e1rm был вечным None на
# живом эндпоинте (см. отчёт Задачи 19). Чинится вторым источником точек:
# прошлое — из истории лифта (_history_weekly_points), будущее — из
# симуляции как и раньше; шов — today.

def test_history_weekly_points_returns_empty_without_history():
    """Лифт без истории не получает ни одной точки факта — не "рано", а
    честно "нечего показать"."""
    assert _history_weekly_points([], date(2026, 3, 20)) == []


def test_history_weekly_points_buckets_weekly_before_today():
    """Недельная сетка привязана к today+1 (последняя неделя — [today-6,
    today+1)) — тренировка, завершённая СЕГОДНЯ, тоже факт и обязана
    попасть в последнюю неделю, а не потеряться на границе. Внутри недели
    побеждает последнее по дате значение; дата на/после today+1 (защита от
    будущего, которого в history_points в проде не бывает) и глубже
    _HISTORY_WEEKS_BACK недель назад — не берутся вовсе."""
    today = date(2026, 3, 20)
    history_points = [
        (date(2026, 3, 10), 98.0),   # неделя [3-07, 3-14)
        (date(2026, 3, 19), 100.5),  # неделя [3-14, 3-21) — вчера, попадает в текущую
        (date(2026, 3, 21), 999.0),  # завтра — за границей today+1, исключается
        (date(2020, 1, 1), 1.0),     # далеко за пределами lookback-окна
    ]

    points = _history_weekly_points(history_points, today)

    assert points == [
        (date(2026, 3, 7), 98.0),
        (date(2026, 3, 14), 100.5),
    ]


def test_timeline_milestones_past_and_future_meet_at_today_without_overlap():
    """Факт (из истории) и план (вехи симуляции) — на одной недельной оси,
    шов — today: ни одна неделя факта не залезает в будущее, ни одна веха
    плана не начинается раньше today — иначе график задваивал бы линию.
    Каждая точка несёт РОВНО одно из двух полей — то, что заполнить нельзя
    никогда, отсутствует, а не остаётся вечным null."""
    today = date(2026, 3, 20)
    history_points = [(date(2026, 3, 10), 98.0), (date(2026, 3, 17), 99.5)]
    milestones = [
        Milestone(week_start=date(2026, 3, 23), expected_e1rm=100.0),
        Milestone(week_start=date(2026, 3, 30), expected_e1rm=101.0),
    ]

    result = _timeline_milestones(milestones, history_points, today)

    past = [r for r in result if r.get("actual_e1rm") is not None]
    future = [r for r in result if r.get("expected_e1rm") is not None]

    assert {r["week_start"] for r in past} == {"2026-03-07", "2026-03-14"}
    assert {r["week_start"] for r in future} == {"2026-03-23", "2026-03-30"}
    assert not ({r["week_start"] for r in past} & {r["week_start"] for r in future})
    assert all(date.fromisoformat(r["week_start"]) < today for r in past)
    assert all(date.fromisoformat(r["week_start"]) >= today for r in future)
    assert "expected_e1rm" not in past[0]
    assert "actual_e1rm" not in future[0]
    # Прошлое перед будущим, обе половины по возрастанию недели.
    assert [r["week_start"] for r in result] == sorted(r["week_start"] for r in result)
