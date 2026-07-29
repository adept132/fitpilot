"""Фикстуры для интеграционных тестов API (идут против реальной Postgres).

Модели завязаны на Postgres-специфику (JSONB, UUID, BigInteger), поэтому SQLite
не подходит. Firebase-авторизация подменяется через dependency_overrides:
создаётся временный AppUser, который удаляется после теста (каскад уносит все
созданные тренировки/замеры/упражнения).

Важно: движок SQLAlchemy общий (app.database), а pytest-asyncio даёт КАЖДОМУ
тесту свой event loop — поэтому пул соединений сбрасывается после каждого теста,
иначе получаем "Event loop is closed".
"""
from __future__ import annotations

import os
import uuid

import pytest_asyncio
from fastapi import Depends
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

# TEST_DATABASE_URL имеет приоритет; иначе используем обычный DATABASE_URL.
if os.getenv("TEST_DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]

from api.main import app  # noqa: E402
from api.services.app_user_service import (  # noqa: E402
    get_current_app_user,
    get_current_app_user_allow_pending,
)
from api.services.models import (  # noqa: E402
    AppUser,
    Exercise,
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)
from api.deps import get_db  # noqa: E402
from app.database import SessionLocal, engine, init_db  # noqa: E402


@pytest_asyncio.fixture(autouse=True)
async def _db_lifecycle():
    """Схема перед тестом; сброс пула после — соединения привязаны к loop теста."""
    await init_db()
    yield
    await engine.dispose()


@pytest_asyncio.fixture
async def test_user():
    """Временный пользователь. Отдаём ОТСОЕДИНЁННЫЙ объект: привязанный к
    закрытой сессии ORM-инстанс при чтении атрибутов уходит в lazy-load и падает
    с MissingGreenlet."""
    marker = uuid.uuid4().hex[:12]
    firebase_uid = f"test-{marker}"
    email = f"test-{marker}@example.com"

    async with SessionLocal() as session:
        user = AppUser(
            firebase_uid=firebase_uid,
            email=email,
            display_name="Test User",
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        user_id = user.id

    detached = AppUser(
        id=user_id,
        firebase_uid=firebase_uid,
        email=email,
        display_name="Test User",
    )

    yield detached

    # Порядок важен: workout_session_exercises.exercise_id → exercises имеет
    # ON DELETE RESTRICT, поэтому сначала сносим тренировки, потом упражнения.
    # Чистим строго в порядке зависимостей. В реальной БД у
    # workout_session_exercises НЕТ внешнего ключа на workout_sessions (есть
    # только exercise_id → exercises ON DELETE RESTRICT), поэтому bulk-удаление
    # тренировок не уносит дочерние строки — удаляем их явно.
    async with SessionLocal() as session:
        workout_ids = (
            await session.execute(
                select(WorkoutSession.id).where(
                    WorkoutSession.app_user_id == user_id
                )
            )
        ).scalars().all()

        if workout_ids:
            se_ids = (
                await session.execute(
                    select(WorkoutSessionExercise.id).where(
                        WorkoutSessionExercise.workout_session_id.in_(workout_ids)
                    )
                )
            ).scalars().all()

            if se_ids:
                await session.execute(
                    delete(WorkoutSessionSet).where(
                        WorkoutSessionSet.workout_session_exercise_id.in_(se_ids)
                    )
                )
                await session.execute(
                    delete(WorkoutSessionExercise).where(
                        WorkoutSessionExercise.id.in_(se_ids)
                    )
                )

            await session.execute(
                delete(WorkoutSession).where(WorkoutSession.id.in_(workout_ids))
            )

        await session.execute(
            delete(Exercise).where(Exercise.app_user_id == user_id)
        )
        await session.execute(delete(AppUser).where(AppUser.id == user_id))
        await session.commit()


@pytest_asyncio.fixture
async def client(test_user: AppUser):
    """HTTP-клиент с подменённой Firebase-авторизацией на test_user.

    Подменяем обе зависимости. Отсоединённый test_user не отражает изменения,
    сделанные эндпоинтами (напр. заявку на удаление), поэтому перечитываем
    пользователя из БД — иначе guard на deletion_requested_at не сработает и
    тесты жизненного цикла аккаунта проверяли бы не то.
    """

    async def _current_user_allow_pending(
        db: AsyncSession = Depends(get_db),
    ) -> AppUser:
        # Именно из СЕССИИ ЗАПРОСА: эндпоинты /account мутируют пользователя и
        # коммитят через тот же db. Отсоединённый объект молча не сохранился бы.
        fresh = await db.get(AppUser, test_user.id)
        return fresh if fresh is not None else test_user

    async def _current_user(
        app_user: AppUser = Depends(_current_user_allow_pending),
    ) -> AppUser:
        return await get_current_app_user(app_user=app_user)

    app.dependency_overrides[get_current_app_user_allow_pending] = (
        _current_user_allow_pending
    )
    app.dependency_overrides[get_current_app_user] = _current_user
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def db():
    async with SessionLocal() as session:
        yield session


# --- Фикстуры для тестов репозитория движка прогрессии (P0-06) ---
#
# ВАЖНО (не упрощать обратно): db_session/app_user НЕ являются алиасами
# db/test_user, и это намеренно, а не забытый рефакторинг.
#
# Раньше здесь так и было — db_session=alias(db), app_user=alias(test_user) —
# и это давало взаимную блокировку с зависанием теста навсегда:
#   1. test_user создаёт пользователя в СВОЁМ соединении и коммитит.
#   2. exercise/session_exercise пишут строки через db_session (ДРУГОЕ
#      соединение) и делают только flush() — транзакция висит открытой и
#      держит блокировки на строки app_users/exercises.
#   3. Teardown test_user открывает ТРЕТЬЕ соединение и делает
#      DELETE FROM app_users/exercises — упирается в блокировки шага 2.
#   4. Порядок разрушения фикстур в pytest обратен порядку создания:
#      test_user создан раньше db_session/app_user → его teardown стартует
#      РАНЬШЕ, чем закроется (и откатится) сессия db_session. Освободить
#      блокировку нечем — DELETE ждёт вечно, весь процесс виснет.
#
# Исправление — не подпирать порядок разрушения, а убрать саму возможность
# конфликта двух соединений: пользователь для тестов прогрессии создаётся
# ВНУТРИ db_session (тот же flush, та же транзакция, то же соединение, что
# и exercise/session_exercise), commit нигде не вызывается, и при закрытии
# сессии всё просто откатывается. Второго/третьего соединения, которое
# могло бы ждать чужую блокировку, не существует в принципе.
#
# Если когда-нибудь понадобится дёрнуть в этих тестах HTTP-клиента (fixture
# `client`, который тянет за собой test_user) — используйте его из ДРУГОГО
# теста, не смешивая с db_session/app_user в одном тесте: иначе блокировка
# вернётся.


@pytest_asyncio.fixture
async def db_session():
    """Отдельная сессия (отдельное соединение) для тестов репозитория
    прогрессии. НЕ алиас `db` — см. пояснение выше о взаимной блокировке.

    Ничего не коммитим: явный rollback на выходе (плюс неявный при закрытии
    сессии) гарантирует, что все строки, созданные тестом (включая
    app_user), исчезают вместе с транзакцией — отдельный teardown не нужен.
    """
    async with SessionLocal() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def app_user(db_session: AsyncSession):
    """Тестовый пользователь для тестов репозитория прогрессии.

    НЕ алиас `test_user` — создаётся той же сессией db_session (только
    flush, без commit), чтобы весь граф тестовых данных жил в одной
    транзакции одного соединения. См. пояснение выше о взаимной блокировке.
    """
    marker = uuid.uuid4().hex[:12]
    user = AppUser(
        firebase_uid=f"test-progression-{marker}",
        email=f"test-progression-{marker}@example.com",
        display_name="Test Progression User",
    )
    db_session.add(user)
    await db_session.flush()
    yield user


@pytest_asyncio.fixture
async def exercise(db_session: AsyncSession, app_user: AppUser):
    """Упражнение, привязанное к тестовому пользователю.

    app_user создан в этой же транзакции db_session (см. выше) — отдельной
    очистки не нужно, всё уйдёт откатом транзакции при закрытии сессии.
    """
    marker = uuid.uuid4().hex[:8]
    ex = Exercise(
        name=f"Тестовое упражнение прогрессии {marker}",
        category="base",
        main_muscle_group="chest",
        difficulty="beginner",
        equipment_needed=[],
        # ck_exercises_source_user: app_user_id NOT NULL требует source != 'default'.
        source="custom",
        app_user_id=app_user.id,
    )
    db_session.add(ex)
    await db_session.flush()
    yield ex


@pytest_asyncio.fixture
async def session_exercise(
    db_session: AsyncSession, app_user: AppUser, exercise: Exercise
):
    """Минимальная активная тренировочная сессия с одним упражнением —
    ровно то, что нужно тестам write-once persist_prescription."""
    workout = WorkoutSession(
        app_user_id=app_user.id,
        source="free",
        status="active",
    )
    db_session.add(workout)
    await db_session.flush()

    se = WorkoutSessionExercise(
        workout_session_id=workout.id,
        exercise_id=exercise.id,
        order_index=0,
    )
    db_session.add(se)
    await db_session.flush()
    yield se
