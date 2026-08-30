"""Системный каталог сплитов (P1-03 часть 1, §5.1).

Данные, не код: список читается сидом и автоподбором. Элементы `schedule` —
имена DayBlueprint из api/seed_splits.py; связывание по имени, а не по id,
потому что id системных дней генерируются при сиде.

Инвариант, который держит тест: ни один сплит не оставляет микроцикл без дня
отдыха. Раскладка вида «шесть тренировок в шестидневном микроцикле» означала
бы тренировки каждый день бессрочно — по той же причине не покрывается и
частота 7.
"""

from __future__ import annotations

from dataclasses import dataclass

from api.i18n import SupportedLanguage, tr

REST_DAY = "Rest"


@dataclass(frozen=True)
class SplitDef:
    code: str
    name_key: str
    name: str
    length_days: int
    schedule: tuple[str, ...]


def training_days(split: SplitDef) -> int:
    """Число тренировочных слотов (всё, что не день отдыха)."""
    return sum(1 for day in split.schedule if day != REST_DAY)


def sessions_per_week(split: SplitDef) -> float:
    """Сессий в неделю с поправкой на длину микроцикла.

    Сравнивать число слотов напрямую с training_frequency нельзя: на
    восьмидневке четыре тренировки дают 3.5 сессии в неделю, а не четыре.
    """
    return round(training_days(split) / split.length_days * 7, 2)


SPLITS: tuple[SplitDef, ...] = (
    SplitDef("full_body_2", "structure.split.full_body_2.name", "Full Body (2 дня)", 7, (
        "Full Body", "Rest", "Rest", "Full Body", "Rest", "Rest", "Rest")),
    SplitDef("upper_lower_2", "structure.split.upper_lower_2.name", "Верх / низ (2 дня)", 7, (
        "Upper", "Rest", "Rest", "Lower", "Rest", "Rest", "Rest")),
    SplitDef("full_body_3", "structure.split.full_body_3.name", "Full Body (3 Дня)", 7, (
        "Full Body", "Rest", "Full Body", "Rest", "Full Body", "Rest", "Rest")),
    SplitDef("upper_lower_full_3", "structure.split.upper_lower_full_3.name", "Верх / низ / всё тело (3 дня)", 7, (
        "Upper", "Rest", "Lower", "Rest", "Full Body", "Rest", "Rest")),
    SplitDef("ppl_3", "structure.split.ppl_3.name", "PPL (3 дня)", 7, (
        "Push", "Rest", "Pull", "Rest", "Legs", "Rest", "Rest")),
    SplitDef("upper_lower_4", "structure.split.upper_lower_4.name", "Upper / Lower (4 Дня)", 7, (
        "Upper", "Lower", "Rest", "Upper", "Lower", "Rest", "Rest")),
    SplitDef("lower_priority_4", "structure.split.lower_priority_4.name", "Верх / низ с приоритетом низа (4 дня)", 7, (
        "Lower", "Upper", "Rest", "Lower", "Rest", "Lower", "Rest")),
    SplitDef("ppl_full_4", "structure.split.ppl_full_4.name", "PPL + всё тело (4 дня)", 7, (
        "Push", "Pull", "Rest", "Legs", "Rest", "Full Body", "Rest")),
    SplitDef("upper_lower_8d", "structure.split.upper_lower_8d.name", "Верх / низ на восьмидневке", 8, (
        "Upper", "Lower", "Rest", "Upper", "Lower", "Rest", "Rest", "Rest")),
    SplitDef("phat_5", "structure.split.phat_5.name", "Гибрид PHAT-style (5 Дней)", 7, (
        "Upper", "Lower", "Rest", "Push", "Pull", "Legs", "Rest")),
    SplitDef("ppl_upper_lower_5", "structure.split.ppl_upper_lower_5.name", "PPL + верх / низ (5 дней)", 7, (
        "Push", "Pull", "Legs", "Rest", "Upper", "Lower", "Rest")),
    SplitDef("ppl_arms_shoulders_5", "structure.split.ppl_arms_shoulders_5.name", "PPL + руки и плечи (5 дней)", 7, (
        "Push", "Pull", "Legs", "Arms & Shoulders", "Rest", "Full Body", "Rest")),
    SplitDef("ppl_8d", "structure.split.ppl_8d.name", "PPL на восьмидневке", 8, (
        "Push", "Pull", "Legs", "Rest", "Push", "Pull", "Legs", "Rest")),
    SplitDef("upper_lower_6d", "structure.split.upper_lower_6d.name", "Верх / низ на шестидневке", 6, (
        "Upper", "Lower", "Rest", "Upper", "Lower", "Rest")),
    SplitDef("ppl_x2_6", "structure.split.ppl_x2_6.name", "PPL x2 (6 Дней)", 7, (
        "Push", "Pull", "Legs", "Push", "Pull", "Legs", "Rest")),
    SplitDef("upper_lower_x3_6", "structure.split.upper_lower_x3_6.name", "Верх / низ x3 (6 дней)", 7, (
        "Upper", "Lower", "Upper", "Lower", "Upper", "Lower", "Rest")),
)

_BY_CODE = {split.code: split for split in SPLITS}
_NAME_KEY_BY_STORED_NAME = {split.name: split.name_key for split in SPLITS}


def localized_split_name(
    stored_name: str,
    is_system: bool,
    language: SupportedLanguage,
) -> str:
    """Render a seeded system name without changing stored/custom names."""
    key = _NAME_KEY_BY_STORED_NAME.get(stored_name) if is_system else None
    return tr(language, key) if key else stored_name


def localized_split(code: str, language: SupportedLanguage) -> SplitDef:
    split = _BY_CODE[code]
    return SplitDef(
        code=split.code,
        name_key=split.name_key,
        name=tr(language, split.name_key),
        length_days=split.length_days,
        schedule=split.schedule,
    )
