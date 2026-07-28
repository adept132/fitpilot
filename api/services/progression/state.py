"""Оценка результата и восстановление состояния прогрессии.

evaluate() сравнивает факт с предписанием. Ключевая асимметрия (спека §8.1):
rep_max — планка повышения веса, rep_min — планка провала. Недобор до
растянутой цели при взятом rep_min провалом не считается.
"""

from __future__ import annotations

from typing import Optional, Sequence

from api.services.progression import params
from api.services.progression.metrics import e1rm
from api.services.progression.types import (
    ExerciseHistory,
    Outcome,
    Prescription,
    ProgressionState,
    SetFact,
    SetPrescription,
)

_IGNORED_SET_TYPES = {"warmup", "warmup_effort", "drop"}


def working_sets(facts: Sequence[SetFact]) -> list[SetFact]:
    """Подходы, пригодные для оценки: без разминки, дропов и пустых значений.

    Аномальные оставляем — их отсеивает evaluate(), которому нужно отличить
    «все подходы аномальны» от «подходов не было».
    """
    result = []
    for s in facts:
        if (s.set_type or "normal").lower() in _IGNORED_SET_TYPES:
            continue
        if s.weight_kg is None or s.reps is None:
            continue
        if s.reps <= 0:
            continue
        result.append(s)
    return result


def _prescription_for(
    prescription: Prescription, set_number: int
) -> Optional[SetPrescription]:
    """Предписание на подход. Лишние подходы берут цель последнего известного."""
    if not prescription.sets:
        return None
    for sp in prescription.sets:
        if sp.set_number == set_number:
            return sp
    return prescription.sets[-1]


def evaluate(
    prescription: Optional[Prescription],
    facts: Sequence[SetFact],
    step_kg: float,
) -> Outcome:
    """Вердикт по сессии. Приоритет: no_basis, deviated, miss, overshoot, hit."""
    if prescription is None or not prescription.sets:
        return Outcome(status="no_basis")

    candidates = working_sets(facts)
    if not candidates:
        return Outcome(status="skipped")

    usable = [s for s in candidates if not s.is_anomalous]
    if not usable:
        return Outcome(status="no_basis")

    hit = miss = deviated = overshoot = 0
    achieved: Optional[float] = None

    for s in usable:
        sp = _prescription_for(prescription, s.set_number)
        if sp is None:
            continue

        value = e1rm(float(s.weight_kg), int(s.reps), int(s.rir))
        achieved = value if achieved is None else max(achieved, value)

        if sp.weight_kg is not None and abs(float(s.weight_kg) - sp.weight_kg) > step_kg:
            deviated += 1
            continue
        if s.reps < sp.rep_min:
            miss += 1
            continue
        if sp.rep_max is not None and s.reps > sp.rep_max:
            overshoot += 1
            hit += 1
            continue
        hit += 1

    total = hit + miss + deviated

    if deviated:
        status = "deviated"
    elif miss:
        status = "miss"
    elif overshoot and overshoot == hit:
        status = "overshoot"
    else:
        status = "hit"

    return Outcome(
        status=status,
        hit_sets=hit,
        miss_sets=miss,
        total_sets=total,
        achieved_e1rm=achieved,
    )
