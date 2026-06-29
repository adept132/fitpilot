from datetime import datetime, timezone

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_firebase_claims, get_db
from api.services.models import AppUser


async def get_or_create_app_user(
        db: AsyncSession,
        firebase_claims: dict,
) -> AppUser:
    firebase_uid = firebase_claims["uid"]
    email = firebase_claims.get("email")
    email_verified = bool(firebase_claims.get("email_verified", False))
    display_name = firebase_claims.get("name")

    result = await db.execute(
        select(AppUser).where(AppUser.firebase_uid == firebase_uid)
    )
    app_user = result.scalar_one_or_none()

    # 1. Сценарий: НОВЫЙ ПОЛЬЗОВАТЕЛЬ
    if app_user is None:
        app_user = AppUser(
            firebase_uid=firebase_uid,
            email=email or "",
            display_name=display_name,
            email_verified=email_verified,
            is_active=True,
            last_seen_at=datetime.now(timezone.utc),
        )
        db.add(app_user)
        await db.commit()
        await db.refresh(app_user)
        return app_user

    # 2. Сценарий: СУЩЕСТВУЮЩИЙ ПОЛЬЗОВАТЕЛЬ

    # Обновляем email или имя ТОЛЬКО если они в нашей базе почему-то пустые
    # (например, Firebase не отдал их при первой регистрации, а отдал сейчас)
    if email and not app_user.email:
        app_user.email = email

    if display_name and not app_user.display_name:
        app_user.display_name = display_name

    # Статус верификации обновляем всегда (вдруг юзер только что подтвердил почту)
    app_user.email_verified = email_verified

    # Обновляем время последней активности
    app_user.last_seen_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(app_user)
    return app_user

async def get_current_app_user(
    firebase_claims: dict = Depends(get_current_firebase_claims),
    db: AsyncSession = Depends(get_db),
) -> AppUser:
    return await get_or_create_app_user(db=db, firebase_claims=firebase_claims)