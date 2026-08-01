"""Жизненный цикл аккаунта: статус, сводка данных, удаление и его отмена."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from api.services.account_service import (
    DELETION_GRACE_PERIOD,
    cancel_deletion,
    data_summary,
    purge_at,
    request_deletion,
)
from api.services.app_user_service import get_current_app_user_allow_pending
from api.services.models import AppUser

router = APIRouter(prefix="/account", tags=["account"])


class AccountStatusResponse(BaseModel):
    email: str
    display_name: str | None = None
    email_verified: bool = False
    created_at: datetime | None = None
    deletion_requested_at: datetime | None = None
    # Когда данные будут удалены физически (None, если заявки нет).
    purge_at: datetime | None = None
    grace_period_days: int = DELETION_GRACE_PERIOD.days


class DataSummaryResponse(BaseModel):
    """Что именно хранится на сервере — ровно то, что удалит purge_user."""
    workouts: int = 0
    sets: int = 0
    custom_exercises: int = 0
    body_measurements: int = 0
    goals: int = 0
    splits: int = 0
    plans: int = 0


def _status(app_user: AppUser) -> AccountStatusResponse:
    requested = app_user.deletion_requested_at
    return AccountStatusResponse(
        email=app_user.email,
        display_name=app_user.display_name,
        email_verified=bool(app_user.email_verified),
        created_at=app_user.created_at,
        deletion_requested_at=requested,
        purge_at=purge_at(requested) if requested else None,
    )


@router.get("", response_model=AccountStatusResponse)
async def get_account(
    app_user: AppUser = Depends(get_current_app_user_allow_pending),
):
    """Статус аккаунта. Доступен и при поданной заявке на удаление —
    иначе пользователь не смог бы увидеть, до какого числа может передумать."""
    return _status(app_user)


@router.get("/data-summary", response_model=DataSummaryResponse)
async def get_data_summary(
    db: AsyncSession = Depends(get_db),
    app_user: AppUser = Depends(get_current_app_user_allow_pending),
):
    return DataSummaryResponse(**await data_summary(db, app_user.id))


@router.post("/deletion", response_model=AccountStatusResponse)
async def request_account_deletion(
    db: AsyncSession = Depends(get_db),
    app_user: AppUser = Depends(get_current_app_user_allow_pending),
):
    """Подать заявку на удаление. Доступ к приложению закрывается сразу,
    данные удаляются физически через grace period. Повтор не продлевает срок."""
    await request_deletion(db, app_user)
    return _status(app_user)


@router.delete("/deletion", response_model=AccountStatusResponse)
async def cancel_account_deletion(
    db: AsyncSession = Depends(get_db),
    app_user: AppUser = Depends(get_current_app_user_allow_pending),
):
    """Передумал — заявка снимается, доступ возвращается, данные на месте."""
    await cancel_deletion(db, app_user)
    return _status(app_user)
