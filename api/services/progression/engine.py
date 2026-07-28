"""Оркестратор движка прогрессии.

Порядок шагов (спека §5.4):
  1) оценить факт прошлой сессии против её предписания;
  2) восстановить состояние из истории;
  3) выбрать схему;
  4) схема считает, КАК расти;
  5) общий слой решает, расти ли ВООБЩЕ.

Шагов 1 и 5 в старом движке не было вовсе — он умножал на коэффициент
независимо от того, справился ли пользователь.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from api.services.progression.reduction import apply_reduction
from api.services.progression.resolve import resolve_scheme
from api.services.progression.rounding import step_kg
from api.services.progression.schemes import plan_with
from api.services.progression.state import evaluate, rebuild_state
from api.services.progression.types import (
    Outcome,
    Prescription,
    SchemeContext,
)


def _latest_outcome(ctx: SchemeContext, step: float) -> Optional[Outcome]:
    """Вердикт по самой свежей сессии, в которой были рабочие подходы."""
    for session in ctx.history.sessions:
        outcome = evaluate(session.prescription, session.sets, step)
        if outcome.status != "skipped":
            return outcome
    return None


def plan_exercise(
    ctx: SchemeContext,
    override: Optional[str] = None,
    provisional: bool = False,
) -> Prescription:
    """Предписание на упражнение. Единственная точка входа движка."""
    step = step_kg(list(ctx.equipment), ctx.unit, ctx.weight_steps or None)

    outcome = _latest_outcome(ctx, step)
    state = rebuild_state(ctx.history, step)
    enriched = replace(ctx, state=state, last_outcome=outcome)

    scheme = resolve_scheme(enriched, override)
    prescription = plan_with(scheme, enriched)
    prescription = apply_reduction(prescription, enriched)

    if provisional:
        prescription = replace(prescription, provisional=True)
    return prescription
