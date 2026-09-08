"""Смена сплита пересобирает неправленые микроциклы (P1-03 ч.1, §5.6)."""
import pytest
from sqlalchemy import select

from api.seed_splits import ensure_system_splits
from api.services.models import AppUserMicrocycle, SplitBlueprint
from api.services.structure.bootstrap import ensure_structure
from app.database import SessionLocal

pytestmark = pytest.mark.asyncio

SEVEN_DAY = "Upper / Lower (4 Дня)"
EIGHT_DAY = "Верх / низ на восьмидневке"


async def _blueprint_id(name: str) -> str:
    async with SessionLocal() as db:
        await ensure_system_splits(db)
        await db.commit()
        row = (await db.execute(
            select(SplitBlueprint).where(
                SplitBlueprint.is_system.is_(True), SplitBlueprint.name == name,
            )
        )).scalars().first()
        return str(row.id)


async def _micros(user_id: int) -> dict[str, AppUserMicrocycle]:
    async with SessionLocal() as db:
        rows = (await db.execute(
            select(AppUserMicrocycle).where(AppUserMicrocycle.app_user_id == user_id)
        )).scalars().all()
    return {row.name: row for row in rows}


async def test_switching_split_rebuilds_untouched_microcycles(
    client, auth_headers, test_user,
):
    seven = await _blueprint_id(SEVEN_DAY)
    eight = await _blueprint_id(EIGHT_DAY)

    r = await client.post(
        "/splits/active", json={"blueprint_id": seven}, headers=auth_headers,
    )
    assert r.status_code == 200, r.text

    async with SessionLocal() as db:
        await ensure_structure(db, test_user.id)
        await db.commit()

    before = await _micros(test_user.id)
    assert all(m.length_days == 7 for m in before.values())

    r = await client.post(
        "/splits/active", json={"blueprint_id": eight}, headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    # Именно это поле отличает «пересобрал» от «молча ничего не сделал».
    assert r.json()["microcycles_rebuilt"] == len(before)

    after = await _micros(test_user.id)
    assert all(m.length_days == 8 for m in after.values()), {
        name: m.length_days for name, m in after.items()
    }
    assert all(len(m.days_mapping) == 8 for m in after.values())


async def test_patch_workout_center_split_also_rebuilds_microcycles(
    client, auth_headers, test_user,
):
    """Финальное ревью P1-03, Critical 1: реальная смена сплита в приложении
    идёт через PATCH /workout-center/split, а не POST /splits/active —
    мобильный селектор на экране тренировки бьёт именно сюда. До фикса этот
    обработчик менял blueprint_id и НЕ пересобирал микроциклы: тест упал бы
    (length_days осталось 7) на версии до правки."""
    seven = await _blueprint_id(SEVEN_DAY)
    eight = await _blueprint_id(EIGHT_DAY)

    r = await client.post(
        "/splits/active", json={"blueprint_id": seven}, headers=auth_headers,
    )
    assert r.status_code == 200, r.text

    async with SessionLocal() as db:
        await ensure_structure(db, test_user.id)
        await db.commit()

    before = await _micros(test_user.id)
    assert all(m.length_days == 7 for m in before.values())

    r = await client.patch(
        "/workout-center/split", json={"split_id": eight}, headers=auth_headers,
    )
    assert r.status_code == 200, r.text

    after = await _micros(test_user.id)
    assert all(m.length_days == 8 for m in after.values()), {
        name: m.length_days for name, m in after.items()
    }
    assert all(len(m.days_mapping) == 8 for m in after.values())


async def test_hand_edited_microcycle_is_left_alone(client, auth_headers, test_user):
    seven = await _blueprint_id(SEVEN_DAY)
    eight = await _blueprint_id(EIGHT_DAY)

    await client.post(
        "/splits/active", json={"blueprint_id": seven}, headers=auth_headers,
    )
    async with SessionLocal() as db:
        await ensure_structure(db, test_user.id)
        await db.commit()

    # Портим одну раскладку так, как это сделал бы человек руками.
    async with SessionLocal() as db:
        row = (await db.execute(
            select(AppUserMicrocycle).where(
                AppUserMicrocycle.app_user_id == test_user.id,
                AppUserMicrocycle.name == "Равномерный",
            )
        )).scalars().first()
        edited = dict(row.days_mapping)
        edited["1"] = {"type": "hard", "tag": edited["1"]["tag"]}
        row.days_mapping = edited
        await db.commit()

    await client.post(
        "/splits/active", json={"blueprint_id": eight}, headers=auth_headers,
    )

    after = await _micros(test_user.id)
    assert after["Равномерный"].length_days == 7
    assert after["Тяжёлый–лёгкий"].length_days == 8
