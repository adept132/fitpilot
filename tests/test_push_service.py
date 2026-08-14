from types import SimpleNamespace

from api.services.push_service import (
    QUIET_EVENT_TYPES,
    channel_for,
    priority_for,
    safe_push_content,
    safe_push_data,
)


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


def test_channel_for_prefers_device_reported_ids():
    device = SimpleNamespace(
        notification_channel_id="eurith-actions-v2",
        quiet_channel_id="eurith-digest-v2",
    )
    assert channel_for(device, "periodization_proposal") == "eurith-actions-v2"
    assert channel_for(device, "period_report") == "eurith-digest-v2"


def test_channel_for_falls_back_to_legacy_channel_on_old_builds():
    """Сборка до P1-06 ничего про каналы не сообщает — шлём старый id,
    иначе Android положит уведомление в фолбэк со своими настройками."""
    device = SimpleNamespace(notification_channel_id=None, quiet_channel_id=None)
    assert channel_for(device, "periodization_proposal") == "eurith-updates"
    assert channel_for(device, "period_report") == "eurith-updates"


def test_priority_is_high_only_for_actionable_events():
    assert priority_for("periodization_proposal") == "high"
    assert priority_for("period_report") == "normal"
    assert "period_report" in QUIET_EVENT_TYPES
