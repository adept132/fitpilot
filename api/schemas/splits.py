from datetime import date

from pydantic import BaseModel, ConfigDict, Field
from typing import List, Optional
from uuid import UUID

# --- Схемы для отдачи данных (Response) ---

class DayBlueprintOut(BaseModel):
    id: UUID
    name: str
    template_type: str
    is_system: bool
    muscle_targets: List[str] = Field(validation_alias='muscle_target_names')

    model_config = ConfigDict(from_attributes=True)

class SplitDaySlotOut(BaseModel):
    day_order: int
    day: DayBlueprintOut

    model_config = ConfigDict(from_attributes=True)

class SplitBlueprintOut(BaseModel):
    id: UUID
    name: str
    length_days: int
    is_system: bool
    slots: List[SplitDaySlotOut]

    model_config = ConfigDict(from_attributes=True)

# --- Схемы для приема данных (Request) ---

class ActivateSplitRequest(BaseModel):
    blueprint_id: UUID

class CreateCustomSplitRequest(BaseModel):
    name: str
    length_days: int
    day_blueprint_ids: List[UUID | None]

class UpdateCustomSplitRequest(BaseModel):
    name: str | None = None
    length_days: int | None = None
    day_blueprint_ids: List[UUID | None] | None = None

class CreateCustomDayRequest(BaseModel):
    name: str
    muscle_targets: List[str]

class UpdateCustomDayRequest(BaseModel):
    name: str | None = None
    muscle_targets: List[str] | None = None

class SchedulePreviewRequest(BaseModel):
    blueprint_id: UUID
    start_date: date
    # Список дней недели (0 - ПН, 6 - ВС), в которые юзер принципиально не тренируется
    blackout_weekdays: List[int] = Field(default_factory=list)
    # Сколько дней вперед генерировать (по умолчанию 35 - классическая сетка календаря)
    preview_length: int = 35

# --- Исходящие данные (Что возвращаем для календаря) ---
class CalendarDayPreview(BaseModel):
    date: date
    is_blackout: bool # Если True - юзер забанил этот день
    is_rest_day: bool # Если True - это день отдыха из самого сплита
    slot_id: Optional[UUID] = None # ID слота сплита (если есть)
    day_name: Optional[str] = None # Название (Upper, Lower, Rest)
    muscle_targets: List[str] = []

class SchedulePreviewResponse(BaseModel):
    blueprint_id: UUID
    start_date: date
    calendar: List[CalendarDayPreview]
    start_slot_id: Optional[UUID] = None

class ScheduleLaunchRequest(BaseModel):
    blueprint_id: UUID
    start_date: date
    blackout_weekdays: List[int] = Field(default_factory=list)