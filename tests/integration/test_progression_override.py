"""Ручной выбор схемы через настройки профиля.

Пути и структура сверены по реальному коду, а не угаданы черновиком брифа:
- профиля (AppUserProfile) у только что созданного test_user ещё нет —
  PATCH /profile/settings 404-ит без него (см. api/routers/profile.py), а
  автосоздания профиля при регистрации нет нигде в app_user_service.py —
  поэтому фикстура with_profile создаёт строку профиля явно;
- настройки читаются через GET /profile (роута /profile/me не существует);
- создание тренировки — POST /workouts/start (а не POST /workouts, живёт в
  api/routers/workout_center.py — см. тот же вывод в docstring
  test_progression_lifecycle.py);
- seeded_history — фикстура Exercise, её id — это id упражнения
  (seeded_history.exercise_id у Exercise-модели не существует).

with_profile создаётся через `db`/`test_user` (коммитящее соединение) и
коммитится ДО обращения к `client`, ровно как это уже делает seeded_history —
`client` открывает своё соединение на каждый запрос и не увидит незакоммиченную
строку. Смешивать в одном тесте `client` с некоммитящей `db_session`/`app_user`
нельзя — см. комментарий в conftest.py про вечную блокировку.
"""

import pytest
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


@pytest.mark.asyncio
async def test_override_is_persisted_in_settings(client, auth_headers, with_profile):
    resp = await client.patch(
        "/profile/settings",
        headers=auth_headers,
        json={"progression": {"overrides": {"7": "double"}}},
    )
    assert resp.status_code == 200, resp.text

    profile = (await client.get("/profile", headers=auth_headers)).json()
    assert profile["settings"]["progression"]["overrides"]["7"] == "double"


@pytest.mark.asyncio
async def test_unknown_scheme_is_rejected(client, auth_headers, with_profile):
    resp = await client.patch(
        "/profile/settings",
        headers=auth_headers,
        json={"progression": {"overrides": {"7": "astrology"}}},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_settings_patch_without_progression_key_still_works(
    client, auth_headers, with_profile
):
    """Подавляющее большинство запросов ключ progression не шлют вообще —
    обработчик обязан пройти этот путь без исключений."""
    resp = await client.patch(
        "/profile/settings",
        headers=auth_headers,
        json={"weight_unit": "kg"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["settings"]["weight_unit"] == "kg"


@pytest.mark.asyncio
async def test_override_changes_the_prescribed_scheme(
    client, auth_headers, with_profile, seeded_history
):
    await client.patch(
        "/profile/settings",
        headers=auth_headers,
        json={
            "progression": {
                "overrides": {str(seeded_history.id): "fixed_increment"}
            }
        },
    )
    workout = (
        await client.post(
            "/workouts/start", headers=auth_headers, json={"source": "free"}
        )
    ).json()
    added = (
        await client.post(
            f"/workouts/{workout['id']}/exercises",
            headers=auth_headers,
            json={"exercise_id": seeded_history.id},
        )
    ).json()["exercises"][-1]

    assert added["prescription"]["scheme"] == "fixed_increment"
