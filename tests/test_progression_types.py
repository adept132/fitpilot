"""Типы движка прогрессии: сериализация предписания и целостность констант."""

import pytest

from api.services.progression import params
from api.services.progression.types import (
    ENGINE_VERSION,
    Prescription,
    SetPrescription,
)


def _sample() -> Prescription:
    return Prescription(
        scheme="double",
        sets=(
            SetPrescription(1, 40.0, 8, 12, 2, "normal"),
            SetPrescription(2, 40.0, 8, None, 1, "amrap"),
        ),
        reason_code="progressed",
        reason_text="Все подходы дошли до потолка — добавили вес.",
        basis={"e1rm": 55.0},
        provisional=True,
    )


def test_prescription_round_trip_preserves_everything():
    original = _sample()
    restored = Prescription.from_dict(original.to_dict())
    assert restored == original


def test_prescription_to_dict_is_json_serializable():
    import json

    raw = json.dumps(_sample().to_dict())
    assert Prescription.from_dict(json.loads(raw)) == _sample()


def test_open_rep_max_survives_round_trip():
    restored = Prescription.from_dict(_sample().to_dict())
    assert restored.sets[1].rep_max is None


def test_engine_version_is_one():
    assert ENGINE_VERSION == 1


def test_prescription_is_immutable():
    with pytest.raises(Exception):
        _sample().scheme = "double_v2"


def test_every_reason_code_has_text():
    expected = {
        "progressed",
        "hold_after_miss",
        "repeated_miss",
        "plateau_reset",
        "deload_phase",
        "weight_deviation",
        "layoff",
        "bootstrap_no_prescription",
        "needs_external_load",
        "no_basis",
    }
    assert set(params.REASON_TEXTS) == expected
    assert all(text.strip() for text in params.REASON_TEXTS.values())


def test_tier_rep_fallback_covers_three_tiers():
    assert params.TIER_REP_FALLBACK == {1: (6, 8), 2: (8, 12), 3: (12, 15)}


def test_percent_table_last_set_is_amrap_outside_deload():
    for tier, rows in params.PERCENT_TABLE.items():
        if tier == "deload":
            continue
        assert rows[-1][2] == "amrap", tier
