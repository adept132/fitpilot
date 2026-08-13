"""Persistence and domain-event projection for the notification centre."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.models import (
    AppNotification,
    BodyMeasurement,
    PeriodizationProposal,
    PushDelivery,
    PushDevice,
    UserAnthropometry,
    UserCalendarDay,
    UserGoal,
)


async def create_notification(
    db: AsyncSession,
    *,
    app_user_id: int,
    event_type: str,
    title: str,
    body: str,
    dedupe_key: str,
    entity_type: str | None = None,
    entity_id: str | int | None = None,
    payload: dict[str, Any] | None = None,
) -> AppNotification:
    """Create exactly one durable event for a user and semantic dedupe key."""

    values = {
        "app_user_id": app_user_id,
        "event_type": event_type,
        "entity_type": entity_type,
        "entity_id": None if entity_id is None else str(entity_id),
        "title": title,
        "body": body,
        "payload": payload or {},
        "dedupe_key": dedupe_key,
    }
    statement = (
        insert(AppNotification)
        .values(**values)
        .on_conflict_do_nothing(
            index_elements=[AppNotification.app_user_id, AppNotification.dedupe_key]
        )
        .returning(AppNotification.id)
    )
    notification_id = (await db.execute(statement)).scalar_one_or_none()
    if notification_id is None:
        notification_id = (
            await db.execute(
                select(AppNotification.id).where(
                    AppNotification.app_user_id == app_user_id,
                    AppNotification.dedupe_key == dedupe_key,
                )
            )
        ).scalar_one()
    notification = (
        await db.execute(
            select(AppNotification).where(AppNotification.id == notification_id)
        )
    ).scalar_one()
    active_device_ids = (
        await db.execute(
            select(PushDevice.id).where(
                PushDevice.app_user_id == app_user_id,
                PushDevice.push_enabled.is_(True),
                PushDevice.disabled_at.is_(None),
            )
        )
    ).scalars().all()
    for device_id in active_device_ids:
        await db.execute(
            insert(PushDelivery)
            .values(notification_id=notification.id, device_id=device_id)
            .on_conflict_do_nothing(
                index_elements=[PushDelivery.notification_id, PushDelivery.device_id]
            )
        )
    return notification


async def materialize_domain_notifications(
    db: AsyncSession,
    app_user_id: int,
    today: date,
) -> None:
    """Project current actionable domain state into durable, deduplicated events."""

    proposals = (
        await db.execute(
            select(PeriodizationProposal).where(
                PeriodizationProposal.app_user_id == app_user_id,
                PeriodizationProposal.status == "pending",
            )
        )
    ).scalars().all()
    for proposal in proposals:
        await create_notification(
            db,
            app_user_id=app_user_id,
            event_type="periodization_proposal",
            entity_type="periodization_proposal",
            entity_id=proposal.id,
            title="План можно адаптировать",
            body="Появилось предложение по адаптации тренировочного плана.",
            payload={
                "route": "/periodization",
                "proposalId": proposal.id,
                "proposalKind": proposal.kind,
                "blockId": proposal.block_id,
            },
            dedupe_key=f"periodization_proposal:{proposal.id}",
        )

    unplanned_day = (
        await db.execute(
            select(UserCalendarDay).where(
                UserCalendarDay.app_user_id == app_user_id,
                UserCalendarDay.target_date == today,
                UserCalendarDay.is_rest_day.is_(False),
                UserCalendarDay.is_blackout.is_(False),
                UserCalendarDay.plan_id.is_(None),
                UserCalendarDay.status == "planned",
            )
        )
    ).scalars().first()
    if unplanned_day is not None:
        await create_notification(
            db,
            app_user_id=app_user_id,
            event_type="training_day_without_plan",
            entity_type="calendar_day",
            entity_id=unplanned_day.id,
            title="На сегодня нет плана",
            body="Выберите план из библиотеки или создайте его в генераторе.",
            payload={
                "route": "/plan-generator",
                "calendarDayId": unplanned_day.id,
                "targetDate": today.isoformat(),
            },
            dedupe_key=f"training_day_without_plan:{today.isoformat()}",
        )

    deadline_limit = today + timedelta(days=3)
    goals = (
        await db.execute(
            select(UserGoal).where(
                UserGoal.app_user_id == app_user_id,
                UserGoal.is_completed.is_(False),
                UserGoal.deadline.is_not(None),
                UserGoal.deadline >= today,
                UserGoal.deadline <= deadline_limit,
            )
        )
    ).scalars().all()
    for goal in goals:
        days_left = (goal.deadline - today).days
        body = (
            "Срок цели наступает сегодня. Проверьте прогресс."
            if days_left == 0
            else f"До срока цели осталось {days_left} дн. Проверьте прогресс."
        )
        await create_notification(
            db,
            app_user_id=app_user_id,
            event_type="goal_deadline",
            entity_type="goal",
            entity_id=goal.id,
            title="Приближается срок цели",
            body=body,
            payload={"route": "/progress", "goalId": goal.id},
            dedupe_key=f"goal_deadline:{goal.id}:{goal.deadline.isoformat()}",
        )

    last_body_measurement = (
        await db.execute(
            select(func.max(BodyMeasurement.recorded_at)).where(
                BodyMeasurement.app_user_id == app_user_id
            )
        )
    ).scalar_one_or_none()
    last_anthropometry = (
        await db.execute(
            select(func.max(UserAnthropometry.recorded_at)).where(
                UserAnthropometry.app_user_id == app_user_id
            )
        )
    ).scalar_one_or_none()
    timestamps = [value for value in (last_body_measurement, last_anthropometry) if value]
    latest_measurement = max(timestamps) if timestamps else None
    if latest_measurement is not None:
        if latest_measurement.tzinfo is None:
            latest_measurement = latest_measurement.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - latest_measurement
        if age >= timedelta(days=14):
            source_date = latest_measurement.date().isoformat()
            await create_notification(
                db,
                app_user_id=app_user_id,
                event_type="measurements_due",
                entity_type="body_measurements",
                title="Пора обновить замеры",
                body="Свежие замеры сделают динамику и прогнозы точнее.",
                payload={"route": "/progress/body-composition"},
                dedupe_key=f"measurements_due:{source_date}",
            )


async def unread_count(db: AsyncSession, app_user_id: int) -> int:
    return int(
        (
            await db.execute(
                select(func.count(AppNotification.id)).where(
                    AppNotification.app_user_id == app_user_id,
                    AppNotification.read_at.is_(None),
                )
            )
        ).scalar_one()
    )


async def mark_all_read(db: AsyncSession, app_user_id: int) -> int:
    result = await db.execute(
        update(AppNotification)
        .where(
            AppNotification.app_user_id == app_user_id,
            AppNotification.read_at.is_(None),
        )
        .values(read_at=datetime.now(timezone.utc))
    )
    return int(result.rowcount or 0)
