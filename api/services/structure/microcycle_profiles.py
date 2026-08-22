"""Профили усилия микроцикла (P1-03 часть 1, §5.4).

Профиль — правило построения days_mapping, а не готовая раскладка. Длина и
позиции дней отдыха берутся из слотов сплита: они ОБЯЗАНЫ совпадать, иначе
день сплита и день микроцикла расходятся (дефект §2.7). Поэтому один профиль
применим к любому сплиту, и заводить раскладки под каждую пару не нужно.

Профиль выбирает человек — значит предписание осознанное, и
resolve_rep_range_with_source честно вернёт REP_SOURCE_MICROCYCLE.
"""

from __future__ import annotations

from dataclasses import dataclass

REST_TYPE = "active_rest"


@dataclass(frozen=True)
class SlotView:
    template_type: str


@dataclass(frozen=True)
class MicrocycleProfile:
    code: str
    name: str
    description: str


MICROCYCLE_PROFILES: tuple[MicrocycleProfile, ...] = (
    MicrocycleProfile(
        code="even",
        name="Равномерный",
        description="Все тренировки в среднем диапазоне повторов. Простейший вариант.",
    ),
    MicrocycleProfile(
        code="hard_easy",
        name="Тяжёлый–лёгкий",
        description="Чередование малых и больших повторов — классический DUP.",
    ),
    MicrocycleProfile(
        code="one_hard",
        name="Один тяжёлый",
        description="Одна силовая тренировка в цикле, остальные средние.",
    ),
    MicrocycleProfile(
        code="strength_bias",
        name="Силовой уклон",
        description="Две силовые тренировки: меньше повторов, больше веса.",
    ),
    MicrocycleProfile(
        code="volume_bias",
        name="Объёмный уклон",
        description="Больше повторов почти во всех тренировках.",
    ),
)

DEFAULT_FOR_BEGINNER = "even"
DEFAULT_FOR_EXPERIENCED = "hard_easy"

_BY_CODE = {profile.code: profile for profile in MICROCYCLE_PROFILES}


def profile_by_code(code: str) -> MicrocycleProfile:
    return _BY_CODE[code]


def _type_for(profile_code: str, training_index: int) -> str:
    """Тип дня по порядковому номеру среди ТРЕНИРОВОЧНЫХ дней (с нуля).

    Считаем по тренировочным, а не по календарным: иначе чередование
    «тяжёлый–лёгкий» сбивалось бы каждым днём отдыха.
    """
    if profile_code == "even":
        return "medium"
    if profile_code == "hard_easy":
        return "hard" if training_index % 2 == 0 else "easy"
    if profile_code == "one_hard":
        return "hard" if training_index == 0 else "medium"
    if profile_code == "strength_bias":
        return "hard" if training_index < 2 else "medium"
    if profile_code == "volume_bias":
        return "medium" if training_index == 0 else "easy"
    raise KeyError(profile_code)


def build_days_mapping(profile_code: str, slots: list[SlotView]) -> dict[str, dict]:
    """days_mapping длиной ровно len(slots) — формат AppUserMicrocycle."""
    if profile_code not in _BY_CODE:
        raise KeyError(profile_code)

    mapping: dict[str, dict] = {}
    training_index = 0
    for position, slot in enumerate(slots, start=1):
        if slot.template_type == REST_TYPE:
            mapping[str(position)] = {"type": "rest", "tag": None}
            continue
        mapping[str(position)] = {
            "type": _type_for(profile_code, training_index),
            "tag": slot.template_type,
        }
        training_index += 1
    return mapping
