"""Пресеты мезоцикла (P1-03 часть 1, §5.3).

Константа, а не системные строки в БД: применяются созданием личной копии
пользователя (решение 3), поэтому ни миграции, ни правки POST /mesocycles
не требуется.

Длина каждой фазы равна длине микроцикла — правило уже зашито в
periodization/repository._snapshot_phases_from_template, здесь не дублируется.
"""

from __future__ import annotations

from dataclasses import dataclass

_PHASE_NAMES = {
    "deload": "Разгрузка",
    "easy": "Втягивающая",
    "medium": "Средняя",
    "prefailure": "Тяжёлая",
    "failure": "Отказная",
}


def phase_name(tier: str) -> str:
    return _PHASE_NAMES.get(tier, tier)


@dataclass(frozen=True)
class MesocyclePreset:
    code: str
    name: str
    description: str
    tiers: tuple[str, ...]


MESOCYCLE_PRESETS: tuple[MesocyclePreset, ...] = (
    MesocyclePreset(
        code="linear_4w",
        name="Линейный 4-недельный",
        description="Плавный набор усилия и разгрузка в конце. Базовый вариант.",
        tiers=("easy", "medium", "prefailure", "deload"),
    ),
    MesocyclePreset(
        code="accumulation_3_1",
        name="Накопительный 3+1",
        description="Короткая полка на входе, отказная неделя и разгрузка. Опытным.",
        tiers=("medium", "prefailure", "failure", "deload"),
    ),
    MesocyclePreset(
        code="strength",
        name="Силовой",
        description="Две тяжёлые недели подряд: тяжёлая база уходит на проценты от 1ПМ.",
        tiers=("medium", "prefailure", "prefailure", "deload"),
    ),
    MesocyclePreset(
        code="stretched_5w",
        name="Растянутый 5-недельный",
        description="Реже разгрузки — тем, кто плохо переносит частые сбросы.",
        tiers=("easy", "medium", "medium", "prefailure", "deload"),
    ),
    MesocyclePreset(
        code="no_deload_3w",
        name="Без разгрузки, 3 недели",
        description="Новичку: усталость ещё не копится настолько, чтобы разгружаться.",
        tiers=("easy", "medium", "medium"),
    ),
)

DEFAULT_FOR_BEGINNER = "no_deload_3w"
DEFAULT_FOR_EXPERIENCED = "linear_4w"

_BY_CODE = {preset.code: preset for preset in MESOCYCLE_PRESETS}


def preset_by_code(code: str) -> MesocyclePreset:
    """Пресет по коду. Неизвестный код — KeyError, как в SCHEMES прогрессии."""
    return _BY_CODE[code]
