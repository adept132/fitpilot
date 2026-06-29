from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.services.exercise_utils import get_base_exercise_query
from api.services.models import Exercise, WorkoutSession, WorkoutSessionExercise
from api.services.exercise_matcher import ExerciseMatcher


def normalize_exercise_type(value: Optional[str]) -> Optional[str]:
    if not value:
        return None

    value = value.strip().lower()

    mapping = {
        "base": "base",
        "базовое": "base",
        "compound": "base",
        "isolation": "isolation",
        "isolating": "isolation",
        "изолирующее": "isolation",
    }

    return mapping.get(value, value)

def normalize_equipment_values(equipment_needed) -> set[str]:
    if equipment_needed is None:
        return set()

    if isinstance(equipment_needed, str):
        raw_values = [equipment_needed]
    else:
        raw_values = list(equipment_needed)

    normalized = set()

    for item in raw_values:
        if not item:
            continue

        value = str(item).strip().lower().replace("ё", "е")

        if value:
            normalized.add(value)

    return normalized


def classify_equipment(equipment_needed) -> str:
    normalized = normalize_equipment_values(equipment_needed)

    if not normalized:
        return "unknown"

    machine_values = {
        "тренажер",
        "тренажеры",
        "машина смита",
        "смит",
    }

    free_values = {
        "штанга",
        "гантели",
        "гантель",
        "перекладина",
        "скамья",
    }

    if normalized.intersection(machine_values):
        return "machine"

    if normalized.intersection(free_values):
        return "free"

    return "unknown"


def matches_equipment_filter(equipment_needed, equipment_filter: str | None) -> bool:
    if not equipment_filter:
        return True

    equipment_type = classify_equipment(equipment_needed)

    if equipment_filter == "machine":
        return equipment_type == "machine"

    if equipment_filter == "free":
        return equipment_type == "free"

    return True


class ExerciseSearchService:
    @staticmethod
    async def _get_recent_exercise_ids(
        session: AsyncSession,
        app_user_id: int,
    ) -> set[int]:
        last_month = datetime.now(timezone.utc) - timedelta(days=30)

        stmt = (
            select(WorkoutSessionExercise.exercise_id)
            .join(
                WorkoutSession,
                WorkoutSession.id == WorkoutSessionExercise.workout_session_id,
            )
            .where(
                WorkoutSession.app_user_id == app_user_id,
                WorkoutSession.started_at >= last_month,
            )
            .distinct()
        )

        result = await session.execute(stmt)
        return {row[0] for row in result.all()}

    @staticmethod
    async def search_exercises(
        session: AsyncSession,
        user_id: int,
        q: Optional[str] = None,
        muscle_group: Optional[str] = None,
        type: Optional[str] = None,
        equipment: Optional[str] = None,
        recent: bool = False,
        source: Optional[str] = None,
    ):
        normalized_type = normalize_exercise_type(type)
        recent_ids: set[int] | None = None

        if recent:
            recent_ids = await ExerciseSearchService._get_recent_exercise_ids(
                session=session,
                app_user_id=user_id,
            )

            if not recent_ids:
                return []

        if q:
            _, matches = await ExerciseMatcher.find_or_create_exercise(
                session=session,
                user_id=user_id,
                exercise_name=q,
            )

            filtered = matches

            if muscle_group:
                filtered = [
                    item for item in filtered
                    if item.get("main_muscle_group") == muscle_group
                ]

            if normalized_type:
                filtered = [
                    item for item in filtered
                    if normalize_exercise_type(item.get("category")) == normalized_type
                ]

            if equipment:
                filtered = [
                    item for item in filtered
                    if matches_equipment_filter(item.get("equipment_needed"), equipment)
                ]

            if recent and recent_ids is not None:
                filtered = [
                    item for item in filtered
                    if item.get("id") in recent_ids
                ]

            if source:
                # В ExerciseMatcher мы возвращали 'user' для кастомных,
                # или 'custom' если ты уже поправил. Проверяй оба варианта:
                filtered = [
                    item for item in filtered
                    if item.get("source") in (source, "user")
                ]

            return filtered

        stmt = (
            get_base_exercise_query(user_id)
            .where(Exercise.main_muscle_group.is_not(None))
            .order_by(Exercise.main_muscle_group, Exercise.name)
        )

        result = await session.execute(stmt)
        items = result.scalars().all()

        filtered_items = items

        if muscle_group:
            filtered_items = [
                item for item in filtered_items
                if item.main_muscle_group == muscle_group
            ]

        if normalized_type:
            filtered_items = [
                item for item in filtered_items
                if normalize_exercise_type(item.category) == normalized_type
            ]

        if equipment:
            print("EQUIPMENT FILTER:", equipment)
            print(
                "EQUIPMENT SAMPLE:",
                [
                    {
                        "id": item.id,
                        "name": item.name,
                        "equipment_needed": item.equipment_needed,
                    }
                    for item in filtered_items[:15]
                ],
            )

            filtered_items = [
                item for item in filtered_items
                if matches_equipment_filter(item.equipment_needed, equipment)
            ]

        if recent and recent_ids is not None:
            filtered_items = [
                item for item in filtered_items
                if item.id in recent_ids
            ]

        if source:
            filtered_items = [
                item for item in filtered_items
                if item.source == source
            ]

        return filtered_items

    @staticmethod
    async def get_muscle_groups(
        session: AsyncSession,
        user_id: int,
        type: Optional[str] = None,
        equipment: Optional[str] = None,
        recent: bool = False,
    ):
        normalized_type = normalize_exercise_type(type)
        recent_ids: set[int] | None = None

        if recent:
            recent_ids = await ExerciseSearchService._get_recent_exercise_ids(
                session=session,
                app_user_id=user_id,
            )

            if not recent_ids:
                return []

        stmt = (
            get_base_exercise_query(user_id)
            .where(Exercise.main_muscle_group.is_not(None))
            .order_by(Exercise.name)
            .limit(300)
        )

        result = await session.execute(stmt)
        items = result.scalars().all()

        filtered_items = items

        if normalized_type:
            filtered_items = [
                item for item in filtered_items
                if normalize_exercise_type(item.category) == normalized_type
            ]

        if equipment:
            print("SEARCH EQUIPMENT FILTER:", equipment)
            print(
                "SEARCH EQUIPMENT SAMPLE:",
                [
                    {
                        "id": item.id,
                        "name": item.name,
                        "equipment_needed": item.equipment_needed,
                        "classified": classify_equipment(item.equipment_needed),
                    }
                    for item in filtered_items[:20]
                ],
            )

            filtered_items = [
                item for item in filtered_items
                if matches_equipment_filter(item.equipment_needed, equipment)
            ]

            print("SEARCH FILTERED COUNT:", len(filtered_items))

        if recent and recent_ids is not None:
            filtered_items = [
                item for item in filtered_items
                if item.id in recent_ids
            ]

        counts: dict[str, int] = {}

        for item in filtered_items:
            group = item.main_muscle_group
            if not group:
                continue
            counts[group] = counts.get(group, 0) + 1

        return [
            {"name": name, "count": count}
            for name, count in sorted(counts.items(), key=lambda pair: pair[0])
        ]

    @staticmethod
    async def get_last_workout_for_context(
        session: AsyncSession,
        user_id: int,
        current_workout: WorkoutSession,
    ):
        stmt = select(WorkoutSession).where(
            WorkoutSession.app_user_id == user_id,
            WorkoutSession.status == "finished",
        )

        if current_workout.source == "split_day" and current_workout.split_day_id:
            stmt = stmt.where(
                WorkoutSession.split_day_id == current_workout.split_day_id
            )

        stmt = stmt.order_by(WorkoutSession.finished_at.desc()).limit(1)

        result = await session.execute(stmt)
        last_workout = result.scalar_one_or_none()

        if not last_workout:
            return []

        exercises_stmt = (
            select(WorkoutSessionExercise)
            .options(selectinload(WorkoutSessionExercise.exercise))
            .where(WorkoutSessionExercise.workout_session_id == last_workout.id)
            .order_by(WorkoutSessionExercise.order_index)
        )

        exercises_result = await session.execute(exercises_stmt)
        items = exercises_result.scalars().all()

        return [
            {
                "exercise_id": item.exercise_id,
                "name": item.exercise.name if item.exercise else "Без названия",
                "main_muscle_group": item.exercise.main_muscle_group if item.exercise else None,
                "category": item.exercise.category if item.exercise else None,
            }
            for item in items
        ]

    @staticmethod
    async def get_exercise_analytics_history(
            session: AsyncSession,
            user_id: int,
            exercise_id: int
    ) -> Optional[dict]:
        # Безопасная проверка: юзер не сможет просмотреть аналитику чужого кастомного упражнения
        exercise_stmt = get_base_exercise_query(user_id).where(Exercise.id == exercise_id)
        exercise_res = await session.execute(exercise_stmt)
        exercise = exercise_res.scalar_one_or_none()

        if not exercise:
            return None

        # 2. Вытягиваем все сессии этого упражнения со всеми выполненными подходами
        stmt = (
            select(WorkoutSessionExercise)
            .join(WorkoutSession, WorkoutSession.id == WorkoutSessionExercise.workout_session_id)
            .options(selectinload(WorkoutSessionExercise.sets))
            .where(
                WorkoutSession.app_user_id == user_id,
                WorkoutSession.status == "finished",
                WorkoutSessionExercise.exercise_id == exercise_id
            )
            .order_by(WorkoutSession.finished_at.asc())
        )

        result = await session.execute(stmt)
        session_exercises = result.scalars().all()

        # Группируем данные по датам (на случай, если было 2 тренировки в день или для четкого таймлайна)
        daily_data = defaultdict(list)

        # Нам также понадобятся даты завершения тренировок
        # (предполагаем, что у WorkoutSessionExercise есть доступ к дате или через lazy load/join)
        # Для скорости вытащим даты напрямую из связанных сессий
        for se in session_exercises:
            # Получаем дату из finished_at сессии
            stmt_session = select(WorkoutSession.finished_at).where(WorkoutSession.id == se.workout_session_id)
            res_session = await session.execute(stmt_session)  # Исправлено здесь
            finished_at = res_session.scalar()

            if not finished_at:
                continue

            workout_date = finished_at.date()

            # Собираем только успешно выполненные рабочие подходы
            completed_sets = [s for s in se.sets if s.is_completed and s.weight is not None and s.reps is not None]
            if completed_sets:
                daily_data[workout_date].extend(completed_sets)

        history_points = []

        # 3. Рассчитываем математику для каждого дня
        for workout_date, sets in sorted(daily_data.items()):
            day_volume = 0.0
            max_e1rm = 0.0
            best_set = None

            day_sets = []  # <-- СОБИРАЕМ ВСЕ ПОДХОДЫ ЗДЕСЬ

            for s in sets:
                weight = float(s.weight)
                set_volume = weight * s.reps
                day_volume += set_volume

                # Сохраняем подход для истории объема
                day_sets.append({
                    "weight": int(weight) if weight.is_integer() else round(weight, 1),
                    "reps": s.reps
                })

                current_e1rm = weight * (1.0 + s.reps / 30.0) if s.reps > 0 else weight

                if current_e1rm > max_e1rm:
                    max_e1rm = current_e1rm
                    best_set = s

            if best_set:
                best_weight = float(best_set.weight)
                formatted_weight = int(best_weight) if best_weight.is_integer() else round(best_weight, 1)
                best_set_str = f"{formatted_weight} кг х {best_set.reps}"
            else:
                best_set_str = "0 кг х 0"

            history_points.append({
                "date": str(workout_date),  # <-- ИСПРАВЛЕНИЕ ЗДЕСЬ (оборачиваем в str)
                "e1rm": round(max_e1rm, 1),
                "volume": round(day_volume, 1),
                "best_set_str": best_set_str,
                "sets": day_sets
            })
        return {
            "exercise_id": exercise.id,
            "name": exercise.name,
            "category": exercise.category or "base",
            "main_muscle_group": exercise.main_muscle_group or "Не указано",
            "history": history_points
        }