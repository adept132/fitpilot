from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from api.services.models import (
    AppNotification,
    BodyMeasurement,
    PushDelivery,
    PushDevice,
    UserCalendarDay,
    UserGoal,
)
from api.services.notification_service import create_notification
from api.services import push_service


@pytest.mark.asyncio
async def test_notification_crud_and_deduplication(client, db, test_user):
    first = await create_notification(
        db,
        app_user_id=test_user.id,
        event_type="test_event",
        title="First",
        body="Body",
        dedupe_key="same-event",
        payload={"route": "/progress"},
    )
    duplicate = await create_notification(
        db,
        app_user_id=test_user.id,
        event_type="test_event",
        title="Ignored duplicate",
        body="Ignored",
        dedupe_key="same-event",
    )
    await db.commit()
    assert duplicate.id == first.id

    response = await client.get("/notifications")
    assert response.status_code == 200
    payload = response.json()
    assert payload["unread_count"] == 1
    assert len(payload["items"]) == 1
    assert payload["items"][0]["payload"]["route"] == "/progress"

    notification_id = payload["items"][0]["id"]
    response = await client.post(f"/notifications/{notification_id}/read")
    assert response.status_code == 200
    assert response.json()["read_at"] is not None

    response = await client.get("/notifications/badge")
    assert response.status_code == 200
    assert response.json() == {"unread_count": 0}


@pytest.mark.asyncio
async def test_materializes_initial_domain_events(client, db, test_user):
    today = date.today()
    db.add(
        UserCalendarDay(
            app_user_id=test_user.id,
            target_date=today,
            plan_id=None,
            is_rest_day=False,
            is_blackout=False,
            status="planned",
        )
    )
    db.add(
        UserGoal(
            app_user_id=test_user.id,
            goal_type="bodyweight",
            target_value=80,
            unit="kg",
            deadline=today + timedelta(days=2),
            is_completed=False,
        )
    )
    db.add(
        BodyMeasurement(
            app_user_id=test_user.id,
            metric_key="waist",
            value=80,
            recorded_at=datetime.now(timezone.utc) - timedelta(days=20),
        )
    )
    await db.commit()

    response = await client.get(
        "/notifications", params={"local_date": today.isoformat()}
    )
    assert response.status_code == 200
    event_types = {item["event_type"] for item in response.json()["items"]}
    assert event_types == {
        "training_day_without_plan",
        "goal_deadline",
        "measurements_due",
    }
    assert response.json()["unread_count"] == 3

    # Re-projecting the same domain state must not create duplicates.
    response = await client.get(
        "/notifications", params={"local_date": today.isoformat()}
    )
    assert response.status_code == 200
    assert len(response.json()["items"]) == 3

    response = await client.post("/notifications/read-all")
    assert response.status_code == 200
    assert response.json() == {"updated_count": 3, "unread_count": 0}


@pytest.mark.asyncio
async def test_notification_cursor_pagination(client, db, test_user):
    for index in range(3):
        await create_notification(
            db,
            app_user_id=test_user.id,
            event_type="test_event",
            title=f"Event {index}",
            body="Body",
            dedupe_key=f"event:{index}",
        )
    await db.commit()

    first_page = (await client.get("/notifications", params={"limit": 2})).json()
    assert len(first_page["items"]) == 2
    assert first_page["next_cursor"] is not None

    second_page = (
        await client.get(
            "/notifications",
            params={"limit": 2, "cursor": first_page["next_cursor"]},
        )
    ).json()
    assert len(second_page["items"]) == 1
    assert second_page["next_cursor"] is None


@pytest.mark.asyncio
async def test_register_update_and_unregister_push_device(client, db, test_user):
    registration = {
        "installation_id": "installation-123",
        "expo_push_token": "ExponentPushToken[test-device-token-123456789]",
        "platform": "android",
        "timezone_offset_minutes": -180,
    }
    response = await client.put("/notifications/devices", json=registration)
    assert response.status_code == 200
    assert response.json()["push_enabled"] is True

    # Idempotent registration must update, not duplicate, the installation.
    response = await client.put("/notifications/devices", json=registration)
    assert response.status_code == 200
    devices = (await db.execute(select(PushDevice).where(
        PushDevice.app_user_id == test_user.id
    ))).scalars().all()
    assert len(devices) == 1

    response = await client.patch(
        "/notifications/devices/installation-123/preferences",
        json={"push_enabled": True, "disabled_event_types": ["measurements_due"]},
    )
    assert response.status_code == 200
    assert response.json()["disabled_event_types"] == ["measurements_due"]

    response = await client.delete("/notifications/devices/installation-123")
    assert response.status_code == 204
    await db.refresh(devices[0])
    assert devices[0].push_enabled is False


@pytest.mark.asyncio
async def test_push_ticket_receipt_and_invalid_device(client, db, test_user, monkeypatch):
    device = PushDevice(
        app_user_id=test_user.id,
        installation_id="receipt-installation",
        expo_push_token="ExponentPushToken[receipt-device-token-123456]",
        platform="android",
        timezone_offset_minutes=0,
    )
    db.add(device)
    await db.flush()
    notification = await create_notification(
        db,
        app_user_id=test_user.id,
        event_type="goal_deadline",
        title="Sensitive persisted title",
        body="Sensitive persisted body",
        dedupe_key="push-receipt-test",
        payload={"route": "/progress", "goalId": 123, "weight": 80},
    )
    await db.commit()

    sent_payloads = []
    monkeypatch.setattr(push_service, "_expo_request", lambda _url, payload: (
        sent_payloads.append(payload) or {"data": {"status": "ok", "id": "ticket-1"}}
    ))
    assert await push_service.send_pending(db) == 1
    await db.commit()
    assert sent_payloads[0]["title"] == "Приближается срок цели"
    assert sent_payloads[0]["data"] == {
        "notificationId": notification.id,
        "eventType": "goal_deadline",
        "route": "/progress",
    }

    delivery = (await db.execute(select(PushDelivery).where(
        PushDelivery.notification_id == notification.id,
        PushDelivery.device_id == device.id,
    ))).scalar_one()
    delivery.sent_at = datetime.now(timezone.utc) - timedelta(minutes=16)
    await db.commit()
    monkeypatch.setattr(push_service, "_expo_request", lambda _url, _payload: {
        "data": {"ticket-1": {
            "status": "error",
            "message": "Device is not registered",
            "details": {"error": "DeviceNotRegistered"},
        }}
    })
    assert await push_service.check_receipts(db) == 1
    await db.commit()
    await db.refresh(device)
    await db.refresh(delivery)
    assert device.push_enabled is False
    assert delivery.status == "failed"


@pytest.mark.asyncio
async def test_register_device_persists_and_uses_reported_channel_ids(client, db, test_user, monkeypatch):
    registration = {
        "installation_id": "channel-installation",
        "expo_push_token": "ExponentPushToken[channel-device-token-123456]",
        "platform": "android",
        "timezone_offset_minutes": 0,
        "notification_channel_id": "eurith-actions-v2",
        "quiet_channel_id": "eurith-digest-v2",
    }
    response = await client.put("/notifications/devices", json=registration)
    assert response.status_code == 200

    device = (await db.execute(select(PushDevice).where(
        PushDevice.installation_id == "channel-installation"
    ))).scalar_one()
    assert device.notification_channel_id == "eurith-actions-v2"
    assert device.quiet_channel_id == "eurith-digest-v2"

    notification = await create_notification(
        db,
        app_user_id=test_user.id,
        event_type="goal_deadline",
        title="Sensitive persisted title",
        body="Sensitive persisted body",
        dedupe_key="push-channel-test",
        payload={"route": "/progress"},
    )
    await db.commit()

    sent_payloads = []
    monkeypatch.setattr(push_service, "_expo_request", lambda _url, payload: (
        sent_payloads.append(payload) or {"data": {"status": "ok", "id": "ticket-2"}}
    ))
    assert await push_service.send_pending(db) == 1
    await db.commit()
    assert sent_payloads[0]["channelId"] == "eurith-actions-v2"
    assert sent_payloads[0]["priority"] == "high"
    assert sent_payloads[0]["data"]["notificationId"] == notification.id
