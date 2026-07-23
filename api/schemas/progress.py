from datetime import datetime
from typing import Literal, Optional, List

from pydantic import BaseModel


class HistorySetResponse(BaseModel):
    weight: float
    reps: int

# 2. Обновляем твою текущую модель точки графика
class ExerciseHistoryPointResponse(BaseModel): # У тебя она может называться немного иначе
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
    history: List[ExerciseHistoryPointResponse]

class FatigueWeekData(BaseModel):
    week_start: str
    direct_volume: float   # Прямой объем (изоляция, коэффициент 1.0)
    indirect_volume: float # Косвенный объем (синергисты, коэффициент 0.5)

class FatigueArchitectureResponse(BaseModel):
    muscle_group: str
    history: List[FatigueWeekData]


class ReadinessBand(BaseModel):
    # Абсолютных процентов усталости здесь нет: только z-оценка к собственной
    # истории (может отсутствовать при cold-start) и качественная полоса.
    z: float | None = None
    band: str


class ProgressionResponse(BaseModel):
    ratio: float | None = None
    wow_change_pct: float | None = None
    chronic_level: float | None = None
    flag: str


class DataQualityResponse(BaseModel):
    effort_labeled_pct: float
    imported_pct: float


class ReadinessResponse(BaseModel):
    model_version: str
    computed_at: datetime
    confidence: str
    systemic: ReadinessBand
    muscular: dict[str, ReadinessBand]
    mechanical: ReadinessBand
    progression: ProgressionResponse
    data_quality: DataQualityResponse


class DisciplineDay(BaseModel):
    date: str
    sets: int
    sessions: int
    volume_kg: float


class DisciplineDensity(BaseModel):
    sets_per_hour_28d: float | None = None
    sessions_28d: int
    median_duration_min: float | None = None


class DisciplineResponse(BaseModel):
    weeks: int
    days: List[DisciplineDay]
    density: DisciplineDensity