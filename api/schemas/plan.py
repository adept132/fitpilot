from datetime import date

from pydantic import BaseModel, Field
from typing import List, Dict, Optional
from uuid import UUID


# Описание упражнения внутри плана
class PlanExerciseCreate(BaseModel):
    exercise_id: int = Field(..., description="ID упражнения из глобальной базы")
    order_index: int = Field(..., description="Порядковый номер в тренировке")
    target_sets: int = Field(default=1, ge=1, description="Количество подходов")

    # Если заполнено одним и тем же UUID для нескольких упражнений - они объединятся в суперсет
    superset_group_id: Optional[UUID] = None

    # --- ДАННЫЕ ДЛЯ ВАЛИДАТОРА ---
    # Важный нюанс: в реальном production-коде бэкенд не должен доверять фронтенду эти поля.
    # Бэкенд должен сам сходить в БД (в таблицу exercises), взять exercise_id и достать оттуда
    # fatigue_tier и primary_muscle, чтобы пользователь не смог "хакнуть" валидатор, подменив данные.
    # Но для удобства архитектуры (или если фронтенд собирает объект целиком) мы кладем их сюда:
    fatigue_tier: int = Field(..., ge=1, le=3, description="1 - База, 2 - Тренажеры, 3 - Изоляция")
    primary_muscle: str = Field(..., example="chest")
    secondary_muscle: Optional[str] = Field(None, example="triceps")


# Главная схема создания плана
class WorkoutPlanCreate(BaseModel):
    name: str = Field(..., max_length=255, example="Push - Adaptive - Hard")

    # Теги для каскадной фильтрации
    day_tag: str = Field(..., example="push")
    micro_tag: str = Field(..., description="'easy', 'medium', 'hard', 'adaptive'")
    meso_tag: str = Field(..., description="'deload', 'easy', 'medium', 'prefailure', 'failure', 'adaptive'")

    # Массив упражнений, который валидатор будет проверять на перекрытие мышц в суперсетах и хард-капы
    exercises: List[PlanExerciseCreate]

class MuscleTarget(BaseModel):
    target_sets: int
    max_session_cap: int

class VolumeTargetsResponse(BaseModel):
    day_tag: str
    split_duration: int
    targets: Dict[str, MuscleTarget]

class PlanApplyRequest(BaseModel):
    apply_mode: str
    target_date: date
    day_tag: str
    micro_tag: str

class UpdatePlanContextPayload(BaseModel):
    plan_id: Optional[int] = None

class WorkoutCenterPlanRead(BaseModel):
    id: int
    name: str