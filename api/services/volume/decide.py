"""Чистый решатель объёма: вход -> список правок.

Никаких действий и никакой БД. Решение о том, ЧТО делать, отделено от того,
КАК это применяется (service.py) — ровно как в progression и periodization.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from api.services.volume import params
from api.services.volume.landmarks import Landmarks
from api.services.volume.measure import MuscleContribution


@dataclass(frozen=True)
class MuscleState:
    target: float
    prescribed: float
    performed_direct: float
    performed_indirect: float
    landmarks: Landmarks

    @property
    def performed_effective(self) -> float:
        return MuscleContribution(
            direct=self.performed_direct, indirect=self.performed_indirect
        ).effective


@dataclass(frozen=True)
class DecisionInput:
    closed: dict[str, MuscleState]
    previous: Optional[dict[str, MuscleState]]
    next_prescribed: dict[str, float]
    adherence_ratios: list[float]  # новые первыми
    is_deload: bool


@dataclass(frozen=True)
class Adjustment:
    kind: str
    muscle: Optional[str]
    reason_code: str
    delta_sets: int
    detail: dict = field(default_factory=dict)


def decide(inp: DecisionInput) -> list[Adjustment]:
    """Все уместные сейчас правки. Порядок — по приоритету оснований."""
    result: list[Adjustment] = []

    for muscle, now in inp.closed.items():
        if now.target <= 0:
            # Мышца не тренируется по бюджету: судить не о чем.
            continue
        result.extend(_ceiling(muscle, now))
        result.extend(_floor(muscle, now, inp))

    result.extend(_prescription_gap(inp))
    result.extend(_adherence(inp))

    order = {code: i for i, code in enumerate(params.REASON_PRIORITY)}
    result.sort(key=lambda a: order.get(a.reason_code, len(order)))
    return result


def _ceiling(muscle: str, now: MuscleState) -> list[Adjustment]:
    """Потолок реагирует с первого окна.

    Прямое превышение проверяется ОТДЕЛЬНО и не размывается тем, что
    эффективная сумма ещё в диапазоне: десять прямых подходов и двадцать
    косвенных дают одинаковые десять эффективных, но локальная усталость
    у них разная.
    """
    lm = now.landmarks

    if now.performed_direct > lm.mrv_direct:
        return [Adjustment(
            kind=params.KIND_PRESCRIPTION_CUT,
            muscle=muscle,
            reason_code=params.REASON_DIRECT_ABOVE_CAP,
            delta_sets=-int(math.ceil(now.performed_direct - lm.mrv_direct)),
            detail={"direct": now.performed_direct, "cap": lm.mrv_direct},
        )]

    effective = now.performed_effective
    if effective > lm.mrv:
        excess = effective - lm.mrv
        direct_share = (
            now.performed_direct / effective if effective > 0 else 0.0
        )
        # Избыток преимущественно косвенный — рычаг лежит не на этой мышце,
        # а на базовом упражнении выше по цепочке. Требовать «убери подходы
        # с трицепса», когда трицепс забит жимами, — вредный совет, поэтому
        # правка предлагается на бюджет, а не на предписание мышцы.
        kind = (
            params.KIND_PRESCRIPTION_CUT
            if direct_share >= params.DIRECT_SHARE_MAJORITY_RATIO
            else params.KIND_BUDGET_TO_RANGE
        )
        return [Adjustment(
            kind=kind,
            muscle=muscle,
            reason_code=params.REASON_ABOVE_MRV,
            delta_sets=-int(math.ceil(excess)),
            detail={
                "effective": effective,
                "mrv": lm.mrv,
                "direct_share": round(direct_share, 2),
            },
        )]
    return []


def _floor(muscle: str, now: MuscleState, inp: DecisionInput) -> list[Adjustment]:
    """Пол требует подтверждения вторым окном и молчит в разгрузку.

    В окне разгрузки объём снижен намеренно, и требовать добора значит
    ломать разгрузку.
    """
    if inp.is_deload:
        return []
    if params.FLOOR_REQUIRES_PREVIOUS_WINDOW:
        if inp.previous is None:
            return []
        before = inp.previous.get(muscle)
        if before is None:
            return []
    else:
        before = None

    lm = now.landmarks

    if now.performed_effective < lm.mev:
        if before is not None and before.performed_effective >= before.landmarks.mev:
            return []
        return [Adjustment(
            kind=params.KIND_PRESCRIPTION_ADD,
            muscle=muscle,
            reason_code=params.REASON_BELOW_MEV,
            delta_sets=int(math.ceil(lm.mev - now.performed_effective)),
            detail={"effective": now.performed_effective, "mev": lm.mev},
        )]

    if now.performed_direct < lm.mev_direct:
        if before is not None and before.performed_direct >= before.landmarks.mev_direct:
            return []
        return [Adjustment(
            kind=params.KIND_PRESCRIPTION_ADD,
            muscle=muscle,
            reason_code=params.REASON_DIRECT_BELOW_FLOOR,
            delta_sets=int(math.ceil(lm.mev_direct - now.performed_direct)),
            detail={"direct": now.performed_direct, "floor": lm.mev_direct},
        )]

    return []


def _prescription_gap(inp: DecisionInput) -> list[Adjustment]:
    """Дефект планирования: следующее окно не добирает до цели.

    Детерминирован — считается по уже сгенерированным дням, а не
    экстраполируется, поэтому шума не даёт и срабатывает с первого окна.
    """
    result: list[Adjustment] = []
    for muscle, now in inp.closed.items():
        if now.target <= 0:
            continue
        # Отсутствие мышцы в предписании следующего окна — НЕ «нет данных»,
        # а нулевое предписание при ненулевой цели, то есть самый сильный
        # разрыв планирования. Раньше он молча пропускался.
        planned = inp.next_prescribed.get(muscle, 0.0)
        gap = now.target - planned
        if gap >= params.PRESCRIPTION_GAP_MIN_SETS:
            result.append(Adjustment(
                kind=params.KIND_PRESCRIPTION_ADD,
                muscle=muscle,
                reason_code=params.REASON_PRESCRIPTION_GAP,
                delta_sets=int(math.floor(gap)),
                detail={"target": now.target, "prescribed": planned},
            ))
    return result


def _adherence(inp: DecisionInput) -> list[Adjustment]:
    """Расписание не выполняется — предложить привести цель к реальности.

    Одно окно ниже порога — это отпуск, два подряд — это расписание.
    """
    window = inp.adherence_ratios[: params.WINDOWS_FOR_ADHERENCE_TRIGGER]
    if len(window) < params.WINDOWS_FOR_ADHERENCE_TRIGGER:
        return []
    if any(ratio >= params.ADHERENCE_MIN_RATIO for ratio in window):
        return []
    return [Adjustment(
        kind=params.KIND_BUDGET_TO_FREQUENCY,
        muscle=None,
        reason_code=params.REASON_ADHERENCE_GAP,
        delta_sets=0,
        detail={"ratios": [round(r, 2) for r in window]},
    )]


def headline_reason(adjustments: list[Adjustment]) -> Optional[str]:
    """Основание для одной фразы на карточке Home."""
    if not adjustments:
        return None
    return adjustments[0].reason_code
