from datetime import datetime, timezone
from importlib import import_module
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB

from api.routers.notifications import _read
from api.schemas.notifications import NotificationRead
from api.services.models import AppNotification
from api.services.notification_service import create_notification, render_notification
from api.services.push_service import safe_push_content, send_pending


def _notification(**overrides):
    values = {
        "id": 41,
        "event_type": "goal_deadline",
        "entity_type": "goal",
        "entity_id": "17",
        "title": "Legacy title",
        "body": "Legacy body",
        "message_key": "notification.goal_deadline",
        "message_params": {"days": 3},
        "payload": {"route": "/progress"},
        "read_at": None,
        "created_at": datetime(2026, 8, 30, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalar_one(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self.value


class _SequenceSession:
    def __init__(self, *values):
        self.values = list(values)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        if not self.values:
            raise AssertionError("unexpected database query")
        return _Result(self.values.pop(0))


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        (
            "ru",
            (
                "Приближается срок цели",
                "До срока цели осталось 3 дн. Проверьте прогресс.",
            ),
        ),
        (
            "en",
            (
                "Goal deadline approaching",
                "Your goal deadline is in 3 days. Check your progress.",
            ),
        ),
    ],
)
def test_render_notification_uses_semantic_key_in_current_language(
    language, expected
):
    assert render_notification(_notification(), language) == expected


@pytest.mark.parametrize(
    "row",
    [
        _notification(message_key=None, message_params=None),
        _notification(message_key="notification.unknown", message_params={}),
    ],
)
def test_render_notification_preserves_legacy_copy_when_semantics_are_unusable(row):
    assert render_notification(row, "en") == ("Legacy title", "Legacy body")


def test_render_notification_preserves_legacy_copy_for_malformed_params():
    row = _notification(message_params=["not", "an", "object"])

    assert render_notification(row, "en") == ("Legacy title", "Legacy body")


@pytest.mark.parametrize(
    "message_params",
    [
        {"days": "PRIVATE-COPY"},
        {"days": True},
        {"days": False},
        {"days": -1},
        {"days": 3651},
        {"days": None},
        {"days": []},
        {"days": {}},
        {"days": 3, "extra": "PRIVATE-COPY"},
    ],
)
def test_render_notification_rejects_semantically_invalid_goal_params(
    message_params,
):
    row = _notification(message_params=message_params)

    assert render_notification(row, "en") == ("Legacy title", "Legacy body")


def test_notification_read_renders_copy_without_exposing_internal_semantics():
    response = _read(_notification(), "en")

    assert response.title == "Goal deadline approaching"
    assert response.body == "Your goal deadline is in 3 days. Check your progress."
    assert set(NotificationRead.model_fields) == {
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


def test_notification_model_adds_nullable_semantic_columns():
    key = AppNotification.__table__.c.message_key
    params = AppNotification.__table__.c.message_params

    assert key.nullable is True
    assert key.type.length == 128
    assert params.nullable is True
    assert isinstance(params.type, JSONB)


@pytest.mark.asyncio
async def test_create_notification_keeps_legacy_arguments_and_null_semantics():
    stored = _notification(message_key=None, message_params=None)
    session = _SequenceSession(41, stored, [])

    result = await create_notification(
        session,
        app_user_id=9,
        event_type="legacy_event",
        title="Legacy title",
        body="Legacy body",
        dedupe_key="legacy:9",
    )

    values = session.statements[0].compile(
        dialect=postgresql.dialect()
    ).params
    assert result is stored
    assert values["title"] == "Legacy title"
    assert values["body"] == "Legacy body"
    assert values["message_key"] is None
    assert values["message_params"] is None


def test_safe_push_content_is_localized_without_persisted_copy():
    notification = _notification(
        title="Sensitive persisted title",
        body="Sensitive persisted body",
    )

    assert safe_push_content(notification, "en") == (
        "Goal deadline approaching",
        "Your goal deadline is in 3 days. Check your progress.",
    )


def test_safe_push_content_never_falls_back_to_malformed_persisted_copy():
    notification = _notification(
        title="Sensitive persisted title",
        body="Sensitive persisted body",
        message_params={},
    )

    assert safe_push_content(notification, "en") == (
        "Goal deadline approaching",
        "Open Eurith to check your progress.",
    )


@pytest.mark.parametrize(
    "message_params",
    [
        {"days": "PRIVATE-COPY"},
        {"days": True},
        {"days": False},
        {"days": -1},
        {"days": 3651},
        {"days": None},
        {"days": []},
        {"days": {}},
        {"days": 3, "extra": "PRIVATE-COPY"},
    ],
)
def test_safe_push_rejects_untrusted_goal_params_without_leaking_marker(
    message_params,
):
    notification = _notification(
        title="PRIVATE-COPY stored title",
        body="PRIVATE-COPY stored body",
        message_params=message_params,
    )

    content = safe_push_content(notification, "en")

    assert content == (
        "Goal deadline approaching",
        "Open Eurith to check your progress.",
    )
    assert "PRIVATE-COPY" not in " ".join(content)


@pytest.mark.parametrize(
    "notification",
    [
        _notification(
            message_key="notification.unknown",
            message_params={},
            title="PRIVATE-COPY stored title",
            body="PRIVATE-COPY stored body",
        ),
        _notification(
            event_type="measurements_due",
            message_key="notification.measurements_due",
            message_params={"extra": "PRIVATE-COPY"},
            title="PRIVATE-COPY stored title",
            body="PRIVATE-COPY stored body",
        ),
    ],
)
def test_safe_push_uses_generic_copy_for_unknown_or_extra_semantics(notification):
    content = safe_push_content(notification, "en")

    assert "PRIVATE-COPY" not in " ".join(content)


@pytest.mark.asyncio
async def test_send_pending_renders_safe_copy_from_profile_language(monkeypatch):
    delivery = SimpleNamespace(
        status="pending",
        attempts=0,
        expo_ticket_id=None,
        sent_at=None,
        last_error=None,
        next_attempt_at=None,
    )
    device = SimpleNamespace(
        expo_push_token="ExponentPushToken[db-free-device-123456]",
        disabled_event_types=[],
        notification_channel_id=None,
        quiet_channel_id=None,
    )
    notification = _notification(app_user_id=73)
    session = _SequenceSession(
        [(delivery, device, notification)],
        [(73, {"language": "en"})],
    )
    payloads = []
    monkeypatch.setattr(
        "api.services.push_service._expo_request",
        lambda _url, payload: payloads.append(payload)
        or {"data": {"status": "ok", "id": "ticket-db-free"}},
    )

    assert await send_pending(session) == 1
    assert payloads[0]["title"] == "Goal deadline approaching"
    assert payloads[0]["body"] == (
        "Your goal deadline is in 3 days. Check your progress."
    )
    assert delivery.status == "ticketed"


@pytest.mark.asyncio
async def test_send_pending_bulk_loads_mixed_profile_languages_once(monkeypatch):
    def delivery():
        return SimpleNamespace(
            status="pending",
            attempts=0,
            expo_ticket_id=None,
            sent_at=None,
            last_error=None,
            next_attempt_at=None,
        )

    def device(token):
        return SimpleNamespace(
            expo_push_token=token,
            disabled_event_types=[],
            notification_channel_id=None,
            quiet_channel_id=None,
        )

    rows = [
        (
            delivery(),
            device("ExponentPushToken[user-1-device-a-123456]"),
            _notification(id=101, app_user_id=1),
        ),
        (
            delivery(),
            device("ExponentPushToken[user-1-device-b-123456]"),
            _notification(id=102, app_user_id=1),
        ),
        (
            delivery(),
            device("ExponentPushToken[user-2-device-a-123456]"),
            _notification(id=201, app_user_id=2),
        ),
        (
            delivery(),
            device("ExponentPushToken[user-3-device-a-123456]"),
            _notification(id=301, app_user_id=3),
        ),
        (
            delivery(),
            device("ExponentPushToken[user-4-device-a-123456]"),
            _notification(id=401, app_user_id=4),
        ),
    ]
    session = _SequenceSession(
        rows,
        [
            (1, {"language": "en"}),
            (2, {"language": "ru"}),
            (3, {"language": "de"}),
        ],
    )
    payloads = []
    monkeypatch.setattr(
        "api.services.push_service._expo_request",
        lambda _url, payload: payloads.append(payload)
        or {"data": {"status": "ok", "id": "ticket-bulk"}},
    )

    assert await send_pending(session) == 5
    assert len(session.statements) == 2
    assert [payload["title"] for payload in payloads] == [
        "Goal deadline approaching",
        "Goal deadline approaching",
        "Приближается срок цели",
        "Приближается срок цели",
        "Приближается срок цели",
    ]


def test_notification_migration_is_linear_and_reversible(monkeypatch):
    migration = import_module(
        "migrations.versions.20260830_01_notification_message_keys"
    )
    operations = []

    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda table, column: operations.append(
            ("add", table, column.name, column.nullable)
        ),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_column",
        lambda table, column: operations.append(("drop", table, column)),
    )

    assert migration.revision == "20260830_01"
    assert migration.down_revision == "20260822_02"
    migration.upgrade()
    migration.downgrade()

    assert operations == [
        ("add", "app_notifications", "message_key", True),
        ("add", "app_notifications", "message_params", True),
        ("drop", "app_notifications", "message_params"),
        ("drop", "app_notifications", "message_key"),
    ]
