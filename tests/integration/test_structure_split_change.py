"""Смена сплита пересобирает неправленые микроциклы (P1-03 ч.1, §5.6)."""
from datetime import date, timedelta

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


async def test_hand_edited_active_microcycle_is_deactivated_on_split_change(
    client, auth_headers, test_user,
):
    """Финальное ревью P1-03, правка 1: активный правленый микроцикл должен
    остаться недостижимой парой «сплит длины N, активный микроцикл длины
    M != N» — §10.5. rebuild_profile_microcycles пропускает правленые
    раскладки (§5.6), но раньше никто не проверял, не была ли пропущенная
    строка ещё и активной. До правки это утверждение падает: is_active
    остаётся True при length_days=7 у активного сплита длины 8."""
    seven = await _blueprint_id(SEVEN_DAY)
    eight = await _blueprint_id(EIGHT_DAY)

    await client.post(
        "/splits/active", json={"blueprint_id": seven}, headers=auth_headers,
    )
    async with SessionLocal() as db:
        await ensure_structure(db, test_user.id)
        await db.commit()

    # "Равномерный" — дефолт для новичка (see ensure_structure), значит уже
    # активен. Портим его раскладку так, как это сделал бы человек руками —
    # тот же приём, что и в test_hand_edited_microcycle_is_left_alone.
    async with SessionLocal() as db:
        row = (await db.execute(
            select(AppUserMicrocycle).where(
                AppUserMicrocycle.app_user_id == test_user.id,
                AppUserMicrocycle.name == "Равномерный",
            )
        )).scalars().first()
        assert row.is_active is True
        edited = dict(row.days_mapping)
        edited["1"] = {"type": "hard", "tag": edited["1"]["tag"]}
        row.days_mapping = edited
        await db.commit()

    r = await client.post(
        "/splits/active", json={"blueprint_id": eight}, headers=auth_headers,
    )
    assert r.status_code == 200, r.text

    after = await _micros(test_user.id)
    edited_row = after["Равномерный"]
    # Раскладка правленого не тронута.
    assert edited_row.length_days == 7
    assert edited_row.days_mapping["1"]["type"] == "hard"
    # Но он больше не активен — недостижимая пара §10.5 не должна была
    # пережить смену сплита.
    assert edited_row.is_active is False


async def test_launch_split_rebuilds_untouched_microcycles(
    client, auth_headers, test_user,
):
    """POST /splits/launch is a split-change path, not just a scheduler call."""
    seven = await _blueprint_id(SEVEN_DAY)
    eight = await _blueprint_id(EIGHT_DAY)

    response = await client.post(
        "/splits/active", json={"blueprint_id": seven}, headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    async with SessionLocal() as db:
        await ensure_structure(db, test_user.id)
        await db.commit()

    before = await _micros(test_user.id)
    assert before and all(row.length_days == 7 for row in before.values())

    response = await client.post(
        "/splits/launch",
        json={
            "blueprint_id": eight,
            "start_date": (date.today() + timedelta(days=1)).isoformat(),
            "blackout_weekdays": [],
        },
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text

    after = await _micros(test_user.id)
    assert all(row.length_days == 8 for row in after.values())
    assert all(len(row.days_mapping) == 8 for row in after.values())


async def test_launch_split_deactivates_incompatible_edited_active_microcycle(
    client, auth_headers, test_user,
):
    seven = await _blueprint_id(SEVEN_DAY)
    eight = await _blueprint_id(EIGHT_DAY)

    response = await client.post(
        "/splits/active", json={"blueprint_id": seven}, headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    async with SessionLocal() as db:
        await ensure_structure(db, test_user.id)
        row = (await db.execute(
            select(AppUserMicrocycle).where(
                AppUserMicrocycle.app_user_id == test_user.id,
                AppUserMicrocycle.is_active.is_(True),
            )
        )).scalars().one()
        edited = dict(row.days_mapping)
        edited["1"] = {"type": "hard", "tag": edited["1"]["tag"]}
        row.days_mapping = edited
        edited_id = row.id
        await db.commit()

    response = await client.post(
        "/splits/launch",
        json={
            "blueprint_id": eight,
            "start_date": (date.today() + timedelta(days=1)).isoformat(),
            "blackout_weekdays": [],
        },
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text

    async with SessionLocal() as db:
        edited_row = await db.get(AppUserMicrocycle, edited_id)
        assert edited_row.length_days == 7
        assert edited_row.days_mapping["1"]["type"] == "hard"
        assert edited_row.is_active is False
