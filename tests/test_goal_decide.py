"""Классификация разрыва и лестница рычагов (P0-12, Задача 3)."""
from datetime import date, timedelta

from api.services.goal import decide, params
from api.services.goal.types import DecisionInput, Rates

TODAY = date(2026, 3, 2)


def _inp(**over) -> DecisionInput:
    base = dict(
        rates=Rates(required=1.2, plan=0.8, ceiling=2.0),
        deadline=TODAY + timedelta(days=90),
        eta=TODAY + timedelta(days=120),
        lift_in_plan=True,
        trend_slope=0.8,
        microcycles_left=12,
        headroom_sets=4,
        scheme="double",
        is_heavy_compound=True,
        rep_max=12,
        target_reps=3,
    )
    base.update(over)
    return DecisionInput(**base)


def test_on_track_is_silence():
    levers, reason = decide.decide(_inp(rates=Rates(required=0.5, plan=0.8, ceiling=2.0)))
    assert levers == []
    assert reason == ""


def test_missing_lift_is_the_first_lever():
    levers, reason = decide.decide(_inp(lift_in_plan=False))
    assert levers[0].kind == params.LEVER_ENSURE_PRESENT
    assert reason == params.REASON_LIFT_MISSING


def test_above_ceiling_gives_no_levers_but_a_reason():
    levers, reason = decide.decide(_inp(rates=Rates(required=3.0, plan=0.8, ceiling=2.0)))
    assert levers == []
    assert reason == params.REASON_ABOVE_CEILING


def test_falling_trend_does_not_accelerate():
    levers, reason = decide.decide(_inp(trend_slope=-0.4))
    assert levers == []
    assert reason == params.REASON_TREND_DOWN


def test_ladder_stops_as_soon_as_gap_is_closed():
    """Мелкий разрыв закрывается одной ступенью, а не всей лестницей."""
    small, _ = decide.decide(
        _inp(rates=Rates(required=0.9, plan=0.8, ceiling=2.0),
             eta=TODAY + timedelta(days=140))
    )
    big, _ = decide.decide(_inp(rates=Rates(required=1.9, plan=0.4, ceiling=2.0)))
    assert 0 < len(small) < len(big)
    assert params.LEVER_STRUCTURAL not in [l.kind for l in small]


def test_ladder_order_follows_params():
    levers, _ = decide.decide(_inp(rates=Rates(required=1.9, plan=0.4, ceiling=2.0)))
    kinds = [l.kind for l in levers]
    positions = [params.LADDER.index(k) for k in kinds]
    assert positions == sorted(positions)


def test_no_sets_lever_without_volume_headroom():
    levers, _ = decide.decide(_inp(headroom_sets=0, rates=Rates(required=1.9, plan=0.4, ceiling=2.0)))
    assert all(l.kind != params.LEVER_SETS for l in levers)


def test_structural_levers_are_blocked_on_short_horizon():
    levers, _ = decide.decide(
        _inp(microcycles_left=1, rates=Rates(required=1.9, plan=0.4, ceiling=2.0))
    )
    assert all(l.kind not in params.STRUCTURAL_LEVERS for l in levers)


def test_both_thresholds_must_fire():
    """Отставание по темпу есть, а по ETA — меньше недели: молчим."""
    levers, reason = decide.decide(
        _inp(rates=Rates(required=0.9, plan=0.8, ceiling=2.0),
             eta=TODAY + timedelta(days=93))
    )
    assert levers == []
    assert reason == ""


def test_levers_are_indexed_from_zero():
    levers, _ = decide.decide(_inp(rates=Rates(required=1.9, plan=0.4, ceiling=2.0)))
    assert [l.index for l in levers] == list(range(len(levers)))
