"""Server-backed notification centre endpoints."""

from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from api.errors import LocalizedHTTPException
from api.i18n import SupportedLanguage, resolve_language
from api.schemas.notifications import (
    MarkAllReadResponse,
    NotificationBadge,
    NotificationList,
    NotificationRead,
    PushDeviceRead,
    PushDeviceRegistration,
    PushPreferencesUpdate,
)
from api.services.app_user_service import get_current_app_user
from api.services.models import AppNotification, AppUser, PushDevice
from api.services.notification_service import (
    mark_all_read,
    materialize_domain_notifications,
    render_notification,
    unread_count,
)
from api.services.push_service import disable_device, register_device

router = APIRouter(prefix="/notifications", tags=["Notifications"])


def _device_read(device: PushDevice) -> PushDeviceRead:
    return PushDeviceRead(
        installation_id=device.installation_id,
        platform=device.platform,
        push_enabled=device.push_enabled,
        disabled_event_types=device.disabled_event_types or [],
        last_registered_at=device.last_registered_at,
    )


@router.put("/devices", response_model=PushDeviceRead)
async def register_push_device(
    payload: PushDeviceRegistration,
    current_user: AppUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
) -> PushDeviceRead:
    if not (payload.expo_push_token.startswith("ExponentPushToken[") or
            payload.expo_push_token.startswith("ExpoPushToken[")):
        raise LocalizedHTTPException(422, "notification.invalid_expo_token")
    device = await register_device(db, app_user_id=current_user.id, **payload.model_dump())
    await db.commit()
    await db.refresh(device)
    return _device_read(device)


@router.delete("/devices/{installation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unregister_push_device(
    installation_id: str,
    current_user: AppUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await disable_device(db, current_user.id, installation_id)
    await db.commit()


@router.patch("/devices/{installation_id}/preferences", response_model=PushDeviceRead)
async def update_push_preferences(
    installation_id: str,
    payload: PushPreferencesUpdate,
    current_user: AppUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
) -> PushDeviceRead:
    device = (await db.execute(select(PushDevice).where(
        PushDevice.app_user_id == current_user.id,
        PushDevice.installation_id == installation_id,
    ))).scalar_one_or_none()
    if device is None:
        raise LocalizedHTTPException(404, "notification.device_not_registered")
    device.push_enabled = payload.push_enabled
    device.disabled_event_types = sorted(set(payload.disabled_event_types))
    device.disabled_at = None if payload.push_enabled else datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(device)
    return _device_read(device)


def _request_language(request: Request) -> SupportedLanguage:
    return getattr(
        request.state,
        "language",
        resolve_language(request.headers.get("Accept-Language"), None),
    )


def _read(row: AppNotification, language: SupportedLanguage) -> NotificationRead:
    title, body = render_notification(row, language)
    return NotificationRead(
        id=row.id,
        event_type=row.event_type,
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        title=title,
        body=body,
        payload=row.payload or {},
        read_at=row.read_at,
        created_at=row.created_at,
    )


@router.get("", response_model=NotificationList)
async def list_notifications(
    request: Request,
    cursor: int | None = Query(default=None, ge=1),
    limit: int = Query(default=30, ge=1, le=100),
    unread_only: bool = False,
    local_date: date | None = None,
    current_user: AppUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
) -> NotificationList:
    await materialize_domain_notifications(
        db, current_user.id, local_date or date.today()
    )
    await db.commit()

    statement = select(AppNotification).where(
        AppNotification.app_user_id == current_user.id
    )
    if cursor is not None:
        statement = statement.where(AppNotification.id < cursor)
    if unread_only:
        statement = statement.where(AppNotification.read_at.is_(None))
    rows = (
        await db.execute(
            statement.order_by(AppNotification.id.desc()).limit(limit + 1)
        )
    ).scalars().all()
    has_more = len(rows) > limit
    page = rows[:limit]
    return NotificationList(
        items=[_read(row, _request_language(request)) for row in page],
        unread_count=await unread_count(db, current_user.id),
        next_cursor=page[-1].id if has_more and page else None,
    )


@router.get("/badge", response_model=NotificationBadge)
async def get_badge(
    local_date: date | None = None,
    current_user: AppUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
) -> NotificationBadge:
    await materialize_domain_notifications(
        db, current_user.id, local_date or date.today()
    )
    await db.commit()
    return NotificationBadge(unread_count=await unread_count(db, current_user.id))


@router.post("/{notification_id}/read", response_model=NotificationRead)
async def mark_notification_read(
    request: Request,
    notification_id: int,
    current_user: AppUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
) -> NotificationRead:
    notification = (
        await db.execute(
            select(AppNotification).where(
                AppNotification.id == notification_id,
                AppNotification.app_user_id == current_user.id,
            )
        )
    ).scalar_one_or_none()
    if notification is None:
        raise LocalizedHTTPException(
            status.HTTP_404_NOT_FOUND, "notification.not_found"
        )
    if notification.read_at is None:
        notification.read_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(notification)
    return _read(notification, _request_language(request))


@router.post("/read-all", response_model=MarkAllReadResponse)
async def mark_everything_read(
    current_user: AppUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
) -> MarkAllReadResponse:
    updated = await mark_all_read(db, current_user.id)
    await db.commit()
    return MarkAllReadResponse(updated_count=updated)
