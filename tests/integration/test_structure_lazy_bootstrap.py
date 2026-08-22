"""Существующий пользователь со сплитом, но без структуры, чинится сам (P1-03 ч.1, §5.5)."""
import pytest
from sqlalchemy import select

from api.seed_splits import ensure_system_splits
from api.services.models import (
    AppUserMesocycle, AppUserMicrocycle, SplitBlueprint, UserSplit,
)
from app.database import SessionLocal

pytestmark = pytest.mark.asyncio


async def _activate_split(user_id: int) -> None:
    async with SessionLocal() as db:
        await ensure_system_splits(db)
        blueprint = (await db.execute(
            select(SplitBlueprint).where(
                SplitBlueprint.is_system.is_(True),
                SplitBlueprint.name == "Upper / Lower (4 Дня)",
            )
        )).scalars().first()
        db.add(UserSplit(
            app_user_id=user_id, blueprint_id=blueprint.id,
            is_active=True, current_day=1,
        ))
        await db.commit()


async def test_opening_the_workout_center_fills_missing_structure(
    client, auth_headers, test_user,
):
    await _activate_split(test_user.id)

    r = await client.get("/workout-center/context", headers=auth_headers)
    assert r.status_code == 200, r.text

    async with SessionLocal() as db:
        active_meso = (await db.execute(
            select(AppUserMesocycle).where(
                AppUserMesocycle.app_user_id == test_user.id,
                AppUserMesocycle.is_active.is_(True),
            )
        )).scalars().first()
        micros = (await db.execute(
            select(AppUserMicrocycle).where(
                AppUserMicrocycle.app_user_id == test_user.id
            )
        )).scalars().all()

    assert active_meso is not None
    assert len(micros) == 5
    assert any(m.is_active for m in micros)


async def test_user_with_existing_structure_is_not_touched(
    client, auth_headers, test_user,
):
    await _activate_split(test_user.id)

    async with SessionLocal() as db:
        micro = AppUserMicrocycle(
            app_user_id=test_user.id, name="Мой ручной",
            length_days=7, days_mapping={"1": {"type": "hard", "tag": "upper"}},
            is_active=True,
        )
        db.add(micro)
        await db.commit()

    r = await client.get("/workout-center/context", headers=auth_headers)
    assert r.status_code == 200, r.text

    async with SessionLocal() as db:
        active = (await db.execute(
            select(AppUserMicrocycle).where(
                AppUserMicrocycle.app_user_id == test_user.id,
                AppUserMicrocycle.is_active.is_(True),
            )
        )).scalars().all()

    assert [m.name for m in active] == ["Мой ручной"]
