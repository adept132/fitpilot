"""Чистый решатель автопилота: разрыв -> минимальный набор рычагов.

Никаких действий и никакой БД. Решение о том, ЧТО делать, отделено от того,
КАК это применяется (service.py) — та же конвенция, что в periodization и
volume.
"""

from __future__ import annotations

from api.services.goal import params
from api.services.goal.types import DecisionInput, Lever

# Вклад ступени в темп — фиксированная величина, кг e1RM в неделю, а НЕ доля
# от разрыва: реальный эффект рычага (смена схемы, доп. подход) не растёт
# вместе с тем, насколько пользователь отстаёт. Именно поэтому лестница
# закрывает мелкий разрыв одной-двумя ступенями, а крупный — доходит до
# структурных: чем дороже для пользователя ступень, тем больше её вклад.
# LEVER_ENSURE_PRESENT сюда не входит: он не прибавляет темп по частям,
# а возвращает лифт в план — без этого остальные ступени неприменимы
# (см. _applicable), так что он всегда закрывает разрыв целиком, один.
_LEVER_SHARE: dict[str, float] = {
    params.LEVER_SCHEME: 0.15,
    params.LEVER_REP_RANGE: 0.12,
    params.LEVER_SETS: 0.10,
    params.LEVER_LIFT_FREQUENCY: 0.35,
    params.LEVER_STRUCTURAL: 0.50,
}


def decide(inp: DecisionInput) -> tuple[list[Lever], str]:
    """Рычаги и код причины. Пустой список — правильный ответ в трёх случаях
    из четырёх (см. таблицу классификации в спеке §5.4)."""
    if inp.trend_slope < 0:
        return [], params.REASON_TREND_DOWN

    if inp.rates.required <= inp.rates.plan:
        return [], ""

    if inp.rates.required > inp.rates.ceiling:
        return [], params.REASON_ABOVE_CEILING

    if not _thresholds_fire(inp):
        return [], ""

    gap = inp.rates.required - inp.rates.plan
    levers: list[Lever] = []
    covered = 0.0

    for kind in params.LADDER:
        if covered >= gap:
            break
        if not _applicable(kind, inp):
            continue
        if kind == params.LEVER_ENSURE_PRESENT:
            share = gap  # возврат лифта в план закрывает разрыв целиком
        else:
            share = _LEVER_SHARE[kind]
        covered += share
        levers.append(Lever(
            index=len(levers),
            kind=kind,
            reason_code=(
                params.REASON_LIFT_MISSING
                if kind == params.LEVER_ENSURE_PRESENT
                else params.REASON_PACE_BEHIND
            ),
            effect_slope=round(share, 3),
            effect_days=_effect_days(inp, share),
            detail=_detail(kind, inp),
        ))

    if not levers:
        return [], params.REASON_NO_LEVER_LEFT

    # Причина верхнего уровня берётся из первого рычага, а не выводится
    # заново — иначе два места легко разъедутся (см. ревью).
    reason = levers[0].reason_code
    if reason == params.REASON_PACE_BEHIND and covered < gap:
        # Лестница кончилась, а рычаги всё равно не закрывают разрыв целиком —
        # это честно другое состояние, чем "рычаги закрывают гап".
        reason = params.REASON_PARTIAL_CATCHUP
    return levers, reason


def _thresholds_fire(inp: DecisionInput) -> bool:
    """Оба порога сразу: 10 % по темпу И 7 дней по ETA (спека §5.4)."""
    rate_gap = (inp.rates.required - inp.rates.plan) / max(inp.rates.plan, 1e-9)
    if rate_gap < params.MIN_RATE_GAP_RATIO:
        return False
    if inp.eta is None:
        return True  # цель не достигается в горизонте вовсе — это отставание
    return (inp.eta - inp.deadline).days > params.MIN_ETA_GAP_DAYS


def _applicable(kind: str, inp: DecisionInput) -> bool:
    if kind == params.LEVER_ENSURE_PRESENT:
        return not inp.lift_in_plan
    if not inp.lift_in_plan:
        # Пока лифта нет в плане, остальные ступени бессмысленны: крутить
        # схему упражнения, которого не будет на неделе, нечего.
        return False
    if kind == params.LEVER_SCHEME:
        return inp.is_heavy_compound and inp.scheme not in ("percent_1rm", "fixed_increment")
    if kind == params.LEVER_REP_RANGE:
        return inp.target_reps < inp.rep_max
    if kind == params.LEVER_SETS:
        return inp.headroom_sets > 0
    if kind in params.STRUCTURAL_LEVERS:
        return inp.microcycles_left >= params.MIN_MICROCYCLES_FOR_STRUCTURAL
    return False


def _effect_days(inp: DecisionInput, share: float) -> int:
    """На сколько дней рычаг приближает ETA при текущем плановом темпе."""
    if inp.eta is None or inp.rates.plan <= 0:
        return 0
    weeks_now = (inp.eta - inp.deadline).days / 7.0
    improved = inp.rates.plan + share
    return max(0, int(round(weeks_now * 7 * (share / improved))))


def _detail(kind: str, inp: DecisionInput) -> dict:
    if kind == params.LEVER_SCHEME:
        # _applicable допускает LEVER_SCHEME только при is_heavy_compound —
        # второй исход недостижим, поэтому веток здесь одна.
        return {"to_scheme": "percent_1rm"}
    if kind == params.LEVER_REP_RANGE:
        return {"rep_min": max(1, inp.target_reps - 1), "rep_max": inp.target_reps + 2}
    if kind == params.LEVER_SETS:
        return {"delta_sets": min(2, inp.headroom_sets)}
    if kind == params.LEVER_LIFT_FREQUENCY:
        return {"delta_sessions": 1}
    return {}
