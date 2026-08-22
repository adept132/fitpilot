"""Автоподбор сплита (P1-03 часть 1, §5.2).

Чистая функция без БД: вызывающая сторона грузит сплиты и переводит мышцы в
системные ключи. Та же конвенция, что в periodization/ и goal/.
"""

from __future__ import annotations

from dataclasses import dataclass

# Допуск в полсессии: неполнонедельные микроциклы попадают к ближайшей целой
# частоте ровно один раз. 3.5 достаётся тройке и четвёрке (обе честно близки),
# 5.25 — только пятёрке, 4.67 — только пятёрке.
FREQUENCY_TOLERANCE = 0.5

REST_TYPE = "active_rest"

# Русские подписи типов дня для человекочитаемой причины.
_TYPE_LABELS = {
    "push": "жимовых",
    "pull": "тяговых",
    "legs": "дня ног",
    "upper": "верха",
    "lower": "низа",
    "arms_shoulders": "рук и плеч",
    "full_body": "на всё тело",
}


@dataclass(frozen=True)
class DayView:
    template_type: str
    muscles: frozenset[str]


@dataclass(frozen=True)
class SplitView:
    id: str
    name: str
    length_days: int
    days: tuple[DayView, ...]


@dataclass(frozen=True)
class SplitCandidate:
    id: str
    name: str
    length_days: int
    training_days: int
    sessions_per_week: float
    coverage: float
    reason: str


def _training_days(split: SplitView) -> tuple[DayView, ...]:
    return tuple(d for d in split.days if d.template_type != REST_TYPE)


def _sessions_per_week(split: SplitView) -> float:
    return round(len(_training_days(split)) / split.length_days * 7, 2)


def _meets_requirement(split: SplitView, requirement: dict | None) -> bool:
    if not requirement:
        return True
    accepted = set(requirement.get("any_of") or [])
    minimum = int(requirement.get("min") or 0)
    if not accepted or minimum <= 0:
        return True
    matching = sum(1 for d in _training_days(split) if d.template_type in accepted)
    return matching >= minimum


def _coverage(split: SplitView, focus_muscles: list[str]) -> float:
    """Доля попаданий фокус-мышц в тренировочные дни, нормированная их числом.

    Считаем не «покрыта или нет», а сколько дней её задевают: сплит с тремя
    днями низа для фокуса на квадрицепсы объективно лучше, чем с двумя.
    """
    days = _training_days(split)
    if not focus_muscles or not days:
        return 0.0
    hits = sum(1 for muscle in focus_muscles for d in days if muscle in d.muscles)
    return round(hits / (len(focus_muscles) * len(days)), 4)


def _reason(split: SplitView, spw: float, requirement: dict | None) -> str:
    parts = [f"{spw:g} тренировки в неделю"]
    if requirement:
        accepted = set(requirement.get("any_of") or [])
        matching = sum(1 for d in _training_days(split) if d.template_type in accepted)
        labels = ", ".join(_TYPE_LABELS.get(t, t) for t in sorted(accepted))
        parts.append(f"{matching} дня из категорий: {labels}")
    if split.length_days != 7:
        parts.append(f"цикл {split.length_days} дней, не привязан к дням недели")
    return "; ".join(parts)


def suggest_splits(
    splits: list[SplitView],
    *,
    training_frequency: int,
    focus_muscles: list[str],
    requirement: dict | None = None,
    limit: int = 3,
) -> list[SplitCandidate]:
    """Ранжированные кандидаты. Меньше `limit` — правильный ответ.

    На двух тренировках в неделю осмысленных форм всего две, и добивать
    список третьей ради числа значило бы предлагать заведомо худшее.
    """
    candidates: list[SplitCandidate] = []
    for split in splits:
        spw = _sessions_per_week(split)
        if abs(spw - training_frequency) > FREQUENCY_TOLERANCE:
            continue
        if not _meets_requirement(split, requirement):
            continue
        candidates.append(SplitCandidate(
            id=split.id,
            name=split.name,
            length_days=split.length_days,
            training_days=len(_training_days(split)),
            sessions_per_week=spw,
            coverage=_coverage(split, focus_muscles),
            reason=_reason(split, spw, requirement),
        ))

    candidates.sort(key=lambda c: (-c.coverage, c.length_days, c.name))
    return candidates[:limit]
