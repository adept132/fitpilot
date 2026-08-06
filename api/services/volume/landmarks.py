"""Физиологические ориентиры объёма по мышце.

ВСЕ ЗНАЧЕНИЯ — СТАРТОВЫЕ, подлежат калибровке на накопленных данных.
Единица — эффективные подходы за микроцикл (measure.INDIRECT_WEIGHT).

Провенанс, в порядке приоритета:
  1. Мета-аналитическая дозозависимость (Schoenfeld/Ogborn/Krieger 2017;
     Baz-Valle et al. 2022; Pelland et al. 2024) — абсолютные рамки:
     измеримый рост от ~4 подходов в неделю, устойчивый в районе 10-20,
     выше отдача убывает при расширяющихся доверительных интервалах.
  2. Практические landmarks (Israetel и соавторы) — ОТНОСИТЕЛЬНАЯ форма и
     ранжирование между мышцами, а не значения: у нас своя таксономия с
     тремя пучками дельты и разделением спины на широчайшие, середину и
     трапеции.
  3. Собственные ограничения проекта — инварианты в tests/test_volume_landmarks.py.

Оценки сознательно КОНСЕРВАТИВНЫ. Три технических довода:
  - каталог secondary_muscle_groups дозаполняется только в P0-10 и сейчас
    систематически НЕДОСЧИТЫВАЕТ косвенный объём; высокий потолок на таком
    входе не пересечётся никогда, и предупреждение окажется мёртвым;
  - MRV здесь не научная константа, а точка запуска предупреждения;
  - приложение должно неохотно требовать большего и быстро замечать
    избыточное.

НЕ путать с volume_tables.WEEKLY_VOLUME_*: та таблица — РАСПРЕДЕЛЕНИЕ
бюджета, уже сжатое системным потолком (сумма 84 при потолке 95 для
intermediate). Landmarks валидны для мышцы В ИЗОЛЯЦИИ, и сумма MAV по
всем мышцам заведомо не влезает ни в какой бюджет — это определяющее
свойство, а не дефект.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

LEVELS: tuple[str, ...] = ("beginner", "intermediate", "advanced")
_FALLBACK_LEVEL = "beginner"

# [КОНФИГ] Системный потолок недельного объёма В ЭФФЕКТИВНЫХ подходах.
# ВНИМАНИЕ: не переносить сюда значения из
# volume_calculator.EXPERIENCE_CONSTRAINTS — те считают ПРЯМЫЕ подходы, и
# эффективная сумма всегда не меньше прямой. Сохранение старых чисел
# зажало бы бюджет сильнее задуманного и заставило бы срез не-фокусных
# мышц срабатывать там, где не должен.
SYSTEMIC_CAP_EFF: dict[str, int] = {
    "beginner": 80,
    "intermediate": 110,
    "advanced": 140,
}

# [КОНФИГ] Потолок подходов на мышцу за одну сессию. Совпадает с
# session_max из volume_calculator.EXPERIENCE_CONSTRAINTS — это про
# переносимость одной тренировки, единица тут прямые подходы.
SESSION_MAX: dict[str, int] = {
    "beginner": 6,
    "intermediate": 8,
    "advanced": 10,
}

# --- Доли прямого объёма по классам мышц -------------------------------
#
# Прямые границы ВЫВОДЯТСЯ из эффективных, а не задаются ещё 96 числами.
# Класс определяет, насколько мышца тонет в косвенном объёме: у передней
# дельты десять жимовых подходов при двух прямых — норма, у квадрицепса
# такого не бывает.
#
# (mev_ratio, mrv_ratio)
_DIRECT_RATIO: dict[str, tuple[float, float]] = {
    "primary_driven": (0.70, 0.80),  # прямой стимул доминирует
    "mixed": (0.50, 0.65),           # смешанный
    "indirect_flooded": (0.30, 0.55),  # тонет в косвенном
}

_MUSCLE_CLASS: dict[str, str] = {
    "chest": "primary_driven",
    "lats": "primary_driven",
    "quads": "primary_driven",
    "side_delts": "primary_driven",
    "calves": "primary_driven",
    "abs": "primary_driven",
    "adductors": "primary_driven",
    "abductors": "primary_driven",
    "mid_back": "mixed",
    "hamstrings": "mixed",
    "rear_delts": "mixed",
    "biceps": "mixed",
    "front_delts": "indirect_flooded",
    "traps": "indirect_flooded",
    "triceps": "indirect_flooded",
    "glutes": "indirect_flooded",
}

# --- Эффективные landmarks: (MEV, MAV, MRV) ----------------------------
_TABLE: dict[str, dict[str, tuple[int, int, int]]] = {
    "beginner": {
        "chest": (4, 9, 14),
        "lats": (4, 9, 14),
        "mid_back": (4, 9, 15),
        "side_delts": (4, 9, 15),
        "front_delts": (3, 6, 11),
        "rear_delts": (3, 7, 13),
        "traps": (2, 6, 12),
        "biceps": (4, 8, 14),
        "triceps": (4, 8, 14),
        "quads": (4, 9, 14),
        "hamstrings": (4, 7, 11),
        "glutes": (2, 6, 11),
        "calves": (4, 9, 15),
        "abs": (2, 6, 12),
        "adductors": (0, 3, 8),
        "abductors": (0, 3, 8),
    },
    "intermediate": {
        "chest": (6, 12, 18),
        "lats": (6, 12, 18),
        "mid_back": (6, 12, 20),
        "side_delts": (6, 12, 20),
        "front_delts": (4, 8, 14),
        "rear_delts": (4, 10, 18),
        "traps": (3, 8, 16),
        "biceps": (6, 11, 18),
        "triceps": (6, 11, 18),
        "quads": (6, 12, 18),
        "hamstrings": (5, 10, 15),
        "glutes": (3, 8, 14),
        "calves": (6, 12, 20),
        "abs": (3, 8, 16),
        "adductors": (0, 4, 10),
        "abductors": (0, 4, 10),
    },
    "advanced": {
        "chest": (8, 15, 22),
        "lats": (8, 15, 22),
        "mid_back": (8, 15, 24),
        "side_delts": (8, 15, 24),
        "front_delts": (5, 10, 17),
        "rear_delts": (5, 12, 22),
        "traps": (4, 10, 20),
        "biceps": (8, 14, 22),
        "triceps": (8, 14, 22),
        "quads": (8, 15, 22),
        "hamstrings": (6, 12, 18),
        "glutes": (4, 10, 17),
        "calves": (8, 15, 24),
        "abs": (4, 10, 20),
        "adductors": (0, 5, 12),
        "abductors": (0, 5, 12),
    },
}

MUSCLES: tuple[str, ...] = tuple(_TABLE["intermediate"].keys())


@dataclass(frozen=True)
class Landmarks:
    mev: int
    mav: int
    mrv: int
    mev_direct: int
    mrv_direct: int


def _level_or_fallback(level: Optional[str]) -> str:
    key = (level or "").strip().lower()
    return key if key in _TABLE else _FALLBACK_LEVEL


def landmarks_for(muscle: str, level: Optional[str]) -> Optional[Landmarks]:
    """Ориентиры мышцы на уровне опыта, или None для незнакомой мышцы."""
    row = _TABLE[_level_or_fallback(level)].get((muscle or "").strip().lower())
    if row is None:
        return None

    mev, mav, mrv = row
    mev_ratio, mrv_ratio = _DIRECT_RATIO[_MUSCLE_CLASS[muscle.strip().lower()]]
    # Обе границы округляются ВНИЗ. Для пола это делает требование мягче,
    # для потолка — держит инвариант достижимости при частоте 2.
    return Landmarks(
        mev=mev,
        mav=mav,
        mrv=mrv,
        mev_direct=math.floor(mev * mev_ratio),
        mrv_direct=math.floor(mrv * mrv_ratio),
    )


def reachable_mrv(muscle: str, level: Optional[str], frequency: int) -> Optional[int]:
    """Достижимый потолок ПРЯМЫХ подходов в конкретном сплите.

    Табличный потолок предполагает разумную частоту. У человека, который
    тренирует грудь раз в неделю, физически недостижим — и висел бы
    молчащим предупреждением. Поэтому показываемая граница клампится
    произведением сессионного потолка на реальную частоту.
    """
    lm = landmarks_for(muscle, level)
    if lm is None:
        return None
    ceiling = SESSION_MAX[_level_or_fallback(level)] * max(1, int(frequency or 1))
    return min(lm.mrv_direct, ceiling)
