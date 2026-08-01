"""Парсер CSV тренировок: Strong (современный и легаси) + наш «Eurith CSV v1».

Модуль намеренно чистый (без БД и FastAPI) — это самая хрупкая часть импорта,
и она должна целиком покрываться тестами. Резолв упражнений и запись в базу
живут в роутере.

Ключевые решения:
- колонки читаем ТОЛЬКО по именам из шапки, не по позициям — это разом
  покрывает и современный Strong (',' без единиц), и легаси (';' + Weight Unit);
- единица веса берётся из колонки Weight Unit, если она есть, иначе — из
  переданной default_unit (её у пользователя спрашивает UI).
"""

import csv
import hashlib
import io
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from api.services.csv_format import (
    COL_COMPLETED,
    COL_DATE,
    COL_DISTANCE,
    COL_DURATION,
    COL_EFFORT,
    COL_EXERCISE_ID,
    COL_EXERCISE_NAME,
    COL_NOTES,
    COL_PARENT_SET_ORDER,
    COL_REPS,
    COL_RPE,
    COL_SECONDS,
    COL_SET_ORDER,
    COL_SET_TYPE,
    COL_SUPERSET_GROUP,
    COL_SUPERSET_ROUND,
    COL_WEIGHT,
    COL_WEIGHT_UNIT,
    COL_WORKOUT_NAME,
    COL_WORKOUT_NOTES,
    DATE_FORMAT,
    DEFAULT_WORKOUT_NAME,
    UNIT_KG,
    normalize_unit,
    parse_duration,
    rpe_to_effort,
    unit_to_kg,
)

VALID_SET_TYPES = {"normal", "warmup", "drop"}


@dataclass
class ParsedSet:
    set_order: int
    weight_kg: Optional[float] = None
    reps: Optional[int] = None
    notes: Optional[str] = None
    effort_level: Optional[str] = None
    set_type: str = "normal"
    is_completed: bool = True
    parent_set_order: Optional[int] = None
    superset_round: Optional[int] = None


@dataclass
class ParsedExercise:
    name: str
    exercise_id: Optional[int] = None
    superset_group: Optional[str] = None
    sets: List[ParsedSet] = field(default_factory=list)


@dataclass
class ParsedWorkout:
    started_at: datetime
    name: str
    import_key: str
    duration_seconds: Optional[int] = None
    notes: Optional[str] = None
    exercises: List[ParsedExercise] = field(default_factory=list)

    @property
    def total_sets(self) -> int:
        return sum(len(e.sets) for e in self.exercises)


@dataclass
class SkippedRow:
    line: int
    reason: str


@dataclass
class ParseResult:
    workouts: List[ParsedWorkout] = field(default_factory=list)
    skipped: List[SkippedRow] = field(default_factory=list)
    file_unit: Optional[str] = None  # из колонки Weight Unit, если она есть
    missing_columns: List[str] = field(default_factory=list)

    @property
    def exercise_names(self) -> List[str]:
        seen: Dict[str, None] = {}
        for w in self.workouts:
            for e in w.exercises:
                seen.setdefault(e.name, None)
        return list(seen.keys())


class CsvParseError(Exception):
    """Файл невозможно разобрать (не CSV / нет обязательных колонок)."""


def sniff_delimiter(text: str) -> str:
    """Определяет разделитель по шапке. Strong бывает и с ',', и с ';'."""
    header = text.lstrip("﻿").splitlines()[0] if text.strip() else ""
    try:
        return csv.Sniffer().sniff(header, delimiters=",;\t").delimiter
    except csv.Error:
        # Запасной путь: считаем разделителем тот, которого в шапке больше.
        return max(",;\t", key=header.count) if header else ","


def _clean(value: Optional[str]) -> str:
    return (value or "").strip()


def _to_float(value: Optional[str]) -> Optional[float]:
    """'82,5' и '82.5' -> 82.5 (в CSV из Excel часто десятичная запятая)."""
    raw = _clean(value).replace(",", ".")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _to_int(value: Optional[str]) -> Optional[int]:
    f = _to_float(value)
    return int(f) if f is not None else None


def _to_bool(value: Optional[str], default: bool = True) -> bool:
    raw = _clean(value).lower()
    if not raw:
        return default
    return raw in ("true", "1", "yes", "y")


def _workout_key(date_iso: str, name: str) -> str:
    digest = hashlib.sha1(f"{date_iso}|{name}".encode("utf-8")).hexdigest()
    return f"strong:{digest}"


def parse_csv(text: str, default_unit: str = UNIT_KG) -> ParseResult:
    """Разбирает CSV в список тренировок.

    default_unit используется, только если в файле нет колонки Weight Unit.
    """
    if not text or not text.strip():
        raise CsvParseError("Файл пустой")

    text = text.lstrip("﻿")
    delimiter = sniff_delimiter(text)
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)

    if not reader.fieldnames:
        raise CsvParseError("Не удалось прочитать заголовок файла")

    # Нормализуем имена колонок: регистр и пробелы у экспортов гуляют.
    field_map = {(_clean(f) or "").lower(): f for f in reader.fieldnames}

    def col(row: Dict[str, str], name: str) -> Optional[str]:
        actual = field_map.get(name.lower())
        return row.get(actual) if actual else None

    result = ParseResult()

    required = [COL_DATE, COL_EXERCISE_NAME]
    result.missing_columns = [c for c in required if c.lower() not in field_map]
    if result.missing_columns:
        raise CsvParseError(
            "В файле нет обязательных колонок: " + ", ".join(result.missing_columns)
        )

    has_unit_column = COL_WEIGHT_UNIT.lower() in field_map

    # Тренировка = (Date, Workout Name). Порядок вставки сохраняем.
    workouts: Dict[tuple, ParsedWorkout] = {}
    # (ключ тренировки, имя упражнения) -> упражнение
    exercises: Dict[tuple, ParsedExercise] = {}

    for line_number, row in enumerate(reader, start=2):  # 1 — это шапка
        date_raw = _clean(col(row, COL_DATE))
        name_raw = _clean(col(row, COL_EXERCISE_NAME))

        if not date_raw:
            result.skipped.append(SkippedRow(line_number, "пустая дата"))
            continue
        if not name_raw:
            result.skipped.append(SkippedRow(line_number, "пустое название упражнения"))
            continue

        started_at = _parse_date(date_raw)
        if started_at is None:
            result.skipped.append(
                SkippedRow(line_number, f"не распознана дата: {date_raw!r}")
            )
            continue

        # Строки вида «Rest Timer» — служебные, у них нет номера подхода.
        set_order = _to_int(col(row, COL_SET_ORDER))
        if set_order is None:
            result.skipped.append(
                SkippedRow(
                    line_number,
                    f"нечисловой номер подхода: {_clean(col(row, COL_SET_ORDER))!r}",
                )
            )
            continue

        weight_raw = _to_float(col(row, COL_WEIGHT))
        reps = _to_int(col(row, COL_REPS))

        # Кардио: есть дистанция/время, но нет веса и повторов — наша модель
        # такое не хранит, честно сообщаем в отчёте вместо тихой потери.
        if not weight_raw and not reps:
            distance = _to_float(col(row, COL_DISTANCE)) or 0
            seconds = _to_float(col(row, COL_SECONDS)) or 0
            reason = (
                "кардио-подход (только дистанция/время)"
                if distance or seconds
                else "пустой подход (нет веса и повторов)"
            )
            result.skipped.append(SkippedRow(line_number, reason))
            continue

        row_unit = normalize_unit(col(row, COL_WEIGHT_UNIT)) if has_unit_column else None
        unit = row_unit or default_unit
        if row_unit and result.file_unit is None:
            result.file_unit = row_unit

        workout_name = _clean(col(row, COL_WORKOUT_NAME)) or DEFAULT_WORKOUT_NAME
        wkey = (date_raw, workout_name)

        if wkey not in workouts:
            workouts[wkey] = ParsedWorkout(
                started_at=started_at,
                name=workout_name,
                import_key=_workout_key(started_at.isoformat(), workout_name),
                duration_seconds=parse_duration(col(row, COL_DURATION)),
                notes=_clean(col(row, COL_WORKOUT_NOTES)) or None,
            )

        ekey = (wkey, name_raw)
        if ekey not in exercises:
            exercise = ParsedExercise(
                name=name_raw,
                exercise_id=_to_int(col(row, COL_EXERCISE_ID)),
                superset_group=_clean(col(row, COL_SUPERSET_GROUP)) or None,
            )
            exercises[ekey] = exercise
            workouts[wkey].exercises.append(exercise)

        set_type = _clean(col(row, COL_SET_TYPE)).lower()
        if set_type not in VALID_SET_TYPES:
            set_type = "normal"

        # Наш Effort приоритетнее RPE: он точнее (RPE — производная от него).
        effort = _clean(col(row, COL_EFFORT)).lower() or None
        if not effort:
            effort = rpe_to_effort(_to_float(col(row, COL_RPE)))

        exercises[ekey].sets.append(
            ParsedSet(
                set_order=set_order,
                weight_kg=unit_to_kg(weight_raw, unit),
                reps=reps,
                notes=_clean(col(row, COL_NOTES)) or None,
                effort_level=effort,
                set_type=set_type,
                is_completed=_to_bool(col(row, COL_COMPLETED), default=True),
                parent_set_order=_to_int(col(row, COL_PARENT_SET_ORDER)),
                superset_round=_to_int(col(row, COL_SUPERSET_ROUND)),
            )
        )

    result.workouts = list(workouts.values())
    return result


_DATE_FORMATS = (
    DATE_FORMAT,  # 2020-12-30 18:51:52 — современный Strong
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y %H:%M",
    "%d.%m.%Y",
)


def _parse_date(value: str) -> Optional[datetime]:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
