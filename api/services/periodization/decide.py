"""Чистый решатель периодизации: вход → список предложений.

Никаких действий и никакой БД. Решение о том, ЧТО делать, отделено от того,
КАК это применяется (service.py), ровно как в progression: схемы считают,
reduction решает, репозиторий пишет.
"""

from __future__ import annotations

from typing import Optional

from api.services.periodization import params
from api.services.periodization.types import DecisionInput, Proposal


def decide(inp: DecisionInput) -> list[Proposal]:
    """Все уместные сейчас предложения.

    Порядок важен: завершённый блок закрывает вопрос о досрочной разгрузке —
    предлагать разгрузиться в блоке, который уже кончился, бессмысленно.
    """
    if inp.position.is_complete:
        return _boundary_proposals(inp)

    early = _early_deload(inp)
    if early is not None:
        return [early]

    postpone = _postpone(inp)
    if postpone is not None:
        return [postpone]

    return []


def _triggers_allowed(inp: DecisionInput) -> bool:
    """Предохранители, общие для всех триггеров досрочной разгрузки."""
    if inp.early_deload_used:
        return False
    if inp.position.phase_ordinal < params.MIN_PHASE_ORDINAL_FOR_TRIGGERS:
        return False
    if inp.position.effort_tier == params.DELOAD_TIER:
        return False
    if (
        inp.workouts_to_planned_deload is not None
        and inp.workouts_to_planned_deload <= params.PLANNED_DELOAD_NEAR_WORKOUTS
    ):
        return False
    return True


def _early_deload(inp: DecisionInput) -> Optional[Proposal]:
    """Не более одного предложения: приоритет усталость → плато → готовность.

    Приоритет не косметика: усталость — самый прямой довод, и именно её
    формулировку пользователю полезнее всего увидеть, когда сработало всё сразу.
    """
    if not _triggers_allowed(inp):
        return None

    fatigue = inp.fatigue
    if fatigue.band_known:
        if fatigue.fatigued_days >= params.FATIGUED_DAYS_FOR_DELOAD:
            return _deload_proposal(
                inp, params.REASON_FATIGUE_HIGH, {"fatigued_days": fatigue.fatigued_days}
            )
        if fatigue.sharp_rise:
            return _deload_proposal(inp, params.REASON_LOAD_SPIKE, {"sharp_rise": True})

    plateau = inp.plateau
    if (
        plateau.exercises_with_history >= params.PLATEAU_MIN_EXERCISES
        and plateau.stalled >= plateau.exercises_with_history * params.PLATEAU_STALLED_RATIO
    ):
        return _deload_proposal(
            inp,
            params.REASON_BLOCK_PLATEAU,
            {"stalled": plateau.stalled, "of": plateau.exercises_with_history},
        )

    window = inp.readiness.recent_levels[: params.READINESS_LIMIT_WINDOW]
    limited = sum(1 for level in window if level == "limit")
    if limited >= params.READINESS_LIMIT_COUNT:
        return _deload_proposal(
            inp, params.REASON_READINESS_LIMITED, {"limited_sessions": limited}
        )

    return None


def _deload_proposal(inp: DecisionInput, reason_code: str, evidence: dict) -> Proposal:
    payload = dict(evidence)
    payload["after_phase_number"] = inp.position.phase_number
    return Proposal(
        kind=params.KIND_EARLY_DELOAD, reason_code=reason_code, payload=payload
    )


def _postpone(inp: DecisionInput) -> Optional[Proposal]:
    """Плановая разгрузка на носу, а грузиться было нечем — сдвигаем.

    Зеркало предохранителя из _triggers_allowed: досрочная разгрузка и
    перенос плановой по построению не могут предлагаться одновременно.
    """
    if inp.workouts_to_planned_deload is None:
        return None
    if inp.workouts_to_planned_deload > params.PLANNED_DELOAD_NEAR_WORKOUTS:
        return None

    chronic = inp.fatigue.chronic_level
    baseline = inp.fatigue.chronic_at_block_start
    if chronic is None or not baseline:
        return None
    if chronic / baseline > params.POSTPONE_CHRONIC_RATIO:
        return None

    return Proposal(
        kind=params.KIND_POSTPONE_DELOAD,
        reason_code=params.REASON_LOAD_DROPPED,
        payload={"chronic_ratio": round(chronic / baseline, 3)},
    )


def _boundary_proposals(inp: DecisionInput) -> list[Proposal]:
    """Заглушка границы блока — наполняется в Task 5."""
    return [
        Proposal(
            kind=params.KIND_BLOCK_BOUNDARY,
            reason_code=params.REASON_BLOCK_COMPLETED,
            payload={},
        )
    ]
