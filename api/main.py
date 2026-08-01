from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import init_db
from api.deps import get_db
from api.routers.splits import router as splits_router
from api.routers.exercises import router as exercises_router
from api.routers.auth import router as auth_router
from api.routers.workout_center import router as workout_center_router
from api.routers.workouts import router as workouts_router
from api.routers.progress import router as progress_router
from api.routers.profile import router as profile_router
from api.routers.workout_supersets import router as workout_supersets_router
from api.routers.mesocycles import router as mesocycles_router
from api.routers.microcycles import router as microcycles_router
from api.routers.plans import router as plans_router
from api.routers.calendar import router as calendar_router
from api.routers.data_io import router as data_io_router
from api.routers.goals import router as goals_router
from api.routers.body import router as body_router
from api.routers.sync import router as sync_router
from api.routers.account import router as account_router
from api.routers.readiness import router as readiness_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Синхронизация схемы БД на старте: создаём отсутствующие таблицы
    # и достраиваем недостающие столбцы.
    await init_db()
    await _purge_expired_accounts()
    yield


async def _purge_expired_accounts():
    """Дочистить аккаунты, у которых истёк grace period.

    Планировщика в проекте нет, поэтому чистка привязана к старту приложения
    (и дублируется скриптом scripts/purge_deleted_accounts.py для регулярного
    запуска). Ошибка здесь не должна ронять старт.
    """
    try:
        from api.services.account_service import purge_expired
        from app.database import SessionLocal

        async with SessionLocal() as session:
            purged = await purge_expired(session)
        if purged:
            print(f"[account] удалено аккаунтов по истечении срока: {len(purged)}")
    except Exception as e:  # noqa: BLE001
        print(f"[account] чистка удалённых аккаунтов не выполнена: {e}")


app = FastAPI(title="Eurith API", lifespan=lifespan)

# Статика изображений техники (free-exercise-db) — отдаём с бэкенда, чтобы в рантайме
# не ходить в GitHub. Файлы кладёт scripts/ingest_exercise_images.py в media/exercises/.
MEDIA_DIR = Path(__file__).resolve().parent.parent / "media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=str(MEDIA_DIR)), name="media")

app.include_router(exercises_router, prefix="/exercises", tags=["exercises"])
app.include_router(auth_router)
app.include_router(workout_center_router)
app.include_router(workouts_router)
app.include_router(splits_router)
app.include_router(mesocycles_router)
app.include_router(microcycles_router)
app.include_router(plans_router)
app.include_router(progress_router)
app.include_router(exercises_router)
app.include_router(profile_router)
app.include_router(calendar_router)
app.include_router(workout_supersets_router)
app.include_router(data_io_router)
app.include_router(goals_router)
app.include_router(body_router)
app.include_router(sync_router)
app.include_router(account_router)
app.include_router(readiness_router)

@app.api_route("/health", methods=["GET", "HEAD"])
async def health_check(db: AsyncSession = Depends(get_db)):
    try:
        # Дергаем базу
        await db.execute(text("SELECT 1"))
        return {"status": "ok", "database": "connected"}
    except Exception as e:
        # Если база лежит, возвращаем 503 Service Unavailable
        raise HTTPException(status_code=503, detail="Database connection failed")