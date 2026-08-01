"""Жизненный цикл аккаунта: заявка на удаление, отмена, физическая чистка.

Почему удаление написано вручную, а не отдано каскаду БД.

Все 23 внешних ключа на `app_users` объявлены ON DELETE CASCADE, поэтому
`DELETE FROM app_users` выглядит достаточным. Но в живой БД у
`workout_session_exercises` НЕТ внешнего ключа на `workout_sessions` (init_db
достраивает только таблицы и колонки, констрейнты — никогда). Из-за этого:

  1. удаление пользователя сносит его `workout_sessions`,
  2. строки `workout_session_exercises` остаются сиротами — то есть личные
     данные пользователя физически НЕ удалены,
  3. а следом каскад пытается удалить его кастомные упражнения и упирается в
     `workout_session_exercises.exercise_id -> exercises ON DELETE RESTRICT`,
     который держат те самые сироты, — и всё удаление падает целиком.

Поэтому дочерние строки тренировок удаляются явно и в правильном порядке,
включая сирот, ссылающихся на упражнения этого пользователя.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.models import AppUser, Exercise, WorkoutSession

logger = logging.getLogger(__name__)

# Сколько у пользователя есть времени передумать после заявки на удаление.
DELETION_GRACE_PERIOD = timedelta(days=30)


def purge_at(requested_at: datetime) -> datetime:
    """Момент, начиная с которого данные удаляются физически."""
    return requested_at + DELETION_GRACE_PERIOD


async def request_deletion(db: AsyncSession, app_user: AppUser) -> datetime:
    """Поставить аккаунт в очередь на удаление. Идемпотентно.

    Повторный вызов НЕ продлевает срок: иначе случайный двойной тап отодвигал бы
    удаление, а пользователь считал бы, что заявка подана давно.
    """
    if app_user.deletion_requested_at is None:
        app_user.deletion_requested_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(app_user)
    return app_user.deletion_requested_at


async def cancel_deletion(db: AsyncSession, app_user: AppUser) -> None:
    """Отменить заявку. Работает, пока данные ещё не вычищены физически."""
    if app_user.deletion_requested_at is not None:
        app_user.deletion_requested_at = None
        await db.commit()
        await db.refresh(app_user)


async def purge_user(db: AsyncSession, app_user_id: int) -> dict[str, int]:
    """Физически удалить пользователя и ВСЕ его данные.

    Возвращает счётчики удалённого — по ним видно, что чистка действительно
    отработала, а не молча ничего не нашла.
    """
    sessions_subq = select(WorkoutSession.id).where(
        WorkoutSession.app_user_id == app_user_id
    )
    exercises_subq = select(Exercise.id).where(Exercise.app_user_id == app_user_id)

    # Дочерние строки тренировок: и принадлежащие сессиям пользователя, и
    # сироты, ссылающиеся на его кастомные упражнения (последние иначе заблокируют
    # удаление упражнений через RESTRICT).
    target_session_exercises = text(
        """
        SELECT e.id FROM workout_session_exercises e
        WHERE e.workout_session_id IN (
                SELECT id FROM workout_sessions WHERE app_user_id = :uid
              )
           OR (
                e.exercise_id IN (SELECT id FROM exercises WHERE app_user_id = :uid)
                AND NOT EXISTS (
                    SELECT 1 FROM workout_sessions s WHERE s.id = e.workout_session_id
                )
              )
        """
    )

    se_ids = (
        await db.execute(target_session_exercises, {"uid": app_user_id})
    ).scalars().all()

    counts = {"sets": 0, "session_exercises": 0, "workouts": 0}

    if se_ids:
        result = await db.execute(
            text(
                "DELETE FROM workout_session_sets "
                "WHERE workout_session_exercise_id = ANY(:ids)"
            ),
            {"ids": list(se_ids)},
        )
        counts["sets"] = result.rowcount or 0

        result = await db.execute(
            text("DELETE FROM workout_session_exercises WHERE id = ANY(:ids)"),
            {"ids": list(se_ids)},
        )
        counts["session_exercises"] = result.rowcount or 0

    result = await db.execute(
        delete(WorkoutSession).where(WorkoutSession.app_user_id == app_user_id)
    )
    counts["workouts"] = result.rowcount or 0

    # Всё остальное (профиль, замеры, цели, сплиты, планы, надгробия синка,
    # кастомные упражнения) уносит каскад — эти FK в живой БД есть и корректны.
    await db.execute(delete(AppUser).where(AppUser.id == app_user_id))
    await db.commit()

    logger.info("[account] purged user %s: %s", app_user_id, counts)
    return counts


async def delete_firebase_user(firebase_uid: str) -> bool:
    """Удалить учётку в Firebase. Ошибку не пробрасываем.

    Данные в нашей БД уже удалены — если Firebase недоступен, повторно
    вычищать нечего, а «висящая» учётка без строки в app_users при следующем
    входе просто создастся заново как новый пустой пользователь.
    """
    try:
        from firebase_admin import auth

        auth.delete_user(firebase_uid)
        return True
    except Exception as e:  # noqa: BLE001 — намеренно широко
        logger.warning("[account] не удалось удалить Firebase-пользователя %s: %s",
                       firebase_uid, e)
        return False


async def purge_expired(db: AsyncSession, now: datetime | None = None) -> list[int]:
    """Вычистить всех, у кого grace period истёк. Возвращает id удалённых.

    Планировщика в проекте нет, поэтому вызывается на старте приложения
    (lifespan) и вручную через scripts/purge_deleted_accounts.py.
    """
    moment = now or datetime.now(timezone.utc)
    cutoff = moment - DELETION_GRACE_PERIOD

    rows = (
        await db.execute(
            select(AppUser.id, AppUser.firebase_uid).where(
                AppUser.deletion_requested_at.is_not(None),
                AppUser.deletion_requested_at <= cutoff,
            )
        )
    ).all()

    purged: list[int] = []
    for user_id, firebase_uid in rows:
        try:
            await purge_user(db, user_id)
        except Exception as e:  # noqa: BLE001
            # Один проблемный аккаунт не должен останавливать чистку остальных
            # и уж точно не должен ронять старт приложения.
            await db.rollback()
            logger.error("[account] чистка пользователя %s не удалась: %s", user_id, e)
            continue
        if firebase_uid:
            await delete_firebase_user(firebase_uid)
        purged.append(user_id)

    return purged


async def data_summary(db: AsyncSession, app_user_id: int) -> dict[str, int]:
    """Сколько чего хранится — для экрана «Мои данные».

    Считаем по тем же таблицам, что чистит purge_user: пользователь должен
    видеть ровно то, что будет удалено.
    """
    from api.services.models import (
        BodyMeasurement,
        UserGoal,
        UserSplit,
        WorkoutPlan,
        WorkoutSessionExercise,
        WorkoutSessionSet,
    )

    sessions_subq = select(WorkoutSession.id).where(
        WorkoutSession.app_user_id == app_user_id
    )
    se_subq = select(WorkoutSessionExercise.id).where(
        WorkoutSessionExercise.workout_session_id.in_(sessions_subq)
    )

    async def count(stmt) -> int:
        return int((await db.execute(stmt)).scalar_one() or 0)

    return {
        "workouts": await count(
            select(func.count()).select_from(WorkoutSession).where(
                WorkoutSession.app_user_id == app_user_id
            )
        ),
        "sets": await count(
            select(func.count()).select_from(WorkoutSessionSet).where(
                WorkoutSessionSet.workout_session_exercise_id.in_(se_subq)
            )
        ),
        "custom_exercises": await count(
            select(func.count()).select_from(Exercise).where(
                Exercise.app_user_id == app_user_id
            )
        ),
        "body_measurements": await count(
            select(func.count()).select_from(BodyMeasurement).where(
                BodyMeasurement.app_user_id == app_user_id
            )
        ),
        "goals": await count(
            select(func.count()).select_from(UserGoal).where(
                UserGoal.app_user_id == app_user_id
            )
        ),
        "splits": await count(
            select(func.count()).select_from(UserSplit).where(
                UserSplit.app_user_id == app_user_id
            )
        ),
        "plans": await count(
            select(func.count()).select_from(WorkoutPlan).where(
                WorkoutPlan.app_user_id == app_user_id
            )
        ),
    }
