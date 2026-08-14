from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from api.services.push_service import (
    MATERIALIZE_INTERVAL_MINUTES,
    QUIET_EVENT_TYPES,
    _LAST_MATERIALIZED,
    channel_for,
    local_date_for,
    priority_for,
    safe_push_content,
    safe_push_data,
    should_materialize,
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


def test_local_date_prefers_profile_timezone():
    """23:30 UTC 14 августа в Москве — уже 15-е."""
    now = datetime(2026, 8, 14, 23, 30, tzinfo=timezone.utc)
    assert local_date_for("Europe/Moscow", None, now).isoformat() == "2026-08-15"


def test_local_date_falls_back_to_device_offset():
    """getTimezoneOffset() на клиенте отдаёт МИНУС смещение (МСК = -180)."""
    now = datetime(2026, 8, 14, 23, 30, tzinfo=timezone.utc)
    assert local_date_for(None, -180, now).isoformat() == "2026-08-15"


def test_local_date_defaults_to_utc_when_nothing_is_known():
    now = datetime(2026, 8, 14, 23, 30, tzinfo=timezone.utc)
    assert local_date_for(None, None, now).isoformat() == "2026-08-14"


def test_first_visit_always_materializes():
    _LAST_MATERIALIZED.clear()
    assert should_materialize(1, datetime(2026, 8, 17, 9, tzinfo=timezone.utc)) is True


def test_repeat_within_interval_is_skipped():
    _LAST_MATERIALIZED.clear()
    now = datetime(2026, 8, 17, 9, tzinfo=timezone.utc)
    should_materialize(1, now)

    assert should_materialize(1, now + timedelta(minutes=5)) is False


def test_next_interval_materializes_again():
    _LAST_MATERIALIZED.clear()
    now = datetime(2026, 8, 17, 9, tzinfo=timezone.utc)
    should_materialize(1, now)

    later = now + timedelta(minutes=MATERIALIZE_INTERVAL_MINUTES + 1)
    assert should_materialize(1, later) is True


def test_throttle_is_per_user():
    _LAST_MATERIALIZED.clear()
    now = datetime(2026, 8, 17, 9, tzinfo=timezone.utc)
    should_materialize(1, now)

    assert should_materialize(2, now) is True
