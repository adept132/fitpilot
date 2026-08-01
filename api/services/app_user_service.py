from datetime import datetime, timezone

from fastapi import Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
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
        # На email висит уникальный индекс uq_app_users_email_lower. Тот же адрес
        # с ДРУГИМ firebase_uid означает, что в Firebase появились две учётки на
        # один email. Молча заводить второго AppUser нельзя — история разъедется.
        #
        # В норме сюда не попадаем: при штатной настройке Firebase «один аккаунт
        # на email» второй uid не создаётся, а клиент связывает провайдеров прямо
        # при входе (см. GoogleSignInButton). Это страховка на случай, если
        # настройку сменят, — иначе пользователь молча получил бы пустой профиль
        # вместо своей истории.
        if email:
            conflict = (
                await db.execute(
                    select(AppUser).where(
                        func.lower(AppUser.email) == email.lower()
                    )
                )
            ).scalars().first()
            if conflict is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "error": "email_already_linked",
                        "message": (
                            "На этот email в Firebase заведено несколько учётных "
                            "записей. Обратитесь в поддержку — иначе история "
                            "тренировок окажется разделённой между ними."
                        ),
                    },
                )

        app_user = AppUser(
            firebase_uid=firebase_uid,
            email=email or "",
            display_name=display_name,
            email_verified=email_verified,
            is_active=True,
            last_seen_at=datetime.now(timezone.utc),
        )
        db.add(app_user)
        try:
            await db.commit()
        except IntegrityError:
            # Гонка: параллельный запрос успел создать того же пользователя.
            await db.rollback()
            app_user = (
                await db.execute(
                    select(AppUser).where(AppUser.firebase_uid == firebase_uid)
                )
            ).scalars().first()
            if app_user is None:
                raise
            return app_user
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

async def get_current_app_user_allow_pending(
    firebase_claims: dict = Depends(get_current_firebase_claims),
    db: AsyncSession = Depends(get_db),
) -> AppUser:
    """Пользователь БЕЗ проверки заявки на удаление.

    Нужен эндпоинтам /account: пока заявка активна, обычный доступ закрыт, но
    посмотреть статус и передумать пользователь обязан мочь.
    """
    return await get_or_create_app_user(db=db, firebase_claims=firebase_claims)


async def get_current_app_user(
    app_user: AppUser = Depends(get_current_app_user_allow_pending),
) -> AppUser:
    """Пользователь для обычных эндпоинтов.

    Аккаунт с поданной заявкой на удаление считается закрытым: данные ещё живы
    (идёт grace period), но пользоваться приложением уже нельзя — иначе он
    продолжал бы копить данные, которые вот-вот сотрут.
    """
    if app_user.deletion_requested_at is not None:
        from api.services.account_service import purge_at

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "account_pending_deletion",
                "purge_at": purge_at(app_user.deletion_requested_at).isoformat(),
            },
        )
    return app_user