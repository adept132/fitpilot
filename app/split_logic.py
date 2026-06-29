import logging
from datetime import datetime
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.models import UserSplit, WorkoutLog, WorkoutSplit, SplitDay, DayExercise

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def get_user_active_split(session: AsyncSession, user_id: int):
    from sqlalchemy.orm import selectinload

    stmt = select(UserSplit).where(
        UserSplit.user_id == user_id,
        UserSplit.is_active == True
    ).options(
        selectinload(UserSplit.split).selectinload(WorkoutSplit.days).selectinload(SplitDay.exercises).selectinload(
            DayExercise.exercise)
    )

    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_next_split_day(session: AsyncSession, user_id: int) -> SplitDay:
    user_split = await get_user_active_split(session, user_id)
    if not user_split:
        raise ValueError("У пользователя нет активного сплита")

    if not user_split.last_trained_date:
        next_day_num = user_split.current_day
    else:
        days_since_last = (datetime.now() - user_split.last_trained_date).days
        if days_since_last > 7:
            next_day_num = 1
        else:
            next_day_num = user_split.current_day + 1
            if next_day_num > user_split.split.days_in_cycle:
                next_day_num = 1

    stmt = select(SplitDay).where(
        SplitDay.split_id == user_split.split_id,
        SplitDay.day_number == next_day_num
    )
    result = await session.execute(stmt)
    day = result.scalar_one_or_none()

    if not day:
        stmt = select(SplitDay).where(
            SplitDay.split_id == user_split.split_id,
            SplitDay.day_number == 1
        )
        result = await session.execute(stmt)
        day = result.scalar_one_or_none()

    return day


async def update_split_progress(session: AsyncSession, user_id: int, completed_day: SplitDay):
    user_split = await get_user_active_split(session, user_id)
    if user_split:
        user_split.current_day = completed_day.day_number
        user_split.last_trained_date = datetime.now()
        await session.commit()


async def get_alternative_days(session: AsyncSession, split_id: int) -> list[SplitDay]:
    stmt = select(SplitDay).where(
        SplitDay.split_id == split_id,
        SplitDay.rest_day == False
    ).order_by(SplitDay.day_number)
    result = await session.execute(stmt)
    return result.scalars().all()


async def assign_split_to_user(
        session: AsyncSession,
        user_id: int,
        split_code: str,
        max_retries: int = 5
) -> "UserSplit":
    """
    Назначает сплит пользователю с retry при конфликте ID.
    """
    from datetime import datetime
    from sqlalchemy import select, and_

    # Находим сплит по коду
    stmt = select(WorkoutSplit).where(WorkoutSplit.code == split_code)
    result = await session.execute(stmt)
    split = result.scalar_one_or_none()

    if not split:
        raise ValueError(f"Сплит с кодом '{split_code}' не найден")

    # Ищем существующую активную запись
    stmt = select(UserSplit).where(
        and_(
            UserSplit.user_id == user_id,
            UserSplit.is_active == True
        )
    )
    result = await session.execute(stmt)
    user_split = result.scalar_one_or_none()

    if user_split:
        logger.info(f"Обновляем существующую запись ID={user_split.id}")

        # Обновляем существующую запись
        user_split.split_id = split.id
        user_split.selected_plans = {}
        user_split.start_date = datetime.utcnow()
        user_split.current_day = 1
        user_split.last_trained_date = None

        await session.commit()
        await session.refresh(user_split)
        return user_split

    else:
        logger.info("Создаем новую запись с retry-механизмом")

        # Пытаемся создать запись с увеличением ID при конфликте
        for attempt in range(max_retries):
            try:
                # Очищаем сессию перед каждой попыткой
                session.expunge_all()
                await session.flush()

                # Получаем максимальный существующий ID
                stmt = select(func.max(UserSplit.id))
                result = await session.execute(stmt)
                max_id = result.scalar() or 0

                # Пробуем использовать ID = max_id + 1
                next_id = max_id + 1

                # Создаем новый объект с явным ID
                new_user_split = UserSplit()
                new_user_split.id = next_id
                new_user_split.user_id = user_id
                new_user_split.split_id = split.id
                new_user_split.selected_plans = {}
                new_user_split.start_date = datetime.utcnow()
                new_user_split.is_active = True
                new_user_split.current_day = 1
                new_user_split.last_trained_date = None

                session.add(new_user_split)
                await session.commit()

                # Обновляем, чтобы получить "реальный" ID из базы
                await session.refresh(new_user_split)

                logger.info(f"Успешно создана запись user_splits с ID={new_user_split.id}")
                return new_user_split

            except IntegrityError as e:
                logger.warning(f"Попытка {attempt + 1}/{max_retries} не удалась: {e}")
                await session.rollback()

                # Очищаем identity map
                session.expunge_all()

                if attempt < max_retries - 1:
                    # Продолжаем пробовать
                    logger.info(f"Пробуем снова с увеличенным ID...")
                    continue
                else:
                    # Все попытки исчерпаны
                    logger.error(f"Не удалось создать запись после {max_retries} попыток")
                    raise

async def get_day_exercises(session: AsyncSession, day_id: int) -> list[DayExercise]:
    from sqlalchemy import select
    from api.services.models import DayExercise, Exercise

    stmt = select(
        DayExercise,
        Exercise
    ).join(
        Exercise, DayExercise.exercise_id == Exercise.id
    ).where(
        DayExercise.day_id == day_id
    ).order_by(DayExercise.order)

    result = await session.execute(stmt)

    day_exercises = []
    for day_ex, exercise in result.all():
        day_ex.exercise = exercise
        day_exercises.append(day_ex)

    return day_exercises