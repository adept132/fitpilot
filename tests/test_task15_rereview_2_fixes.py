from copy import deepcopy
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import api.routers.plans as plans_module
import api.routers.reports as reports_module
from api.schemas.plan import (
    GenerateConfig,
    GeneratedDayOut,
    GeneratedExerciseOut,
    GenerationComparison,
    GeneratePlanPreviewRequest,
)
from api.services.exercise_pattern_tags import ExerciseAction
from api.services.plan_generation_insights import compare_generated_day


def _canonical_exercise(
    exercise_id: int,
    *,
    name: str,
    name_en: str | None,
    description: str,
    description_en: str | None,
    source: str = "default",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=exercise_id,
        name=name,
        name_en=name_en,
        description=description,
        description_en=description_en,
        source=source,
        app_user_id=None if source == "default" else 73,
        action=ExerciseAction.push,
        fatigue_tier=2,
        equipment_needed=["dumbbell"],
        main_muscle_group="Грудь",
        secondary_muscle_groups=[],
    )


def _generated_row(exercise_id: int, *, name: str, sets: int) -> GeneratedExerciseOut:
    return GeneratedExerciseOut(
        exercise_id=exercise_id,
        name=name,
        localized_names={"ru": name},
        target_sets=sets,
        order_index=exercise_id,
        fatigue_tier=2,
        primary_muscle="Грудь",
    )


def _previous_plan(exercises: list[SimpleNamespace]) -> SimpleNamespace:
    rows = [
        SimpleNamespace(
            exercise_id=exercise.id,
            exercise=exercise,
            target_sets=3,
            order_index=index,
            superset_group_id=None,
            override_reps=None,
            override_rir=None,
        )
        for index, exercise in enumerate(exercises, start=1)
    ]
    return SimpleNamespace(id=41, name="Stored push", exercises=rows)


@pytest.mark.asyncio
async def test_preview_rehydrates_names_before_comparison_and_returns_same_maps(
    monkeypatch,
):
    bench = _canonical_exercise(
        1,
        name="Жим лёжа",
        name_en="Bench Press",
        description="Жим штанги лёжа.",
        description_en="Barbell bench press.",
    )
    fly = _canonical_exercise(
        2,
        name="Разводка гантелей",
        name_en="Dumbbell Fly",
        description="Сведение гантелей лёжа.",
        description_en="Lying dumbbell fly.",
    )
    pool = [bench, fly]
    previous = _previous_plan(pool)
    profile = SimpleNamespace(
        experience_level="intermediate",
        settings={"locations": ["gym"], "prehab_flags": []},
        volume_budget={"weekly_targets": {}},
    )
    day_blueprint = SimpleNamespace(
        name="Push", template_type="push", muscle_targets=[]
    )
    blueprint = SimpleNamespace(
        id=uuid4(),
        name="Push split",
        length_days=1,
        slots=[SimpleNamespace(day_order=1, day=day_blueprint)],
    )
    request = GeneratePlanPreviewRequest(
        blueprint_id=blueprint.id,
        config=GenerateConfig(),
        days=[GeneratedDayOut(
            day_tag="Push",
            day_name="Push",
            coverage={},
            exercises=[
                _generated_row(1, name="SPOOFED BENCH", sets=3),
                _generated_row(2, name="SPOOFED FLY", sets=4),
            ],
        )],
    )

    async def comparison(_db, _user, _blueprint, days, target_date, single_day=False):
        compared = compare_generated_day(
            days[0], previous_plans=[previous], sources={41: "split"}, affected_dates=[]
        )
        return GenerationComparison(
            applied_from=target_date or date.today(),
            mode="single_day" if single_day else "full",
            days=[compared],
        )

    monkeypatch.setattr(
        plans_module,
        "_load_generation_context",
        AsyncMock(return_value=(profile, blueprint, pool)),
    )
    monkeypatch.setattr(plans_module, "_generation_comparison", comparison)
    monkeypatch.setattr(
        plans_module, "_day_effort_by_tag", AsyncMock(return_value={})
    )
    monkeypatch.setattr(plans_module, "estimate_duration_seconds", lambda *_a, **_k: 120)
    monkeypatch.setattr(
        plans_module.VolumeService,
        "calculate_session_targets",
        AsyncMock(return_value={}),
    )

    response = await plans_module.preview_generated_plan(
        request=request,
        db=object(),
        current_user=SimpleNamespace(id=73, _request_language="en"),
    )

    assert [row.name for row in response.days[0].exercises] == [
        "Жим лёжа",
        "Разводка гантелей",
    ]
    assert response.days[0].exercises[1].localized_names == {
        "ru": "Разводка гантелей",
        "en": "Dumbbell Fly",
    }
    compared = response.comparison.days[0]
    assert compared.status == "changed"
    assert [row.exercise_id for row in compared.modified_exercises] == [2]
    assert compared.modified_exercises[0].name == "Разводка гантелей"
    assert compared.modified_exercises[0].localized_names == {
        "ru": "Разводка гантелей",
        "en": "Dumbbell Fly",
    }
    assert compared.modified_exercises[0].localized_descriptions == {
        "ru": "Сведение гантелей лёжа.",
        "en": "Lying dumbbell fly.",
    }


def test_comparison_maps_cover_added_modified_and_removed_custom_exercises():
    bench = _canonical_exercise(
        1,
        name="Жим лёжа",
        name_en="Bench Press",
        description="Жим штанги лёжа.",
        description_en="Barbell bench press.",
    )
    fly = _canonical_exercise(
        2,
        name="Разводка гантелей",
        name_en="Dumbbell Fly",
        description="Сведение гантелей лёжа.",
        description_en="Lying dumbbell fly.",
    )
    custom = _canonical_exercise(
        3,
        name="Моя тяга",
        name_en="SHADOW MUST STAY HIDDEN",
        description="Моя заметка",
        description_en="SHADOW DESCRIPTION",
        source="custom",
    )
    previous = _previous_plan([bench, custom])
    day = GeneratedDayOut(
        day_tag="Push",
        day_name="Push",
        coverage={},
        exercises=[
            _generated_row(1, name=bench.name, sets=4).model_copy(update={
                "localized_names": {"ru": "Жим лёжа", "en": "Bench Press"},
                "localized_descriptions": {
                    "ru": "Жим штанги лёжа.",
                    "en": "Barbell bench press.",
                },
            }),
            _generated_row(2, name=fly.name, sets=3).model_copy(update={
                "localized_names": {"ru": "Разводка гантелей", "en": "Dumbbell Fly"},
                "localized_descriptions": {
                    "ru": "Сведение гантелей лёжа.",
                    "en": "Lying dumbbell fly.",
                },
            }),
        ],
    )

    compared = compare_generated_day(
        day,
        previous_plans=[previous],
        sources={41: "split"},
        affected_dates=[],
    )

    assert compared.added_exercises[0].localized_names == {
        "ru": "Разводка гантелей",
        "en": "Dumbbell Fly",
    }
    assert compared.added_exercises[0].localized_descriptions["en"] == (
        "Lying dumbbell fly."
    )
    assert compared.modified_exercises[0].localized_names["en"] == "Bench Press"
    assert compared.modified_exercises[0].localized_descriptions["ru"] == (
        "Жим штанги лёжа."
    )
    assert compared.removed_exercises[0].localized_names == {"ru": "Моя тяга"}
    assert compared.removed_exercises[0].localized_descriptions == {
        "ru": "Моя заметка"
    }


class _ExerciseResult:
    def __init__(self, exercises: list[SimpleNamespace]):
        self._exercises = exercises

    def scalars(self):
        return self

    def all(self):
        return self._exercises


class _HistoricalReportSession:
    def __init__(self, exercises: list[SimpleNamespace]):
        self._exercises = exercises
        self.execute_count = 0

    async def execute(self, _statement):
        self.execute_count += 1
        if self.execute_count > 1:
            raise AssertionError("historical report localization used N+1 queries")
        return _ExerciseResult(self._exercises)


@pytest.mark.asyncio
async def test_historical_report_enriches_copy_in_one_query_with_safe_fallbacks(
    monkeypatch,
):
    system = _canonical_exercise(
        76,
        name="Жим лёжа",
        name_en="Bench Press",
        description="Жим штанги лёжа.",
        description_en="Barbell bench press.",
    )
    custom = _canonical_exercise(
        501,
        name="Мой жим",
        name_en="SHADOW MUST STAY HIDDEN",
        description="Моя заметка",
        description_en="SHADOW DESCRIPTION",
        source="custom",
    )
    stored_payload = {
        "shape_version": 2,
        "rules_version": 2,
        "metrics": {
            "records": [
                {"exercise_id": 76, "exercise_name": "Старый жим"},
                {"exercise_id": 501, "exercise_name": "Мой жим"},
                {"exercise_id": 999, "exercise_name": "Удалённое упражнение"},
                {
                    "exercise_id": 77,
                    "exercise_name": "Already localized",
                    "localized_names": {"ru": "Сохранено", "en": "Stored"},
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
    session = _HistoricalReportSession([system, custom])
    monkeypatch.setattr(reports_module, "_load", AsyncMock(return_value=report))

    response = await reports_module.get_report(
        request=SimpleNamespace(state=SimpleNamespace(language="en")),
        period_type="week",
        period_start=date(2026, 8, 17),
        current_user=SimpleNamespace(id=73),
        db=session,
    )

    records = response.metrics["records"]
    assert records[0]["exercise_name"] == "Старый жим"
    assert records[0]["localized_names"] == {
        "ru": "Жим лёжа",
        "en": "Bench Press",
    }
    assert records[1]["localized_names"] == {"ru": "Мой жим"}
    assert records[2]["exercise_id"] == 999
    assert records[2]["exercise_name"] == "Удалённое упражнение"
    assert records[2]["localized_names"] == {"ru": "Удалённое упражнение"}
    assert records[3]["localized_names"] == {"ru": "Сохранено", "en": "Stored"}
    assert report.payload == original_payload
    assert session.execute_count == 1
