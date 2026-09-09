"""Мезоциклы не текут между пользователями (финальное ревью P1-03, правка 2)."""
import uuid
from importlib import import_module

import pytest
from sqlalchemy import delete, select, update

from api.services.models import AppUser, AppUserMesocycle, Mesocycle, MesocyclePhase
from api.services.structure.bootstrap import ensure_structure
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


async def test_historical_foreign_active_relation_is_not_serialized(
    client, auth_headers, test_user,
):
    """A legacy bad join row must not expose another user's periodization."""
    # First create the user's normal structure.  That makes the later foreign
    # relation the only active one while ensuring the context bootstrap takes
    # its idempotent path and does not repair the fixture for us.
    async with SessionLocal() as db:
        await ensure_structure(db, test_user.id)
        await db.commit()

    other_id = await _make_other_user()
    try:
        foreign_name = f"TOP SECRET {uuid.uuid4().hex[:8]}"
        foreign_id = await _make_mesocycle(other_id, foreign_name)
        async with SessionLocal() as db:
            await db.execute(
                update(AppUserMesocycle)
                .where(AppUserMesocycle.app_user_id == test_user.id)
                .values(is_active=False)
            )
            db.add(AppUserMesocycle(
                app_user_id=test_user.id,
                mesocycle_id=uuid.UUID(foreign_id),
                is_active=True,
                microcycle_length=7,
                current_phase=1,
            ))
            await db.commit()

        response = await client.get("/workout-center/context", headers=auth_headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["selected_periodization"] is None
        assert foreign_name not in response.text
    finally:
        await _delete_user(other_id)


async def test_cleanup_migration_deactivates_only_cross_user_relations(test_user):
    """The repair keeps own/system selections and neutralizes legacy leaks."""
    other_id = await _make_other_user()
    system_id = None
    try:
        own_id = await _make_mesocycle(test_user.id, f"Own {uuid.uuid4().hex[:8]}")
        system_id = await _make_mesocycle(None, f"System {uuid.uuid4().hex[:8]}")
        foreign_id = await _make_mesocycle(other_id, f"Foreign {uuid.uuid4().hex[:8]}")
        async with SessionLocal() as db:
            relations = [
                AppUserMesocycle(
                    app_user_id=test_user.id,
                    mesocycle_id=uuid.UUID(mesocycle_id),
                    is_active=True,
                    microcycle_length=7,
                    current_phase=1,
                )
                for mesocycle_id in (own_id, system_id, foreign_id)
            ]
            db.add_all(relations)
            await db.commit()
            relation_ids = [relation.id for relation in relations]

        migration = import_module(
            "migrations.versions.20260909_01_deactivate_cross_user_mesocycles"
        )
        async with SessionLocal() as db:
            await db.run_sync(migration.deactivate_cross_user_relations)
            await db.commit()
            rows = (await db.execute(
                select(AppUserMesocycle).where(
                    AppUserMesocycle.id.in_(relation_ids)
                )
            )).scalars().all()

        active_by_meso = {str(row.mesocycle_id): row.is_active for row in rows}
        assert active_by_meso[own_id] is True
        assert active_by_meso[system_id] is True
        assert active_by_meso[foreign_id] is False
    finally:
        if system_id is not None:
            async with SessionLocal() as db:
                await db.execute(
                    delete(Mesocycle).where(Mesocycle.id == uuid.UUID(system_id))
                )
                await db.commit()
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
