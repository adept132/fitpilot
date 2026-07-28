"""Сравнение факта с предписанием (спека P0-06 §8.1)."""

import pytest

from api.services.progression.state import evaluate, working_sets
from api.services.progression.types import Prescription, SetFact, SetPrescription

STEP = 2.5


def presc(*rows, scheme="double") -> Prescription:
    """rows: (set_number, weight, rep_min, rep_max)"""
    return Prescription(
        scheme=scheme,
        sets=tuple(SetPrescription(n, w, lo, hi, 2, "normal") for n, w, lo, hi in rows),
        reason_code="progressed",
        reason_text="x",
    )


def fact(n, w, reps, rir=2, set_type="normal", anomalous=False) -> SetFact:
    return SetFact(n, w, reps, rir, set_type, anomalous)


def test_all_sets_at_or_above_rep_min_is_hit():
    p = presc((1, 40.0, 8, 12), (2, 40.0, 8, 12))
    out = evaluate(p, [fact(1, 40.0, 11), fact(2, 40.0, 9)], STEP)
    assert out.status == "hit"
    assert out.hit_sets == 2
    assert out.total_sets == 2


def test_short_of_stretched_target_but_above_rep_min_is_still_hit():
    # Предписано 11, взято 9 при rep_min=8 — это не провал.
    p = presc((1, 40.0, 8, 12))
    assert evaluate(p, [fact(1, 40.0, 9)], STEP).status == "hit"


def test_below_rep_min_is_miss():
    p = presc((1, 40.0, 8, 12), (2, 40.0, 8, 12))
    out = evaluate(p, [fact(1, 40.0, 8), fact(2, 40.0, 6)], STEP)
    assert out.status == "miss"
    assert out.miss_sets == 1
    assert out.hit_sets == 1


def test_lighter_weight_is_deviated_not_miss():
    # Пользователь сам снизил нагрузку — наказывать вторично нельзя.
    p = presc((1, 40.0, 8, 12))
    out = evaluate(p, [fact(1, 30.0, 8)], STEP)
    assert out.status == "deviated"


def test_weight_within_one_step_is_not_deviation():
    p = presc((1, 40.0, 8, 12))
    assert evaluate(p, [fact(1, 42.5, 9)], STEP).status != "deviated"


def test_all_sets_above_rep_max_is_overshoot():
    p = presc((1, 40.0, 8, 10))
    assert evaluate(p, [fact(1, 40.0, 14)], STEP).status == "overshoot"


def test_open_rep_max_never_overshoots():
    p = Prescription(
        scheme="percent_1rm",
        sets=(SetPrescription(1, 90.0, 1, None, 0, "amrap"),),
        reason_code="progressed",
        reason_text="x",
    )
    assert evaluate(p, [fact(1, 90.0, 7)], STEP).status == "hit"


def test_open_rep_max_below_rep_min_is_miss():
    p = Prescription(
        scheme="percent_1rm",
        sets=(SetPrescription(1, 90.0, 3, None, 0, "amrap"),),
        reason_code="progressed",
        reason_text="x",
    )
    assert evaluate(p, [fact(1, 90.0, 2)], STEP).status == "miss"


def test_no_prescription_is_no_basis():
    assert evaluate(None, [fact(1, 40.0, 10)], STEP).status == "no_basis"


def test_no_facts_is_skipped():
    assert evaluate(presc((1, 40.0, 8, 12)), [], STEP).status == "skipped"


def test_all_anomalous_is_no_basis():
    # Один ошибочный ввод «700 кг» не должен ни разогнать прогрессию, ни откатить.
    p = presc((1, 40.0, 8, 12))
    out = evaluate(p, [fact(1, 700.0, 10, anomalous=True)], STEP)
    assert out.status == "no_basis"


def test_warmup_and_drop_sets_are_ignored():
    p = presc((1, 40.0, 8, 12))
    facts = [
        fact(0, 20.0, 15, set_type="warmup"),
        fact(1, 40.0, 10),
        fact(2, 30.0, 12, set_type="drop"),
    ]
    out = evaluate(p, facts, STEP)
    assert out.status == "hit"
    assert out.total_sets == 1


def test_extra_sets_reuse_last_known_prescription():
    p = presc((1, 40.0, 8, 12))
    out = evaluate(p, [fact(1, 40.0, 10), fact(2, 40.0, 9)], STEP)
    assert out.total_sets == 2
    assert out.status == "hit"


def test_achieved_e1rm_is_max_over_working_sets():
    p = presc((1, 40.0, 8, 12), (2, 40.0, 8, 12))
    out = evaluate(p, [fact(1, 40.0, 10, rir=2), fact(2, 40.0, 8, rir=2)], STEP)
    assert out.achieved_e1rm == pytest.approx(40.0 * (1 + 12 / 30))


def test_working_sets_filters_incomplete_data():
    facts = [fact(1, None, 10), fact(2, 40.0, 0), fact(3, 40.0, 10)]
    assert [s.set_number for s in working_sets(facts)] == [3]


def test_partial_overshoot_with_hit_is_still_hit():
    # Один подход выше rep_max, другой в диапазоне — это НЕ overshoot.
    # overshoot выставляется только когда абсолютно все засчитанные подходы
    # ушли выше потолка; частичный перебор не даёт оснований для ускоренного
    # роста веса, поэтому итоговый статус — обычный "hit".
    p = presc((1, 40.0, 8, 10), (2, 40.0, 8, 10))
    out = evaluate(p, [fact(1, 40.0, 14), fact(2, 40.0, 9)], STEP)
    assert out.status == "hit"
    assert out.hit_sets == 2
    assert out.total_sets == 2


def test_bodyweight_prescription_with_no_weight_fact_is_hit_not_skipped():
    # Предписание без веса (упражнение со своим весом) + факт без веса —
    # require_weight решается по предписанию, а не по факту (находка 1).
    p = presc((1, None, 8, 12))
    out = evaluate(p, [fact(1, None, 10)], STEP)
    assert out.status == "hit"


def test_weighted_prescription_with_no_weight_fact_is_still_skipped():
    # Предписание С весом + факт без веса — подход отброшен как неполный,
    # поведение как до фикса находки 1.
    p = presc((1, 40.0, 8, 12))
    out = evaluate(p, [fact(1, None, 10)], STEP)
    assert out.status == "skipped"


def test_partial_anomaly_is_evaluated_on_normal_set_only():
    # Один подход аномален (абсурдный вес), второй — нормальный и выполненный.
    # Аномальный исключается из оценки, статус определяется только по
    # оставшемуся нормальному подходу.
    p = presc((1, 40.0, 8, 12), (2, 40.0, 8, 12))
    out = evaluate(
        p,
        [fact(1, 700.0, 10, anomalous=True), fact(2, 40.0, 10)],
        STEP,
    )
    assert out.status == "hit"
    assert out.total_sets == 1
