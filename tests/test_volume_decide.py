"""Чистый решатель объёма: снимок окна -> список правок."""
import pytest

from api.services.volume import params
from api.services.volume.decide import (
    DecisionInput,
    MuscleState,
    decide,
    headline_reason,
)
from api.services.volume.landmarks import Landmarks
from api.services.volume.measure import INDIRECT_WEIGHT

LM = Landmarks(mev=6, mav=12, mrv=18, mev_direct=4, mrv_direct=14)


def state(target=12.0, prescribed=12.0, direct=0.0, indirect=0.0, lm=LM):
    return MuscleState(
        target=target, prescribed=prescribed,
        performed_direct=direct, performed_indirect=indirect, landmarks=lm,
    )


def inp(closed, previous=None, next_prescribed=None, adherence=(1.0,), deload=False):
    return DecisionInput(
        closed=closed,
        previous=previous,
        next_prescribed=next_prescribed or {},
        adherence_ratios=list(adherence),
        is_deload=deload,
    )


def reasons(adjustments):
    return {a.reason_code for a in adjustments}


def test_ceiling_fires_after_one_window():
    closed = {"chest": state(direct=10.0, indirect=20.0)}  # effective 20 > 18
    assert params.REASON_ABOVE_MRV in reasons(decide(inp(closed)))


def test_direct_cap_fires_even_when_effective_is_inside_range():
    # 15 прямых = 15 эффективных, диапазон соблюдён, но прямой потолок 14 пробит.
    closed = {"chest": state(direct=15.0, indirect=0.0)}
    result = reasons(decide(inp(closed)))
    assert params.REASON_DIRECT_ABOVE_CAP in result
    assert params.REASON_ABOVE_MRV not in result


def test_floor_does_not_fire_on_a_single_window():
    closed = {"chest": state(direct=2.0)}
    assert params.REASON_BELOW_MEV not in reasons(decide(inp(closed)))


def test_floor_fires_on_two_consecutive_windows():
    closed = {"chest": state(direct=2.0)}
    previous = {"chest": state(direct=1.0)}
    assert params.REASON_BELOW_MEV in reasons(decide(inp(closed, previous=previous)))


def test_direct_floor_fires_when_effective_is_met_by_indirect_only():
    # 1 прямой + 12 косвенных = 7 эффективных: MEV 6 закрыт, но прямой пол 4 нет.
    closed = {"chest": state(direct=1.0, indirect=12.0)}
    previous = {"chest": state(direct=1.0, indirect=12.0)}
    result = reasons(decide(inp(closed, previous=previous)))
    assert params.REASON_DIRECT_BELOW_FLOOR in result
    assert params.REASON_BELOW_MEV not in result


def test_deload_window_silences_floor_but_not_ceiling():
    closed = {"chest": state(direct=1.0)}
    previous = {"chest": state(direct=1.0)}
    quiet = reasons(decide(inp(closed, previous=previous, deload=True)))
    assert params.REASON_BELOW_MEV not in quiet
    assert params.REASON_DIRECT_BELOW_FLOOR not in quiet

    loud = {"chest": state(direct=16.0)}
    assert params.REASON_DIRECT_ABOVE_CAP in reasons(
        decide(inp(loud, deload=True))
    )


def test_untracked_muscle_never_triggers():
    closed = {"traps": state(target=0.0, prescribed=0.0, direct=0.0)}
    previous = {"traps": state(target=0.0, prescribed=0.0, direct=0.0)}
    assert decide(inp(closed, previous=previous)) == []


def test_prescription_gap_fires_on_next_window():
    closed = {"chest": state(direct=12.0)}
    assert params.REASON_PRESCRIPTION_GAP in reasons(
        decide(inp(closed, next_prescribed={"chest": 8.0}))
    )


def test_muscle_absent_from_next_window_is_the_biggest_gap():
    # Цель 12 при нулевом предписании — самый сильный дефект планирования,
    # а не отсутствие данных. Молчать здесь хуже, чем показать лишнюю правку.
    closed = {"chest": state(target=12.0, direct=12.0)}
    result = decide(inp(closed, next_prescribed={}))
    gap = next(
        a for a in result if a.reason_code == params.REASON_PRESCRIPTION_GAP
    )
    assert gap.delta_sets == 12


def test_prescription_gap_ignores_rounding_noise():
    closed = {"chest": state(direct=12.0)}
    assert params.REASON_PRESCRIPTION_GAP not in reasons(
        decide(inp(closed, next_prescribed={"chest": 11.0}))
    )


def test_adherence_gap_needs_two_low_windows():
    closed = {"chest": state(direct=12.0)}
    one_low = decide(inp(closed, adherence=(0.5, 1.0)))
    assert params.REASON_ADHERENCE_GAP not in reasons(one_low)

    two_low = decide(inp(closed, adherence=(0.5, 0.4)))
    assert params.REASON_ADHERENCE_GAP in reasons(two_low)


def test_headline_prefers_direct_ceiling():
    adjustments = decide(inp({
        "chest": state(direct=16.0),
        "lats": state(direct=1.0),
    }, previous={"lats": state(direct=1.0)}))
    assert headline_reason(adjustments) == params.REASON_DIRECT_ABOVE_CAP


def test_headline_of_empty_list_is_none():
    assert headline_reason([]) is None


def test_ceiling_indirect_excess_targets_budget_not_prescription():
    """Избыток преимущественно косвенный: рычаг лежит не на этой мышце,
    а на базовом упражнении выше по цепочке. Требовать «убери подходы с
    трицепса», когда трицепс забит жимами, — вредный совет, поэтому правка
    должна адресовать бюджет (budget_to_range), а не предписание мышцы.
    """
    # effective = 2 + 40*0.5 = 22 > MRV(18); direct_share = 2/22 ~ 0.09 — явное меньшинство.
    closed = {"chest": state(direct=2.0, indirect=40.0)}
    result = decide(inp(closed))
    adj = next(a for a in result if a.reason_code == params.REASON_ABOVE_MRV)
    assert adj.kind == params.KIND_BUDGET_TO_RANGE


def test_ceiling_direct_excess_targets_prescription_cut():
    """Избыток преимущественно прямой: рычаг лежит на самой мышце, поэтому
    правка режет предписание (prescription_cut), а не уходит в бюджет.
    """
    # effective = 13 + 12*0.5 = 19 > MRV(18); direct_share = 13/19 ~ 0.68 — явное большинство.
    closed = {"chest": state(direct=13.0, indirect=12.0)}
    result = decide(inp(closed))
    adj = next(a for a in result if a.reason_code == params.REASON_ABOVE_MRV)
    assert adj.kind == params.KIND_PRESCRIPTION_CUT


def test_ceiling_direct_share_boundary_falls_to_prescription_cut():
    """Ровно на границе DIRECT_SHARE_MAJORITY_RATIO сравнение ">=" в _ceiling
    относит долю к большинству, поэтому граница уходит в prescription_cut,
    а не в budget_to_range. Значения выведены из самой константы, чтобы
    тест следовал за порогом, если его перекалибруют.
    """
    effective_target = 20.0  # > MRV(18) с запасом
    ratio = params.DIRECT_SHARE_MAJORITY_RATIO
    direct = effective_target * ratio
    indirect = (effective_target - direct) / INDIRECT_WEIGHT
    closed = {"chest": state(direct=direct, indirect=indirect)}
    result = decide(inp(closed))
    adj = next(a for a in result if a.reason_code == params.REASON_ABOVE_MRV)
    assert adj.kind == params.KIND_PRESCRIPTION_CUT


def test_ceiling_direct_cap_and_effective_breach_yield_one_adjustment():
    """Прямой потолок и эффективный потолок нарушены одновременно: правка
    ровно одна, и это правка прямого потолка — прямое превышение
    проверяется первым и возвращается сразу, не размываясь тем, что
    эффективная сумма тоже вне диапазона.
    """
    # direct=16 > mrv_direct(14); effective = 16 + 10*0.5 = 21 > mrv(18) — оба потолка пробиты.
    closed = {"chest": state(direct=16.0, indirect=10.0)}
    result = decide(inp(closed, next_prescribed={"chest": 12.0}))
    assert len(result) == 1
    assert result[0].reason_code == params.REASON_DIRECT_ABOVE_CAP
