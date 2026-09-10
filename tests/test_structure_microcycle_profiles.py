"""Профили микроцикла: раскладка усилия по слотам сплита (P1-03 ч.1, §5.4)."""
import pytest

from api.services.structure.microcycle_profiles import (
    DEFAULT_FOR_BEGINNER,
    DEFAULT_FOR_EXPERIENCED,
    MICROCYCLE_PROFILES,
    SlotView,
    build_days_mapping,
    profile_by_code,
)
from api.services.structure.split_catalog import SPLITS

# Соответствие имени дня из каталога сплитов его template_type.
DAY_TYPES = {
    "Push": "push", "Pull": "pull", "Legs": "legs", "Upper": "upper",
    "Lower": "lower", "Arms & Shoulders": "arms_shoulders",
    "Full Body": "full_body", "Rest": "active_rest",
}

VALID_TYPES = {"easy", "medium", "hard", "rest"}


def slots_for(split) -> list[SlotView]:
    return [SlotView(template_type=DAY_TYPES[name]) for name in split.schedule]


UL4 = [SlotView("upper"), SlotView("lower"), SlotView("active_rest"),
       SlotView("upper"), SlotView("lower"), SlotView("active_rest"),
       SlotView("active_rest")]


def test_there_are_five_profiles_with_unique_codes():
    assert len(MICROCYCLE_PROFILES) == 5
    codes = [p.code for p in MICROCYCLE_PROFILES]
    assert len(codes) == len(set(codes))


def test_mapping_length_always_equals_slot_count_on_every_catalog_split():
    # Инвариант §5.4: расхождение длин уводит раскладку повторов относительно
    # дней сплита (см. дефект §2.7).
    for split in SPLITS:
        slots = slots_for(split)
        for profile in MICROCYCLE_PROFILES:
            mapping = build_days_mapping(profile.code, slots)
            assert len(mapping) == len(slots), f"{split.name}/{profile.code}"
            assert set(mapping) == {str(i) for i in range(1, len(slots) + 1)}


def test_rest_slots_always_become_rest_with_no_tag():
    for split in SPLITS:
        slots = slots_for(split)
        mapping = build_days_mapping("hard_easy", slots)
        for index, slot in enumerate(slots, start=1):
            if slot.template_type == "active_rest":
                assert mapping[str(index)] == {"type": "rest", "tag": None}


def test_training_slots_keep_their_tag_and_get_a_valid_type():
    for split in SPLITS:
        slots = slots_for(split)
        mapping = build_days_mapping("even", slots)
        for index, slot in enumerate(slots, start=1):
            if slot.template_type != "active_rest":
                assert mapping[str(index)]["tag"] == slot.template_type
                assert mapping[str(index)]["type"] in VALID_TYPES


def test_even_profile_makes_every_training_day_medium():
    mapping = build_days_mapping("even", UL4)
    assert [mapping[str(i)]["type"] for i in range(1, 8)] == [
        "medium", "medium", "rest", "medium", "medium", "rest", "rest",
    ]


def test_hard_easy_alternates_over_training_days_not_calendar_days():
    mapping = build_days_mapping("hard_easy", UL4)
    assert [mapping[str(i)]["type"] for i in range(1, 8)] == [
        "hard", "easy", "rest", "hard", "easy", "rest", "rest",
    ]


def test_one_hard_marks_only_the_first_training_day():
    mapping = build_days_mapping("one_hard", UL4)
    assert [mapping[str(i)]["type"] for i in range(1, 8)] == [
        "hard", "medium", "rest", "medium", "medium", "rest", "rest",
    ]


def test_strength_bias_marks_the_first_two_training_days():
    mapping = build_days_mapping("strength_bias", UL4)
    assert [mapping[str(i)]["type"] for i in range(1, 8)] == [
        "hard", "hard", "rest", "medium", "medium", "rest", "rest",
    ]


def test_volume_bias_keeps_only_the_first_training_day_medium():
    mapping = build_days_mapping("volume_bias", UL4)
    assert [mapping[str(i)]["type"] for i in range(1, 8)] == [
        "medium", "easy", "rest", "easy", "easy", "rest", "rest",
    ]


def test_defaults_point_at_existing_profiles():
    assert profile_by_code(DEFAULT_FOR_BEGINNER).code == "even"
    assert profile_by_code(DEFAULT_FOR_EXPERIENCED).code == "hard_easy"


def test_unknown_profile_raises():
    with pytest.raises(KeyError):
        build_days_mapping("nope", UL4)
