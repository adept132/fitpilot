"""Вердикт доезжает до предписаний сессии (спека P0-07 §6.1, §12.1).

PATCH /profile/settings 404-ит без строки AppUserProfile (см.
api/routers/profile.py) — она заводится только онбордингом, автосоздания
нигде нет. with_profile ниже — тот же приём, что и в test_readiness_api.py /
test_progression_override.py: строка профиля через `db`/`test_user`
(коммитящее соединение), закоммичена ДО обращения к `client`.
"""

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.models import AppUser, AppUserProfile


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


async def test_context_without_verdict_stays_ok(db_session, app_user, session_exercise):
    from api.services.progression import repository as progression_repo

    ctx = await progression_repo.build_context(
        db_session, session_exercise, app_user.id, "intermediate", {}
    )
    assert ctx.readiness_level == "ok"
    assert ctx.readiness_source is None


async def test_context_resolves_the_verdict_for_this_exercise(
    db_session, app_user, session_exercise
):
    from api.services.progression import repository as progression_repo
    from api.services.readiness.types import CheckinSignals
    from api.services.readiness.verdict import build_verdict

    # Фикстура session_exercise — упражнение с main_muscle_group="chest".
    verdict = build_verdict(CheckinSignals(sleep=5, stress=1, soreness={"chest": 3}))
    ctx = await progression_repo.build_context(
        db_session, session_exercise, app_user.id, "intermediate", {},
        readiness=verdict,
    )
    assert ctx.readiness_level == "limit"
    assert ctx.readiness_source == "soreness"


async def test_start_workout_without_checkin_uuid_works(client, auth_headers, seeded_plan):
    response = await client.post(
        "/workouts/start",
        headers=auth_headers,
        json={"source": "free", "plan_id": seeded_plan.id},
    )
    assert response.status_code == 200, response.text


async def test_start_workout_applies_the_checkin(client, auth_headers, seeded_plan):
    await client.post(
        "/readiness/checkin",
        headers=auth_headers,
        json={"client_uuid": "chk-start", "sleep": 1, "stress": 5},
    )
    response = await client.post(
        "/workouts/start",
        headers=auth_headers,
        json={
            "source": "free",
            "plan_id": seeded_plan.id,
            "readiness_checkin_uuid": "chk-start",
        },
    )
    assert response.status_code == 200, response.text

    detail = await client.get(
        f"/workouts/{response.json()['id']}", headers=auth_headers
    )
    exercises = detail.json()["exercises"]
    assert exercises
    # Глобальный limit обязан оставить след в предписании каждого упражнения.
    assert any(
        ex["prescription"]["volume_reason_code"] == "readiness_volume"
        or ex["prescription"]["reason_code"] == "readiness_limit"
        for ex in exercises
    )


async def test_unknown_checkin_uuid_is_ignored_not_fatal(
    client, auth_headers, seeded_plan
):
    # Клиент мог отправить uuid чек-ина, который ещё не доехал синхронизацией.
    response = await client.post(
        "/workouts/start",
        headers=auth_headers,
        json={
            "source": "free",
            "plan_id": seeded_plan.id,
            "readiness_checkin_uuid": "never-synced",
        },
    )
    assert response.status_code == 200, response.text


async def test_disabled_checkin_is_ignored_even_if_uuid_is_sent(
    client, auth_headers, seeded_plan, with_profile
):
    # Тумблер выключен — вердикта нет, что бы клиент ни прислал.
    await client.post(
        "/readiness/checkin",
        headers=auth_headers,
        json={"client_uuid": "chk-off", "sleep": 1, "stress": 5},
    )
    await client.patch(
        "/profile/settings",
        headers=auth_headers,
        json={"readiness": {"checkin_enabled": False}},
    )
    response = await client.post(
        "/workouts/start",
        headers=auth_headers,
        json={
            "source": "free",
            "plan_id": seeded_plan.id,
            "readiness_checkin_uuid": "chk-off",
        },
    )
    assert response.status_code == 200, response.text

    detail = await client.get(
        f"/workouts/{response.json()['id']}", headers=auth_headers
    )
    for ex in detail.json()["exercises"]:
        assert ex["prescription"]["volume_reason_code"] is None
        assert ex["prescription"]["reason_code"] != "readiness_limit"
