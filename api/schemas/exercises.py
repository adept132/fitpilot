from datetime import datetime, date
from typing import Optional, List

from pydantic import BaseModel, Field


class ExerciseListItemResponse(BaseModel):
    id: int
    name: str
    category: str
    main_muscle_group: str
    secondary_muscle_groups: list[str] = []
    difficulty: str
    equipment_needed: list[str]
    fatigue_tier: int
    source: str


class ExerciseDetailResponse(BaseModel):
    id: int
    name: str
    category: str
    main_muscle_group: str
    secondary_muscle_groups: list[str]
    equipment_needed: list[str]
    difficulty: str
    description: str | None = None
    source: str | None = None
    video_url: str | None = None


class ExerciseHistoryItemResponse(BaseModel):
    workout_id: int
    finished_at: datetime
    source: str
    sets_count: int
    total_reps: int
    total_volume: float


class ExerciseHistoryWorkoutSetResponse(BaseModel):
    id: int
    set_number: int
    set_type: str
    weight: float | None = None
    reps: int | None = None
    notes: str | None = None
    is_completed: bool


class ExerciseHistoryWorkoutDetailResponse(BaseModel):
    workout_id: int
    finished_at: datetime | None = None
    source: str
    exercise_id: int
    exercise_name: str
    sets_count: int
    total_reps: int
    total_volume: float
    sets: list[ExerciseHistoryWorkoutSetResponse]

class ExerciseLastPerformanceResponse(BaseModel):
    workout_id: int
    finished_at: datetime | None = None
    source: str
    exercise_id: int
    exercise_name: str
    sets: list[ExerciseHistoryWorkoutSetResponse]

class ExerciseType(str):
    BASE = "base"
    ISOLATION = "isolation"


class EquipmentFilter(str):
    FREE = "free"
    MACHINE = "machine"

class ExerciseSearchItem(BaseModel):
    id: int
    name: str
    main_muscle_group: str
    secondary_muscle_groups: Optional[List[str]] = []
    category: str
    equipment_needed: Optional[List[str]] = None
    source: str


class MuscleGroupItem(BaseModel):
    name: str
    count: int


class LastWorkoutExerciseItem(BaseModel):
    exercise_id: int
    name: str
    main_muscle_group: str | None = None
    category: str | None = None


class LastWorkoutResponse(BaseModel):
    exercises: List[LastWorkoutExerciseItem]

class ExerciseAlternativeResponse(BaseModel):
    id: int
    name: str
    main_muscle_group: str
    equipment_needed: List[str]
    match_score: int  # <-- Сюда бэкенд положит баллы совпадения

    class Config:
        from_attributes = True

class ReplaceExerciseRequest(BaseModel):
    new_exercise_id: int

class HistorySetResponse(BaseModel):
    weight: float
    reps: int

# 2. Обновляем твою текущую модель точки графика
class ExerciseHistoryPoint(BaseModel): # У тебя она может называться немного иначе
    date: str
    e1rm: float
    volume: float
    best_set_str: str
    sets: Optional[List[HistorySetResponse]] = None # <-- ВОТ ОНО

# 3. Сама схема ответа остается такой же, просто внутри нее теперь обновленный массив history
class ExerciseFullHistoryResponse(BaseModel):
    exercise_id: int
    name: str
    category: str
    main_muscle_group: str
    history: List[ExerciseHistoryPoint]

class CustomExerciseCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=200)
    main_muscle_group: str
    secondary_muscle_groups: Optional[List[str]] = []
    equipment_needed: Optional[List[str]] = []
    description: Optional[str] = None