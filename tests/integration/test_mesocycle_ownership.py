"""Мезоциклы не текут между пользователями (финальное ревью P1-03, правка 2)."""
import uuid

import pytest
from sqlalchemy import delete

from api.services.models import AppUser, Mesocycle, MesocyclePhase
from app.database import SessionLocal

pytestmark = pytest.mark.asyncio


async def _make_other_user() -> int:
    marker = uuid.uuid4().hex[:12]
    async with SessionLocal() as db:
        user = AppUser(
            firebase_uid=f"test-{marker}",
            email=f"test-{marker}@example.com",
            display_name="Other Test User",
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user.id


async def _make_mesocycle(author_id: int | None, name: str) -> str:
    async with SessionLocal() as db:
        meso = Mesocycle(
            author_id=author_id,
            name=name,
            code=f"test-{uuid.uuid4().hex[:8]}",
            phases_in_cycle=1,
        )
        db.add(meso)
        await db.flush()
        db.add(MesocyclePhase(
            mesocycle_id=meso.id, phase_number=1, name="Тест", effort_tier="medium",
        ))
        await db.commit()
        return str(meso.id)


async def _delete_user(user_id: int) -> None:
    async with SessionLocal() as db:
        # ondelete="CASCADE" на mesocycles.author_id уносит и мезоцикл.
        await db.execute(delete(AppUser).where(AppUser.id == user_id))
        await db.commit()


async def test_foreign_mesocycle_not_visible_in_workout_center_context(
    client, auth_headers, test_user,
):
    other_id = await _make_other_user()
    try:
        foreign_name = f"Чужой {uuid.uuid4().hex[:8]}"
        foreign_id = await _make_mesocycle(other_id, foreign_name)

        r = await client.get("/workout-center/context", headers=auth_headers)
        assert r.status_code == 200, r.text
        ids = [m["id"] for m in r.json()["available_mesocycles"]]
        assert foreign_id not in ids
    finally:
        await _delete_user(other_id)


async def test_system_mesocycle_visible_in_workout_center_context(
    client, auth_headers, test_user,
):
    system_name = f"Системный {uuid.uuid4().hex[:8]}"
    system_id = await _make_mesocycle(None, system_name)
    try:
        r = await client.get("/workout-center/context", headers=auth_headers)
        assert r.status_code == 200, r.text
        ids = [m["id"] for m in r.json()["available_mesocycles"]]
        assert system_id in ids
    finally:
        async with SessionLocal() as db:
            await db.execute(delete(Mesocycle).where(Mesocycle.id == system_id))
            await db.commit()


async def test_get_mesocycles_list_filters_by_owner(client, auth_headers, test_user):
    other_id = await _make_other_user()
    try:
        foreign_name = f"Чужой {uuid.uuid4().hex[:8]}"
        foreign_id = await _make_mesocycle(other_id, foreign_name)
        own_id = await _make_mesocycle(test_user.id, f"Своё {uuid.uuid4().hex[:8]}")

        r = await client.get("/mesocycles/", headers=auth_headers)
        assert r.status_code == 200, r.text
        ids = [m["id"] for m in r.json()]
        assert own_id in ids
        assert foreign_id not in ids
    finally:
        await _delete_user(other_id)


async def test_activating_foreign_mesocycle_is_404(client, auth_headers, test_user):
    other_id = await _make_other_user()
    try:
        foreign_id = await _make_mesocycle(other_id, f"Чужой {uuid.uuid4().hex[:8]}")

        r = await client.patch(
            "/workout-center/context/mesocycle",
            json={"mesocycle_id": foreign_id, "microcycle_length": 7},
            headers=auth_headers,
        )
        assert r.status_code == 404, r.text
    finally:
        await _delete_user(other_id)
