"""API schemas for the server-backed notification centre."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class NotificationRead(BaseModel):
    id: int
    event_type: str
    entity_type: str | None = None
    entity_id: str | None = None
    title: str
    body: str
    payload: dict[str, Any] = Field(default_factory=dict)
    read_at: datetime | None = None
    created_at: datetime


class NotificationList(BaseModel):
    items: list[NotificationRead]
    unread_count: int
    next_cursor: int | None = None


class NotificationBadge(BaseModel):
    unread_count: int


class MarkAllReadResponse(BaseModel):
    updated_count: int
    unread_count: int = 0


class PushDeviceRegistration(BaseModel):
    installation_id: str = Field(min_length=8, max_length=64)
    expo_push_token: str = Field(min_length=20, max_length=255)
    platform: str = Field(pattern="^(android|ios)$")
    timezone_offset_minutes: int = Field(default=0, ge=-840, le=840)
    notification_channel_id: str | None = Field(default=None, max_length=64)
    quiet_channel_id: str | None = Field(default=None, max_length=64)


class PushPreferencesUpdate(BaseModel):
    push_enabled: bool
    disabled_event_types: list[str] = Field(default_factory=list, max_length=32)


class PushDeviceRead(BaseModel):
    installation_id: str
    platform: str
    push_enabled: bool
    disabled_event_types: list[str] = Field(default_factory=list)
    last_registered_at: datetime
