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