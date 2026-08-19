"""Чистый решатель автопилота: разрыв -> минимальный набор рычагов.

Никаких действий и никакой БД. Решение о том, ЧТО делать, отделено от того,
КАК это применяется (service.py) — та же конвенция, что в periodization и
volume.

ФИКС C1 (финальное ревью P0-12): раньше вклад каждой ступени лестницы был
фиксированной выдуманной константой (_LEVER_SHARE), никак не связанной с
планом конкретного пользователя, а `_effect_days` был арифметикой над теми
же константами. Спека §5.4 требует обратного: «эффект каждого рычага —
результат пересимуляции, а не оценки». Теперь decide() ничего не оценивает
сам — он просит вызывающую сторону пересимулировать движок через
`simulate_with` (см. её контракт в types.SimulateWith) и берёт числа из
ответа. Сама механика "что именно меняет рычаг в плане" decide.py
принципиально не знает — это знание живёт в simulate.apply_lever и в
service.evaluate, у которых есть настоящий SchemeContext и настоящие
будущие сессии. Это и держит decide.py чистым и юнит-тестируемым без БД:
тесты подают простую подмену `simulate_with`.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from api.services.goal import params
from api.services.goal.types import DecisionInput, Lever, SimulateWith


def decide(inp: DecisionInput, simulate_with: SimulateWith) -> tuple[list[Lever], str]:
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

    levers: list[Lever] = []
    plan_slope = inp.rates.plan
    eta = inp.eta
    # Присутствие лифта в плане меняется ВНУТРИ этого прохода лестницы, как
    # только LEVER_ENSURE_PRESENT принят, — остальные ступени (scheme,
    # частота) обязаны увидеть это сразу же, а не только на СЛЕДУЮЩЕМ вызове
    # decide(). inp.lift_in_plan — снимок ДО решения и не меняется;
    # lift_present — то, что решатель знает СЕЙЧАС.
    lift_present = inp.lift_in_plan

    for kind in params.LADDER:
        if plan_slope >= inp.rates.required:
            break
        if not _applicable(kind, inp, lift_present):
            continue

        detail = _detail(kind)
        new_slope, new_eta = simulate_with(tuple(levers), kind, detail)
        effect_slope = round(new_slope - plan_slope, 3)
        if effect_slope <= 0:
            # Пересимуляция показала: рычаг реально ничего не даёт (потолок
            # уже исчерпан предыдущими ступенями, изменение физически не
            # двигает темп) — честнее промолчать про бесполезный рычаг, чем
            # предложить его с нулевым или отрицательным эффектом.
            continue

        levers.append(Lever(
            index=len(levers),
            kind=kind,
            reason_code=(
                params.REASON_LIFT_MISSING
                if kind == params.LEVER_ENSURE_PRESENT
                else params.REASON_PACE_BEHIND
            ),
            effect_slope=effect_slope,
            effect_days=_effect_days(eta, new_eta),
            detail=detail,
        ))
        plan_slope = new_slope
        eta = new_eta
        if kind == params.LEVER_ENSURE_PRESENT:
            lift_present = True

    if not levers:
        return [], params.REASON_NO_LEVER_LEFT

    # Причина верхнего уровня берётся из первого рычага, а не выводится
    # заново — иначе два места легко разъедутся (см. ревью).
    reason = levers[0].reason_code
    if reason == params.REASON_PACE_BEHIND and plan_slope < inp.rates.required:
        # Лестница кончилась (или дальше пересимуляция перестала помогать), а
        # рычаги всё равно не закрывают разрыв целиком — это честно другое
        # состояние, чем "рычаги закрывают гап" (REASON_PACE_BEHIND).
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


def _applicable(kind: str, inp: DecisionInput, lift_present: bool) -> bool:
    if kind == params.LEVER_ENSURE_PRESENT:
        return not inp.lift_in_plan
    if not lift_present:
        # Пока лифта нет в плане (и его ещё не вернул более ранний рычаг в
        # ЭТОМ ЖЕ проходе лестницы), остальные ступени бессмысленны: крутить
        # схему упражнения, которого не будет на неделе, нечего.
        return False
    if kind == params.LEVER_SCHEME:
        return inp.is_heavy_compound and inp.scheme not in ("percent_1rm", "fixed_increment")
    if kind in params.STRUCTURAL_LEVERS:
        return inp.microcycles_left >= params.MIN_MICROCYCLES_FOR_STRUCTURAL
    return False


def _effect_days(eta_before: Optional[date], eta_after: Optional[date]) -> int:
    """Разница СИМУЛИРОВАННЫХ ETA до и после рычага — прямо то число, что
    проверяемо тем же способом, каким получено (спека §5.4). None с любой
    стороны (горизонт не увидел пересечения цели) не даёт числа вовсе — 0,
    а не выдуманная оценка."""
    if eta_before is None or eta_after is None:
        return 0
    return max(0, (eta_before - eta_after).days)


def _detail(kind: str) -> dict:
    if kind == params.LEVER_SCHEME:
        # _applicable допускает LEVER_SCHEME только при is_heavy_compound —
        # второй исход недостижим, поэтому веток здесь одна.
        return {"to_scheme": "percent_1rm"}
    if kind == params.LEVER_LIFT_FREQUENCY:
        return {"delta_sessions": 1}
    return {}
