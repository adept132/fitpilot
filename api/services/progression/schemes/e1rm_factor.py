"""Бутстрап-схема: e1RM x коэффициент.

Воспроизводит нынешний compute_autoprogression. Применяется, когда в истории
нет ни одного сохранённого предписания и сравнивать факт не с чем: первая
тренировка упражнения, импорт из Strong, ручной лог.
"""

from __future__ import annotations

from typing import Any, Optional

from api.services.progression import params
from api.services.progression.metrics import e1rm, weight_for_e1rm
from api.services.progression.rounding import round_to_step
from api.services.progression.types import Prescription, SchemeContext, SetPrescription

DEFAULT_FACTOR_BY_LEVEL = {
    "beginner": 1.03,
    "intermediate": 1.02,
    "advanced": 1.01,
}
FALLBACK_FACTOR = 1.03


def progression_factor_for(
    experience_level: Optional[str], settings: Optional[dict[str, Any]]
) -> float:
    if settings:
        raw = settings.get("progression_factor")
        if raw is not None:
            try:
                value = float(raw)
                if value > 0:
                    return value
            except (TypeError, ValueError):
                pass
    level = (experience_level or "beginner").strip().lower()
    return DEFAULT_FACTOR_BY_LEVEL.get(level, FALLBACK_FACTOR)


def _no_basis() -> Prescription:
    return Prescription(
        scheme=params.SCHEME_E1RM_FACTOR,
        sets=(),
        reason_code="no_basis",
        reason_text=params.REASON_TEXTS["no_basis"],
    )


def plan(ctx: SchemeContext) -> Prescription:
    base = ctx.state.working_e1rm
    if base is None or ctx.rep_max <= 0:
        return _no_basis()

    modified = base * progression_factor_for(ctx.experience_level, ctx.settings)

    best: Optional[tuple[float, float, int]] = None
    for reps in range(ctx.rep_min, ctx.rep_max + 1):
        weight = round_to_step(
            weight_for_e1rm(modified, reps, ctx.target_rir),
            list(ctx.equipment),
            ctx.unit,
            ctx.weight_steps or None,
        )
        diff = abs(e1rm(weight, reps, ctx.target_rir) - modified)
        if best is None or diff < best[0]:
            best = (diff, weight, reps)

    if best is None:
        return _no_basis()

    _, weight, reps = best
    sets = tuple(
        SetPrescription(n, weight, reps, reps, ctx.target_rir, "normal")
        for n in range(1, max(1, ctx.target_sets) + 1)
    )
    return Prescription(
        scheme=params.SCHEME_E1RM_FACTOR,
        sets=sets,
        reason_code="bootstrap_no_prescription",
        reason_text=params.REASON_TEXTS["bootstrap_no_prescription"],
        basis={"e1rm": base, "modified_target": round(modified, 2)},
    )
