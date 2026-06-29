from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel


class SupersetWorkoutSetItem(BaseModel):
    id: int
    set_number: int
    weight: float | None = None
    reps: int | None = None
    effort_level: str | None = None
    notes: str | None = None
    is_completed: bool


class WorkoutStructureExerciseItem(BaseModel):
    type: Literal["exercise"] = "exercise"
    session_exercise_id: int
    order_index: int
    exercise_id: int
    exercise_name: str
    sets_count: int
    volume_total: float
    sets: list[SupersetWorkoutSetItem] = []


class WorkoutStructureSupersetMember(BaseModel):
    session_exercise_id: int
    order_index: int
    exercise_id: int
    exercise_name: str
    sets_count: int
    volume_total: float


class WorkoutStructureSupersetItem(BaseModel):
    type: Literal["superset"] = "superset"
    superset_group: str
    label: str
    order_index: int
    rounds_completed: int
    sets_total: int
    volume_total: float
    has_incomplete_round: bool
    exercises: list[WorkoutStructureSupersetMember]


class WorkoutStructureResponse(BaseModel):
    items: list[WorkoutStructureExerciseItem | WorkoutStructureSupersetItem]


class CreateSupersetRequest(BaseModel):
    source_session_exercise_id: int
    target_session_exercise_ids: list[int] = []


class AddExerciseToSupersetRequest(BaseModel):
    session_exercise_id: int


class AddNewExerciseToSupersetRequest(BaseModel):
    exercise_id: int


class RemoveExerciseFromSupersetRequest(BaseModel):
    session_exercise_id: int


class SupersetFlowExerciseItem(BaseModel):
    session_exercise_id: int
    order_index: int
    exercise_id: int
    exercise_name: str
    sets: list[SupersetWorkoutSetItem]
    last_performance_sets: list[SupersetWorkoutSetItem] = []
    is_current_round_completed: bool

    recommended_rir: Optional[int] = None
    recommended_rep_min: Optional[int] = None
    recommended_rep_max: Optional[int] = None


class SupersetFlowResponse(BaseModel):
    workout_id: int
    superset_group: str
    label: str
    current_round_number: int
    has_incomplete_round: bool
    first_incomplete_session_exercise_id: int | None = None
    exercises: list[SupersetFlowExerciseItem]


class ReorderWorkoutStructureExerciseItem(BaseModel):
    type: Literal["exercise"] = "exercise"
    session_exercise_id: int
    order_index: int


class ReorderWorkoutStructureSupersetMember(BaseModel):
    session_exercise_id: int
    order_index: int


class ReorderWorkoutStructureSupersetItem(BaseModel):
    type: Literal["superset"] = "superset"
    superset_group: str
    order_index: int
    members: list[ReorderWorkoutStructureSupersetMember]


class ReorderWorkoutStructureRequest(BaseModel):
    items: list[dict]

class StartSupersetResponse(BaseModel):
    superset_group: str
    session_exercise_id: int