"""Свойства, которые обязаны выполняться при любых входных данных.

Тест 1 (сетка) — предписанный вес обязан быть неподвижной точкой
round_to_step (округление к БЛИЖАЙШЕМУ шагу — каноническая операция «какой
вес можно набрать»; в отличие от округления ВНИЗ, на неё не влияет допуск
LB_ROUNDING_TOLERANCE). Раньше тест проверял идемпотентность
round_down_to_step, а входные веса заранее приводились к сетке — то есть
по сути дублировал tests/test_progression_metrics.py::
test_round_down_to_step_is_idempotent и не был чувствителен к сквозным
веткам, переносящим исторический/фактический вес как есть (Находка 2
ревью Задачи 12, P0-06). Параметризация теперь несёт и заведомо НЕ-
сеточные веса (OFF_GRID_WEIGHTS) без предварительного приведения — именно
они нагружали double.plan() в не-потолочной ветке и
reduction._with_weight (deload_phase/hold_after_miss): оба места
переносили anchor без округления. Это был реальный дефект — исправлен в
api/services/progression/schemes/double.py и
api/services/progression/reduction.py тем же коммитом, что и этот тест.
Ветка reduction «weight_deviation» СОЗНАТЕЛЬНО не приведена к сетке (там
это факт того, что пользователь реально поднял, а не будущее
предписание) — приведение сломало бы принятый тест
test_deviation_reanchors_to_actual_weight_from_history; подробности в
комментарии на месте вызова в reduction.py и в отчёте
p0-06-task-12-report.md, раздел «Фиксы по ревью», Находка 2.

Тесты 2 и 3 по-прежнему приводят входной вес к сетке (_on_grid): они
конструируют сценарий вокруг ОДНОГО конкретного прошлого веса, и если бы
он сам не существовал в системе, падение означало бы не «движок сломан»,
а «тест скормил нереалистичный вход» — здесь это приведение осмысленно
и его снятие ничего бы не проверило.

Тест 2 — прямая защита от того, из-за чего задача и заведена: старый
движок умножал e1RM на коэффициент независимо от результата прошлой
сессии. Раньше параметризация всегда резолвилась в "double" с паттерном
повторов [3, 2, 1] — в этой ветке double.plan() структурно не поднимает
вес (ceiling_reached=False), и свойство выполнялось «бесплатно», ещё до
слоя снижения (Находка 1 ревью Задачи 12). Теперь параметризация несёт
три группы случаев:
  - MISS_BASE — исходный кейс (double, [3, 2, 1]), сохранён как базовая
    линия, но рост в нём и так невозможен структурно;
  - MISS_FIXED_INCREMENT — новичок, тяжёлая штанговая база,
    fatigue_tier=1, rep_min == rep_max: резолвится в "fixed_increment",
    где рост управляется _last_session_completed;
  - MISS_DOUBLE_REP_RANGE_CHANGE — смена диапазона повторов между
    сессиями (переход блока tier3 (12-15) -> tier1 (6-8), см.
    params.TIER_REP_FALLBACK): фактические повторы прошлой сессии
    достаточны для ПОТОЛКА ТЕКУЩЕГО (нового) диапазона —
    double.plan() готов поднять вес по ceiling_reached, — но недостаточны
    для rep_min ИСТОРИЧЕСКОГО предписания, под которое сессия была
    прописана: это объективный недобор цели прошлой сессии. Это
    единственная из трёх групп, где схема САМА, без
    reduction.apply_reduction, действительно поднимает вес — именно на
    ней тест краснеет, если apply_reduction в engine.py закомментировать
    (проверено вручную, см. отчёт p0-06-task-12-report.md, раздел
    «Фиксы по ревью», Находка 1).
"""

import itertools

import pytest

from api.services.progression import params
from api.services.progression.engine import plan_exercise
from api.services.progression.reduction import apply_reduction
from api.services.progression.rounding import round_to_step, step_kg
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
# Заведомо НЕ на сетке ни для одного оборудования из EQUIPMENT (шаг 2.5 кг
# для штанги/гантелей/смита, ~4.53 кг для блочного тренажёра 10 lb) — см.
# docstring модуля, тест 1.
OFF_GRID_WEIGHTS = [41.3, 13.1, 28.7]
REP_PATTERNS = [[12, 12, 12], [10, 9, 7], [8, 8, 8], [5, 4, 3], [3, 2, 1]]

_BARBELL_LIKE_EQUIPMENT = [("barbell",), ("smith",)]


def _on_grid(equipment, weight: float) -> float:
    """Ближайшая точка сетки этого оборудования — см. docstring модуля."""
    return round_to_step(weight, list(equipment), "kg", None)


def presc(weight, sets_count=3, rep_min=8, rep_max=12) -> Prescription:
    return Prescription(
        scheme=params.SCHEME_DOUBLE,
        sets=tuple(
            SetPrescription(n, weight, rep_min, rep_max, 2, "normal")
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


def build_ctx_miss(
    equipment,
    weight,
    reps,
    *,
    rep_min=8,
    rep_max=12,
    hist_rep_min=None,
    hist_rep_max=None,
    experience_level="intermediate",
    fatigue_tier=2,
) -> SchemeContext:
    """Контекст для теста 2: текущий диапазон повторов (rep_min/rep_max)
    может отличаться от диапазона, под который была прописана прошлая
    сессия (hist_rep_min/hist_rep_max, по умолчанию совпадает с текущим —
    старое поведение теста). Расхождение — см. группу
    MISS_DOUBLE_REP_RANGE_CHANGE в docstring модуля.
    """
    hist_rep_min = rep_min if hist_rep_min is None else hist_rep_min
    hist_rep_max = rep_max if hist_rep_max is None else hist_rep_max
    facts = tuple(SetFact(i + 1, weight, r, 2) for i, r in enumerate(reps))
    history = ExerciseHistory(
        exercise_id=1,
        sessions=(
            SessionFact(
                1, None, presc(weight, len(reps), hist_rep_min, hist_rep_max), facts
            ),
        ),
    )
    return SchemeContext(
        history=history,
        state=ProgressionState(),
        last_outcome=None,
        target_sets=3,
        rep_min=rep_min,
        rep_max=rep_max,
        rep_range_source=params.REP_SOURCE_FALLBACK,
        target_rir=2,
        equipment=equipment,
        experience_level=experience_level,
        fatigue_tier=fatigue_tier,
        phase_effort_tier="medium",
        days_since_last_session=3,
    )


@pytest.mark.parametrize(
    "equipment,weight,reps",
    list(itertools.product(EQUIPMENT, WEIGHTS, REP_PATTERNS))
    + list(itertools.product(EQUIPMENT, OFF_GRID_WEIGHTS, REP_PATTERNS)),
)
def test_prescribed_weight_is_always_on_an_equipment_step(equipment, weight, reps):
    """Свойство 1: движок не выдаёт вес, которого нельзя набрать.

    Вес НЕ приводится к сетке перед вызовом движка (в отличие от тестов
    2 и 3) — см. docstring модуля: часть значений (OFF_GRID_WEIGHTS)
    специально выбрана вне сетки, чтобы нагрузить сквозные ветки.
    """
    p = plan_exercise(build_ctx(equipment, weight, reps))
    for s in p.sets:
        if s.weight_kg is None:
            continue
        # Неподвижная точка round_to_step — округления к БЛИЖАЙШЕМУ шагу,
        # а не идемпотентность round_down_to_step (см. docstring модуля).
        assert s.weight_kg == pytest.approx(
            round_to_step(s.weight_kg, list(equipment), "kg", None), abs=1e-6
        )


MISS_BASE = [
    pytest.param(equipment, weight, [3, 2, 1], {}, id=f"base-{equipment[0]}-{weight}")
    for equipment, weight in itertools.product(EQUIPMENT, WEIGHTS)
]

MISS_FIXED_INCREMENT = [
    pytest.param(
        equipment,
        weight,
        [3, 2, 1],
        dict(rep_min=8, rep_max=8, experience_level="beginner", fatigue_tier=1),
        id=f"fixed_increment-{equipment[0]}-{weight}",
    )
    for equipment, weight in itertools.product(_BARBELL_LIKE_EQUIPMENT, WEIGHTS)
]

_TIER3_REP_MIN, _TIER3_REP_MAX = params.TIER_REP_FALLBACK[3]
_TIER1_REP_MIN, _TIER1_REP_MAX = params.TIER_REP_FALLBACK[1]

MISS_DOUBLE_REP_RANGE_CHANGE = [
    pytest.param(
        equipment,
        weight,
        [_TIER1_REP_MAX, _TIER1_REP_MAX, _TIER1_REP_MAX],
        dict(
            rep_min=_TIER1_REP_MIN,
            rep_max=_TIER1_REP_MAX,
            hist_rep_min=_TIER3_REP_MIN,
            hist_rep_max=_TIER3_REP_MAX,
        ),
        id=f"double_rep_range_change-{equipment[0]}-{weight}",
    )
    for equipment, weight in itertools.product(EQUIPMENT, WEIGHTS)
]


@pytest.mark.parametrize(
    "equipment,weight,reps,overrides",
    MISS_BASE + MISS_FIXED_INCREMENT + MISS_DOUBLE_REP_RANGE_CHANGE,
)
def test_weight_never_grows_after_a_miss(equipment, weight, reps, overrides):
    """Свойство 2: недобор цели не может привести к росту веса.

    См. docstring модуля — три группы кейсов бьют в разные ветки движка,
    и только MISS_DOUBLE_REP_RANGE_CHANGE ловит регрессию без
    reduction.apply_reduction (Находка 1 ревью Задачи 12).
    """
    weight = _on_grid(equipment, weight)
    ctx = build_ctx_miss(equipment, weight, reps, **overrides)
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
