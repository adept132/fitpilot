"""Expo transport for durable notifications, with privacy-safe templates."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import date, datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.models import AppNotification, AppUser, AppUserProfile, PushDelivery, PushDevice
from api.services.notification_service import materialize_domain_notifications
from app.database import SessionLocal

EXPO_SEND_URL = "https://exp.host/--/api/v2/push/send"
EXPO_RECEIPTS_URL = "https://exp.host/--/api/v2/push/getReceipts"
MAX_ATTEMPTS = 5

SAFE_TEMPLATES = {
    "periodization_proposal": ("План можно адаптировать", "Откройте Eurith, чтобы проверить предложение."),
    "training_day_without_plan": ("На сегодня нет плана", "Выберите план или создайте его в генераторе."),
    "goal_deadline": ("Приближается срок цели", "Откройте Eurith, чтобы проверить прогресс."),
    "measurements_due": ("Пора обновить замеры", "Свежие данные сделают динамику точнее."),
    "sync_conflict": ("Нужно проверить синхронизацию", "Откройте Eurith, чтобы сохранить актуальные данные."),
    "period_report": ("Отчёт готов", "Откройте Eurith, чтобы посмотреть итоги периода."),
}

SAFE_ROUTES = {
    "periodization_proposal": "/periodization",
    "training_day_without_plan": "/workout",
    "goal_deadline": "/progress",
    "measurements_due": "/progress/body-composition",
    "sync_conflict": "/home",
    "period_report": "/reports",
}

# Канал, который создавали сборки до P1-06. Устройства, которые ещё не
# сообщили свои id, обязаны получать именно его: пуш в несуществующий канал
# Android кладёт в фолбэк со своими настройками, а не в наш.
LEGACY_CHANNEL_ID = "eurith-updates"

# Информационные события идут в тихий канал и без high-priority: недельный
# отчёт не срочен, всплывать баннером наравне с конфликтом синхронизации ему
# незачем.
QUIET_EVENT_TYPES = frozenset({"period_report"})


def channel_for(device, event_type: str) -> str:
    quiet = event_type in QUIET_EVENT_TYPES
    reported = (
        getattr(device, "quiet_channel_id", None)
        if quiet
        else getattr(device, "notification_channel_id", None)
    )
    return reported or LEGACY_CHANNEL_ID


def priority_for(event_type: str) -> str:
    return "normal" if event_type in QUIET_EVENT_TYPES else "high"


def safe_push_content(event_type: str) -> tuple[str, str] | None:
    """Never forward entity names, measurements or arbitrary persisted copy."""
    return SAFE_TEMPLATES.get(event_type)


def safe_push_data(notification: AppNotification) -> dict:
    return {
        "notificationId": notification.id,
        "eventType": notification.event_type,
        **(
            {"route": SAFE_ROUTES[notification.event_type]}
            if notification.event_type in SAFE_ROUTES
            else {}
        ),
    }


def _expo_request(url: str, payload: object) -> dict:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if access_token := os.getenv("EXPO_ACCESS_TOKEN"):
        headers["Authorization"] = f"Bearer {access_token}"
    request = Request(url, json.dumps(payload).encode(), headers, method="POST")
    try:
        with urlopen(request, timeout=15) as response:  # noqa: S310
            return json.loads(response.read().decode())
    except HTTPError as exc:
        error = RuntimeError(f"Expo HTTP {exc.code}: {exc.read().decode(errors='replace')[:500]}")
        error.transient = exc.code == 429 or exc.code >= 500  # type: ignore[attr-defined]
        raise error from exc
    except (URLError, TimeoutError) as exc:
        error = RuntimeError(f"Expo network error: {exc}")
        error.transient = True  # type: ignore[attr-defined]
        raise error from exc


def _retry_at(attempts: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=min(3600, 30 * 2**attempts))


async def register_device(db: AsyncSession, *, app_user_id: int, installation_id: str,
                          expo_push_token: str, platform: str,
                          timezone_offset_minutes: int,
                          notification_channel_id: str | None = None,
                          quiet_channel_id: str | None = None) -> PushDevice:
    now = datetime.now(timezone.utc)
    installation = (await db.execute(select(PushDevice).where(
        PushDevice.app_user_id == app_user_id,
        PushDevice.installation_id == installation_id,
    ))).scalar_one_or_none()
    token_owner = (await db.execute(select(PushDevice).where(
        PushDevice.expo_push_token == expo_push_token,
    ))).scalar_one_or_none()
    if installation and token_owner and installation.id != token_owner.id:
        token_owner.push_enabled = False
        token_owner.disabled_at = now
    device = installation or token_owner
    if device is None:
        device = PushDevice(app_user_id=app_user_id, installation_id=installation_id)
        db.add(device)
    device.app_user_id = app_user_id
    device.installation_id = installation_id
    device.expo_push_token = expo_push_token
    device.platform = platform
    device.timezone_offset_minutes = timezone_offset_minutes
    device.notification_channel_id = notification_channel_id
    device.quiet_channel_id = quiet_channel_id
    device.push_enabled = True
    device.disabled_at = None
    device.last_registered_at = now
    await db.flush()
    unread = (await db.execute(select(AppNotification.id).where(
        AppNotification.app_user_id == app_user_id,
        AppNotification.read_at.is_(None),
    ))).scalars().all()
    for notification_id in unread:
        await db.execute(insert(PushDelivery).values(
            notification_id=notification_id, device_id=device.id,
        ).on_conflict_do_nothing(index_elements=[
            PushDelivery.notification_id, PushDelivery.device_id,
        ]))
    return device


async def disable_device(db: AsyncSession, app_user_id: int, installation_id: str) -> bool:
    device = (await db.execute(select(PushDevice).where(
        PushDevice.app_user_id == app_user_id,
        PushDevice.installation_id == installation_id,
    ))).scalar_one_or_none()
    if not device:
        return False
    device.push_enabled = False
    device.disabled_at = datetime.now(timezone.utc)
    return True


def _fail_or_retry(delivery: PushDelivery, message: str, transient: bool = True) -> None:
    delivery.last_error = message[:1000]
    if transient and delivery.attempts < MAX_ATTEMPTS:
        delivery.status = "pending"
        delivery.next_attempt_at = _retry_at(delivery.attempts)
    else:
        delivery.status = "failed"


async def send_pending(
    db: AsyncSession,
    limit: int = 100,
    *,
    app_user_id: int | None = None,
) -> int:
    now = datetime.now(timezone.utc)
    statement = (
        select(PushDelivery, PushDevice, AppNotification)
        .join(PushDevice, PushDevice.id == PushDelivery.device_id)
        .join(AppNotification, AppNotification.id == PushDelivery.notification_id)
        .where(PushDelivery.status == "pending", PushDelivery.next_attempt_at <= now,
               PushDevice.push_enabled.is_(True), PushDevice.disabled_at.is_(None),
               AppNotification.read_at.is_(None))
    )
    if app_user_id is not None:
        statement = statement.where(PushDevice.app_user_id == app_user_id)
    statement = (
        statement.order_by(PushDelivery.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    rows = (await db.execute(statement)).all()
    sent = 0
    for delivery, device, notification in rows:
        content = safe_push_content(notification.event_type)
        if not content or notification.event_type in (device.disabled_event_types or []):
            delivery.status = "suppressed"
            continue
        delivery.attempts += 1
        title, body = content
        message = {"to": device.expo_push_token, "title": title, "body": body,
                   "sound": "default",
                   "channelId": channel_for(device, notification.event_type),
                   "priority": priority_for(notification.event_type),
                   "data": safe_push_data(notification)}
        try:
            ticket = (await asyncio.to_thread(_expo_request, EXPO_SEND_URL, message)).get("data") or {}
            if ticket.get("status") == "ok" and ticket.get("id"):
                delivery.status, delivery.expo_ticket_id, delivery.sent_at = "ticketed", ticket["id"], now
                sent += 1
            else:
                code = (ticket.get("details") or {}).get("error")
                if code == "DeviceNotRegistered":
                    device.push_enabled, device.disabled_at = False, now
                _fail_or_retry(delivery, ticket.get("message") or str(ticket), False)
        except Exception as exc:  # noqa: BLE001
            _fail_or_retry(delivery, str(exc), bool(getattr(exc, "transient", True)))
    return sent


async def check_receipts(db: AsyncSession, limit: int = 1000) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=15)
    rows = (await db.execute(select(PushDelivery, PushDevice)
        .join(PushDevice, PushDevice.id == PushDelivery.device_id)
        .where(PushDelivery.status == "ticketed", PushDelivery.sent_at <= cutoff,
               PushDelivery.expo_ticket_id.is_not(None)).limit(limit)
        .with_for_update(skip_locked=True))).all()
    if not rows:
        return 0
    try:
        response = await asyncio.to_thread(_expo_request, EXPO_RECEIPTS_URL, {
            "ids": [delivery.expo_ticket_id for delivery, _ in rows]
        })
    except Exception:
        return 0
    checked, now = 0, datetime.now(timezone.utc)
    for delivery, device in rows:
        receipt = (response.get("data") or {}).get(delivery.expo_ticket_id)
        if not receipt:
            continue
        checked += 1
        delivery.receipt_checked_at = now
        if receipt.get("status") == "ok":
            delivery.status = "delivered"
            continue
        code = (receipt.get("details") or {}).get("error")
        delivery.last_error = (receipt.get("message") or code or "Receipt error")[:1000]
        if code == "DeviceNotRegistered":
            device.push_enabled, device.disabled_at, delivery.status = False, now, "failed"
        elif delivery.attempts < MAX_ATTEMPTS:
            delivery.status, delivery.next_attempt_at = "pending", _retry_at(delivery.attempts)
        else:
            delivery.status = "failed"
    return checked


# [КОНФИГ] Насколько давно пользователь должен был заходить, чтобы фоновая
# материализация продолжала его обслуживать. Заброшенным аккаунтам события не
# нужны, а обход всех пользователей раз в минуту на единственном процессе
# Render — бессмысленная работа.
ACTIVE_WINDOW_DAYS = 30

# [КОНФИГ] Как часто фоновая проекция трогает одного пользователя. Воркер
# просыпается раз в минуту ради отправки пушей, но пересчитывать доменное
# состояние так же часто незачем: сутки не меняются шестьдесят раз в час.
MATERIALIZE_INTERVAL_MINUTES = 60

# Память процесса, а не БД: колонка ради оптимизации не нужна. Несколько
# инстансов Render просто продросселируют независимо, а материализация
# идемпотентна — худшее последствие расхождения это лишний дешёвый проход.
_LAST_MATERIALIZED: dict[int, datetime] = {}


def local_date_for(user_timezone: str | None, device_offset_minutes: int | None,
                   now: datetime) -> date:
    """Локальная дата пользователя.

    Профиль важнее устройства: часовой пояс переживает переустановку и
    переезд между устройствами. Офсет устройства — фолбэк в знаке
    JS-getTimezoneOffset (для МСК это -180).
    """
    if user_timezone:
        try:
            return now.astimezone(ZoneInfo(user_timezone)).date()
        except (ZoneInfoNotFoundError, ValueError):
            pass
    if device_offset_minutes is not None:
        return (now - timedelta(minutes=device_offset_minutes)).date()
    return now.date()


def claim_materialization_slot(app_user_id: int, now: datetime) -> bool:
    """Забронировать право на материализацию для пользователя прямо сейчас.

    Не чистый предикат: возврат True записывает `now` как момент последней
    материализации. Вызов дважды подряд для одного пользователя молча
    съедает окно дросселирования — по имени видно, что функция что-то
    забирает, а не просто спрашивает.
    """
    previous = _LAST_MATERIALIZED.get(app_user_id)
    if previous is not None and now - previous < timedelta(minutes=MATERIALIZE_INTERVAL_MINUTES):
        return False
    _LAST_MATERIALIZED[app_user_id] = now
    return True


async def materialize_for_active_users(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Спроецировать доменное состояние всем недавно активным пользователям.

    Раньше обход шёл по PushDevice, из-за чего у пользователя без пушей не
    появлялось ни записи в центре уведомлений, ни бейджа: создание события
    было связано с каналом доставки. Это разные вещи.
    """
    moment = now or datetime.now(timezone.utc)
    cutoff = moment - timedelta(days=ACTIVE_WINDOW_DAYS)

    offsets = dict((await db.execute(
        select(PushDevice.app_user_id, PushDevice.timezone_offset_minutes)
        .where(PushDevice.disabled_at.is_(None))
        .distinct(PushDevice.app_user_id)
    )).all())

    # Часовой пояс живёт в профиле, а не на самом AppUser — профиль
    # заводится не сразу при регистрации, поэтому outerjoin: без него
    # пользователь без профиля выпадал бы из обхода целиком.
    users = (await db.execute(
        select(AppUser.id, AppUserProfile.timezone)
        .outerjoin(AppUserProfile, AppUserProfile.app_user_id == AppUser.id)
        .where(AppUser.last_seen_at >= cutoff)
    )).all()

    processed = 0
    for user_id, user_timezone in users:
        if not claim_materialization_slot(user_id, moment):
            continue
        today = local_date_for(user_timezone, offsets.get(user_id), moment)
        await materialize_domain_notifications(db, user_id, today)
        processed += 1
    return processed


async def push_worker(stop: asyncio.Event, interval_seconds: int = 60) -> None:
    while not stop.is_set():
        try:
            async with SessionLocal() as db:
                await materialize_for_active_users(db)
                await send_pending(db)
                await check_receipts(db)
                await db.commit()
        except Exception as exc:  # noqa: BLE001
            print(f"[push] worker iteration failed: {exc}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass
