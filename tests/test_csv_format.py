"""Тесты формата «Eurith CSV v1».

Главный тест здесь — STRONG_HEADER_FROM_REAL_EXPORT: это контракт совместимости
со Strong. Если он падает, значит мы сломали импорт в чужие приложения.
"""

import pytest

from api.services.csv_export import iter_csv, columns_for
from api.services.csv_format import (
    EURITH_COLUMNS,
    FULL_COLUMNS,
    STRONG_COLUMNS,
    UNIT_KG,
    UNIT_LBS,
    effort_to_rpe,
    format_duration,
    kg_to_unit,
    normalize_unit,
    parse_duration,
    rpe_to_effort,
    unit_to_kg,
)

# Заголовок, снятый с реального экспорта Strong (StrongAppAnalytics/Data/strong.csv).
STRONG_HEADER_FROM_REAL_EXPORT = (
    "Date,Workout Name,Duration,Exercise Name,Set Order,Weight,Reps,"
    "Distance,Seconds,Notes,Workout Notes,RPE"
)


def test_strong_columns_match_real_export_byte_for_byte():
    assert ",".join(STRONG_COLUMNS) == STRONG_HEADER_FROM_REAL_EXPORT


def test_full_format_is_strict_superset_of_strong():
    # Первые 12 колонок обязаны идти ровно в порядке Strong, иначе чужие
    # парсеры, читающие по позициям, развалятся.
    assert FULL_COLUMNS[: len(STRONG_COLUMNS)] == STRONG_COLUMNS
    assert FULL_COLUMNS[len(STRONG_COLUMNS) :] == EURITH_COLUMNS


# --- Длительность ---

@pytest.mark.parametrize(
    "seconds,expected",
    [(9480, "2h 38m"), (2700, "45m"), (3600, "1h"), (0, ""), (None, "")],
)
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


@pytest.mark.parametrize(
    "text,expected",
    [("2h 38m", 9480), ("45m", 2700), ("1h", 3600), ("1h 2m 3s", 3723), ("", None), (None, None)],
)
def test_parse_duration(text, expected):
    assert parse_duration(text) == expected


def test_duration_roundtrip():
    assert format_duration(parse_duration("2h 38m")) == "2h 38m"


# --- Единицы ---

def test_weight_roundtrip_lbs():
    assert unit_to_kg(kg_to_unit(100, UNIT_LBS), UNIT_LBS) == 100.0


def test_weight_kg_is_identity():
    assert kg_to_unit(82.5, UNIT_KG) == 82.5
    assert unit_to_kg(82.5, UNIT_KG) == 82.5


@pytest.mark.parametrize(
    "raw,expected",
    [("kg", UNIT_KG), ("KG", UNIT_KG), ("кг", UNIT_KG), ("lbs", UNIT_LBS),
     ("LB", UNIT_LBS), ("pounds", UNIT_LBS), ("bogus", None), ("", None), (None, None)],
)
def test_normalize_unit(raw, expected):
    assert normalize_unit(raw) == expected


# --- Усилие <-> RPE ---

@pytest.mark.parametrize("effort", ["warmup", "light", "medium", "prefailure", "failure"])
def test_effort_rpe_roundtrip(effort):
    assert rpe_to_effort(effort_to_rpe(effort)) == effort


def test_effort_to_rpe_known_values():
    assert effort_to_rpe("failure") == 10  # RIR 0
    assert effort_to_rpe("medium") == 8  # RIR 2
    assert effort_to_rpe(None) is None


def test_rpe_out_of_scale_is_clamped():
    assert rpe_to_effort(11) == "failure"  # RIR < 0 -> зажимаем
    assert rpe_to_effort(1) == "warmup"  # RIR > 4 -> зажимаем


# --- Запись CSV ---

def _row(**overrides):
    row = {c: "" for c in FULL_COLUMNS}
    row.update(overrides)
    return row


def test_iter_csv_emits_bom_and_strong_header():
    out = "".join(iter_csv([], columns_for("strong")))
    assert out.startswith("﻿")  # без BOM Excel ломает кириллицу
    assert out[1:].strip() == STRONG_HEADER_FROM_REAL_EXPORT


def test_iter_csv_strong_format_drops_extensions():
    out = "".join(iter_csv([_row(**{"Exercise Name": "Жим", "Set Type": "drop"})], columns_for("strong")))
    assert "Set Type" not in out
    assert "drop" not in out
    assert "Жим" in out


def test_iter_csv_full_format_keeps_extensions():
    out = "".join(iter_csv([_row(**{"Set Type": "drop", "Exercise ID": "42"})], columns_for("full")))
    assert "Set Type" in out
    assert "drop" in out


def test_iter_csv_quotes_values_with_commas():
    out = "".join(iter_csv([_row(**{"Notes": "note, with comma"})], columns_for("full")))
    assert '"note, with comma"' in out


# --- Часовые пояса ---
# Регрессия: в файле дата — настенное время без смещения, а в БД это
# timestamptz. Без явной конверсии наивную дату интерпретирует таймзона
# соединения с БД, и на каждом цикле импорт->экспорт время уезжает.

from datetime import datetime, timezone as dt_timezone  # noqa: E402

from api.services.csv_format import utc_to_wall_clock, wall_clock_to_utc  # noqa: E402


def test_wall_clock_to_utc_uses_user_timezone():
    naive = datetime(2019, 3, 1, 10, 0, 0)  # 10:00 у пользователя в Москве
    utc = wall_clock_to_utc(naive, "Europe/Moscow")
    assert utc.hour == 7  # -> 07:00 UTC
    assert utc.tzinfo is not None


def test_wall_clock_roundtrip_is_exact():
    naive = datetime(2019, 3, 1, 10, 0, 0)
    for tz in ("Europe/Moscow", "UTC", "America/New_York"):
        back = utc_to_wall_clock(wall_clock_to_utc(naive, tz), tz)
        assert back.strftime("%Y-%m-%d %H:%M:%S") == "2019-03-01 10:00:00", tz


def test_naive_utc_default_when_timezone_unknown():
    naive = datetime(2019, 3, 1, 10, 0, 0)
    assert wall_clock_to_utc(naive, None).hour == 10
    assert wall_clock_to_utc(naive, "Bogus/Zone").hour == 10  # битая зона -> UTC


def test_utc_to_wall_clock_handles_naive_input():
    naive = datetime(2019, 3, 1, 7, 0, 0)  # из БД могло прийти без tzinfo
    assert utc_to_wall_clock(naive, "Europe/Moscow").hour == 10


def test_aware_input_is_converted_not_relabelled():
    aware = datetime(2019, 3, 1, 7, 0, 0, tzinfo=dt_timezone.utc)
    assert wall_clock_to_utc(aware, "Europe/Moscow").hour == 7
