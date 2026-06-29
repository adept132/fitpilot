import os
os.environ['PGSSLMODE'] = 'disable'

from functools import wraps
from typing import Callable
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from api.services.models import Base

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

# Проверяем, что URL содержит asyncpg
if "asyncpg" not in DATABASE_URL:
    DATABASE_URL = "postgresql+asyncpg://postgres:fitpilotbd132@localhost:5432/fitpilot_bot"

engine = create_async_engine(
    DATABASE_URL,
    echo=True,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    pool_timeout=30,
    pool_recycle=3600,
    # Для asyncpg используем server_settings, а не connect_args
)
SessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False
)

async_sessionmaker = async_sessionmaker(bind=engine, expire_on_commit=False)

def with_session(func: Callable) -> Callable:
    """
    Декоратор для автоматического управления жизненным циклом асинхронной сессии.
    Гарантирует создание, коммит и закрытие сессии для любого обработчика.
    """

    @wraps(func)
    async def wrapper(*args, **kwargs):
        session: AsyncSession = None
        try:
            # Создаём новую сессию
            session = SessionLocal()

            # "Прикрепляем" сессию к аргументам функции
            kwargs['session'] = session

            # Вызываем оригинальную функцию
            result = await func(*args, **kwargs)

            # Если в процессе были изменения - коммитим
            await session.commit()

            return result

        except Exception as e:
            # При ошибке - откатываем
            if session:
                await session.rollback()
            # Пробрасываем ошибку дальше (aiogram её перехватит)
            raise e

        finally:
            # ВСЕГДА закрываем сессию
            if session:
                await session.close()

    return wrapper


async def get_session() -> AsyncSession:
    async with SessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("✅ DB + Mappers configured (relationships OK!)")

async def close_db():
    await engine.dispose()