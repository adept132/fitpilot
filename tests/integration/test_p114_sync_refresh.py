"""P1-14: боевой путь завершения тренировки идёт через POST /sync/workouts.

refresh_state вызывается только из workout_center.finish_workout, которую
приложение больше не дёргает (тренировка завершается в офлайн-репозитории
и уезжает синком). Без хука в этой ручке ни состояние прогрессии, ни
рекорды на живом пути не обновляются.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from api.services.models import UserExerciseProgressionState


def _payload(exercise_id: int, *, client_uuid: str, weight: float, reps: int) -> dict:
    return {
        "client_uuid": client_uuid,
        "source": "free",
        "status": "finished",
        "split_day_id": None,
        "plan_id": None,
        "notes": None,
        "volume_targets": None,
        "started_at": "2026-08-12T10:00:00Z",
        "finished_at": "2026-08-12T11:00:00Z",
        "exercises": [
            {
                "client_uuid": f"{client_uuid}-ex-1",
                "exercise_id": exercise_id,
                "order_index": 0,
                "superset_group": None,
                "notes": None,
                "prescription": None,
                "sets": [
                    {
                        "client_uuid": f"{client_uuid}-set-1",
                        "set_number": 1,
                        "set_type": "normal",
                        "weight": weight,
                        "reps": reps,
                        "effort_level": "medium",
                        "is_completed": True,
                    }
                ],
            }
        ],
    }


async def _state_row(db, exercise_id: int):
    return (await db.execute(
        select(UserExerciseProgressionState).where(
            UserExerciseProgressionState.exercise_id == exercise_id,
        )
    )).scalar_one_or_none()


@pytest.mark.asyncio
async def test_sync_finished_workout_rebuilds_records(
    client, auth_headers, db, seeded_history,
):
    """Тренировка, завершённая офлайн, обновляет рекорды при приёме синка."""
    resp = await client.post(
        "/sync/workouts",
        headers=auth_headers,
        json=_payload(seeded_history.id, client_uuid="p114-rec-1", weight=82.5, reps=5),
    )
    assert resp.status_code == 200, resp.text

    row = await _state_row(db, seeded_history.id)
    assert row is not None
    assert row.records["weight_at_reps"]["5"]["weight"] == 82.5


@pytest.mark.asyncio
async def test_sync_finished_workout_refreshes_next_prescription(
    client, auth_headers, db, seeded_history,
):
    """Без этого «в следующий раз: N кг» показывал бы вчерашнее число."""
    resp = await client.post(
        "/sync/workouts",
        headers=auth_headers,
        json=_payload(seeded_history.id, client_uuid="p114-presc-1", weight=45.0, reps=10),
    )
    assert resp.status_code == 200, resp.text

    row = await _state_row(db, seeded_history.id)
    assert row is not None
    assert row.next_prescription is not None


@pytest.mark.asyncio
async def test_sync_survives_failing_recompute(
    client, auth_headers, seeded_history, monkeypatch,
):
    """Падение пересчёта не рвёт приём тренировки — её нельзя потерять."""
    import api.routers.sync as sync_module

    async def boom(*args, **kwargs):
        raise RuntimeError("пересчёт упал")

    monkeypatch.setattr(sync_module, "rebuild_records", boom)

    resp = await client.post(
        "/sync/workouts",
        headers=auth_headers,
        json=_payload(seeded_history.id, client_uuid="p114-boom-1", weight=50.0, reps=8),
    )
    assert resp.status_code == 200, resp.text
