"""Оркестратор движка прогрессии.

Порядок шагов (спеки P0-06 §5.4 и P0-07 §5.2):
  1) оценить факт прошлой сессии против её предписания;
  2) восстановить состояние из истории;
  3) выбрать схему;
  4) схема считает, КАК расти;
  5) общий слой решает, расти ли ВООБЩЕ (объективные правила);
  6) субъективный потолок — min() поверх результата таблицы;
  7) подрезка объёма по вердикту готовности.

Шаги 6 и 7 при readiness_source=None — тождественное преобразование:
это строгий инвариант, и он же делает безопасной офлайн-накладку.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from api.services.progression.reduction import apply_reduction, apply_readiness_cap
from api.services.progression.resolve import resolve_scheme
from api.services.progression.rounding import step_kg
from api.services.progression.schemes import plan_with
from api.services.progression.state import evaluate, rebuild_state
from api.services.progression.types import (
    Outcome,
    Prescription,
    SchemeContext,
)
from api.services.progression.volume import apply_volume_trim


def _latest_outcome(ctx: SchemeContext, step: float) -> Optional[Outcome]:
    """Вердикт по самой свежей сессии, в которой были рабочие подходы."""
    for session in ctx.history.sessions:
        outcome = evaluate(session.prescription, session.sets, step)
        if outcome.status != "skipped":
            return outcome
    return None


def _last_session_skipped(ctx: SchemeContext, step: float) -> bool:
    """Самая свежая сессия была без залогированных рабочих подходов.

    Отдельно от _latest_outcome намеренно: тот пролистывает пропуски в
    поисках последней РЕЗУЛЬТАТИВНОЙ сессии, и менять его семантику ради
    одного правила нельзя — на нём стоят принятые тесты P0-06.
    """
    if not ctx.history.sessions:
        return False
    newest = ctx.history.sessions[0]
    return evaluate(newest.prescription, newest.sets, step).status == "skipped"


def plan_exercise(
    ctx: SchemeContext,
    override: Optional[str] = None,
    provisional: bool = False,
) -> Prescription:
    """Предписание на упражнение. Единственная точка входа движка."""
    step = step_kg(list(ctx.equipment), ctx.unit, ctx.weight_steps or None)

    outcome = _latest_outcome(ctx, step)
    state = rebuild_state(ctx.history, step)
    enriched = replace(
        ctx,
        state=state,
        last_outcome=outcome,
        last_session_skipped=_last_session_skipped(ctx, step),
    )

    scheme = resolve_scheme(enriched, override)
    prescription = plan_with(scheme, enriched)
    prescription = apply_reduction(prescription, enriched)
    prescription = apply_readiness_cap(prescription, enriched)
    prescription = apply_volume_trim(prescription, enriched)

    if provisional:
        prescription = replace(prescription, provisional=True)
    return prescription
