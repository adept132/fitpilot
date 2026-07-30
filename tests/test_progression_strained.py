"""Работа в отказ при выполненной цели (спека P0-07 §8.1)."""

import pytest

from api.services.progression import params
from api.services.progression.state import evaluate
from api.services.progression.types import Prescription, SetFact, SetPrescription

STEP = 2.5


def presc(*rows, rir=2) -> Prescription:
    """rows: (set_number, weight, rep_min, rep_max)"""
    return Prescription(
        scheme="double",
        sets=tuple(
            SetPrescription(n, w, lo, hi, rir, "normal") for n, w, lo, hi in rows
        ),
        reason_code="progressed",
        reason_text="x",
    )


def fact(n, w, reps, rir=2) -> SetFact:
    return SetFact(n, w, reps, rir, "normal", False)


def test_all_sets_to_failure_is_strained():
    p = presc((1, 40.0, 8, 12), (2, 40.0, 8, 12))
    out = evaluate(p, [fact(1, 40.0, 10, rir=0), fact(2, 40.0, 9, rir=0)], STEP)
    assert out.status == "strained"
    assert out.strained_sets == 2


def test_half_the_sets_to_failure_is_enough():
    p = presc((1, 40.0, 8, 12), (2, 40.0, 8, 12))
    out = evaluate(p, [fact(1, 40.0, 10, rir=0), fact(2, 40.0, 9, rir=2)], STEP)
    assert out.status == "strained"


def test_a_single_failure_set_out_of_three_is_normal_practice():
    p = presc((1, 40.0, 8, 12), (2, 40.0, 8, 12), (3, 40.0, 8, 12))
    out = evaluate(
        p,
        [fact(1, 40.0, 10, rir=2), fact(2, 40.0, 9, rir=2), fact(3, 40.0, 8, rir=0)],
        STEP,
    )
    assert out.status == "hit"
    assert out.strained_sets == 1


def test_failure_is_not_strain_when_it_was_prescribed():
    # Предписали RIR 1 — работа на 0 это норма, а не сигнал перегруза.
    p = presc((1, 40.0, 8, 12), rir=1)
    out = evaluate(p, [fact(1, 40.0, 10, rir=0)], STEP)
    assert out.status == "hit"
    assert out.strained_sets == 0


def test_amrap_at_rir_zero_is_not_strain():
    # У percent_1rm AMRAP предписан с RIR 0 — в этом весь его смысл.
    p = Prescription(
        scheme="percent_1rm",
        sets=(SetPrescription(1, 90.0, 1, None, 0, "amrap"),),
        reason_code="progressed",
        reason_text="x",
    )
    out = evaluate(p, [fact(1, 90.0, 5, rir=0)], STEP)
    assert out.status == "hit"
    assert out.strained_sets == 0


def test_miss_outranks_strain():
    # Недобор важнее: он про невыполнение, а strain — про цену выполнения.
    p = presc((1, 40.0, 8, 12), (2, 40.0, 8, 12))
    out = evaluate(p, [fact(1, 40.0, 10, rir=0), fact(2, 40.0, 5, rir=0)], STEP)
    assert out.status == "miss"


def test_deviation_outranks_strain():
    p = presc((1, 40.0, 8, 12))
    out = evaluate(p, [fact(1, 25.0, 10, rir=0)], STEP)
    assert out.status == "deviated"


def test_strain_outranks_overshoot():
    p = presc((1, 40.0, 8, 10))
    out = evaluate(p, [fact(1, 40.0, 14, rir=0)], STEP)
    assert out.status == "strained"


def test_anomalous_failure_sets_do_not_count():
    p = presc((1, 40.0, 8, 12))
    out = evaluate(p, [SetFact(1, 700.0, 10, 0, "normal", True)], STEP)
    assert out.status == "no_basis"
    assert out.strained_sets == 0


def test_thresholds_are_named_constants():
    assert params.STRAIN_SET_RATIO == 0.5
    assert params.STRAIN_MIN_PRESCRIBED_RIR == 2
