from copy import deepcopy
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import api.routers.plans as plans_module
import api.routers.reports as reports_module
from api.errors import LocalizedHTTPException
from api.schemas.commands import ApplyCommandsRequest
from api.schemas.plan import GeneratedDayOut
from api.services.exercise_pattern_tags import ExerciseAction
from api.services.reports.service import enrich_report_record_localizations


class _NoQuerySession:
    async def execute(self, _statement):
        raise AssertionError("legacy report fallback must not query without canonical IDs")


@pytest.mark.asyncio
async def test_report_route_falls_back_for_legacy_records_without_canonical_ids(
    monkeypatch,
):
    stored_payload = {
        "shape_version": 2,
        "rules_version": 2,
        "metrics": {
            "records": [
                {
                    "exercise_id": None,
                    "user_exercise_id": 401,
                    "exercise_name": "Legacy custom",
                    "exercise_description": "Durable custom note",
                },
                {
                    "exercise_id": "not-an-id",
                    "exercise_name": "Imported legacy",
                    "description": "Imported durable description",
                },
                {
                    "exercise_id": -7,
                    "exercise_name": "Already localized",
                    "localized_names": {"ru": "Stored name", "en": "Stored English"},
                    "localized_descriptions": {"ru": "Stored description"},
                },
            ]
        },
        "actions": [],
    }
    original_payload = deepcopy(stored_payload)
    report = SimpleNamespace(
        shape_version=2,
        rules_version=2,
        period_type="week",
        period_start=date(2026, 8, 17),
        period_end=date(2026, 8, 23),
        generated_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
        seen_at=None,
        payload=stored_payload,
    )
    monkeypatch.setattr(reports_module, "_load", AsyncMock(return_value=report))

    response = await reports_module.get_report(
        request=SimpleNamespace(state=SimpleNamespace(language="en")),
        period_type="week",
        period_start=date(2026, 8, 17),
        current_user=SimpleNamespace(id=73),
        db=_NoQuerySession(),
    )

    records = response.metrics["records"]
    assert records[0]["localized_names"] == {"ru": "Legacy custom"}
    assert records[0]["localized_descriptions"] == {"ru": "Durable custom note"}
    assert records[1]["localized_names"] == {"ru": "Imported legacy"}
    assert records[1]["localized_descriptions"] == {
        "ru": "Imported durable description"
    }
    assert records[2]["localized_names"] == {
        "ru": "Stored name",
        "en": "Stored English",
    }
    assert records[2]["localized_descriptions"] == {"ru": "Stored description"}
    assert report.payload == original_payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metrics",
    [
        None,
        [],
        {"records": None},
        {"records": "not-a-list"},
        {"records": [None, "not-a-record", 17]},
    ],
)
async def test_report_enrichment_keeps_unknown_historical_shapes_graceful(metrics):
    original = deepcopy(metrics)

    enriched = await enrich_report_record_localizations(_NoQuerySession(), metrics)

    assert enriched == original
    if isinstance(metrics, (dict, list)):
        assert enriched is not metrics


def _canonical_exercise(
    exercise_id: int,
    *,
    name: str,
    name_en: str | None,
    description: str,
    description_en: str | None,
    source: str = "default",
    category: str = "Базовое",
):
    return SimpleNamespace(
        id=exercise_id,
        name=name,
        name_en=name_en,
        description=description,
        description_en=description_en,
        source=source,
        app_user_id=None if source == "default" else 73,
        category=category,
        main_muscle_group="Грудь",
        secondary_muscle_groups=[],
        equipment_needed=[],
        fatigue_tier=2,
        action=ExerciseAction.push,
    )


def _draft_exercise(exercise_id: int, *, name: str = "SPOOF") -> dict:
    return {
        "exercise_id": exercise_id,
        "name": name,
        "localized_names": {"ru": "SPOOF RU", "en": "SPOOF EN"},
        "localized_descriptions": {
            "ru": "SPOOF DESCRIPTION RU",
            "en": "SPOOF DESCRIPTION EN",
        },
        "target_sets": 3,
        "order_index": 0,
        "superset_group_id": None,
        "fatigue_tier": 2,
        "primary_muscle": "Грудь",
        "secondary_muscle": None,
    }


def _day(exercises: list[dict]) -> GeneratedDayOut:
    return GeneratedDayOut(
        day_tag="push",
        day_name="Push",
        coverage={"chest": {"target": 4, "filled": 3}},
        exercises=exercises,
    )


def _profile(*commands: dict):
    return SimpleNamespace(
        experience_level="intermediate",
        settings={
            "locations": ["gym"],
            "prehab_flags": [],
            "generator_rules": [
                {
                    "id": f"rule-{index}",
                    "enabled": True,
                    "scope": "all",
                    "command": command,
                }
                for index, command in enumerate(commands)
            ],
        },
    )


async def _apply_route(monkeypatch, day, pool, *, command_log=None):
    monkeypatch.setattr(
        plans_module,
        "_load_commands_context",
        AsyncMock(return_value=(_profile(), pool)),
    )
    request = ApplyCommandsRequest(
        base_draft=day,
        command_log=command_log or [],
        context={},
    )
    return await plans_module.apply_commands(
        request=request,
        db=object(),
        current_user=SimpleNamespace(id=73),
    )


@pytest.mark.asyncio
async def test_apply_command_route_rehydrates_spoofed_system_presentation(monkeypatch):
    system = _canonical_exercise(
        76,
        name="Жим лёжа",
        name_en="Bench Press",
        description="Жим штанги лёжа.",
        description_en="Barbell bench press.",
    )

    response = await _apply_route(
        monkeypatch,
        _day([_draft_exercise(76)]),
        [system],
        command_log=[{
            "type": "SET_ALL_SETS",
            "params": {"n": 4},
            "summary": "4 подхода",
        }],
    )

    row = response.final_draft.exercises[0]
    assert row.target_sets == 4
    assert row.name == "Жим лёжа"
    assert row.localized_names == {"ru": "Жим лёжа", "en": "Bench Press"}
    assert row.localized_descriptions == {
        "ru": "Жим штанги лёжа.",
        "en": "Barbell bench press.",
    }


@pytest.mark.asyncio
async def test_apply_command_route_canonicalizes_even_when_command_log_is_empty(
    monkeypatch,
):
    system = _canonical_exercise(
        76,
        name="Жим лёжа",
        name_en="Bench Press",
        description="Жим штанги лёжа.",
        description_en="Barbell bench press.",
    )

    response = await _apply_route(
        monkeypatch,
        _day([_draft_exercise(76)]),
        [system],
    )

    row = response.final_draft.exercises[0]
    assert row.name == "Жим лёжа"
    assert row.localized_names["en"] == "Bench Press"
    assert row.localized_descriptions["en"] == "Barbell bench press."


@pytest.mark.asyncio
async def test_apply_command_route_rejects_exercises_outside_authorized_pool(
    monkeypatch,
):
    with pytest.raises(LocalizedHTTPException) as exc_info:
        await _apply_route(
            monkeypatch,
            _day([_draft_exercise(999)]),
            [],
    )

    assert exc_info.value.status_code == 400
    assert exc_info.value.code == "plan.exercise_unavailable"


def test_saved_rule_insert_populates_both_system_localization_maps():
    inserted = _canonical_exercise(
        77,
        name="Разводка гантелей",
        name_en="Dumbbell Fly",
        description="Сведение гантелей лёжа.",
        description_en="Lying dumbbell fly.",
        category="Изолированное",
    )
    profile = _profile({
        "type": "ADD_MUSCLE",
        "params": {"muscles": ["chest"], "target_override": 4},
        "seed": 0,
        "summary": "Добавить грудь",
    })

    result = plans_module._apply_saved_generator_rules(
        _day([]), profile, None, [inserted], None, []
    )

    row = result.exercises[0]
    assert row.exercise_id == 77
    assert row.localized_names == {
        "ru": "Разводка гантелей",
        "en": "Dumbbell Fly",
    }
    assert row.localized_descriptions == {
        "ru": "Сведение гантелей лёжа.",
        "en": "Lying dumbbell fly.",
    }


def test_saved_rule_replace_populates_both_system_localization_maps():
    original = _canonical_exercise(
        76,
        name="Жим лёжа",
        name_en="Bench Press",
        description="Жим штанги лёжа.",
        description_en="Barbell bench press.",
    )
    replacement = _canonical_exercise(
        77,
        name="Разводка гантелей",
        name_en="Dumbbell Fly",
        description="Сведение гантелей лёжа.",
        description_en="Lying dumbbell fly.",
        category="Изолированное",
    )
    profile = _profile({
        "type": "REPLACE_EXERCISE",
        "params": {
            "from_exercise_id": 76,
            "to": {"criteria": {"category": "isolation"}},
        },
        "seed": 0,
        "summary": "Заменить на изоляцию",
    })

    result = plans_module._apply_saved_generator_rules(
        _day([_draft_exercise(76)]),
        profile,
        None,
        [original, replacement],
        None,
        [],
    )

    row = result.exercises[0]
    assert row.exercise_id == 77
    assert row.name == "Разводка гантелей"
    assert row.localized_names["en"] == "Dumbbell Fly"
    assert row.localized_descriptions["en"] == "Lying dumbbell fly."


def test_saved_rule_custom_exercise_suppresses_shadow_english_maps():
    custom = _canonical_exercise(
        501,
        name="Мой жим",
        name_en="SHADOW NAME",
        description="Моя техника",
        description_en="SHADOW DESCRIPTION",
        source="custom",
    )
    profile = _profile({
        "type": "SET_ALL_SETS",
        "params": {"n": 4},
        "summary": "4 подхода",
    })

    result = plans_module._apply_saved_generator_rules(
        _day([_draft_exercise(501)]), profile, None, [custom], None, []
    )

    row = result.exercises[0]
    assert row.name == "Мой жим"
    assert row.localized_names == {"ru": "Мой жим"}
    assert row.localized_descriptions == {"ru": "Моя техника"}
