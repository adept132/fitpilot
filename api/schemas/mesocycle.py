from pydantic import BaseModel, Field
from typing import List, Dict, Optional
from uuid import UUID


# Описание одной фазы (итерации) мезоцикла
class PhaseCreate(BaseModel):
    phase_number: int = Field(..., gt=0, description="Порядковый номер фазы")
    name: str = Field(..., max_length=120, example="Втягивающая фаза")

    # Уровень стратегического усилия для RIR
    effort_tier: str = Field(..., description="Допустимо: 'deload', 'easy', 'medium', 'prefailure', 'failure'")


# Главная схема создания мезоцикла
class MesocycleCreate(BaseModel):
    name: str = Field(..., max_length=120, example="Гипертрофия: База")
    code: str = Field(..., max_length=50, example="hypertrophy_base_01")
    description: Optional[str] = None

    phases_in_cycle: int = Field(..., gt=0, example=4)

    # Массив фаз, который мы отправляем в AntiSuicideValidator для проверки скачков
    phases: List[PhaseCreate]

class UpdateSelectedMesocyclePayload(BaseModel):
    mesocycle_id: UUID

class UpdateMesocyclePhasePayload(BaseModel):
    phase: int

class UpdateMesocycleContextPayload(BaseModel):
    mesocycle_id: Optional[UUID] = None  # <--- ЗАМЕНИТЬ INT НА UUID
    microcycle_length: int = 7

class WorkoutCenterMesocycleRead(BaseModel):
    id: UUID
    name: str
    phases_in_cycle: int
    # Делаем опциональным, так как у доступных (available) мезоциклов длины еще нет
    microcycle_length: Optional[int] = None