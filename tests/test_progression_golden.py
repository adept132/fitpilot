"""Golden-фикстуры движка прогрессии.

Ядро — чистые функции, поэтому фикстура описывает весь контракт целиком.
Добавить случай = добавить JSON-файл, а не написать код.

Если ожидания в фикстуре разошлись с реализацией — сначала убедитесь, что
права реализация, и только потом правьте JSON.
"""

import json
from pathlib import Path

import pytest

from api.services.progression.engine import plan_exercise
from api.services.progression.types import (
    ExerciseHistory,
    Prescription,
    ProgressionState,
    SchemeContext,
    SessionFact,
    SetFact,
    SetPrescription,
)

FIXTURES = sorted((Path(__file__).parent / "progression_fixtures").glob("*.json"))


def _session(index: int, raw: dict) -> SessionFact:
    weight = raw["weight"]
    reps = raw["reps"]
    rir = raw.get("rir", 2)

    # "no_prescription": true — сессия без сохранённого предписания (импорт,
    # ручной лог, первая тренировка упражнения). Нужна, чтобы покрыть
    # бутстрап-схему e1rm_factor: resolve_scheme() выбирает её только тогда,
    # когда НИ ОДНА сессия в истории не несёт Prescription (см. resolve.py,
    # _has_prescription_history). rep_min/rep_max в фикстуре для такой
    # сессии не нужны — предписания, куда их класть, попросту нет.
    prescription = None
    if not raw.get("no_prescription", False):
        prescription = Prescription(
            scheme=raw.get("scheme", "double"),
            sets=tuple(
                SetPrescription(
                    i + 1, weight, raw["rep_min"], raw["rep_max"], rir, "normal"
                )
                for i in range(len(reps))
            ),
            reason_code="progressed",
            reason_text="x",
        )

    facts = tuple(SetFact(i + 1, weight, r, rir) for i, r in enumerate(reps))
    return SessionFact(
        session_id=index,
        finished_at=None,
        prescription=prescription,
        sets=facts,
        is_deload=raw.get("is_deload", False),
    )


def _context(case: dict) -> SchemeContext:
    sessions = [_session(i + 1, s) for i, s in enumerate(case["sessions"])]
    raw = dict(case["ctx"])
    raw["equipment"] = tuple(raw.get("equipment", ()))
    raw.setdefault("rep_range_source", "tier_fallback")
    return SchemeContext(
        history=ExerciseHistory(exercise_id=1, sessions=tuple(reversed(sessions))),
        state=ProgressionState(),
        last_outcome=None,
        **raw,
    )


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_golden_case(path: Path):
    case = json.loads(path.read_text(encoding="utf-8"))
    result = plan_exercise(_context(case), override=case.get("override"))

    expected = case["expect"]
    assert result.scheme == expected["scheme"], case["name"]
    assert result.reason_code == expected["reason_code"], case["name"]
    assert len(result.sets) == len(expected["sets"]), case["name"]

    for actual, want in zip(result.sets, expected["sets"]):
        if want["weight_kg"] is None:
            assert actual.weight_kg is None
        else:
            assert actual.weight_kg == pytest.approx(want["weight_kg"], abs=0.01)
        assert actual.rep_min == want["rep_min"]
        assert actual.rep_max == want["rep_max"]
        assert actual.kind == want["kind"]


def test_fixture_directory_is_not_empty():
    assert FIXTURES, "golden-фикстуры не найдены"


def test_every_scheme_has_at_least_one_fixture():
    schemes = set()
    for path in FIXTURES:
        schemes.add(json.loads(path.read_text(encoding="utf-8"))["expect"]["scheme"])
    assert {"double", "fixed_increment", "percent_1rm", "e1rm_factor"} <= schemes
