"""Свойства, которые обязаны выполняться при любых входных данных.

Тест 2 — прямая защита от того, из-за чего задача и заведена: старый движок
умножал e1RM на коэффициент независимо от результата прошлой сессии.

Исторический вес во всех трёх тестах приводится к сетке шага оборудования
(_on_grid) перед тем, как попасть в SessionFact/anchor: движок хранит вес
прошлой сессии как есть и не перепроверяет его на сетку — anchor у double.py
и fixed_increment.py берётся из факта дословно. В проде туда попадают только
веса, уже прошедшие через round_to_step/round_down_to_step этого же движка,
поэтому вход теста обязан быть таким же реалистичным. Для блочного тренажёра
(шаг 10 lb, всегда в фунтах) круглые килограммовые числа вроде 140.0 на сетку
не попадают вовсе (ближайшая точка — 140.61), и без приведения тест путает
«движок вернул кривой вес» с «тест скормил вес, которого в системе не бывает».
"""

import itertools

import pytest

from api.services.progression import params
from api.services.progression.engine import plan_exercise
from api.services.progression.reduction import apply_reduction
from api.services.progression.rounding import (
    round_down_to_step,
    round_to_step,
    step_kg,
)
from api.services.progression.types import (
    ExerciseHistory,
    Outcome,
    Prescription,
    ProgressionState,
    SchemeContext,
    SessionFact,
    SetFact,
    SetPrescription,
)

EQUIPMENT = [("barbell",), ("dumbbell",), ("block_machine",), ("smith",)]
WEIGHTS = [12.5, 20.0, 40.0, 77.5, 140.0]
REP_PATTERNS = [[12, 12, 12], [10, 9, 7], [8, 8, 8], [5, 4, 3], [3, 2, 1]]

# Допуск на неточность обратной конвертации кг -> lb -> кг при округлении
# ВНИЗ (params.LB_ROUNDING_TOLERANCE): легитимная точка сетки может
# вернуться из round_down_to_step на ~0.013 кг выше себя.
GRID_TOLERANCE_KG = 0.02


def _on_grid(equipment, weight: float) -> float:
    """Ближайшая точка сетки этого оборудования — см. docstring модуля."""
    return round_to_step(weight, list(equipment), "kg", None)


def presc(weight, sets_count=3) -> Prescription:
    return Prescription(
        scheme=params.SCHEME_DOUBLE,
        sets=tuple(
            SetPrescription(n, weight, 8, 12, 2, "normal")
            for n in range(1, sets_count + 1)
        ),
        reason_code="progressed",
        reason_text="x",
    )


def build_ctx(equipment, weight, reps) -> SchemeContext:
    facts = tuple(SetFact(i + 1, weight, r, 2) for i, r in enumerate(reps))
    history = ExerciseHistory(
        exercise_id=1,
        sessions=(SessionFact(1, None, presc(weight, len(reps)), facts),),
    )
    return SchemeContext(
        history=history,
        state=ProgressionState(),
        last_outcome=None,
        target_sets=3,
        rep_min=8,
        rep_max=12,
        rep_range_source=params.REP_SOURCE_FALLBACK,
        target_rir=2,
        equipment=equipment,
        experience_level="intermediate",
        phase_effort_tier="medium",
        days_since_last_session=3,
    )


@pytest.mark.parametrize(
    "equipment,weight,reps", list(itertools.product(EQUIPMENT, WEIGHTS, REP_PATTERNS))
)
def test_prescribed_weight_is_always_on_an_equipment_step(equipment, weight, reps):
    """Свойство 1: движок не выдаёт вес, которого нельзя набрать."""
    weight = _on_grid(equipment, weight)
    p = plan_exercise(build_ctx(equipment, weight, reps))
    for s in p.sets:
        if s.weight_kg is None:
            continue
        # Принадлежность сетке, а не точное равенство свежему округлению:
        # округление вниз идемпотентно (см. docstring модуля и GRID_TOLERANCE_KG).
        floored = round_down_to_step(s.weight_kg, list(equipment), "kg", None)
        assert floored == pytest.approx(s.weight_kg, abs=GRID_TOLERANCE_KG)


@pytest.mark.parametrize(
    "equipment,weight", list(itertools.product(EQUIPMENT, WEIGHTS))
)
def test_weight_never_grows_after_a_miss(equipment, weight):
    """Свойство 2: недобор цели не может привести к росту веса."""
    weight = _on_grid(equipment, weight)
    ctx = build_ctx(equipment, weight, [3, 2, 1])
    p = plan_exercise(ctx)
    assert p.top_weight is not None
    assert p.top_weight <= weight + 1e-6, p.reason_code


@pytest.mark.parametrize(
    "equipment,weight", list(itertools.product(EQUIPMENT, WEIGHTS))
)
def test_reduction_always_lowers_by_at_least_one_step(equipment, weight):
    """Свойство 3: «снизили на ноль» невозможно."""
    weight = _on_grid(equipment, weight)
    ctx = SchemeContext(
        history=ExerciseHistory(exercise_id=1),
        state=ProgressionState(last_top_weight=weight, stalled=True),
        last_outcome=Outcome(status="hit", hit_sets=3, total_sets=3),
        target_sets=3,
        rep_min=8,
        rep_max=12,
        rep_range_source=params.REP_SOURCE_FALLBACK,
        target_rir=2,
        equipment=equipment,
    )
    reduced = apply_reduction(presc(weight), ctx)
    assert reduced.reason_code == "plateau_reset"
    step = step_kg(list(equipment), "kg", None)
    assert reduced.top_weight <= weight - step + 0.01
