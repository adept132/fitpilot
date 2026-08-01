"""Формат «Eurith CSV v1» — единый источник правды для экспорта и импорта.

Первые 12 колонок повторяют заголовок экспорта Strong байт-в-байт, дальше идут
наши расширения. Сторонние парсеры мапят колонки по именам и хвост игнорируют,
а наш импорт восстанавливает данные без потерь (round-trip).

Импорт читает колонки ТОЛЬКО по именам (не по позициям) — это бесплатно
покрывает оба варианта Strong: современный (запятая, без единиц) и легаси
(';' + колонки Weight Unit / Distance Unit).
"""

import re
from datetime import datetime, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from api.services.autoprogression import DEFAULT_RIR, effort_to_rir

# --- Колонки Strong (порядок и написание менять нельзя: это контракт совместимости) ---
COL_DATE = "Date"
COL_WORKOUT_NAME = "Workout Name"
COL_DURATION = "Duration"
COL_EXERCISE_NAME = "Exercise Name"
COL_SET_ORDER = "Set Order"
COL_WEIGHT = "Weight"
COL_REPS = "Reps"
COL_DISTANCE = "Distance"
COL_SECONDS = "Seconds"
COL_NOTES = "Notes"
COL_WORKOUT_NOTES = "Workout Notes"
COL_RPE = "RPE"

STRONG_COLUMNS: List[str] = [
    COL_DATE,
    COL_WORKOUT_NAME,
    COL_DURATION,
    COL_EXERCISE_NAME,
    COL_SET_ORDER,
    COL_WEIGHT,
    COL_REPS,
    COL_DISTANCE,
    COL_SECONDS,
    COL_NOTES,
    COL_WORKOUT_NOTES,
    COL_RPE,
]

# --- Наши расширения ---
# "Weight Unit" намеренно назван как в легаси-Strong: снимает неоднозначность
# единиц и читается чужими парсерами старого формата.
COL_WEIGHT_UNIT = "Weight Unit"
COL_SET_TYPE = "Set Type"
COL_EFFORT = "Effort"
COL_COMPLETED = "Completed"
COL_PARENT_SET_ORDER = "Parent Set Order"
COL_SUPERSET_GROUP = "Superset Group"
COL_SUPERSET_ROUND = "Superset Round"
COL_EXERCISE_ID = "Exercise ID"
COL_MESO_PHASE = "Meso Phase"
COL_MICRO_DAY = "Micro Day"

EURITH_COLUMNS: List[str] = [
    COL_WEIGHT_UNIT,
    COL_SET_TYPE,
    COL_EFFORT,
    COL_COMPLETED,
    COL_PARENT_SET_ORDER,
    COL_SUPERSET_GROUP,
    COL_SUPERSET_ROUND,
    COL_EXERCISE_ID,
    COL_MESO_PHASE,
    COL_MICRO_DAY,
]

FULL_COLUMNS: List[str] = STRONG_COLUMNS + EURITH_COLUMNS

DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_WORKOUT_NAME = "Свободная тренировка"


# --- Часовые пояса ---
# В файле (и у Strong, и у нас) дата — это «настенное» локальное время без
# смещения. В БД started_at это timestamptz. Поэтому конвертацию делаем ЯВНО
# через часовой пояс пользователя: иначе наивную дату интерпретирует таймзона
# соединения с БД, и на каждом цикле импорт->экспорт время уезжает.

def _zone(tz_name: Optional[str]):
    try:
        return ZoneInfo(tz_name) if tz_name else timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def wall_clock_to_utc(naive: datetime, tz_name: Optional[str]) -> datetime:
    """Настенное время из файла -> момент времени в UTC (для записи в БД)."""
    if naive.tzinfo is not None:
        return naive.astimezone(timezone.utc)
    return naive.replace(tzinfo=_zone(tz_name)).astimezone(timezone.utc)


def utc_to_wall_clock(value: datetime, tz_name: Optional[str]) -> datetime:
    """Момент из БД -> настенное время пользователя (для записи в файл)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_zone(tz_name))

# Единицы веса
UNIT_KG = "kg"
UNIT_LBS = "lbs"
LB_PER_KG = 2.2046226218
KG_PER_LB = 0.45359237


def kg_to_unit(kg: Optional[float], unit: str) -> Optional[float]:
    """КГ (канон БД) -> значение в целевой единице, округлённое до 0.01."""
    if kg is None:
        return None
    value = float(kg) * LB_PER_KG if unit == UNIT_LBS else float(kg)
    return round(value, 2)


def unit_to_kg(value: Optional[float], unit: str) -> Optional[float]:
    """Значение в единице файла -> КГ (как в БД: Numeric(8,2))."""
    if value is None:
        return None
    kg = float(value) * KG_PER_LB if unit == UNIT_LBS else float(value)
    return round(kg, 2)


def normalize_unit(value: Optional[str]) -> Optional[str]:
    """'kg'/'kgs'/'кг'/'lb'/'lbs'/'pounds' -> канон, иначе None."""
    if not value:
        return None
    v = str(value).strip().lower()
    if v in ("kg", "kgs", "kilograms", "кг"):
        return UNIT_KG
    if v in ("lb", "lbs", "pounds", "фунты"):
        return UNIT_LBS
    return None


# --- Усилие <-> RPE ---
# RIR (reps in reserve) и RPE — зеркальные шкалы: RPE = 10 - RIR.
# RIR берём из общей таблицы EFFORT_TO_RIR, чтобы экспорт совпадал с движком.
RIR_TO_EFFORT = {
    4: "warmup",
    3: "light",
    2: "medium",
    1: "prefailure",
    0: "failure",
}


def effort_to_rpe(effort_level: Optional[str]) -> Optional[float]:
    """effort_level -> RPE для колонки Strong. None, если усилие не указано."""
    if not effort_level:
        return None
    return round(10 - effort_to_rir(effort_level), 1)


def rpe_to_effort(rpe: Optional[float]) -> Optional[str]:
    """RPE из файла Strong -> наш effort_level. Значения вне шкалы зажимаем."""
    if rpe is None:
        return None
    rir = int(round(10 - float(rpe)))
    rir = max(0, min(4, rir))
    return RIR_TO_EFFORT[rir]


# --- Длительность ---
_DURATION_RE = re.compile(r"(\d+)\s*([hms])", re.IGNORECASE)


def format_duration(seconds: Optional[int]) -> str:
    """Секунды -> человекочитаемый вид Strong: '2h 38m', '45m', ''."""
    if not seconds or seconds <= 0:
        return ""
    total_minutes = int(seconds) // 60
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def parse_duration(value: Optional[str]) -> Optional[int]:
    """'2h 38m' / '45m' / '1h 2m 3s' -> секунды. None, если распарсить нечего."""
    if not value:
        return None
    total = 0
    found = False
    for amount, unit in _DURATION_RE.findall(str(value)):
        found = True
        n = int(amount)
        u = unit.lower()
        if u == "h":
            total += n * 3600
        elif u == "m":
            total += n * 60
        else:
            total += n
    return total if found else None
