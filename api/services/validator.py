from typing import List, Dict, Optional
from uuid import UUID
from fastapi import status
from api.errors import LocalizedHTTPException
from pydantic import BaseModel


# Вспомогательные схемы для валидации входящих данных
class PlanExerciseInput(BaseModel):
    exercise_id: int
    fatigue_tier: int
    primary_muscle: str
    secondary_muscle: Optional[str] = None
    target_sets: int
    superset_group_id: Optional[UUID] = None


class AntiSuicideValidator:
    EFFORT_ORDER = ['deload', 'easy', 'medium', 'prefailure', 'failure']
    CAP_MAPPING = {
        "beginner": 6,
        "intermediate": 8,
        "advanced": 10,
    }

    @classmethod
    def workout_set_cap(cls, experience_level: str) -> int:
        return cls.CAP_MAPPING.get(experience_level, 6)

    @classmethod
    def validate_mesocycle_sequence(cls, effort_tiers: List[str]) -> bool:
        """
        Проверяет мезоцикл на плавность прогрессии и безопасность выхода из отказа.
        """
        if not effort_tiers:
            raise LocalizedHTTPException(
                status.HTTP_400_BAD_REQUEST, "validator.mesocycle_empty"
            )

        deload_count = effort_tiers.count('deload')

        # Правило 1: Лимит без отдыха (не более 5 фаз подряд без разгрузки)
        # Ищем максимальный отрезок без 'deload'
        current_streak = 0
        max_streak = 0
        for tier in effort_tiers:
            if tier != 'deload':
                current_streak += 1
                max_streak = max(max_streak, current_streak)
            else:
                current_streak = 0

        if max_streak > 5:
            raise LocalizedHTTPException(
                status.HTTP_400_BAD_REQUEST, "validator.too_many_load_phases"
            )

        # Проверка по цепочке шагов
        for i in range(len(effort_tiers)):
            current_tier = effort_tiers[i]

            # Правило 3: Выход из отказа. После 'failure' обязан идти 'deload'
            if current_tier == 'failure' and i < len(effort_tiers) - 1:
                next_tier = effort_tiers[i + 1]
                if next_tier != 'deload':
                    raise LocalizedHTTPException(
                        status.HTTP_400_BAD_REQUEST,
                        "validator.failure_requires_deload",
                    )

            # Правило 2: Плавность прогрессии вверх (не более чем на 2 шага)
            # После разгрузки (deload, индекс 0) допускается любой скачок вверх
            if i > 0:
                prev_tier = effort_tiers[i - 1]
                prev_idx = cls.EFFORT_ORDER.index(prev_tier)
                curr_idx = cls.EFFORT_ORDER.index(current_tier)

                if prev_idx != 0 and curr_idx > prev_idx and curr_idx - prev_idx > 2:
                    raise LocalizedHTTPException(
                        status.HTTP_400_BAD_REQUEST,
                        "validator.intensity_jump",
                        {"from_tier": prev_tier, "to_tier": current_tier},
                    )

        return True

    @classmethod
    def validate_workout_plan(cls, experience_level: str, exercises: List[PlanExerciseInput]) -> bool:
        """
        Проверяет тренировочный план на жесткие лимиты объемов и синергию в суперсетах.
        """
        # 1. Жесткие ограничения на количество подходов (Hard Caps) за сессию
        user_cap = cls.workout_set_cap(experience_level)

        muscle_volumes: Dict[str, int] = {}
        supersets: Dict[UUID, List[PlanExerciseInput]] = {}

        for ex in exercises:
            # Считаем прямой (Primary) объем на мышечную группу
            muscle_volumes[ex.primary_muscle] = muscle_volumes.get(ex.primary_muscle, 0) + ex.target_sets

            # Проверяем лимит
            if muscle_volumes[ex.primary_muscle] > user_cap:
                raise LocalizedHTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "validator.volume_hard_cap",
                    {
                        "muscle": ex.primary_muscle,
                        "experience_level": experience_level,
                        "max_sets": user_cap,
                    },
                )

            # Собираем группы суперсетов для последующей проверки
            if ex.superset_group_id:
                if ex.superset_group_id not in supersets:
                    supersets[ex.superset_group_id] = []
                supersets[ex.superset_group_id].append(ex)

        # 2. Валидация суперсетов (Запрет на тяжелую базу и синергисты)
        for group_id, group_exercises in supersets.items():
            if len(group_exercises) < 2:
                raise LocalizedHTTPException(
                    status.HTTP_400_BAD_REQUEST, "validator.superset_too_small"
                )
            if len(group_exercises) > 3:
                raise LocalizedHTTPException(
                    status.HTTP_400_BAD_REQUEST, "validator.superset_too_large"
                )

            for i in range(len(group_exercises)):
                for j in range(i + 1, len(group_exercises)):
                    ex1 = group_exercises[i]
                    ex2 = group_exercises[j]

                    # Правило двойной базы: запрет Tier 1 + Tier 1
                    if ex1.fatigue_tier == 1 and ex2.fatigue_tier == 1:
                        raise LocalizedHTTPException(
                            status.HTTP_400_BAD_REQUEST,
                            "validator.superset_two_tier_one",
                        )

                    # Правило синергистов: проверяем пересечения целевых мышц
                    # Набор мышц первого упражнения
                    ex1_muscles = {ex1.primary_muscle}
                    if ex1.secondary_muscle:
                        ex1_muscles.add(ex1.secondary_muscle)

                    # Набор мышц второго упражнения
                    ex2_muscles = {ex2.primary_muscle}
                    if ex2.secondary_muscle:
                        ex2_muscles.add(ex2.secondary_muscle)

                    # Если есть пересечение — значит, они синергисты/дублируют друг друга
                    if ex1_muscles.intersection(ex2_muscles):
                        raise LocalizedHTTPException(
                            status.HTTP_400_BAD_REQUEST,
                            "validator.superset_synergy_conflict",
                        )

        return True
