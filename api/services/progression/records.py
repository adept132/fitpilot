"""Чистые правила личных рекордов (P1-14).

Без БД и без сети: те же правила зеркалит клиент в
features/workout/utils/records.ts, и расхождение здесь означало бы
плашку «новый рекорд» без записи в хронологии достижений.

Фильтр допуска (незавершённые, разминка, drop, аномалии) применяется
ДО вызова fold_records — в SQL-запросе records_repository, ровно тем
же условием, что и в /progress/achievements.
"""

from datetime import datetime
from typing import Iterable, NamedTuple, Optional

# Полосы повторов. Ключ — строка из спеки §4.1, менять нельзя: она едет
# на устройство и служит ключом кэша.
REP_BANDS: tuple[tuple[str, int, int], ...] = (
    ("1-3", 1, 3),
    ("4-6", 4, 6),
    ("7-10", 7, 10),
    ("11-15", 11, 15),
    ("16+", 16, 10**6),
)

# Выше двенадцати вес упирается в выносливость, а не в силу, и «21ПМ»
# ничего не сообщает. Высокие повторы ловят полоса 16+ и объём подхода.
MAX_TRACKED_REPS = 12

# Формула Бржицки не определена при 37 повторах и отрицательна выше.
BRZYCKI_UNDEFINED_REPS = 37


class SetInput(NamedTuple):
    """Один допущенный подход. Вес — в килограммах."""

    weight: float
    reps: int
    at: datetime
    workout_id: int


def band_for(reps: int) -> Optional[str]:
    for key, low, high in REP_BANDS:
        if low <= reps <= high:
            return key
    return None


def brzycki_e1rm(weight: float, reps: int) -> Optional[float]:
    if reps <= 0 or reps >= BRZYCKI_UNDEFINED_REPS:
        return None
    return weight * 36.0 / (37.0 - reps)


def fold_records(sets: Iterable[SetInput]) -> dict:
    """Свёртка допущенных подходов в структуру рекордов.

    Порядок входа значения не имеет: при равенстве побеждает более
    ранний подход — рекорд ставится тогда, когда его поставили впервые,
    а повторение результата рекордом не является.
    """
    weight_at_reps: dict[str, dict] = {}
    bands: dict[str, dict] = {}
    volume: Optional[dict] = None

    for item in sorted(sets, key=lambda s: (s.at, s.workout_id)):
        entry_base = {
            "weight": round(item.weight, 2),
            "reps": item.reps,
            "at": item.at,
            "workout_id": item.workout_id,
        }

        if 1 <= item.reps <= MAX_TRACKED_REPS:
            key = str(item.reps)
            current = weight_at_reps.get(key)
            if current is None or item.weight > current["weight"]:
                weight_at_reps[key] = dict(entry_base)

        e1rm = brzycki_e1rm(item.weight, item.reps)
        band = band_for(item.reps)
        if e1rm is not None and band is not None:
            current = bands.get(band)
            if current is None or e1rm > current["e1rm"]:
                bands[band] = {**entry_base, "e1rm": round(e1rm, 1)}

        value = item.weight * item.reps
        if volume is None or value > volume["value"]:
            volume = {**entry_base, "value": round(value, 2)}

    return {"weight_at_reps": weight_at_reps, "band": bands, "set_volume": volume}
