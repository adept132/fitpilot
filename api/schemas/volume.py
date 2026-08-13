from typing import Optional

from pydantic import BaseModel

# [КОНФИГ] Версия формы ответа. Клиент читает офлайн-кэш ДО сети, и после
# обновления приложения в кэше может лежать структура прошлой версии.
# Несовпадение версии означает «выбросить кэш», а не «попытаться отрисовать».
VOLUME_SHAPE_VERSION = 3


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
    # Physical workout sets in this window. Unlike muscles[*], every set is
    # counted once, irrespective of how many muscles it contributes to.
    planned_work_sets: int = 0
    completed_work_sets: int = 0
    # Сырой volume_budget профиля. Нужен редактору бюджета: окно отдаёт
    # ПРОИЗВОДНЫЕ величины по мышцам, а редактор правит сам бюджет.
    # Отдаётся здесь, а не отдельным запросом, потому что профиль на этом
    # пути уже прочитан.
    budget: Optional[dict] = None
    # P0-09 I4 (Important): shim совместимости со сборками ДО shape v2.
    # Старый клиент делал `setPerformedSets(data.performed_sets)`, а на
    # рендере — `Object.values(performedSets)`; с полем, убранным в v2, это
    # TypeError на экране прогресса, а не деградация вида. Значение — то же
    # эффективное выполненное по мышце, что и в muscles[*].performed_direct
    # + performed_indirect, просто сложенные (см. MuscleRow.performed_effective
    # в volume/repository.py). Заполнять ничего не стоит, новый клиент это
    # поле игнорирует. Можно убрать, когда сборки до v2 гарантированно вымерли.
    performed_sets: dict[str, float] = {}
