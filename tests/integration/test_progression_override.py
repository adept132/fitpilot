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


# --- Фикс Critical: null снимает override, нестроковые значения — 422 ---
# (см. docs/superpowers/plans/2026-07-27-p0-06-progression-engine.md, Задача 15,
# ревью нашло краш на PATCH .../settings {"overrides": {"7": null}} — join()
# необработанного TypeError на нестроковых значениях overrides.values()).


@pytest.mark.asyncio
async def test_null_removes_override_and_keeps_others(client, auth_headers, with_profile):
    """null для одного ключа снимает именно его override, не трогая
    override других упражнений — семантика PATCH здесь слияние, а не замена
    всего словаря целиком."""
    resp = await client.patch(
        "/profile/settings",
        headers=auth_headers,
        json={"progression": {"overrides": {"7": "double", "9": "fixed_increment"}}},
    )
    assert resp.status_code == 200, resp.text

    resp = await client.patch(
        "/profile/settings",
        headers=auth_headers,
        json={"progression": {"overrides": {"7": None}}},
    )
    assert resp.status_code == 200, resp.text

    profile = (await client.get("/profile", headers=auth_headers)).json()
    overrides = profile["settings"]["progression"]["overrides"]
    assert "7" not in overrides
    assert overrides["9"] == "fixed_increment"


@pytest.mark.asyncio
async def test_null_for_unknown_key_is_a_noop(client, auth_headers, with_profile):
    """Снятие override для упражнения, у которого его никогда не было, —
    не ошибка: результат совпадает с тем, что и так уже верно (override
    отсутствует)."""
    resp = await client.patch(
        "/profile/settings",
        headers=auth_headers,
        json={"progression": {"overrides": {"123": None}}},
    )
    assert resp.status_code == 200, resp.text

    profile = (await client.get("/profile", headers=auth_headers)).json()
    overrides = profile["settings"]["progression"]["overrides"]
    assert "123" not in overrides


@pytest.mark.asyncio
async def test_non_string_scheme_value_is_rejected_not_500(client, auth_headers, with_profile):
    """Число вместо имени схемы — ошибка клиента (422), а не необработанный
    TypeError на join() внутри обработчика (500)."""
    resp = await client.patch(
        "/profile/settings",
        headers=auth_headers,
        json={"progression": {"overrides": {"7": 42}}},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_nested_object_scheme_value_is_rejected(client, auth_headers, with_profile):
    """Вложенный объект вместо имени схемы тоже отвергается 422, а не падает
    в join()/сравнении с KNOWN_SCHEMES."""
    resp = await client.patch(
        "/profile/settings",
        headers=auth_headers,
        json={"progression": {"overrides": {"7": {"nested": "value"}}}},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_null_merge_does_not_disturb_unrelated_settings(
    client, auth_headers, with_profile
):
    """Снятие override через null остаётся частью обычного слияния settings —
    остальные поля запроса (и ранее сохранённые) не страдают."""
    await client.patch(
        "/profile/settings",
        headers=auth_headers,
        json={"progression": {"overrides": {"7": "double"}}, "weight_unit": "kg"},
    )

    resp = await client.patch(
        "/profile/settings",
        headers=auth_headers,
        json={"progression": {"overrides": {"7": None}}},
    )
    assert resp.status_code == 200, resp.text

    profile = (await client.get("/profile", headers=auth_headers)).json()
    assert profile["settings"]["weight_unit"] == "kg"
    assert "7" not in profile["settings"]["progression"]["overrides"]


# --- Фикс: остаточная находка ревью — overrides/progression не-словарём
# роняли обработчик необработанным AttributeError (500) вместо 422.
# overrides — Any внутри UpdateSettingsRequest.progression (Dict[str, Any]),
# поэтому только явная проверка isinstance(..., dict) в самом обработчике
# спасает от overrides.items() на списке/строке/числе. progression как
# не-словарь pydantic уже отклоняет схемно (Dict[str, Any]), но обработчик
# теперь проверяет и это явно — на случай если тип поля когда-нибудь
# ослабнет незаметно для этого кода.


@pytest.mark.asyncio
async def test_overrides_as_list_is_rejected_not_500(client, auth_headers, with_profile):
    resp = await client.patch(
        "/profile/settings",
        headers=auth_headers,
        json={"progression": {"overrides": [1, 2, 3]}},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_overrides_as_string_is_rejected_not_500(client, auth_headers, with_profile):
    resp = await client.patch(
        "/profile/settings",
        headers=auth_headers,
        json={"progression": {"overrides": "double"}},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_progression_as_list_is_rejected(client, auth_headers, with_profile):
    resp = await client.patch(
        "/profile/settings",
        headers=auth_headers,
        json={"progression": [1, 2, 3]},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_progression_as_string_is_rejected(client, auth_headers, with_profile):
    resp = await client.patch(
        "/profile/settings",
        headers=auth_headers,
        json={"progression": "double"},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_empty_progression_and_overrides_are_noop(client, auth_headers, with_profile):
    """progression: {} и overrides: {} — синтаксически корректный пустой
    ввод, проходит без ошибки и ничего не ломает."""
    resp = await client.patch(
        "/profile/settings",
        headers=auth_headers,
        json={"progression": {}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["settings"]["progression"]["overrides"] == {}

    resp = await client.patch(
        "/profile/settings",
        headers=auth_headers,
        json={"progression": {"overrides": {}}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["settings"]["progression"]["overrides"] == {}


@pytest.mark.asyncio
async def test_missing_progression_key_still_works(client, auth_headers, with_profile):
    """Дублирует test_settings_patch_without_progression_key_still_works
    намеренно — фиксирует именно этот путь как часть фикса недоброкачественного
    payload, а не полагается на совпадение с уже существующим тестом."""
    resp = await client.patch(
        "/profile/settings",
        headers=auth_headers,
        json={"weight_unit": "lbs"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["settings"]["weight_unit"] == "lbs"


@pytest.mark.asyncio
async def test_overrides_as_number_is_rejected_not_500(client, auth_headers, with_profile):
    resp = await client.patch(
        "/profile/settings",
        headers=auth_headers,
        json={"progression": {"overrides": 42}},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_overrides_as_bool_is_rejected_not_500(client, auth_headers, with_profile):
    resp = await client.patch(
        "/profile/settings",
        headers=auth_headers,
        json={"progression": {"overrides": True}},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_progression_as_number_is_rejected(client, auth_headers, with_profile):
    resp = await client.patch(
        "/profile/settings",
        headers=auth_headers,
        json={"progression": 42},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_progression_as_bool_is_rejected(client, auth_headers, with_profile):
    resp = await client.patch(
        "/profile/settings",
        headers=auth_headers,
        json={"progression": True},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_non_numeric_key_is_a_safe_noop(client, auth_headers, with_profile):
    """Нечисловой ключ (например, «abc») не совпадёт ни с одним exercise_id
    в resolve.override_for и безопасно игнорируется движком — обработчик
    не обязан отвергать его отдельно, важно лишь не упасть на нём."""
    resp = await client.patch(
        "/profile/settings",
        headers=auth_headers,
        json={"progression": {"overrides": {"abc": "double"}}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["settings"]["progression"]["overrides"]["abc"] == "double"


@pytest.mark.asyncio
async def test_empty_string_key_is_a_safe_noop(client, auth_headers, with_profile):
    resp = await client.patch(
        "/profile/settings",
        headers=auth_headers,
        json={"progression": {"overrides": {"": "double"}}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["settings"]["progression"]["overrides"][""] == "double"


@pytest.mark.asyncio
async def test_very_long_key_is_a_safe_noop(client, auth_headers, with_profile):
    long_key = "7" * 5000
    resp = await client.patch(
        "/profile/settings",
        headers=auth_headers,
        json={"progression": {"overrides": {long_key: "double"}}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["settings"]["progression"]["overrides"][long_key] == "double"


@pytest.mark.asyncio
async def test_empty_string_value_is_rejected_not_500(client, auth_headers, with_profile):
    """Пустая строка — синтаксически строка, но не имя известной схемы:
    ветка «неизвестная схема» (422), а не крах."""
    resp = await client.patch(
        "/profile/settings",
        headers=auth_headers,
        json={"progression": {"overrides": {"7": ""}}},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_nested_list_scheme_value_is_rejected(client, auth_headers, with_profile):
    resp = await client.patch(
        "/profile/settings",
        headers=auth_headers,
        json={"progression": {"overrides": {"7": [1, 2]}}},
    )
    assert resp.status_code == 422, resp.text
