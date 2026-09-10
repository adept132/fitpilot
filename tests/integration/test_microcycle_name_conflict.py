"""uq_app_user_microcycle_name (app_user_id, name): роутер отвечает 409, не 500 (P1-03)."""
import uuid

import pytest
from sqlalchemy import delete

from api.services.models import AppUser, AppUserMicrocycle
from app.database import SessionLocal

pytestmark = pytest.mark.asyncio

_PAYLOAD_DAYS = {"1": {"type": "hard", "tag": "push"}, "2": {"type": "rest", "tag": None}}


def _ru_headers(headers: dict[str, str]) -> dict[str, str]:
    return {**headers, "Accept-Language": "ru"}


def _payload(name: str) -> dict:
    return {"name": name, "length_days": 2, "days_mapping": _PAYLOAD_DAYS}


async def _make_microcycle(user_id: int, name: str) -> int:
    async with SessionLocal() as db:
        micro = AppUserMicrocycle(
            app_user_id=user_id, name=name, length_days=2,
            days_mapping=_PAYLOAD_DAYS,
        )
        db.add(micro)
        await db.commit()
        await db.refresh(micro)
        return micro.id


async def test_create_with_taken_name_is_409(client, auth_headers, test_user):
    name = f"Занято {uuid.uuid4().hex[:8]}"
    await _make_microcycle(test_user.id, name)

    r = await client.post(
        "/microcycles/", json=_payload(name), headers=_ru_headers(auth_headers)
    )
    assert r.status_code == 409, r.text
    assert "имен" in r.json()["detail"].lower()


async def test_rename_into_taken_name_is_409(client, auth_headers, test_user):
    taken = f"Занято {uuid.uuid4().hex[:8]}"
    await _make_microcycle(test_user.id, taken)
    editable_id = await _make_microcycle(test_user.id, f"Своё {uuid.uuid4().hex[:8]}")

    r = await client.put(
        f"/microcycles/{editable_id}",
        json=_payload(taken),
        headers=_ru_headers(auth_headers),
    )
    assert r.status_code == 409, r.text
    assert "имен" in r.json()["detail"].lower()


async def test_same_name_across_different_users_is_allowed(client, auth_headers, test_user):
    # Ограничение уникально в пределах (app_user_id, name) — то же имя у
    # ДРУГОГО пользователя конфликта не создаёт.
    name = f"Общее {uuid.uuid4().hex[:8]}"
    marker = uuid.uuid4().hex[:12]
    async with SessionLocal() as db:
        other = AppUser(
            firebase_uid=f"test-{marker}",
            email=f"test-{marker}@example.com",
            display_name="Other Test User",
        )
        db.add(other)
        await db.commit()
        await db.refresh(other)
        other_id = other.id

        db.add(AppUserMicrocycle(
            app_user_id=other_id, name=name, length_days=2,
            days_mapping=_PAYLOAD_DAYS,
        ))
        await db.commit()

    try:
        r = await client.post("/microcycles/", json=_payload(name), headers=auth_headers)
        assert r.status_code == 201, r.text
    finally:
        async with SessionLocal() as db:
            # Каскад (ondelete="CASCADE") уносит и микроцикл этого пользователя.
            await db.execute(delete(AppUser).where(AppUser.id == other_id))
            await db.commit()
