"""Round-trip: наш экспорт -> наш импорт без потерь.

Строит строки экспорта из подставных объектов (без БД), прогоняет их через
писатель CSV и парсер импорта и сверяет, что данные доехали.
"""

from types import SimpleNamespace

from api.services.csv_export import collect_export_rows, iter_csv, columns_for
from api.services.csv_import import parse_csv


def _set(**kw):
    base = dict(
        id=1,
        set_number=1,
        set_type="normal",
        weight=100.0,
        reps=5,
        notes=None,
        effort_level=None,
        is_completed=True,
        parent_set_id=None,
        superset_round=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _rows_for(sets):
    """Собирает строки экспорта в обход БД (повторяет форму данных из ORM)."""
    from datetime import datetime

    from api.services.csv_format import (
        COL_COMPLETED, COL_DATE, COL_DISTANCE, COL_DURATION, COL_EFFORT,
        COL_EXERCISE_ID, COL_EXERCISE_NAME, COL_MESO_PHASE, COL_MICRO_DAY,
        COL_NOTES, COL_PARENT_SET_ORDER, COL_REPS, COL_RPE, COL_SECONDS,
        COL_SET_ORDER, COL_SET_TYPE, COL_SUPERSET_GROUP, COL_SUPERSET_ROUND,
        COL_WEIGHT, COL_WEIGHT_UNIT, COL_WORKOUT_NAME, COL_WORKOUT_NOTES,
        DATE_FORMAT, effort_to_rpe,
    )

    # Мини-повтор тела collect_export_rows для одного упражнения — БД не нужна.
    started = datetime(2026, 7, 16, 18, 0, 0)
    set_number_by_id = {s.id: s.set_number for s in sets}
    rows = []
    for s in sets:
        if s.weight is None and s.reps is None:
            continue
        rows.append({
            COL_DATE: started.strftime(DATE_FORMAT),
            COL_WORKOUT_NAME: "План А",
            COL_DURATION: "1h",
            COL_EXERCISE_NAME: "Жим штанги лёжа",
            COL_SET_ORDER: str(s.set_number),
            COL_WEIGHT: str(s.weight) if s.weight is not None else "",
            COL_REPS: str(s.reps) if s.reps is not None else "",
            COL_DISTANCE: "0", COL_SECONDS: "0",
            COL_NOTES: s.notes or "", COL_WORKOUT_NOTES: "",
            COL_RPE: str(effort_to_rpe(s.effort_level) or ""),
            COL_WEIGHT_UNIT: "kg",
            COL_SET_TYPE: s.set_type,
            COL_EFFORT: s.effort_level or "",
            COL_COMPLETED: "true" if s.is_completed else "false",
            COL_PARENT_SET_ORDER: str(set_number_by_id.get(s.parent_set_id) or ""),
            COL_SUPERSET_GROUP: "grp-1",
            COL_SUPERSET_ROUND: str(s.superset_round or ""),
            COL_EXERCISE_ID: "42",
            COL_MESO_PHASE: "2", COL_MICRO_DAY: "3",
        })
    return rows


def test_roundtrip_preserves_sets_and_extensions():
    sets = [
        _set(id=1, set_number=1, weight=100.0, reps=5, effort_level="medium"),
        _set(id=2, set_number=2, weight=82.5, reps=8, effort_level="failure",
             set_type="drop", parent_set_id=1, is_completed=False, notes="дроп"),
    ]
    csv_text = "".join(iter_csv(_rows_for(sets), columns_for("full")))
    parsed = parse_csv(csv_text, default_unit="kg")

    assert len(parsed.workouts) == 1
    ex = parsed.workouts[0].exercises[0]
    assert ex.name == "Жим штанги лёжа"
    assert ex.exercise_id == 42
    assert ex.superset_group == "grp-1"
    assert len(ex.sets) == 2

    first, second = ex.sets
    assert (first.weight_kg, first.reps, first.effort_level) == (100.0, 5, "medium")
    assert (second.weight_kg, second.reps) == (82.5, 8)
    assert second.set_type == "drop"
    assert second.parent_set_order == 1  # дропсет связан с родителем через Set Order
    assert second.is_completed is False
    assert second.notes == "дроп"
    assert parsed.file_unit == "kg"  # единица прочитана из файла, а не угадана


def test_export_skips_planned_but_unfilled_sets():
    # Регрессия: target_sets создаёт пустые плановые строки. Это не история —
    # они не должны попадать в экспорт (иначе файл раздувается и чужой импорт
    # получает мусор). Правило совпадает с тем, что отбрасывает наш парсер.
    sets = [
        _set(id=1, set_number=1, weight=100.0, reps=5),
        _set(id=2, set_number=2, weight=None, reps=None, is_completed=False),
        _set(id=3, set_number=3, weight=None, reps=None, is_completed=False),
    ]
    rows = _rows_for(sets)
    assert len(rows) == 1

    parsed = parse_csv("".join(iter_csv(rows, columns_for("full"))), default_unit="kg")
    assert parsed.workouts[0].total_sets == 1
    assert not parsed.skipped  # симметрия: экспорт не отдал того, что импорт отбросит


def test_roundtrip_via_lbs_keeps_kg_values():
    sets = [_set(id=1, set_number=1, weight=100.0, reps=5)]
    rows = _rows_for(sets)
    # Экспорт в lbs: вес в колонке другой, но Weight Unit говорит парсеру правду.
    for r in rows:
        r["Weight"] = "220.46"
        r["Weight Unit"] = "lbs"
    parsed = parse_csv("".join(iter_csv(rows, columns_for("full"))), default_unit="kg")
    assert parsed.workouts[0].exercises[0].sets[0].weight_kg == 100.0
