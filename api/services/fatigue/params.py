"""Параметры усталостной модели.

Две независимые оси:
  provenance — это факт или гипотеза ([ЛИТ] / [ПРИОР] / [КОНФИГ]);
  tier       — способны ли данные отличить это значение от соседнего.

Из [ПРИОР] не следует «будем подгонять»: параметров много, данные разрежены,
и попытка тюнить всё сразу гарантирует переобучение.

  T1  — персонализируется первым, идентифицируемо уже сейчас;
  T2  — позже, когда накопится разметка;
  T3  — постоянно живёт на априоре с жёсткой усадкой. Это штатный режим,
        а не промежуточное состояние;
  ""  — не персонализируется никогда: предохранители и соглашения.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MODEL_VERSION = "fatigue-v0.2-gym-1"

LIT = "[ЛИТ]"
PRIOR = "[ПРИОР]"
CONFIG = "[КОНФИГ]"

T1 = "T1"
T2 = "T2"
T3 = "T3"
TIER_NONE = ""


@dataclass(frozen=True)
class Param:
    value: Any
    provenance: str
    tier: str
    note: str


@dataclass(frozen=True)
class FatigueParams:
    """Полный набор параметров расчёта. Ядро принимает его аргументом, чтобы
    персональные значения подставлялись резолвером без правок математики."""

    tau_systemic_h: float
    tau_muscular_h: float
    tau_mechanical_h: float
    tau_min_mechanical_h: float
    beta: float
    rir_ref: int
    ef_cap: float
    q: float
    w_direct: float
    w_indirect: float
    a_split_by_tier: dict
    tau_acute_d: float
    tau_chronic_d: float
    sigma_floor_ratio: float
    cold_start_days: int
    band_threshold_z: float
    sharp_rise_ratio: float
    window_days: int


PARAM_REGISTRY: dict[str, Param] = {
    "tau_systemic_h": Param(
        36.0, PRIOR, T1,
        "Середина диапазона 24-48 ч, настроен на медленную компоненту (§4.2). "
        "Идентифицируем: управляет временным ходом, а дней с последней "
        "тренировки естественно варьируется по истории.",
    ),
    "tau_muscular_h": Param(
        48.0, PRIOR, T1, "Локальный ремонт волокон. Идентифицируем так же, как tau_systemic_h.",
    ),
    "tau_mechanical_h": Param(
        84.0, PRIOR, T1, "Середина диапазона 72-96 ч для связок и сухожилий.",
    ),
    "tau_min_mechanical_h": Param(
        48.0, CONFIG, TIER_NONE,
        "Нижняя граница safety-рельса (§6.1). Предохранитель не персонализируется.",
    ),
    "beta": Param(
        0.15, PRIOR, T3,
        "Крутизна EF. effort_level даёт пять дискретных уровней и необязателен, "
        "а эффект усилия конфаундится с объёмом — оценка сядет на шум.",
    ),
    "rir_ref": Param(
        4, PRIOR, T3, "Опорный RIR для EF. Не идентифицируем отдельно от beta.",
    ),
    "ef_cap": Param(
        2.5, CONFIG, TIER_NONE,
        "Обязательный потолок EF (§2.2): без него отказные подходы разносит.",
    ),
    "q": Param(
        1.5, PRIOR, T3,
        "Показатель нелинейности механики. Не идентифицируется отдельно от "
        "tau_mechanical_h: оба двигают одну наблюдаемую кривую. Развести их "
        "может только тканевый таргет, которого у нас нет.",
    ),
    "w_direct": Param(
        1.0, PRIOR, T3,
        "Вклад целевой мышцы. Персонализация 16 групп на пользователя — "
        "переобучение по построению; сам w[m] помечен как ненадёжный.",
    ),
    "w_indirect": Param(
        0.5, PRIOR, T3, "Вклад вспомогательной мышцы. См. w_direct.",
    ),
    "a_split_by_tier": Param(
        {1: (0.60, 0.40), 2: (0.45, 0.55), 3: (0.30, 0.70)}, PRIOR, T2,
        "Раздача внутренней нагрузки (a_sys, a_mus) по fatigue_tier. Станет "
        "идентифицируемой благодаря recommended_weight: просадка жима после "
        "тяжёлых приседов — системная усталость, просадка только приседов — локальная.",
    ),
    "tau_acute_d": Param(
        7.0, LIT, TIER_NONE, "Острое окно EWMA. Соглашение, сдвиг лишает сопоставимости.",
    ),
    "tau_chronic_d": Param(
        28.0, LIT, TIER_NONE, "Хроническое окно EWMA. См. tau_acute_d.",
    ),
    "sigma_floor_ratio": Param(
        0.10, CONFIG, TIER_NONE,
        "Пол дисперсии как доля от медианы F_c (§4.3): у очень регулярного "
        "пользователя std стремится к нулю и z взрывается на копейках. Медиана "
        "устойчивее к выбросам, чем среднее.",
    ),
    "cold_start_days": Param(
        14, CONFIG, TIER_NONE,
        "Гвард холодного старта: до накопления истории z — это шум, наружу не отдаём.",
    ),
    "band_threshold_z": Param(
        0.75, CONFIG, TIER_NONE,
        "Граница полос fresh/neutral/fatigued. Продуктовое решение, не свойство человека.",
    ),
    "sharp_rise_ratio": Param(
        1.5, CONFIG, TIER_NONE,
        "Жёлтый флажок темпа роста нагрузки. Именно флажок, а не диагноз (§5).",
    ),
    "window_days": Param(
        35, CONFIG, TIER_NONE,
        "Окно выборки импульсов: покрывает 5 tau для механики с запасом.",
    ),
}

DEFAULT_PARAMS = FatigueParams(**{name: p.value for name, p in PARAM_REGISTRY.items()})
