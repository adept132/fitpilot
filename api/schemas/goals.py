"""Схемы целей пользователя."""

from datetime import date
from typing import Optional

from pydantic import BaseModel, Field


class GoalCreate(BaseModel):
    # Этап 1: только strength. Остальные типы — Этап 3.
    goal_type: str = Field(..., pattern="^(strength|bodyweight|body_fat|measurement|frequency)$")
    target_value: float = Field(..., gt=0)
    unit: Optional[str] = None
    exercise_id: Optional[int] = None
    target_reps: Optional[int] = Field(default=None, ge=1, le=100)
    metric_key: Optional[str] = None
    deadline: Optional[date] = None
    # Идемпотентный ключ offline-создания (дедуп повторной отправки).
    client_uuid: Optional[str] = None


class GoalUpdate(BaseModel):
    target_value: Optional[float] = Field(default=None, gt=0)
    target_reps: Optional[int] = Field(default=None, ge=1, le=100)
    deadline: Optional[date] = None
    is_completed: Optional[bool] = None


class GoalStatus(BaseModel):
    current_value: Optional[float] = None       # текущее значение метрики (для strength — вес на повторы)
    current_e1rm: Optional[float] = None        # только strength
    target_e1rm: Optional[float] = None         # только strength
    target_display: Optional[float] = None      # цель в единицах метрики
    progress_percentage: float = 0.0
    eta_date: Optional[str] = None
    # on_track | ambitious | unrealistic | wrong_way | achieved | insufficient
    realism: str = "insufficient"
    direction: str = "up"                        # up | down — куда движемся к цели
    has_data: bool = False


class GoalResponse(BaseModel):
    id: int
    goal_type: str
    target_value: float
    unit: Optional[str] = None
    exercise_id: Optional[int] = None
    exercise_name: Optional[str] = None
    target_reps: Optional[int] = None
    metric_key: Optional[str] = None
    deadline: Optional[str] = None
    is_completed: bool
    status: GoalStatus
