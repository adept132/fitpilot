from types import SimpleNamespace

from api.services.push_service import safe_push_content, safe_push_data


def test_push_content_is_allowlisted_and_never_uses_persisted_copy():
    assert safe_push_content("measurements_due") == (
        "Пора обновить замеры",
        "Свежие данные сделают динамику точнее.",
    )
    assert safe_push_content("unknown_or_user_generated") is None


def test_push_data_contains_only_technical_id_event_and_safe_route():
    notification = SimpleNamespace(
        id=42,
        event_type="goal_deadline",
        title="User supplied title",
        body="Weight 80 kg",
        payload={"route": "/progress", "goalId": 99, "weight": 80},
    )
    assert safe_push_data(notification) == {
        "notificationId": 42,
        "eventType": "goal_deadline",
        "route": "/progress",
    }


def test_push_data_does_not_trust_persisted_route():
    notification = SimpleNamespace(
        id=43,
        event_type="measurements_due",
        payload={"route": "/not-a-real-screen", "secret": "must-not-leak"},
    )
    assert safe_push_data(notification) == {
        "notificationId": 43,
        "eventType": "measurements_due",
        "route": "/progress/body-composition",
    }
