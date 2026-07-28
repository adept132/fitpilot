"""%1RM от training max с AMRAP-подходом.

Осмысленна только внутри осознанного силового блока: эвристика эту схему не
выбирает никогда (см. resolve.py). AMRAP — единственный вход, который в этой
схеме двигает working_e1rm, а через него и training max.
"""

from __future__ import annotations

from typing import Optional

from api.services.progression import params
from api.services.progression.rounding import round_down_to_step
from api.services.progression.types import (
    Prescription,
    SchemeContext,
    SetPrescription,
)


def training_max_for(ctx: SchemeContext) -> Optional[float]:
    """90 % от рабочего e1RM, округлённые ВНИЗ.

    Вниз, а не к ближайшему: округление вверх сделало бы 95 % от TM
    недостижимыми и превратило бы каждый цикл в провал.
    """
    if ctx.state.working_e1rm is None:
        return None
    raw = ctx.state.working_e1rm * params.TRAINING_MAX_RATIO
    return round_down_to_step(
        raw, list(ctx.equipment), ctx.unit, ctx.weight_steps or None
    )


def plan(ctx: SchemeContext) -> Prescription:
    tm = training_max_for(ctx)
    if tm is None:
        return Prescription(
            scheme=params.SCHEME_PERCENT_1RM,
            sets=(),
            reason_code="no_basis",
            reason_text=params.REASON_TEXTS["no_basis"],
        )

    rows = params.PERCENT_TABLE.get(
        ctx.phase_effort_tier, params.PERCENT_TABLE["medium"]
    )

    sets = []
    for index, (ratio, reps, kind) in enumerate(rows, start=1):
        weight = round_down_to_step(
            tm * ratio, list(ctx.equipment), ctx.unit, ctx.weight_steps or None
        )
        rep_max = None if kind == "amrap" else reps
        sets.append(
            SetPrescription(index, weight, reps, rep_max, ctx.target_rir, kind)
        )

    return Prescription(
        scheme=params.SCHEME_PERCENT_1RM,
        sets=tuple(sets),
        reason_code="progressed",
        reason_text=params.REASON_TEXTS["progressed"],
        basis={"training_max": tm, "phase": ctx.phase_effort_tier},
    )
