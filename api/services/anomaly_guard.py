"""Правила аномального ввода подхода.

Философия: данные пользователя не теряем и не искажаем молча. Абсурдные
значения физически невозможны и помечаются всегда; подозрительные — только
пока пользователь их не подтвердил. Помеченный подход остаётся видимым в
истории, но выпадает из автопрогрессии, прогноза, бюджета объёма и
усталостной модели.

Модуль чистый: без БД, без FastAPI. Историю по упражнению вызывающая сторона
передаёт готовым срезом (ExerciseStats).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# --- Абсолютные пороги [КОНФИГ]: физически невозможные значения. ---
ABSURD_WEIGHT_KG = 600.0
ABSURD_REPS = 300

# --- Относительные пороги [КОНФИГ]: ловят опечатки вида 100 -> 1000. ---
E1RM_JUMP_FACTOR = 1.5
WEIGHT_MEDIAN_FACTOR = 3.0
MIN_HISTORY_SESSIONS = 3

# Типы подходов, которые не участвуют в аналитике, — относительные правила
# к ним не применяем, чтобы не дёргать пользователя на разминке.
_NON_WORKING_SET_TYPES = frozenset({"warmup", "warmup_effort"})

LEVEL_OK = "ok"
LEVEL_SUSPICIOUS = "suspicious"
LEVEL_ABSURD = "absurd"


@dataclass(frozen=True)
class ExerciseStats:
    """Срез истории пользователя по конкретному упражнению за окно наблюдения.

    Считается только по рабочим завершённым подходам, уже помеченные аномалии
    исключаются — иначе одна ошибка расширила бы коридор для следующих.
    """
    sessions: int
    best_e1rm: Optional[float]
    median_weight_kg: Optional[float]


@dataclass(frozen=True)
class AnomalyVerdict:
    level: str
    reason: Optional[str]


def _e1rm(weight_kg: float, reps: int) -> float:
    """Эпли без поправки на RIR — здесь нужна грубая верхняя оценка, а не точность."""
    return weight_kg * (1 + reps / 30)


def check_set(
    weight_kg: Optional[float],
    reps: Optional[int],
    set_type: str,
    stats: Optional[ExerciseStats],
) -> AnomalyVerdict:
    """Оценивает подход. Вес — всегда в килограммах."""
    if weight_kg is None and reps is None:
        return AnomalyVerdict(LEVEL_OK, None)

    w = float(weight_kg or 0.0)
    r = int(reps or 0)

    if w > ABSURD_WEIGHT_KG:
        return AnomalyVerdict(LEVEL_ABSURD, f"Вес {w:.0f} кг превышает предел {ABSURD_WEIGHT_KG:.0f} кг")
    if r > ABSURD_REPS:
        return AnomalyVerdict(LEVEL_ABSURD, f"{r} повторений превышает предел {ABSURD_REPS}")

    if set_type in _NON_WORKING_SET_TYPES:
        return AnomalyVerdict(LEVEL_OK, None)

    if stats is None or stats.sessions < MIN_HISTORY_SESSIONS:
        return AnomalyVerdict(LEVEL_OK, None)

    if stats.best_e1rm and r > 0 and w > 0:
        if _e1rm(w, r) > stats.best_e1rm * E1RM_JUMP_FACTOR:
            return AnomalyVerdict(
                LEVEL_SUSPICIOUS,
                "Результат заметно выше твоего лучшего в этом упражнении",
            )

    if stats.median_weight_kg and w > stats.median_weight_kg * WEIGHT_MEDIAN_FACTOR:
        return AnomalyVerdict(
            LEVEL_SUSPICIOUS,
            "Вес заметно выше твоего обычного в этом упражнении",
        )

    return AnomalyVerdict(LEVEL_OK, None)


def resolve_is_anomalous(verdict: AnomalyVerdict, confirmed: bool) -> bool:
    """Итоговое решение для колонки is_anomalous.

    Подтверждение пользователя снимает только "suspicious": если человек сказал
    «да, 250 кг верно», исключать подход из аналитики было бы неуважением к
    его вводу. Абсурдные значения подтверждению не поддаются.
    """
    if verdict.level == LEVEL_ABSURD:
        return True
    if verdict.level == LEVEL_SUSPICIOUS:
        return not confirmed
    return False
