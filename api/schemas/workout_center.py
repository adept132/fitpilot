from datetime import datetime
from typing import Optional, List
from uuid import UUID

from pydantic import BaseModel

from api.schemas.plan import WorkoutCenterPlanRead


class WorkoutCenterSplitRead(BaseModel):
    id: UUID  # <-- БЫЛО int
    name: str


class WorkoutCenterSplitDayRead(BaseModel):
    id: UUID  # <-- БЫЛО int
    name: str
    day_number: int
    primary_muscles: list[str]
    secondary_muscles: list[str]


class WorkoutCenterActiveWorkoutRead(BaseModel):
    id: int
    started_at: datetime
    source: str

class WorkoutCenterMicrocycleRead(BaseModel):
    id: int
    name: str

# --- ДОБАВЛЯЕМ НОВУЮ СХЕМУ ДЛЯ ССЫЛКИ НА МЕЗОЦИКЛ ---
class WorkoutCenterMesocycleRead(BaseModel):
    id: UUID  # У мезоциклов теперь UUID
    name: str
    phases_in_cycle: int

    class Config:
        from_attributes = True


# --- ОБНОВЛЯЕМ ОСНОВНУЮ СХЕМУ КОНТЕКСТА ---
class WorkoutCenterContextRead(BaseModel):
    selected_split: WorkoutCenterSplitRead | None
    available_splits: list[WorkoutCenterSplitRead]
    selected_split_day: WorkoutCenterSplitDayRead | None
    available_split_days: list[WorkoutCenterSplitDayRead]

    # ИСПРАВЛЕНО: Заменяем dict на строгие схемы и добавляем доступные мезоциклы
    selected_plan: Optional[WorkoutCenterPlanRead] = None
    available_plans: list[WorkoutCenterPlanRead] = []
    selected_periodization: WorkoutCenterMesocycleRead | None  # Текущий активный мезоцикл
    selected_periodization_week: int | None
    available_mesocycles: list[WorkoutCenterMesocycleRead] = []  # Список всех для выбора
    selected_periodization_phase_name: str | None = None
    selected_microcycle: Optional[WorkoutCenterMicrocycleRead] = None
    available_microcycles: List[WorkoutCenterMicrocycleRead] = []
    volume_targets: dict | None = None

    active_workout: WorkoutCenterActiveWorkoutRead | None


class UpdateSelectedSplitPayload(BaseModel):
    split_id: UUID  # <-- БЫЛО int


class UpdateSelectedSplitDayPayload(BaseModel):
    split_day_id: UUID  # <-- БЫЛО int


class StartWorkoutPayload(BaseModel):
    source: str
    split_id: UUID | None = None      # <-- БЫЛО int | None
    split_day_id: UUID | None = None  # <-- БЫЛО int | None
    plan_id: Optional[int] = None
    calendar_day_id: Optional[int] = None


class StartWorkoutResponse(BaseModel):
    id: int
    started_at: datetime
    source: str
    plan_id: Optional[int] = None