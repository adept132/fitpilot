"""Лестница силовых вех (P1-03 ч.2, §5.1).

Константа в коде, а не таблица БД: набор меняется правкой, а не миграцией, и
не требует сида. В базу попадает только код вехи — по нему адресуется
принятие и по нему же цель помнит, из какой вехи выросла.

Движение задано ИМЕНЕМ упражнения, а не id: `Exercise.name` уникален
(api/services/models.py), имена системных упражнений стабильны и читаемы, а
зашитые id разъезжаются между локальной базой и продовой. Резолв имени в id —
забота repository.py; веха, чьего упражнения в каталоге нет, просто не
показывается (§7).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Движение -> точное имя системного упражнения.
LIFTS: dict[str, str] = {
    "squat": "Приседания со штангой",
    "bench": "Жим лежа на прямой скамье",
    "deadlift": "Становая тяга",
    "ohp": "Армейский жим стоя",
    "pullup": "Подтягивания",
    "row": "Тяга штанги в наклоне",
}


@dataclass(frozen=True)
class Milestone:
    """Одна ступень лестницы.

    Ровно одно из `absolute_kg` / `bodyweight_multiple` заполнено: порог либо
    круглое число («сотка»), либо кратность своего веса. Абсолютные стоят
    рядом с относительными намеренно — их гоняются брать именно как круглые
    числа, и подменять их кратностью значило бы отобрать ровно ту цель,
    которую человек себе и ставит (решение 5).

    `bodyweight_addend` — для подтягивания с отягощением: нагрузка равна
    своему весу плюс блины.
    """

    code: str
    lift: str
    title: str
    target_reps: int
    absolute_kg: Optional[float] = None
    bodyweight_multiple: Optional[float] = None
    bodyweight_addend: float = 0.0


MILESTONES: tuple[Milestone, ...] = (
    # Присед
    Milestone("squat_100kg", "squat", "Сотка в приседе", 1, absolute_kg=100.0),
    Milestone("squat_bw_5reps", "squat", "Свой вес на пять", 5, bodyweight_multiple=1.0),
    Milestone("squat_1_5x_bw", "squat", "Полтора своих веса", 1, bodyweight_multiple=1.5),
    Milestone("squat_2x_bw", "squat", "Двойной вес", 1, bodyweight_multiple=2.0),
    # Жим лёжа
    Milestone("bench_bw", "bench", "Свой вес", 1, bodyweight_multiple=1.0),
    Milestone("bench_100kg", "bench", "Сотка", 1, absolute_kg=100.0),
    Milestone("bench_1_5x_bw", "bench", "Полтора своих веса", 1, bodyweight_multiple=1.5),
    # Становая
    Milestone("deadlift_100kg", "deadlift", "Сотка", 1, absolute_kg=100.0),
    Milestone("deadlift_1_5x_bw", "deadlift", "Полтора своих веса", 1, bodyweight_multiple=1.5),
    Milestone("deadlift_2x_bw", "deadlift", "Двойной вес", 1, bodyweight_multiple=2.0),
    Milestone("deadlift_200kg", "deadlift", "Двести", 1, absolute_kg=200.0),
    # Жим стоя
    Milestone("ohp_0_5x_bw", "ohp", "Половина своего веса", 1, bodyweight_multiple=0.5),
    Milestone("ohp_0_75x_bw", "ohp", "Три четверти", 1, bodyweight_multiple=0.75),
    Milestone("ohp_bw", "ohp", "Свой вес", 1, bodyweight_multiple=1.0),
    # Подтягивание
    Milestone("pullup_first", "pullup", "Первое подтягивание", 1, bodyweight_multiple=1.0),
    Milestone("pullup_10_reps", "pullup", "Десять подтягиваний", 10, bodyweight_multiple=1.0),
    Milestone("pullup_plus20", "pullup", "С отягощением +20", 1, bodyweight_multiple=1.0,
              bodyweight_addend=20.0),
    # Тяга в наклоне
    Milestone("row_bw_8reps", "row", "Свой вес на восемь", 8, bodyweight_multiple=1.0),
)

_BY_CODE: dict[str, Milestone] = {m.code: m for m in MILESTONES}


def milestone_by_code(code: str) -> Milestone:
    """Веха по коду. Неизвестный код — KeyError, а не молчаливый дефолт."""
    return _BY_CODE[code]


def target_kg(milestone: Milestone, bodyweight: Optional[float]) -> Optional[float]:
    """Порог вехи в килограммах.

    None означает «посчитать нечем»: у относительной вехи не заполнен вес
    тела. Абсолютная от веса не зависит и считается всегда.
    """
    if milestone.absolute_kg is not None:
        return milestone.absolute_kg
    if bodyweight is None:
        return None
    return round(bodyweight * milestone.bodyweight_multiple + milestone.bodyweight_addend, 1)
