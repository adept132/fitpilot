from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional, Dict, Union, List
from api.schemas.supersets import WorkoutStructureResponse
from pydantic import BaseModel, ConfigDict, Field


WorkoutSource = Literal["free", "split_day", "plan"]
WorkoutStatus = Literal["active", "finished"]
WorkoutSetType = Literal["normal", "warmup", "drop"]

WorkoutEffortLevel = Literal[
    "warmup_effort",
    "light",
    "medium",
    "prefailure",
    "failure",
]


class ExerciseShortResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    fatigue_tier: int | None = None

    category: str | None = None
    equipment_needed: Optional[List[str]] = None

    main_muscle_group: str | None = None
    secondary_muscle_groups: Optional[Union[List[str], str]] = None


class AutoprogressionResponse(BaseModel):
    has_basis: bool
    metric: Optional[Literal["e1rm", "volume"]] = None
    target_weight: float | None = None
    target_reps: int | None = None
    modified_target: float | None = None


class WorkoutSessionSetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    set_number: int
    set_type: WorkoutSetType
    weight: Decimal | None = None
    reps: int | None = None
    effort_level: WorkoutEffortLevel | None = None
    notes: str | None = None
    parent_set_id: int | None = None
    superset_round: int | None = None
    is_completed: bool
    updated_at: datetime


class WorkoutSessionExerciseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_index: int
    superset_group: str | None = None
    notes: str | None = None
    updated_at: datetime

    exercise: ExerciseShortResponse
    sets: list[WorkoutSessionSetResponse]

    recommended_rir: Optional[int] = None
    recommended_rep_min: Optional[int] = None
    recommended_rep_max: Optional[int] = None
    target_sets: Optional[int] = None

class MuscleVolumeTarget(BaseModel):
    target_sets: int
    max_session_cap: int

class WorkoutSessionDetailResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: WorkoutSource
    status: WorkoutStatus

    split_day_id: Optional[uuid.UUID]  = None
    plan_id: int | None = None
    app_user_periodization_id: int | None = None
    periodization_week: int | None = None
    items: list[dict] = []
    notes: str | None = None
    # Субъективная тяжесть сессии по шкале Борга CR10 (0-10).
    session_rpe: float | None = None
    volume_targets: Optional[Dict[str, MuscleVolumeTarget]] = None
    started_at: datetime
    finished_at: datetime | None = None
    updated_at: datetime

    exercises: list[WorkoutSessionExerciseResponse]


class AddWorkoutExerciseRequest(BaseModel):
    exercise_id: int = Field(gt=0)
    notes: str | None = None
    superset_group: str | None = Field(default=None, max_length=64)


class AddWorkoutSetRequest(BaseModel):
    set_type: WorkoutSetType = "normal"
    # Верхние границы — защита от переполнения Numeric(8,2), а не оценка
    # правдоподобности: ею занимается anomaly_guard.
    weight: Decimal | None = Field(default=None, ge=0, le=2000)
    reps: int | None = Field(default=None, ge=0, le=1000)
    effort_level: WorkoutEffortLevel | None = None
    notes: str | None = None
    parent_set_id: int | None = Field(default=None, gt=0)
    superset_round: int | None = Field(default=None, gt=0)
    # Клиент выставляет true, когда пользователь подтвердил подозрительное значение.
    anomaly_confirmed: bool = False


class AddWorkoutSetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    set_number: int
    set_type: WorkoutSetType
    weight: Decimal | None = None
    reps: int | None = None
    effort_level: WorkoutEffortLevel | None = None
    notes: str | None = None
    parent_set_id: int | None = None
    superset_round: int | None = None
    is_completed: bool
    is_anomalous: bool = False
    updated_at: datetime


class UpdateWorkoutSetRequest(BaseModel):
    set_type: WorkoutSetType | None = None
    # Верхние границы — защита от переполнения Numeric(8,2), а не оценка
    # правдоподобности: ею занимается anomaly_guard.
    weight: Decimal | None = Field(default=None, ge=0, le=2000)
    reps: int | None = Field(default=None, ge=0, le=1000)
    effort_level: WorkoutEffortLevel | None = None
    notes: str | None = None
    parent_set_id: int | None = Field(default=None, gt=0)
    superset_round: int | None = Field(default=None, gt=0)
    is_completed: bool | None = None
    # Клиент выставляет true, когда пользователь подтвердил подозрительное значение.
    anomaly_confirmed: bool = False


class RepeatWorkoutSetRequest(BaseModel):
    target_session_exercise_id: int | None = Field(default=None, gt=0)


class WorkoutFinishedExerciseSummary(BaseModel):
    exercise_id: int
    exercise_name: str
    sets_count: int
    total_reps: int
    total_volume: Decimal | None = None


class FinishWorkoutResponse(BaseModel):
    workout_id: int
    source: WorkoutSource
    started_at: datetime
    finished_at: datetime
    duration_seconds: int

    exercises_count: int
    sets_count: int
    total_reps: int
    total_volume: Decimal | None = None

    exercises: list[WorkoutFinishedExerciseSummary]