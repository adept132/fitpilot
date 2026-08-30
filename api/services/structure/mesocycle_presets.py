"""Пресеты мезоцикла (P1-03 часть 1, §5.3).

Константа, а не системные строки в БД: применяются созданием личной копии
пользователя (решение 3), поэтому ни миграции, ни правки POST /mesocycles
не требуется.

Длина каждой фазы равна длине микроцикла — правило уже зашито в
periodization/repository._snapshot_phases_from_template, здесь не дублируется.
"""

from __future__ import annotations

from dataclasses import dataclass

from api.i18n import SupportedLanguage, tr

_PHASE_NAMES = {
    "deload": "Разгрузка",
    "easy": "Втягивающая",
    "medium": "Средняя",
    "prefailure": "Тяжёлая",
    "failure": "Отказная",
}


def phase_name(tier: str, language: SupportedLanguage = "ru") -> str:
    key = f"structure.phase.{tier}.name"
    return tr(language, key) if tier in _PHASE_NAMES else tier


@dataclass(frozen=True)
class MesocyclePreset:
    code: str
    name_key: str
    description_key: str
    name: str
    description: str
    tiers: tuple[str, ...]


MESOCYCLE_PRESETS: tuple[MesocyclePreset, ...] = (
    MesocyclePreset(
        code="linear_4w",
        name_key="structure.mesocycle.linear_4w.name",
        description_key="structure.mesocycle.linear_4w.description",
        name="Линейный 4-недельный",
        description="Плавный набор усилия и разгрузка в конце. Базовый вариант.",
        tiers=("easy", "medium", "prefailure", "deload"),
    ),
    MesocyclePreset(
        code="accumulation_3_1",
        name_key="structure.mesocycle.accumulation_3_1.name",
        description_key="structure.mesocycle.accumulation_3_1.description",
        name="Накопительный 3+1",
        description="Короткая полка на входе, отказная неделя и разгрузка. Опытным.",
        tiers=("medium", "prefailure", "failure", "deload"),
    ),
    MesocyclePreset(
        code="strength",
        name_key="structure.mesocycle.strength.name",
        description_key="structure.mesocycle.strength.description",
        name="Силовой",
        description="Две тяжёлые недели подряд: тяжёлая база уходит на проценты от 1ПМ.",
        tiers=("medium", "prefailure", "prefailure", "deload"),
    ),
    MesocyclePreset(
        code="stretched_5w",
        name_key="structure.mesocycle.stretched_5w.name",
        description_key="structure.mesocycle.stretched_5w.description",
        name="Растянутый 5-недельный",
        description="Реже разгрузки — тем, кто плохо переносит частые сбросы.",
        tiers=("easy", "medium", "medium", "prefailure", "deload"),
    ),
    MesocyclePreset(
        code="no_deload_3w",
        name_key="structure.mesocycle.no_deload_3w.name",
        description_key="structure.mesocycle.no_deload_3w.description",
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


def localized_preset(
    code: str, language: SupportedLanguage
) -> MesocyclePreset:
    preset = preset_by_code(code)
    return MesocyclePreset(
        code=preset.code,
        name_key=preset.name_key,
        description_key=preset.description_key,
        name=tr(language, preset.name_key),
        description=tr(language, preset.description_key),
        tiers=preset.tiers,
    )
