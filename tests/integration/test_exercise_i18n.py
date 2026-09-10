from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete

from api.services.models import Exercise


pytestmark = pytest.mark.asyncio


async def test_system_exercise_returns_both_names_and_is_found_in_english(
    client, auth_headers, db
):
    marker = uuid.uuid4().hex[:8]
    exercise = Exercise(
        name=f"Жим лёжа локализация {marker}",
        name_en=f"Bench Press Localization {marker}",
        description="Контролируемое движение.",
        description_en="Press the bar with a controlled range of motion.",
        category="base",
        main_muscle_group="Грудь",
        secondary_muscle_groups=[],
        equipment_needed=["barbell"],
        difficulty="beginner",
        source="default",
    )
    db.add(exercise)
    await db.commit()
    await db.refresh(exercise)

    try:
        detail = await client.get(
            f"/exercises/{exercise.id}", headers=auth_headers
        )
        assert detail.status_code == 200
        assert detail.json()["name"] == exercise.name
        assert detail.json()["description"] == exercise.description
        assert detail.json()["localized_names"] == {
            "ru": exercise.name,
            "en": exercise.name_en,
        }
        assert detail.json()["localized_descriptions"]["en"] == exercise.description_en

        search = await client.get(
            "/exercises", params={"q": f"bench press localization {marker}"}, headers=auth_headers
        )
        assert search.status_code == 200
        assert exercise.id in [item["id"] for item in search.json()]
    finally:
        await db.execute(delete(Exercise).where(Exercise.id == exercise.id))
        await db.commit()


async def test_custom_exercise_name_is_untouched_for_english_requests(
    client, auth_headers, db, test_user
):
    marker = uuid.uuid4().hex[:8]
    exercise = Exercise(
        name=f"Моё упражнение {marker}",
        category="isolation",
        main_muscle_group="Грудь",
        secondary_muscle_groups=[],
        equipment_needed=[],
        difficulty="beginner",
        source="custom",
        app_user_id=test_user.id,
    )
    db.add(exercise)
    await db.commit()
    await db.refresh(exercise)

    response = await client.get(
        f"/exercises/{exercise.id}",
        headers={**auth_headers, "Accept-Language": "en"},
    )

    assert response.status_code == 200
    assert response.json()["name"] == exercise.name
    assert response.json()["localized_names"] == {"ru": exercise.name}


async def test_custom_exercise_idempotent_retry_keeps_russian_maps(
    client, auth_headers
):
    marker = uuid.uuid4().hex
    payload = {
        "name": f"Мой повтор {marker[:8]}",
        "description": "Моя техника",
        "main_muscle_group": "Грудь",
        "secondary_muscle_groups": [],
        "equipment_needed": [],
        "client_uuid": marker,
    }
    english_headers = {**auth_headers, "Accept-Language": "en"}

    created = await client.post(
        "/exercises", json=payload, headers=english_headers
    )
    replayed = await client.post(
        "/exercises", json=payload, headers=english_headers
    )

    assert created.status_code == replayed.status_code == 201
    assert created.json()["id"] == replayed.json()["id"]
    for body in (created.json(), replayed.json()):
        assert body["name"] == payload["name"]
        assert body["localized_names"] == {"ru": payload["name"]}
        assert body["description"] == payload["description"]
        assert body["localized_descriptions"] == {
            "ru": payload["description"]
        }
