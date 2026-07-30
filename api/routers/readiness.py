"""Ручки чек-ина готовности (P0-07 §12.1)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from api.services.app_user_service import get_current_app_user
from api.schemas.readiness import (
    CheckinContextResponse,
    CheckinRequest,
    CheckinResponse,
    MuscleFlagOut,
    VerdictOut,
)
from api.services.models import AppUser, AppUserProfile, UserObservation
from api.services.readiness import params, repository
from api.services.readiness.types import CheckinSignals
from api.services.readiness.verdict import build_verdict

router = APIRouter(tags=["readiness"])


async def _settings_for(session: AsyncSession, app_user_id: int) -> dict:
    row = (
        await session.execute(
            select(AppUserProfile).where(AppUserProfile.app_user_id == app_user_id)
        )
    ).scalars().first()
    return dict(row.settings) if row and row.settings else {}


@router.post("/readiness/checkin", response_model=CheckinResponse)
async def submit_checkin(
    payload: CheckinRequest,
    current_user: AppUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
) -> CheckinResponse:
    """Записать ответы и вернуть вердикт. Идемпотентно по client_uuid."""
    signals = CheckinSignals(
        sleep=payload.sleep,
        stress=payload.stress,
        soreness=payload.soreness,
        pain=payload.pain,
    )
    await repository.save_signals(
        db, current_user.id, signals, payload.source, payload.client_uuid
    )
    await db.commit()

    # Считаем вердикт из того, что реально легло в журнал: при повторной
    # отправке save_signals ничего не пишет, и брать надо сохранённое.
    stored = await repository.load_signals(db, current_user.id, payload.client_uuid)
    verdict = build_verdict(stored)
    if verdict is None:
        return CheckinResponse(verdict=None)

    return CheckinResponse(
        verdict=VerdictOut(
            level=verdict.level,
            reason_code=verdict.reason_code,
            reason_text=verdict.reason_text,
            muscle_flags=[
                MuscleFlagOut(
                    muscle=f.muscle, level=f.level, reason_code=f.reason_code
                )
                for f in verdict.muscle_flags
            ],
            pain_places=list(verdict.pain_places),
            completeness=verdict.completeness,
        )
    )


@router.get("/readiness/checkin/context", response_model=CheckinContextResponse)
async def checkin_context(
    current_user: AppUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
) -> CheckinContextResponse:
    """Мышцы дня, активная боль и предзаполнение сна/стресса.

    Мышцы дня клиент знает сам из предписанных упражнений; здесь отдаём
    запасной список по недавно нагруженным мышцам — он нужен свободной
    тренировке, где предстоящих упражнений ещё нет (спека §6.2).
    """
    settings = await _settings_for(db, current_user.id)
    enabled = repository.checkin_enabled(settings)

    active = await repository.active_pain(db, current_user.id)

    since = datetime.now(timezone.utc) - timedelta(hours=params.CHECKIN_REASK_HOURS)
    recent_rows = (
        await db.execute(
            select(UserObservation)
            .where(
                UserObservation.app_user_id == current_user.id,
                UserObservation.kind.in_([params.KIND_SLEEP, params.KIND_STRESS]),
                UserObservation.observed_at >= since,
            )
            .order_by(UserObservation.observed_at.asc())
        )
    ).scalars().all()
    recent = {row.kind: int(row.value) for row in recent_rows}

    muscles_since = datetime.now(timezone.utc) - timedelta(
        hours=params.CHECKIN_RECENT_MUSCLE_HOURS
    )
    recent_soreness = (
        await db.execute(
            select(UserObservation.subject)
            .where(
                UserObservation.app_user_id == current_user.id,
                UserObservation.kind == params.KIND_SORENESS,
                UserObservation.observed_at >= muscles_since,
            )
            .order_by(UserObservation.observed_at.desc())
        )
    ).scalars().all()

    muscles: list[str] = []
    for subject in recent_soreness:
        if subject and subject not in muscles:
            muscles.append(subject)
        if len(muscles) >= params.CHECKIN_MAX_MUSCLE_CHIPS:
            break

    return CheckinContextResponse(
        checkin_enabled=enabled,
        muscles=muscles,
        active_pain=active,
        recent_sleep=recent.get(params.KIND_SLEEP),
        recent_stress=recent.get(params.KIND_STRESS),
    )
