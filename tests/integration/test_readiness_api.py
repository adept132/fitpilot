"""Ручки чек-ина по HTTP (спека P0-07 §12.1).

Только client/auth_headers: смешивать их с db_session/app_user в одном
тесте нельзя — см. разбор вечной блокировки в conftest.py.

PATCH /profile/settings 404-ит без строки AppUserProfile (см.
api/routers/profile.py) — она заводится только онбордингом. Тестам, которые
дёргают этот эндпоинт, нужна строка профиля заранее; with_profile ниже — тот
же приём, что и в test_progression_override.py (db/test_user, коммитящее
соединение, закоммичено до обращения к client)."""

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.models import AppUser, AppUserProfile
from api.services.readiness import params


@pytest_asyncio.fixture
async def with_profile(db: AsyncSession, test_user: AppUser):
    """Строка профиля для test_user — без неё PATCH /profile/settings 404-ит.

    Отдельного teardown не нужно: app_user_profiles.app_user_id имеет
    ON DELETE CASCADE на app_users, а teardown фикстуры test_user уже сносит
    строку пользователя.
    """
    profile = AppUserProfile(app_user_id=test_user.id)
    db.add(profile)
    await db.commit()
    yield profile


async def test_checkin_returns_a_verdict(client, auth_headers):
    response = await client.post(
        "/readiness/checkin",
        headers=auth_headers,
        json={
            "client_uuid": "chk-1",
            "sleep": 1,
            "stress": 5,
            "soreness": {"quads": 3},
            "pain": {},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["verdict"]["level"] == "limit"
    assert body["verdict"]["reason_code"] == "readiness_limit"
    assert body["verdict"]["reason_text"].strip()
    flags = {f["muscle"]: f["level"] for f in body["verdict"]["muscle_flags"]}
    assert flags["quads"] == "limit"


async def test_checkin_is_idempotent(client, auth_headers):
    payload = {"client_uuid": "chk-dup", "sleep": 3, "stress": 3}
    first = await client.post("/readiness/checkin", headers=auth_headers, json=payload)
    second = await client.post("/readiness/checkin", headers=auth_headers, json=payload)
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["verdict"]["level"] == first.json()["verdict"]["level"]


async def test_empty_checkin_returns_null_verdict(client, auth_headers):
    response = await client.post(
        "/readiness/checkin", headers=auth_headers, json={"client_uuid": "chk-empty"}
    )
    assert response.status_code == 200
    assert response.json()["verdict"] is None


async def test_out_of_range_scale_is_422(client, auth_headers):
    response = await client.post(
        "/readiness/checkin",
        headers=auth_headers,
        json={"client_uuid": "chk-bad", "sleep": 9},
    )
    assert response.status_code == 422


async def test_unknown_soreness_muscle_is_422(client, auth_headers):
    response = await client.post(
        "/readiness/checkin",
        headers=auth_headers,
        json={"client_uuid": "chk-bad2", "soreness": {"хвост": 2}},
    )
    assert response.status_code == 422


async def test_unknown_pain_place_is_422(client, auth_headers):
    response = await client.post(
        "/readiness/checkin",
        headers=auth_headers,
        json={"client_uuid": "chk-bad3", "pain": {"крыло": 2}},
    )
    assert response.status_code == 422


async def test_joint_is_a_valid_pain_place(client, auth_headers):
    response = await client.post(
        "/readiness/checkin",
        headers=auth_headers,
        json={"client_uuid": "chk-joint", "pain": {"elbow": 2}},
    )
    assert response.status_code == 200


async def test_context_reports_active_pain(client, auth_headers):
    await client.post(
        "/readiness/checkin",
        headers=auth_headers,
        json={"client_uuid": "chk-ctx", "pain": {"knee": 2}},
    )
    response = await client.get("/readiness/checkin/context", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["active_pain"] == {"knee": 2}


async def test_context_reports_the_toggle(client, auth_headers, with_profile):
    response = await client.get("/readiness/checkin/context", headers=auth_headers)
    assert response.json()["checkin_enabled"] is True

    await client.patch(
        "/profile/settings",
        headers=auth_headers,
        json={"readiness": {"checkin_enabled": False}},
    )
    response = await client.get("/readiness/checkin/context", headers=auth_headers)
    assert response.json()["checkin_enabled"] is False


async def test_toggle_rejects_non_boolean(client, auth_headers, with_profile):
    response = await client.patch(
        "/profile/settings",
        headers=auth_headers,
        json={"readiness": {"checkin_enabled": "нет"}},
    )
    assert response.status_code == 422


async def test_muscle_chip_list_is_capped(client, auth_headers):
    response = await client.get("/readiness/checkin/context", headers=auth_headers)
    assert len(response.json()["muscles"]) <= params.CHECKIN_MAX_MUSCLE_CHIPS
