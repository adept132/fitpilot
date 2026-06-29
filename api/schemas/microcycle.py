from pydantic import BaseModel, Field
from typing import List, Dict, Optional
from uuid import UUID


# Описание одного дня внутри микроцикла
class DayMappingItem(BaseModel):
    # Тип нагрузки для DUP-матрицы
    type: str = Field(..., description="Допустимо: 'rest', 'easy', 'medium', 'hard'")

    # Тег сплита (None если это день отдыха)
    tag: Optional[str] = Field(None, description="Например: 'push', 'pull', 'legs', 'upper'")


# Главная схема создания микроцикла
class MicrocycleCreate(BaseModel):
    name: str = Field(..., max_length=120, example="PPL x2 + Rest")

    # Физическая длина (сколько дней крутится этот сплит до повторения)
    length_days: int = Field(..., gt=0, example=8)

    # Словарь, где ключ - номер дня ("1", "2"), а значение - объект DayMappingItem
    days_mapping: Dict[str, DayMappingItem] = Field(
        ...,
        example={
            "1": {"type": "hard", "tag": "push"},
            "2": {"type": "medium", "tag": "pull"},
            "3": {"type": "easy", "tag": "legs"},
            "4": {"type": "rest", "tag": None}
        }
    )

class UpdateMicrocycleContextPayload(BaseModel):
    microcycle_id: Optional[int] = None