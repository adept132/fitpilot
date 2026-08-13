"""P1-14: флаг is_max_reps — режим подхода, ортогональный set_type."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from api.services.models import WorkoutSessionSet


@pytest_asyncio.fixture
async def session_exercise_id(client, auth_headers, seeded_history):
    """Активная тренировка с одним упражнением, готовая принимать подходы."""
    workout = (await client.post(
        "/workouts/start", headers=auth_headers, json={"source": "free"},
    )).json()
    add = (await client.post(
        f"/workouts/{workout['id']}/exercises",
        headers=auth_headers,
        json={"exercise_id": seeded_history.id},
    )).json()
    return add["exercises"][-1]["id"]


@pytest.mark.asyncio
async def test_max_reps_flag_round_trips(client, auth_headers, db, session_exercise_id):
    resp = await client.post(
        f"/workout-session-exercises/{session_exercise_id}/sets",
        headers=auth_headers,
        json={"weight": 82.5, "reps": 9, "effort_level": "failure", "is_max_reps": True},
    )
    assert resp.status_code in (200, 201), resp.text
    body = resp.json()
    assert body["is_max_reps"] is True

    row = (await db.execute(
        select(WorkoutSessionSet).where(WorkoutSessionSet.id == body["id"])
    )).scalar_one()
    assert row.is_max_reps is True


@pytest.mark.asyncio
async def test_max_reps_defaults_to_false(client, auth_headers, session_exercise_id):
    resp = await client.post(
        f"/workout-session-exercises/{session_exercise_id}/sets",
        headers=auth_headers,
        json={"weight": 82.5, "reps": 5, "effort_level": "medium"},
    )
    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["is_max_reps"] is False


@pytest.mark.asyncio
async def test_max_reps_does_not_change_set_type(client, auth_headers, session_exercise_id):
    """Ортогональность осей: режим не подменяет роль подхода."""
    resp = await client.post(
        f"/workout-session-exercises/{session_exercise_id}/sets",
        headers=auth_headers,
        json={"weight": 82.5, "reps": 9, "effort_level": "failure", "is_max_reps": True},
    )
    assert resp.json()["set_type"] == "normal"
