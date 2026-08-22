"""Инвариант: длина микроцикла = число слотов активного сплита (P1-03 ч.1, §5.4)."""
import pytest
from sqlalchemy import select

from api.seed_splits import ensure_system_splits
from api.services.models import AppUserMicrocycle, SplitBlueprint, UserSplit
from api.services.structure.microcycle_profiles import SlotView, build_days_mapping
from app.database import SessionLocal

pytestmark = pytest.mark.asyncio


async def _activate_seven_day_split(user_id: int) -> int:
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
        return blueprint.length_days


async def _make_microcycle(user_id: int, length_days: int) -> int:
    slots = [SlotView("upper")] * length_days
    async with SessionLocal() as db:
        micro = AppUserMicrocycle(
            app_user_id=user_id,
            name=f"Тест {length_days}",
            length_days=length_days,
            days_mapping=build_days_mapping("even", slots),
        )
        db.add(micro)
        await db.commit()
        await db.refresh(micro)
        return micro.id


async def test_matching_length_activates(client, auth_headers, test_user):
    length = await _activate_seven_day_split(test_user.id)
    micro_id = await _make_microcycle(test_user.id, length)

    r = await client.patch(
        "/workout-center/context/microcycle",
        json={"microcycle_id": micro_id},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text


async def test_mismatched_length_is_rejected(client, auth_headers, test_user):
    length = await _activate_seven_day_split(test_user.id)
    micro_id = await _make_microcycle(test_user.id, length + 1)

    r = await client.patch(
        "/workout-center/context/microcycle",
        json={"microcycle_id": micro_id},
        headers=auth_headers,
    )
    assert r.status_code == 409, r.text
    assert "сплит" in r.json()["detail"].lower()


async def test_detaching_the_microcycle_is_always_allowed(client, auth_headers, test_user):
    await _activate_seven_day_split(test_user.id)
    r = await client.patch(
        "/workout-center/context/microcycle",
        json={"microcycle_id": None},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
