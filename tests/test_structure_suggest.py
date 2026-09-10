"""Автоподбор сплита: фильтры и ранжирование (P1-03 ч.1, §5.2)."""
import pytest

from api.services.structure.suggest import (
    DayView,
    SplitView,
    suggest_splits,
)


def day(template_type: str, *muscles: str) -> DayView:
    return DayView(template_type=template_type, muscles=frozenset(muscles))


REST = day("active_rest")

UPPER_LOWER_4 = SplitView(
    id="ul4", name="Верх / низ (4 дня)", length_days=7,
    days=(day("upper", "chest", "lats"), day("lower", "quads", "glutes"), REST,
          day("upper", "chest", "lats"), day("lower", "quads", "glutes"), REST, REST),
)

PPL_3 = SplitView(
    id="ppl3", name="PPL (3 дня)", length_days=7,
    days=(day("push", "chest", "triceps"), REST, day("pull", "lats", "biceps"),
          REST, day("legs", "quads", "glutes"), REST, REST),
)

UPPER_LOWER_8 = SplitView(
    id="ul8", name="Верх / низ на восьмидневке", length_days=8,
    days=(day("upper", "chest"), day("lower", "quads"), REST,
          day("upper", "chest"), day("lower", "quads"), REST, REST, REST),
)

LOWER_PRIORITY_4 = SplitView(
    id="low4", name="Приоритет низа (4 дня)", length_days=7,
    days=(day("lower", "quads", "glutes"), day("upper", "chest"), REST,
          day("lower", "quads", "glutes"), REST, day("lower", "quads", "glutes"), REST),
)

ALL = [UPPER_LOWER_4, PPL_3, UPPER_LOWER_8, LOWER_PRIORITY_4]


def test_frequency_is_a_hard_filter():
    result = suggest_splits(ALL, training_frequency=3, focus_muscles=[])
    assert PPL_3.id in {c.id for c in result}
    assert UPPER_LOWER_4.id not in {c.id for c in result}


def test_eight_day_split_counts_as_three_and_a_half_sessions():
    result = suggest_splits([UPPER_LOWER_8], training_frequency=3, focus_muscles=[])
    assert [c.sessions_per_week for c in result] == [3.5]
    # 3.5 честно близко и к трём, и к четырём — допуск 0.5 отдаёт его обеим.
    assert suggest_splits([UPPER_LOWER_8], training_frequency=4, focus_muscles=[])


def test_six_day_microcycle_goes_only_to_five():
    six = SplitView(
        id="ul6", name="Верх / низ на шестидневке", length_days=6,
        days=(day("upper", "chest"), day("lower", "quads"), REST,
              day("upper", "chest"), day("lower", "quads"), REST),
    )
    assert suggest_splits([six], training_frequency=5, focus_muscles=[])
    assert suggest_splits([six], training_frequency=4, focus_muscles=[]) == []


def test_requirement_filters_out_splits_without_enough_matching_days():
    requirement = {"any_of": ["legs", "lower", "full_body"], "min": 2}
    result = suggest_splits(
        ALL, training_frequency=4, focus_muscles=[], requirement=requirement,
    )
    # UPPER_LOWER_8 тоже проходит: 3.5 сессии в неделю укладываются в допуск,
    # и два дня низа у него есть. PPL_3 отсечён по частоте.
    assert {c.id for c in result} == {
        UPPER_LOWER_4.id, UPPER_LOWER_8.id, LOWER_PRIORITY_4.id,
    }


def test_requirement_rejects_when_matching_days_are_too_few():
    requirement = {"any_of": ["legs"], "min": 2}
    result = suggest_splits(
        [PPL_3], training_frequency=3, focus_muscles=[], requirement=requirement,
    )
    assert result == []


def test_focus_coverage_decides_the_order():
    result = suggest_splits(
        [UPPER_LOWER_4, LOWER_PRIORITY_4],
        training_frequency=4,
        focus_muscles=["quads", "glutes"],
    )
    # У приоритета низа три дня с квадрицепсами против двух.
    assert result[0].id == LOWER_PRIORITY_4.id


def test_shorter_microcycle_wins_a_tie():
    long_variant = SplitView(
        id="long", name="Длинный", length_days=8,
        days=(day("upper", "chest"), day("lower", "quads"), REST,
              day("upper", "chest"), day("lower", "quads"), REST, REST, REST),
    )
    short_variant = SplitView(
        id="short", name="Короткий", length_days=7,
        days=(day("upper", "chest"), day("lower", "quads"), REST,
              day("upper", "chest"), day("lower", "quads"), REST, REST),
    )
    result = suggest_splits(
        [long_variant, short_variant], training_frequency=4, focus_muscles=["chest"],
    )
    assert result[0].id == short_variant.id


def test_limit_caps_the_result():
    # На четвёрку проходят три: два ровно по 4.0 и восьмидневка на 3.5.
    result = suggest_splits(ALL, training_frequency=4, focus_muscles=[], limit=3)
    assert len(result) == 3
    assert len(suggest_splits(ALL, training_frequency=4, focus_muscles=[], limit=2)) == 2


def test_fewer_than_the_limit_is_a_valid_answer():
    # На тройку из этого набора проходят PPL_3 (3.0) и восьмидневка (3.5).
    result = suggest_splits(ALL, training_frequency=3, focus_muscles=[], limit=3)
    assert len(result) == 2


def test_reason_names_frequency_and_matching_days():
    result = suggest_splits(
        [LOWER_PRIORITY_4],
        training_frequency=4,
        focus_muscles=["quads"],
        requirement={"any_of": ["lower"], "min": 2},
    )
    assert "4" in result[0].reason
    assert "низ" in result[0].reason.lower() or "lower" in result[0].reason.lower()
