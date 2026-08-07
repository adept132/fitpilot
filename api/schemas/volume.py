from typing import Optional

from pydantic import BaseModel

# [КОНФИГ] Версия формы ответа. Клиент читает офлайн-кэш ДО сети, и после
# обновления приложения в кэше может лежать структура прошлой версии.
# Несовпадение версии означает «выбросить кэш», а не «попытаться отрисовать».
VOLUME_SHAPE_VERSION = 2


class WindowRead(BaseModel):
    index: int
    day: int
    length: int
    start_date: str
    end_date: str
    phase_number: Optional[int] = None
    is_deload: bool = False


class AdherenceRead(BaseModel):
    planned_days: int
    completed_days: int
    missed_days: int


class MuscleVolumeRead(BaseModel):
    target: float
    prescribed: float
    performed_direct: float
    performed_indirect: float
    forecast: float
    mev: int
    mav: int
    mrv: int
    mev_direct: int
    mrv_direct: int


class VolumeOverviewRead(BaseModel):
    shape_version: int = VOLUME_SHAPE_VERSION
    window: Optional[WindowRead] = None
    level: Optional[str] = None
    adherence: Optional[AdherenceRead] = None
    muscles: dict[str, MuscleVolumeRead] = {}
    # Сырой volume_budget профиля. Нужен редактору бюджета: окно отдаёт
    # ПРОИЗВОДНЫЕ величины по мышцам, а редактор правит сам бюджет.
    # Отдаётся здесь, а не отдельным запросом, потому что профиль на этом
    # пути уже прочитан.
    budget: Optional[dict] = None
