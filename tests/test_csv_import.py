"""Тесты парсера импорта: современный Strong, легаси Strong, наш формат, мусор."""

import pytest

from api.services.csv_import import CsvParseError, parse_csv, sniff_delimiter
from api.services.csv_format import UNIT_KG, UNIT_LBS

# Реальный фрагмент экспорта Strong (StrongAppAnalytics/Data/strong.csv).
STRONG_MODERN = (
    "Date,Workout Name,Duration,Exercise Name,Set Order,Weight,Reps,Distance,Seconds,Notes,Workout Notes,RPE\n"
    '2020-12-30 18:51:52,"Evening Workout",2h 38m,"Snatch (Barbell)",1,40.0,3,0,0,"","",\n'
    '2020-12-30 18:51:52,"Evening Workout",2h 38m,"Snatch (Barbell)",2,40.0,3,0,0,,,\n'
    '2020-12-30 18:51:52,"Evening Workout",2h 38m,"Squat (Barbell)",1,60.0,5,0,0,,,8\n'
    '2021-01-02 10:00:00,"Morning",45m,"Bench Press (Barbell)",1,80.0,5,0,0,,,\n'
)

# Легаси-формат: ';' + колонки единиц.
STRONG_LEGACY = (
    "Date;Workout Name;Duration;Exercise Name;Set Order;Weight;Weight Unit;Reps;Distance;Distance Unit;Seconds;Notes;Workout Notes\n"
    "2019-05-05 09:00:00;Workout A;1h;Bench Press (Barbell);1;100;lbs;5;0;km;0;;\n"
)


def test_sniff_delimiter():
    assert sniff_delimiter(STRONG_MODERN) == ","
    assert sniff_delimiter(STRONG_LEGACY) == ";"


def test_parses_modern_strong_into_workouts():
    r = parse_csv(STRONG_MODERN, default_unit=UNIT_KG)
    assert len(r.workouts) == 2
    assert not r.skipped

    first = r.workouts[0]
    assert first.name == "Evening Workout"
    assert first.duration_seconds == 9480  # 2h 38m
    assert len(first.exercises) == 2
    assert first.exercises[0].name == "Snatch (Barbell)"
    assert len(first.exercises[0].sets) == 2
    assert first.total_sets == 3


def test_groups_by_date_and_keeps_file_order():
    r = parse_csv(STRONG_MODERN)
    assert [w.name for w in r.workouts] == ["Evening Workout", "Morning"]
    assert [e.name for e in r.workouts[0].exercises] == [
        "Snatch (Barbell)",
        "Squat (Barbell)",
    ]


def test_import_key_is_stable_and_unique_per_workout():
    a = parse_csv(STRONG_MODERN)
    b = parse_csv(STRONG_MODERN)
    assert a.workouts[0].import_key == b.workouts[0].import_key  # детерминирован
    assert a.workouts[0].import_key != a.workouts[1].import_key


def test_exercise_names_are_collected_for_mapping_screen():
    r = parse_csv(STRONG_MODERN)
    assert r.exercise_names == [
        "Snatch (Barbell)",
        "Squat (Barbell)",
        "Bench Press (Barbell)",
    ]


# --- Единицы ---

def test_default_unit_used_when_no_unit_column():
    r = parse_csv(STRONG_MODERN, default_unit=UNIT_KG)
    assert r.file_unit is None  # файл не сообщил единицу
    assert r.workouts[0].exercises[0].sets[0].weight_kg == 40.0


def test_default_unit_lbs_is_converted_to_kg():
    r = parse_csv(STRONG_MODERN, default_unit=UNIT_LBS)
    # 40 lbs -> 18.14 кг
    assert r.workouts[0].exercises[0].sets[0].weight_kg == 18.14


def test_legacy_unit_column_wins_over_default():
    # В файле сказано lbs, а по умолчанию просят кг — верим файлу.
    r = parse_csv(STRONG_LEGACY, default_unit=UNIT_KG)
    assert r.file_unit == UNIT_LBS
    assert r.workouts[0].exercises[0].sets[0].weight_kg == 45.36  # 100 lbs


def test_decimal_comma_is_parsed():
    csv_text = (
        "Date,Workout Name,Exercise Name,Set Order,Weight,Reps\n"
        "2021-01-01 10:00:00,W,Жим,1,\"82,5\",5\n"
    )
    r = parse_csv(csv_text)
    assert r.workouts[0].exercises[0].sets[0].weight_kg == 82.5


# --- RPE / усилие ---

def test_rpe_is_mapped_to_effort():
    r = parse_csv(STRONG_MODERN)
    squat = r.workouts[0].exercises[1].sets[0]
    assert squat.effort_level == "medium"  # RPE 8 -> RIR 2


def test_missing_rpe_leaves_effort_empty():
    r = parse_csv(STRONG_MODERN)
    assert r.workouts[0].exercises[0].sets[0].effort_level is None


# --- Наш собственный формат (round-trip) ---

def test_parses_eurith_extensions():
    csv_text = (
        "Date,Workout Name,Duration,Exercise Name,Set Order,Weight,Reps,Distance,Seconds,Notes,Workout Notes,RPE,"
        "Weight Unit,Set Type,Effort,Completed,Parent Set Order,Superset Group,Superset Round,Exercise ID,Meso Phase,Micro Day\n"
        "2026-07-16 18:00:00,План А,1h,Жим штанги лёжа,1,100,5,0,0,заметка,общая,8,"
        "kg,drop,prefailure,false,2,grp-1,3,42,2,3\n"
    )
    r = parse_csv(csv_text)
    ex = r.workouts[0].exercises[0]
    s = ex.sets[0]
    assert ex.exercise_id == 42
    assert ex.superset_group == "grp-1"
    assert s.set_type == "drop"
    assert s.effort_level == "prefailure"  # Effort приоритетнее RPE
    assert s.is_completed is False
    assert s.parent_set_order == 2
    assert s.superset_round == 3
    assert s.notes == "заметка"
    assert r.workouts[0].notes == "общая"


def test_our_effort_column_beats_rpe():
    # RPE=8 (medium), но Effort говорит failure — верим Effort.
    csv_text = (
        "Date,Exercise Name,Set Order,Weight,Reps,RPE,Effort\n"
        "2026-07-16 18:00:00,Жим,1,100,5,8,failure\n"
    )
    r = parse_csv(csv_text)
    assert r.workouts[0].exercises[0].sets[0].effort_level == "failure"


# --- Мусор и краевые случаи ---

def test_skips_rows_and_reports_them():
    csv_text = (
        "Date,Workout Name,Exercise Name,Set Order,Weight,Reps,Distance,Seconds\n"
        "2021-01-01 10:00:00,W,Жим,1,100,5,0,0\n"
        "2021-01-01 10:00:00,W,Бег,1,,,5,600\n"  # кардио
        "2021-01-01 10:00:00,W,Жим,Rest Timer,,,0,0\n"  # служебная строка
        "2021-01-01 10:00:00,W,,1,100,5,0,0\n"  # нет имени упражнения
        ",W,Жим,1,100,5,0,0\n"  # нет даты
        "не-дата,W,Жим,1,100,5,0,0\n"  # битая дата
    )
    r = parse_csv(csv_text)
    assert r.workouts[0].total_sets == 1  # доехал только валидный подход
    assert len(r.skipped) == 5
    reasons = " | ".join(s.reason for s in r.skipped)
    assert "кардио" in reasons
    assert "нечисловой номер подхода" in reasons
    assert "пустое название" in reasons
    assert "пустая дата" in reasons
    assert "не распознана дата" in reasons


def test_bom_is_stripped():
    r = parse_csv("﻿" + STRONG_MODERN)
    assert len(r.workouts) == 2


def test_case_insensitive_headers():
    csv_text = (
        "date,workout name,exercise name,set order,weight,reps\n"
        "2021-01-01 10:00:00,W,Жим,1,100,5\n"
    )
    r = parse_csv(csv_text)
    assert r.workouts[0].exercises[0].sets[0].reps == 5


@pytest.mark.parametrize("text", ["", "   "])
def test_empty_file_raises(text):
    with pytest.raises(CsvParseError):
        parse_csv(text)


def test_missing_required_columns_raises():
    with pytest.raises(CsvParseError, match="обязательных колонок"):
        parse_csv("Foo,Bar\n1,2\n")


def test_workout_without_name_gets_default():
    csv_text = "Date,Exercise Name,Set Order,Weight,Reps\n2021-01-01 10:00:00,Жим,1,100,5\n"
    r = parse_csv(csv_text)
    assert r.workouts[0].name == "Свободная тренировка"
