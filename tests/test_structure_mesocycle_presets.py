"""Пресеты мезоцикла: состав и безопасность последовательностей (P1-03 ч.1, §5.3)."""
import pytest

from api.services.structure.mesocycle_presets import (
    DEFAULT_FOR_BEGINNER,
    DEFAULT_FOR_EXPERIENCED,
    MESOCYCLE_PRESETS,
    phase_name,
    preset_by_code,
)
from api.services.validator import AntiSuicideValidator

STRENGTH_PHASES = {"prefailure", "failure"}


def test_there_are_five_presets_with_unique_codes():
    assert len(MESOCYCLE_PRESETS) == 5
    codes = [p.code for p in MESOCYCLE_PRESETS]
    assert len(codes) == len(set(codes))


def test_every_preset_survives_the_anti_suicide_validator():
    for preset in MESOCYCLE_PRESETS:
        assert AntiSuicideValidator.validate_mesocycle_sequence(list(preset.tiers)), preset.code


def test_strength_preset_holds_two_strength_phases_in_a_row():
    # resolve_scheme слой 2 переводит тяжёлую базу на percent_1rm именно в
    # фазах prefailure/failure — две подряд означают половину блока на
    # процентах от 1ПМ (§5.3).
    strength = preset_by_code("strength")
    pairs = list(zip(strength.tiers, strength.tiers[1:]))
    assert any(a in STRENGTH_PHASES and b in STRENGTH_PHASES for a, b in pairs)


def test_beginner_preset_has_no_deload_and_no_strength_phase():
    beginner = preset_by_code(DEFAULT_FOR_BEGINNER)
    assert "deload" not in beginner.tiers
    assert not STRENGTH_PHASES & set(beginner.tiers)


def test_experienced_default_is_the_linear_four_week_block():
    assert preset_by_code(DEFAULT_FOR_EXPERIENCED).tiers == (
        "easy", "medium", "prefailure", "deload",
    )


def test_unknown_code_raises():
    with pytest.raises(KeyError):
        preset_by_code("nope")


def test_phase_name_is_russian_for_every_tier_in_use():
    used = {tier for preset in MESOCYCLE_PRESETS for tier in preset.tiers}
    for tier in used:
        assert phase_name(tier).strip()
        assert phase_name(tier) != tier
