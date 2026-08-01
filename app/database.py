import os
import uuid

from functools import wraps
from typing import Callable
from dotenv import load_dotenv
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from api.services.models import Base

load_dotenv()

LOCAL_DATABASE_URL = "postgresql+asyncpg://postgres:fitpilotbd132@localhost:5432/fitpilot_bot"

DATABASE_URL = os.getenv("DATABASE_URL") or ""
# Пустой/чужой драйвер (например синхронный psycopg2-URL) -> локальная разработка.
if "asyncpg" not in DATABASE_URL:
    DATABASE_URL = LOCAL_DATABASE_URL

_url = make_url(DATABASE_URL)
_host = _url.host or ""
_is_local = _host in ("localhost", "127.0.0.1", "::1")

# PGSSLMODE=disable нужен ТОЛЬКО локальному постгресу без TLS. Раньше он ставился
# безусловно на уровне модуля, и это мина под любой облачной БД: asyncpg читает
# эту переменную как фолбэк, когда ssl не задан явно (connect_utils: `if ssl is
# None: ssl = os.getenv('PGSSLMODE')`). Neon без TLS соединение не примет.
if _is_local:
    os.environ["PGSSLMODE"] = "disable"

# Neon-пулер (хост с суффиксом `-pooler`) — это PgBouncer в transaction mode, а он
# несовместим с кэшем prepared statements asyncpg: подготовленный в одной транзакции
# statement в следующей уже на другом серверном соединении. Отключаем кэш и делаем
# имена уникальными. Своё пуление у нас и так есть (pool_size ниже).
_connect_args: dict = {}
if "-pooler" in _host:
    _connect_args = {
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
        "prepared_statement_name_func": lambda: f"__asyncpg_{uuid.uuid4()}__",
    }

# echo=True печатает каждый SQL с параметрами — в проде это шум в логах Render
# и утечка пользовательских данных. Включается явно через SQL_ECHO=1.
_echo = os.getenv("SQL_ECHO", "").lower() in ("1", "true", "yes")

engine = create_async_engine(
    DATABASE_URL,
    echo=_echo,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    pool_timeout=30,
    pool_recycle=3600,
    connect_args=_connect_args,
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

def _collect_missing_columns(sync_conn):
    """Возвращает [(table_name, Column), ...] — столбцы моделей, которых нет в БД
    (для уже существующих таблиц; новые таблицы create_all создаёт целиком)."""
    from sqlalchemy import inspect

    inspector = inspect(sync_conn)
    existing_tables = set(inspector.get_table_names())

    missing = []
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        existing_cols = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name not in existing_cols:
                missing.append((table.name, column))
    return missing


# Уникальность client_uuid — единственная НАСТОЯЩАЯ защита от дублей при
# offline-синхронизации: без неё роутер /sync/workouts делает read-then-insert,
# и два конкурентных пуша одного снимка вставят две строки.
# Индексы партиальные, потому что client_uuid nullable и у исторических строк NULL.
_SYNC_INDEXES = [
    (
        "uq_workout_sessions_user_client_uuid",
        'CREATE UNIQUE INDEX IF NOT EXISTS uq_workout_sessions_user_client_uuid '
        'ON workout_sessions (app_user_id, client_uuid) WHERE client_uuid IS NOT NULL',
    ),
    (
        "uq_wse_session_client_uuid",
        'CREATE UNIQUE INDEX IF NOT EXISTS uq_wse_session_client_uuid '
        'ON workout_session_exercises (workout_session_id, client_uuid) WHERE client_uuid IS NOT NULL',
    ),
    (
        "uq_wss_exercise_client_uuid",
        'CREATE UNIQUE INDEX IF NOT EXISTS uq_wss_exercise_client_uuid '
        'ON workout_session_sets (workout_session_exercise_id, client_uuid) WHERE client_uuid IS NOT NULL',
    ),
]


async def _ensure_sync_indexes():
    """Досоздать уникальные индексы синхронизации.

    create_all их не добавляет к УЖЕ существующим таблицам, а ALTER-фаза выше
    занимается только колонками — поэтому отдельный явный шаг.
    """
    from sqlalchemy import text

    for name, ddl in _SYNC_INDEXES:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(ddl))
        except Exception as e:
            # Самая вероятная причина — в таблице уже есть дубли по client_uuid.
            print(
                f"[init_db] ВНИМАНИЕ: не удалось создать {name}: {e}\n"
                f"[init_db] Защита от дублей синхронизации НЕ активна. "
                f"Почистите дубли скриптом scripts/dedupe_sync_uuids.py и перезапустите."
            )


async def _ensure_sequences():
    """Подтянуть счётчики автоинкремента до реального max(id).

    Сид-скрипты (seed_splits, build_exercise_index) вставляют строки с ЯВНЫМИ id,
    а это не двигает sequence. В результате счётчик отстаёт, и следующие вставки
    падают с duplicate key, пока не догонят занятый диапазон. Реально
    наблюдалось на exercises: seq=121 при max(id)=173 — то есть создание
    пользовательских упражнений было сломано на 52 вставки вперёд.

    Идемпотентно: сдвигаем только вперёд, никогда назад.
    """
    from sqlalchemy import text

    # Только пользовательские таблицы, у которых ЕСТЬ колонка id:
    # pg_get_serial_sequence на таблице без такой колонки бросает исключение,
    # а не возвращает NULL.
    tables_query = text(
        """
        SELECT t.table_name
        FROM information_schema.tables t
        JOIN information_schema.columns c
          ON c.table_schema = t.table_schema
         AND c.table_name = t.table_name
         AND c.column_name = 'id'
        WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE'
        """
    )

    async with engine.begin() as conn:
        table_names = (await conn.execute(tables_query)).scalars().all()

    for table_name in table_names:
        try:
            # Каждая таблица в своей транзакции: сбой на одной не должен
            # отменять уже применённые setval для остальных.
            async with engine.begin() as conn:
                seq = (
                    await conn.execute(
                        text("SELECT pg_get_serial_sequence(:t, 'id')"),
                        {"t": table_name},
                    )
                ).scalar_one()
                if not seq:
                    continue  # id есть, но без sequence (напр. UUID или ручной ключ)
                await conn.execute(
                    text(
                        f"SELECT setval('{seq}', GREATEST("
                        f"  (SELECT COALESCE(MAX(id), 0) FROM {table_name}),"
                        f"  (SELECT last_value FROM {seq})"
                        f"))"
                    )
                )
        except Exception as e:
            print(f"[init_db] sequence sync skipped for {table_name}: {e}")


async def init_db():
    from sqlalchemy import text
    from sqlalchemy.schema import CreateColumn

    # 1. Создаём отсутствующие таблицы.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # 2. Достраиваем недостающие столбцы в существующих таблицах (лёгкая авто-миграция).
    async with engine.connect() as conn:
        missing = await conn.run_sync(_collect_missing_columns)

    for table_name, column in missing:
        # Компилируем определение столбца силами SQLAlchemy — тип, DEFAULT и NOT NULL
        # рендерятся ровно так же, как в create_all (корректно для любых типов/дефолтов).
        col_ddl = str(CreateColumn(column).compile(dialect=engine.dialect))
        ddl = f'ALTER TABLE "{table_name}" ADD COLUMN {col_ddl}'
        try:
            # Каждый ALTER в своей транзакции — сбой одной колонки (напр. NOT NULL
            # без дефолта на непустой таблице) не роняет старт и остальные миграции.
            async with engine.begin() as conn:
                await conn.execute(text(ddl))
            print(f"[init_db] ALTER: added {table_name}.{column.name}")
        except Exception as e:
            print(f"[init_db] ALTER skipped for {table_name}.{column.name}: {e}")

    # 3. Уникальные индексы для идемпотентной синхронизации.
    await _ensure_sync_indexes()

    # 4. Счётчики автоинкремента (сид-скрипты вставляют явные id и не двигают их).
    await _ensure_sequences()

    print(
        "[init_db] DB schema synced "
        "(create tables + add missing columns + sync indexes + sequences)"
    )

async def close_db():
    await engine.dispose()