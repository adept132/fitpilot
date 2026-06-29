import logging
from typing import Optional, Dict, Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.advanced_workout_generator import (
    AdvancedWorkoutGenerator,
    AdvancedWorkoutParams,
)
from api.services.models import WorkoutPlan

logger = logging.getLogger(__name__)


class AdvancedTrainingGenerator:
    """Обертка для генерации (без пресетов)"""

    def __init__(self, session: AsyncSession, user_id: int):
        self.session = session
        self.user_id = user_id
        self.generator = AdvancedWorkoutGenerator(session, user_id)

    async def generate_with_default_preset(self, day_tag: str) -> Optional[Dict[str, Any]]:
        """
        Раньше: "дефолтный пресет".
        Теперь: "дефолтные параметры" (без пресетов).
        """
        params = AdvancedWorkoutParams(day_tag=day_tag)
        await self.generator.set_params(params)
        return await self._generate_and_format()

    async def generate_with_preset(self, preset_id: int) -> Optional[Dict[str, Any]]:
        """
        Пресеты отключены/удалены в новой архитектуре.
        Оставлено для совместимости вызовов.
        """
        logger.error(
            f"generate_with_preset({preset_id}) недоступен: пресеты отключены (AdvancedGeneratorPresetService удалён)."
        )
        return None

    async def generate_with_params(self, params: AdvancedWorkoutParams) -> Optional[Dict[str, Any]]:
        """Генерация с явными параметрами"""
        await self.generator.set_params(params)
        return await self._generate_and_format()

    async def _generate_and_format(self) -> Optional[Dict[str, Any]]:
        """Внутренняя генерация"""
        result = await self.generator.generate_workout()
        if not result:
            return None

        return {
            "exercises": result.get("exercises", []),
            "params": result.get("params", {}),
            "stats": result.get("stats", {}),
            "generation_method": "rules",
        }

    async def save_workout_as_plan(self, workout: Dict, name: str) -> Optional[int]:
        """Сохранение как WorkoutPlan"""
        try:
            exercises = workout.get("exercises", [])
            params = workout.get("params", {}) or {}

            plan_exercises = []
            for i, ex in enumerate(exercises, 1):
                # Генератор обычно отдаёт reps как строку "8-12"
                reps = ex.get("reps")
                if not reps:
                    # fallback на старый формат, если вдруг придёт
                    reps = f"{ex.get('reps_min', 8)}-{ex.get('reps_max', 12)}"

                plan_exercises.append({
                    "order": i,
                    "exercise_id": ex.get("exercise_id"),
                    "name": ex.get("name"),
                    "sets": ex.get("sets", 3),
                    "reps": reps,
                    "superset_id": ex.get("superset_id"),
                    "notes": ex.get("notes", ""),
                })

            # В новых params я рекомендовал хранить resolved_day_type
            day_tag = params.get("resolved_day_type") or params.get("day_tag", "custom")

            tags = {
                "day_tag": day_tag,
                "difficulty": params.get("difficulty") or params.get("level") or "intermediate",
                "source": "premium_generator",
                "auto_generated": True,
                "generation_method": "rules",
            }

            # Если новые параметры есть — сохраним (очень помогает дебажить/повторять)
            if "split_tag" in params:
                tags["split_tag"] = params.get("split_tag")
            if "workout_index" in params:
                tags["workout_index"] = params.get("workout_index")

            plan = WorkoutPlan(
                name=name,
                description=f"Авто-генерация | {day_tag}",
                tags=tags,
                exercises=plan_exercises,
                user_id=self.user_id,
                is_generated=True,
                is_public=False,
            )

            self.session.add(plan)
            await self.session.flush()
            await self.session.commit()

            logger.info(f"Сохранен план {plan.id}")
            return plan.id

        except Exception as e:
            logger.error(f"Ошибка сохранения: {e}")
            await self.session.rollback()
            return None

    async def regenerate_with_same_params(self) -> Optional[Dict[str, Any]]:
        """Перегенерация"""
        if not self.generator.params:
            logger.error("Нет параметров")
            return None
        return await self._generate_and_format()