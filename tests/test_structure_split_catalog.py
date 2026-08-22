"""Каталог сплитов: состав, производные величины и инварианты (P1-03 ч.1, §5.1)."""
from api.services.structure.split_catalog import (
    SPLITS,
    sessions_per_week,
    training_days,
)

REST = "Rest"

# Имена, существующие в сиде дней (api/seed_splits.py).
KNOWN_DAYS = {
    "Push", "Pull", "Legs", "Upper", "Lower",
    "Arms & Shoulders", "Full Body", "Rest",
}

# Четыре сплита, существовавшие до P1-03. Имена обязаны совпасть буква в
# букву: сид ищет по имени, и опечатка создала бы дубль вместо совпадения.
LEGACY_NAMES = {
    "Full Body (3 Дня)",
    "Upper / Lower (4 Дня)",
    "Гибрид PHAT-style (5 Дней)",
    "PPL x2 (6 Дней)",
}


def test_catalog_has_sixteen_splits():
    assert len(SPLITS) == 16


def test_names_are_unique():
    names = [s.name for s in SPLITS]
    assert len(names) == len(set(names))


def test_legacy_splits_are_present_verbatim():
    assert LEGACY_NAMES <= {s.name for s in SPLITS}


def test_schedule_length_matches_declared_length():
    for split in SPLITS:
        assert len(split.schedule) == split.length_days, split.name


def test_every_day_name_exists_in_the_day_seed():
    for split in SPLITS:
        for day in split.schedule:
            assert day in KNOWN_DAYS, f"{split.name}: {day}"


def test_no_split_leaves_the_microcycle_without_rest():
    # §5.1: раскладка без единого дня отдыха означала бы тренировки каждый
    # день бессрочно.
    for split in SPLITS:
        assert REST in split.schedule, split.name


def test_training_days_counts_non_rest_slots():
    ppl_x2 = next(s for s in SPLITS if s.name == "PPL x2 (6 Дней)")
    assert training_days(ppl_x2) == 6


def test_sessions_per_week_scales_by_microcycle_length():
    eight_day = next(s for s in SPLITS if s.length_days == 8 and training_days(s) == 4)
    assert sessions_per_week(eight_day) == 3.5


def test_every_integer_frequency_from_two_to_six_has_a_candidate():
    # Допуск 0.5 — тот же, что у автоподбора (§5.2).
    for frequency in (2, 3, 4, 5, 6):
        matches = [s for s in SPLITS if abs(sessions_per_week(s) - frequency) <= 0.5]
        assert matches, f"нет кандидата на частоту {frequency}"


def test_seed_migration_chains_to_the_production_baseline():
    import importlib.util
    from pathlib import Path

    module_path = (
        Path(__file__).resolve().parent.parent
        / "migrations" / "versions" / "20260822_01_seed_split_catalog.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migrations.versions.20260822_01_seed_split_catalog", module_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.revision == "20260822_01"
    assert module.down_revision == "20260821_01"
