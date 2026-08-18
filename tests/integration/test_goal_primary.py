"""Ведущая цель: одна на пользователя, только strength с дедлайном (P0-12, Задача 4)."""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from api.services.models import Exercise, UserGoal
from app.database import SessionLocal

pytestmark = pytest.mark.asyncio


async def _make_goal(user_id: int, exercise_id: int | None, deadline, goal_type="strength"):
    async with SessionLocal() as db:
        goal = UserGoal(
            app_user_id=user_id, goal_type=goal_type, target_value=120.0,
            exercise_id=exercise_id, target_reps=3, deadline=deadline,
        )
        db.add(goal)
        await db.commit()
        await db.refresh(goal)
        return goal.id


async def test_marking_primary_clears_the_previous_one(client, auth_headers, test_user, fresh_exercise):
    deadline = date.today() + timedelta(days=90)
    first = await _make_goal(test_user.id, fresh_exercise.id, deadline)
    second = await _make_goal(test_user.id, fresh_exercise.id, deadline)

    r1 = await client.patch(f"/goals/{first}", json={"is_primary": True}, headers=auth_headers)
    assert r1.status_code == 200, r1.text
    r2 = await client.patch(f"/goals/{second}", json={"is_primary": True}, headers=auth_headers)
    assert r2.status_code == 200, r2.text

    async with SessionLocal() as db:
        rows = (await db.execute(
            select(UserGoal).where(UserGoal.app_user_id == test_user.id)
        )).scalars().all()
    primary = [g.id for g in rows if g.is_primary]
    assert primary == [second]


async def test_non_strength_goal_cannot_be_primary(client, auth_headers, test_user):
    goal_id = await _make_goal(
        test_user.id, None, date.today() + timedelta(days=90), goal_type="bodyweight"
    )
    r = await client.patch(f"/goals/{goal_id}", json={"is_primary": True}, headers=auth_headers)
    assert r.status_code == 400
    assert "силов" in r.json()["detail"].lower()


async def test_goal_without_deadline_cannot_be_primary(client, auth_headers, test_user, fresh_exercise):
    goal_id = await _make_goal(test_user.id, fresh_exercise.id, None)
    r = await client.patch(f"/goals/{goal_id}", json={"is_primary": True}, headers=auth_headers)
    assert r.status_code == 400
    assert "срок" in r.json()["detail"].lower()


async def test_list_exposes_is_primary(client, auth_headers, test_user, fresh_exercise):
    goal_id = await _make_goal(test_user.id, fresh_exercise.id, date.today() + timedelta(days=60))
    await client.patch(f"/goals/{goal_id}", json={"is_primary": True}, headers=auth_headers)
    r = await client.get("/goals", headers=auth_headers)
    assert r.status_code == 200
    assert [g["is_primary"] for g in r.json() if g["id"] == goal_id] == [True]
