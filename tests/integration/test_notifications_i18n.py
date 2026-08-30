from datetime import datetime, timezone

import pytest

from api.services import push_service
from api.services.models import AppUserProfile, PushDevice
from api.services.notification_service import create_notification


@pytest.mark.asyncio
async def test_notification_get_renders_same_semantic_row_in_request_language(
    client, db, test_user
):
    notification = await create_notification(
        db,
        app_user_id=test_user.id,
        event_type="goal_deadline",
        title="Приближается срок цели",
        body="До срока цели осталось 3 дн. Проверьте прогресс.",
        message_key="notification.goal_deadline",
        message_params={"days": 3},
        dedupe_key="notification-i18n-read",
    )
    await db.commit()

    ru = await client.get("/notifications", headers={"Accept-Language": "ru"})
    en = await client.get("/notifications", headers={"Accept-Language": "en"})
    ru_item = next(item for item in ru.json()["items"] if item["id"] == notification.id)
    en_item = next(item for item in en.json()["items"] if item["id"] == notification.id)

    assert ru_item["title"] == "Приближается срок цели"
    assert ru_item["body"] == "До срока цели осталось 3 дн. Проверьте прогресс."
    assert en_item["title"] == "Goal deadline approaching"
    assert en_item["body"] == "Your goal deadline is in 3 days. Check your progress."
    assert ru_item["id"] == en_item["id"]
    assert set(ru_item) == set(en_item) == {
        "id",
        "event_type",
        "entity_type",
        "entity_id",
        "title",
        "body",
        "payload",
        "read_at",
        "created_at",
    }


@pytest.mark.asyncio
async def test_notification_get_preserves_legacy_copy(client, db, test_user):
    notification = await create_notification(
        db,
        app_user_id=test_user.id,
        event_type="legacy_event",
        title="Legacy title byte-for-byte",
        body="Legacy body byte-for-byte",
        dedupe_key="notification-i18n-legacy",
    )
    await db.commit()

    response = await client.get(
        "/notifications", headers={"Accept-Language": "en"}
    )
    item = next(
        item for item in response.json()["items"] if item["id"] == notification.id
    )

    assert item["title"] == "Legacy title byte-for-byte"
    assert item["body"] == "Legacy body byte-for-byte"


@pytest.mark.asyncio
async def test_push_renders_semantic_notification_in_profile_language(
    db, test_user, monkeypatch
):
    db.add(
        AppUserProfile(
            app_user_id=test_user.id,
            settings={"language": "en"},
        )
    )
    db.add(
        PushDevice(
            app_user_id=test_user.id,
            installation_id="i18n-installation",
            expo_push_token="ExponentPushToken[i18n-device-token-123456]",
            platform="android",
            timezone_offset_minutes=0,
        )
    )
    await db.flush()
    notification = await create_notification(
        db,
        app_user_id=test_user.id,
        event_type="goal_deadline",
        title="Приближается срок цели",
        body="До срока цели осталось 3 дн. Проверьте прогресс.",
        message_key="notification.goal_deadline",
        message_params={"days": 3},
        dedupe_key="notification-i18n-push",
    )
    await db.commit()

    sent_payloads = []
    monkeypatch.setattr(
        push_service,
        "_expo_request",
        lambda _url, payload: sent_payloads.append(payload)
        or {"data": {"status": "ok", "id": "ticket-i18n"}},
    )

    assert await push_service.send_pending(db) == 1
    assert sent_payloads[0]["title"] == "Goal deadline approaching"
    assert sent_payloads[0]["data"] == {
        "notificationId": notification.id,
        "eventType": "goal_deadline",
        "route": "/progress",
    }
