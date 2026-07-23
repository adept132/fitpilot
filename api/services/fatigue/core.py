"""Чистая математика усталостной модели. Ни одного обращения к БД.

Инвариант: две валюты не складываются. Внутренняя нагрузка (AU, «как тяжело
далось») и внешняя механика (кг*повт, «какие силы на ткани») идут раздельно
до отсеков включительно и отвечают на разные вопросы.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from api.services.fatigue.params import FatigueParams


@dataclass(frozen=True)
class SetImpulse:
    """Один рабочий подход как источник нагрузки."""
    at: datetime
    weight_kg: float
    reps: int
    rir: int
    main_muscle: str
    secondary_muscles: tuple[str, ...]
    fatigue_tier: int


@dataclass(frozen=True)
class CompartmentLoads:
    """Разложение одного импульса по отсекам.

    systemic и muscular — в AU (трек A); mechanical — в кг*повт (трек B).
    Складывать их между собой нельзя.
    """
    systemic: float
    muscular: dict[str, float]
    mechanical: float


def e1rm_epley(weight_kg: float, reps: int) -> float:
    """«Сырой» e1RM по Эпли, без поправки на RIR.

    ВНУТРЕННЯЯ функция. Наружу не отдаётся: единственный видимый пользователю
    e1RM — autoprogression.set_target_value, который складывает RIR внутрь
    формулы. Здесь RIR учитывается отдельным множителем EF, и применять его
    дважды было бы ошибкой.
    """
    return weight_kg * (1 + reps / 30)


def effort_factor(rir: int, p: FatigueParams) -> float:
    """Множитель близости к отказу: последние повторения непропорционально дороги.

    Потолок обязателен — без него отказные подходы разносит.
    """
    return min(math.exp(p.beta * (p.rir_ref - rir)), p.ef_cap)


def internal_load_set(weight_kg: float, reps: int, rir: int, p: FatigueParams) -> float:
    """Трек A: вклад подхода во внутреннюю нагрузку, в AU."""
    if reps <= 0 or weight_kg <= 0:
        return 0.0
    e1rm = e1rm_epley(weight_kg, reps)
    if e1rm <= 0:
        return 0.0
    return reps * (weight_kg / e1rm) * effort_factor(rir, p)


def mechanical_load_set(weight_kg: float, reps: int, p: FatigueParams) -> float:
    """Трек B: взвешенный по интенсивности тоннаж, в кг*повт.

    Нелинейность (W/e1RM)^q живёт именно здесь: высокая доля от максимума
    непропорционально грузит связки. Из ощущения это не выводится.
    """
    if reps <= 0 or weight_kg <= 0:
        return 0.0
    e1rm = e1rm_epley(weight_kg, reps)
    if e1rm <= 0:
        return 0.0
    return weight_kg * reps * (weight_kg / e1rm) ** p.q


def split_to_compartments(impulse: SetImpulse, p: FatigueParams) -> CompartmentLoads:
    """Раздаёт нагрузку подхода по отсекам.

    Внутренняя нагрузка делится между systemic и muscular по профилю
    fatigue_tier; внутри muscular — пропорционально вовлечённости мышц.
    Механика идёт в свой отсек нетронутой.
    """
    internal = internal_load_set(impulse.weight_kg, impulse.reps, impulse.rir, p)
    mechanical = mechanical_load_set(impulse.weight_kg, impulse.reps, p)

    a_sys, a_mus = p.a_split_by_tier.get(
        impulse.fatigue_tier, p.a_split_by_tier[2]
    )

    weights: dict[str, float] = {impulse.main_muscle: p.w_direct}
    for muscle in impulse.secondary_muscles:
        if muscle and muscle != impulse.main_muscle:
            weights[muscle] = weights.get(muscle, 0.0) + p.w_indirect

    total_weight = sum(weights.values())
    muscular = (
        {m: internal * a_mus * (w / total_weight) for m, w in weights.items()}
        if total_weight > 0
        else {}
    )

    return CompartmentLoads(
        systemic=internal * a_sys,
        muscular=muscular,
        mechanical=mechanical,
    )


@dataclass(frozen=True)
class Readiness:
    """Относительная готовность. Абсолютных процентов усталости наружу нет:
    ложная точность читается как медицинский факт (§4.3)."""
    z: float | None
    band: str  # fresh | neutral | fatigued | unknown


@dataclass(frozen=True)
class Progression:
    """Темп роста нагрузки. Описательный сигнал, а не предсказание травмы (§5)."""
    ratio: float | None
    wow_change_pct: float | None
    chronic_level: float | None
    flag: str  # ok | sharp_rise


BAND_FRESH = "fresh"
BAND_NEUTRAL = "neutral"
BAND_FATIGUED = "fatigued"
BAND_UNKNOWN = "unknown"

FLAG_OK = "ok"
FLAG_SHARP_RISE = "sharp_rise"


def decay(events: list[tuple[datetime, float]], tau_hours: float, now: datetime) -> float:
    """Усталостная половина impulse-response: сумма затухающих импульсов.

    Это EWMA нагрузки, а не полная fitness-fatigue с двумя членами. Линейная
    суперпозиция предполагает, что импульс любой величины тает с одной
    скоростью — на накопленной усталости модель будет систематически ошибаться.
    Для v1 приемлемо.
    """
    if not events or tau_hours <= 0:
        return 0.0

    total = 0.0
    for at, load in events:
        delta_h = (now - at).total_seconds() / 3600.0
        if delta_h < 0:
            # Импульс из будущего: часы клиента уехали. Игнорируем, а не раздуваем.
            continue
        total += load * math.exp(-delta_h / tau_hours)
    return total


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _std(values: list[float], mean: float) -> float:
    if len(values) < 2:
        return 0.0
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def _ewma(values: list[float], tau_days: float) -> float:
    """EWMA по дневному ряду, свежие значения в конце."""
    if not values:
        return 0.0
    alpha = 1.0 - math.exp(-1.0 / tau_days)
    acc = values[0]
    for v in values[1:]:
        acc = alpha * v + (1 - alpha) * acc
    return acc


def readiness_z(series: list[float], p: FatigueParams) -> Readiness:
    """z-оценка последнего значения F_c к собственной истории.

    series — дневной ряд F_c, свежий день последний.
    """
    if len(series) < p.cold_start_days:
        # Гвард холодного старта: без истории z — это шум.
        return Readiness(z=None, band=BAND_UNKNOWN)

    mean = _mean(series)
    std = _std(series, mean)
    # Пол дисперсии: у очень регулярного пользователя std стремится к нулю,
    # и z взрывался бы на копеечных отклонениях.
    floor = abs(mean) * p.sigma_floor_ratio
    denominator = max(std, floor)
    if denominator <= 0:
        return Readiness(z=0.0, band=BAND_NEUTRAL)

    z = (series[-1] - mean) / denominator

    if z >= p.band_threshold_z:
        band = BAND_FATIGUED
    elif z <= -p.band_threshold_z:
        band = BAND_FRESH
    else:
        band = BAND_NEUTRAL

    return Readiness(z=z, band=band)


def ewma_progression(daily_loads: list[float], p: FatigueParams) -> Progression:
    """Темп роста нагрузки: острая EWMA к хронической, без сопряжения окон.

    Показываем не только ratio: он хрупок, поэтому рядом идут изменение
    неделя-к-неделе и абсолютный хронический уровень — величины без проблемы
    сопряжения.
    """
    if not daily_loads:
        return Progression(None, None, None, FLAG_OK)

    acute = _ewma(daily_loads, p.tau_acute_d)
    # Хроническое окно намеренно не включает последнюю неделю: иначе острое
    # окно входит в хроническое и создаёт ложную корреляцию.
    tail = int(p.tau_acute_d)
    chronic_source = daily_loads[:-tail] if len(daily_loads) > tail else daily_loads
    chronic = _ewma(chronic_source, p.tau_chronic_d)

    ratio = acute / chronic if chronic > 0 else None

    wow = None
    if len(daily_loads) >= 14:
        last_week = sum(daily_loads[-7:])
        prev_week = sum(daily_loads[-14:-7])
        if prev_week > 0:
            wow = (last_week - prev_week) / prev_week * 100.0

    flag = FLAG_SHARP_RISE if ratio is not None and ratio > p.sharp_rise_ratio else FLAG_OK

    return Progression(
        ratio=ratio,
        wow_change_pct=wow,
        chronic_level=chronic,
        flag=flag,
    )
