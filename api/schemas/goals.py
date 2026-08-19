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
    # P0-12: назначение ведущей цели. Снятие флага с прежней ведущей
    # происходит в той же транзакции (api/routers/goals.py).
    is_primary: Optional[bool] = None


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
    is_primary: bool = False
    status: GoalStatus


# --- Контекст экрана автопилота цели (P0-12, Задача 11) ---

class GoalEta(BaseModel):
    nominal: Optional[str] = None
    calibrated: Optional[str] = None
    factor: Optional[float] = None
    horizon: str = "materialized"
    calibration_available: bool = False


class GoalRates(BaseModel):
    required: float = 0.0
    plan: float = 0.0
    ceiling: float = 0.0


class GoalMilestone(BaseModel):
    week_start: str
    expected_e1rm: float
    actual_e1rm: Optional[float] = None


class GoalPlanAhead(BaseModel):
    target_lift_sessions: int = 0
    sets_per_window: int = 0
    effort: Optional[str] = None
    next_session_date: Optional[str] = None


class GoalLastApplied(BaseModel):
    proposal_id: int
    applied_at: Optional[str] = None
    can_undo: bool = False
    undo_blocked_reason: Optional[str] = None


class GoalAutopilotRead(BaseModel):
    available: bool = False
    # Причина, по которой автопилот молчит: показывается пользователю as is.
    unavailable_reason: Optional[str] = None
    eta: GoalEta = GoalEta()
    rates: GoalRates = GoalRates()
    milestones: list[GoalMilestone] = []
    plan_ahead: GoalPlanAhead = GoalPlanAhead()
    proposal: Optional[dict] = None
    last_applied: Optional[GoalLastApplied] = None
