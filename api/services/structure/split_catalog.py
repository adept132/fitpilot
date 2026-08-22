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

REST_DAY = "Rest"


@dataclass(frozen=True)
class SplitDef:
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
    SplitDef("Full Body (2 дня)", 7, (
        "Full Body", "Rest", "Rest", "Full Body", "Rest", "Rest", "Rest")),
    SplitDef("Верх / низ (2 дня)", 7, (
        "Upper", "Rest", "Rest", "Lower", "Rest", "Rest", "Rest")),
    SplitDef("Full Body (3 Дня)", 7, (
        "Full Body", "Rest", "Full Body", "Rest", "Full Body", "Rest", "Rest")),
    SplitDef("Верх / низ / всё тело (3 дня)", 7, (
        "Upper", "Rest", "Lower", "Rest", "Full Body", "Rest", "Rest")),
    SplitDef("PPL (3 дня)", 7, (
        "Push", "Rest", "Pull", "Rest", "Legs", "Rest", "Rest")),
    SplitDef("Upper / Lower (4 Дня)", 7, (
        "Upper", "Lower", "Rest", "Upper", "Lower", "Rest", "Rest")),
    SplitDef("Верх / низ с приоритетом низа (4 дня)", 7, (
        "Lower", "Upper", "Rest", "Lower", "Rest", "Lower", "Rest")),
    SplitDef("PPL + всё тело (4 дня)", 7, (
        "Push", "Pull", "Rest", "Legs", "Rest", "Full Body", "Rest")),
    SplitDef("Верх / низ на восьмидневке", 8, (
        "Upper", "Lower", "Rest", "Upper", "Lower", "Rest", "Rest", "Rest")),
    SplitDef("Гибрид PHAT-style (5 Дней)", 7, (
        "Upper", "Lower", "Rest", "Push", "Pull", "Legs", "Rest")),
    SplitDef("PPL + верх / низ (5 дней)", 7, (
        "Push", "Pull", "Legs", "Rest", "Upper", "Lower", "Rest")),
    SplitDef("PPL + руки и плечи (5 дней)", 7, (
        "Push", "Pull", "Legs", "Arms & Shoulders", "Rest", "Full Body", "Rest")),
    SplitDef("PPL на восьмидневке", 8, (
        "Push", "Pull", "Legs", "Rest", "Push", "Pull", "Legs", "Rest")),
    SplitDef("Верх / низ на шестидневке", 6, (
        "Upper", "Lower", "Rest", "Upper", "Lower", "Rest")),
    SplitDef("PPL x2 (6 Дней)", 7, (
        "Push", "Pull", "Legs", "Push", "Pull", "Legs", "Rest")),
    SplitDef("Верх / низ x3 (6 дней)", 7, (
        "Upper", "Lower", "Upper", "Lower", "Upper", "Lower", "Rest")),
)
