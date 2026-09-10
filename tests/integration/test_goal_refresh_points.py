"""Автопилот просыпается на существующих точках и не роняет их (P0-12, Задача 7)."""
from datetime import date, timedelta

import pytest

from api.services.models import UserGoal
from app.database import SessionLocal

pytestmark = pytest.mark.asyncio


async def _primary_goal(user_id: int, exercise_id: int) -> int:
    async with SessionLocal() as db:
        goal = UserGoal(
            app_user_id=user_id, goal_type="strength", target_value=120.0,
            exercise_id=exercise_id, target_reps=3,
            deadline=date.today() + timedelta(days=60), is_primary=True,
        )
        db.add(goal)
        await db.commit()
        await db.refresh(goal)
        return goal.id


async def test_profile_patch_survives_autopilot(client, auth_headers, test_user, fresh_exercise):
    await _primary_goal(test_user.id, fresh_exercise.id)
    r = await client.patch(
        "/profile/me", json={"training_frequency": 4}, headers=auth_headers
    )
    assert r.status_code == 200, r.text


async def test_goal_patch_survives_autopilot(client, auth_headers, test_user, fresh_exercise):
    goal_id = await _primary_goal(test_user.id, fresh_exercise.id)
    r = await client.patch(
        f"/goals/{goal_id}",
        json={"deadline": (date.today() + timedelta(days=120)).isoformat()},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text


async def test_profile_patch_survives_solver_crash(
    client, auth_headers, test_user, fresh_exercise, monkeypatch
):
    """Живучесть: решатель падает, но guarded() не даёт этому уронить commit
    вызывающего эндпоинта (P0-12). Мок допустим только для этой проверки —
    остальные тесты в файле работают на реальных данных.
    """
    await _primary_goal(test_user.id, fresh_exercise.id)

    async def _boom(*args, **kwargs):
        raise RuntimeError("solver exploded")

    monkeypatch.setattr(
        "api.services.goal.service.refresh_goal_proposals", _boom
    )

    r = await client.patch(
        "/profile/me", json={"training_frequency": 4}, headers=auth_headers
    )
    assert r.status_code == 200, r.text
