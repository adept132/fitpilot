import difflib
from typing import Tuple, List

from rapidfuzz import fuzz
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession
from api.services.models import Exercise, WorkoutLog, User
from datetime import datetime


async def get_user(session: AsyncSession, user_id: int) -> User:
    stmt = select(User).where(User.tg_id == user_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def fuzzy_search_exercises(query: str, session: AsyncSession, limit: int = 10, min_score: int = 70):
    """Ищет упражнения по нечёткому соответствию с повышенным порогом"""
    from sqlalchemy import select

    result = await session.execute(select(Exercise))
    all_exercises = result.scalars().all()

    matches = []
    for exercise in all_exercises:
        # Основное сравнение по названию
        name_ratio = fuzz.token_set_ratio(query.lower(), exercise.name.lower())

        # Дополнительно: проверяем содержит ли запрос название упражнения
        # или название упражнения содержит запрос
        if query.lower() in exercise.name.lower() or exercise.name.lower() in query.lower():
            name_ratio = max(name_ratio, 75)  # Повышаем score для частичного совпадения

        # Для базовых упражнений (жим, присед, тяга) можно быть строже
        basic_exercises = ['жим', 'присед', 'тяга', 'подтягивания', 'отжимания']
        if any(basic in query.lower() for basic in basic_exercises):
            # Для базовых упражнений требуем большего сходства
            if name_ratio < 75:
                continue

        if name_ratio >= min_score:  # НОВЫЙ ПОРОГ: 70 вместо 45!
            matches.append({
                'exercise': exercise,
                'score': name_ratio,
                'type': 'fuzzy'
            })

    matches.sort(key=lambda x: x['score'], reverse=True)
    return matches[:limit]

async def get_last_performance(session: AsyncSession, user_id: int, exercise_id: int) -> tuple[float, int]:
    stmt = select(WorkoutLog.weight, WorkoutLog.reps).where(
        WorkoutLog.user_id == user_id,
        WorkoutLog.exercise_id == exercise_id
    ).order_by(desc(WorkoutLog.date)).limit(1)
    result = await session.execute(stmt)
    row = result.first()
    return row if row else (0.0, 0)


async def save_workout_log(session: AsyncSession, user_id: int, exercise_id: int, sets: int, reps: int,
                           weight: float, pre_assess: int, post_assess: int = None):
    log = WorkoutLog(
        user_id=user_id, exercise_id=exercise_id, sets=sets, reps=reps, weight=weight,
        date=datetime.now(), self_assessment_pre=pre_assess, self_assessment_post=post_assess
    )
    session.add(log)
    await session.commit()

async def parse_quick_input(text: str, session: AsyncSession, user_id: int) -> list[dict]:
    lines = text.strip().split('\n')
    logs = []
    for line in lines:
        parts = line.split()
        if len(parts) >= 4:
            ex_query = parts[0]
            sets_reps = parts[1].split('x')  # 3x10
            weight = float(parts[-1].rstrip('кг'))
            ex = (await fuzzy_search_exercises(session, ex_query, user_id, 1))[0]
            logs.append({'exercise_id': ex.id, 'sets': int(sets_reps[0]), 'reps': int(sets_reps[1]), 'weight': weight})
    return logs

async def get_muscle_groups(session: AsyncSession) -> list[str]:
    from sqlalchemy import distinct, select
    stmt = select(distinct(Exercise.main_muscle_group)).order_by(Exercise.main_muscle_group)
    result = await session.execute(stmt)
    return result.scalars().all()

async def get_exercises_by_muscle_group(session: AsyncSession, muscle_group: str, limit: int = 20) -> list[Exercise]:
    stmt = select(Exercise).where(Exercise.main_muscle_group.ilike(f"%{muscle_group}%")).limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()


async def find_similar_exercises(
        session: AsyncSession,
        query: str,
        user_id: int,
        limit: int = 5
) -> List[Tuple[Exercise, float]]:
    """
    Ищет упражнения по схожести названий.
    Возвращает список (упражнение, оценка_схожести).
    """
    from sqlalchemy import select
    from api.services.models import Exercise

    # Получаем все упражнения
    stmt = select(Exercise)
    result = await session.execute(stmt)
    all_exercises = result.scalars().all()

    if not all_exercises:
        return []

    # Вычисляем схожесть для каждого упражнения
    scored_exercises = []
    query_lower = query.lower()

    for exercise in all_exercises:
        # Используем SequenceMatcher для вычисления схожести
        similarity = difflib.SequenceMatcher(
            None, query_lower, exercise.name.lower()
        ).ratio()

        # Бонус за частичное совпадение в начале
        if exercise.name.lower().startswith(query_lower[:3]):
            similarity += 0.1

        # Бонус за полное вхождение
        if query_lower in exercise.name.lower():
            similarity += 0.2

        scored_exercises.append((exercise, similarity))

    # Сортируем по убыванию схожести
    scored_exercises.sort(key=lambda x: x[1], reverse=True)

    # Возвращаем топ-N результатов
    return scored_exercises[:limit]