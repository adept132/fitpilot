"""Чистая мера тренировочного объёма.

Единственное место в проекте, где подходы упражнения превращаются во вклад
по мышцам. До P0-09 эта формула жила в трёх независимых реализациях (SQL в
progress/fatigue, TypeScript в ActiveWorkoutVolumeTracker и её отсутствие в
statistics_service) и давала три разных ответа на один вопрос.

В БД не ходит: на вход приходят уже прочитанные поля Exercise.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from api.services.muscle_keys import to_system_key

# [КОНФИГ] Вклад мышцы-синергиста в объём. Значение унаследовано от
# fatigue architecture (C-05, progress/fatigue/{muscle}), где косвенный
# подход уже считался как половина прямого. Менять только вместе с
# таблицами landmarks — они откалиброваны под эту меру.
INDIRECT_WEIGHT = 0.5


@dataclass(frozen=True)
class MuscleContribution:
    """Вклад в одну мышцу, разложенный по каналам.

    Каналы хранятся раздельно, а не эффективной суммой: отношение к
    превышению прямого объёма жёстче, чем к косвенному (спека §5.1), и без
    состава этого не выразить.
    """

    direct: float
    indirect: float

    @property
    def effective(self) -> float:
        return self.direct + self.indirect * INDIRECT_WEIGHT


def contribution(
    main_muscle_group: Optional[str],
    secondary_muscle_groups: Optional[list[str]],
    sets: int,
) -> dict[str, MuscleContribution]:
    """Вклад `sets` подходов упражнения по системным ключам мышц.

    Неизвестные мышцы молча отбрасываются: каталог дозаполняется в P0-10, и
    падать на незнакомой строке значило бы ронять расчёт объёма всей
    тренировки из-за одного упражнения.
    """
    if not sets or sets <= 0:
        return {}

    result: dict[str, MuscleContribution] = {}

    primary = to_system_key(main_muscle_group)
    if primary:
        result[primary] = MuscleContribution(direct=float(sets), indirect=0.0)

    for raw in secondary_muscle_groups or []:
        key = to_system_key(raw)
        if not key or key in result:
            # `key in result` отсекает случай, когда каталог продублировал
            # главную мышцу в списке синергистов: прямой вклад уже учтён,
            # и добавление косвенного завысило бы объём базовых движений.
            continue
        result[key] = MuscleContribution(direct=0.0, indirect=float(sets))

    return result


# [КОНФИГ] Нижняя граница подходов после срезающей правки. Ноль означал бы
# «убрать упражнение» — это другое решение, с другой карточкой и другими
# последствиями для истории прогрессии (load_history читает по exercise_id).
MIN_SETS_AFTER_ADJUSTMENT = 1


def apply_adjustments(
    compiled: list[dict],
    adjustments: Optional[list[dict]],
) -> list[dict]:
    """Наложить принятые правки объёма на скомпилированный список упражнений.

    Вход и выход — те же словари, что отдаёт calculate_exercise_recommendations.
    Входной список не мутируется.
    """
    if not adjustments:
        return compiled

    delta_by_exercise: dict[int, int] = {}
    for item in adjustments:
        try:
            exercise_id = int(item["exercise_id"])
            delta = int(item["delta_sets"])
        except (KeyError, TypeError, ValueError):
            continue
        delta_by_exercise[exercise_id] = delta_by_exercise.get(exercise_id, 0) + delta

    result: list[dict] = []
    for item in compiled:
        delta = delta_by_exercise.get(item.get("exercise_id"))
        if not delta:
            result.append(item)
            continue
        patched = dict(item)
        patched["target_sets"] = max(
            MIN_SETS_AFTER_ADJUSTMENT,
            int(item.get("target_sets") or 0) + delta,
        )
        result.append(patched)
    return result


def accumulate(
    total: dict[str, MuscleContribution],
    part: dict[str, MuscleContribution],
) -> dict[str, MuscleContribution]:
    """Сложить два разложения, не мутируя входы."""
    merged = dict(total)
    for key, add in part.items():
        have = merged.get(key)
        if have is None:
            merged[key] = add
        else:
            merged[key] = MuscleContribution(
                direct=have.direct + add.direct,
                indirect=have.indirect + add.indirect,
            )
    return merged
