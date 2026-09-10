from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import api.routers.workout_center as workout_center_module
from api.schemas.plan import GeneratedDayOut, GeneratedExerciseOut
from api.services.exercise_pattern_tags import ExerciseAction
from api.services.periodization.service import block_coordinate
from api.services.plan_generation_insights import explain_day
from api.services.structure.mesocycle_presets import phase_name


class _Result:
    def __init__(self, value):
        self.value = value

    def scalars(self):
        return self

    def all(self):
        return self.value

    def scalar_one(self):
        return self.value

    def scalar_one_or_none(self):
        return self.value


class _SequenceSession:
    def __init__(self, *values):
        self.values = list(values)

    async def execute(self, _statement):
        if not self.values:
            raise AssertionError("unexpected database query")
        return _Result(self.values.pop(0))


def _block(stored_phase_name: str):
    start = date(2026, 8, 30)
    return SimpleNamespace(
        id=91,
        block_index=3,
        mesocycle_id=uuid4(),
        phases=[
            {
                "phase_number": 1,
                "name": stored_phase_name,
                "effort_tier": "deload",
                "length_days": 7,
            }
        ],
        microcycle_length=7,
        start_date=start,
        planned_end_date=start + timedelta(days=6),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("global_mesocycle_id", "expected"),
    [(True, "Deload"), (False, "Alex Recovery Phase")],
)
async def test_block_coordinate_localizes_only_global_phase_snapshot(
    global_mesocycle_id, expected
):
    stored_name = "Разгрузка" if global_mesocycle_id else "Alex Recovery Phase"
    block = _block(stored_name)
    session = _SequenceSession(block.mesocycle_id if global_mesocycle_id else None)

    response = await block_coordinate(
        session,
        73,
        block,
        block.start_date,
        language="en",
    )

    assert response.phase_name == expected
    assert block.phases[0]["name"] == stored_name


async def _discard_guarded(_session, _label, operation):
    operation.close()


async def _noop(*_args, **_kwargs):
    return None


async def _workout_center_context(
    monkeypatch,
    *,
    language: str,
    is_system: bool,
    with_block: bool,
):
    import api.services.goal.service as goal_service
    import api.services.periodization.repository as periodization_repository
    import api.services.volume.repository as volume_repository
    import api.services.volume.service as volume_service

    monkeypatch.setattr(volume_repository, "guarded", _discard_guarded)
    monkeypatch.setattr(volume_repository, "mark_missed_days", _noop)
    monkeypatch.setattr(volume_service, "refresh_volume_proposals", _noop)
    monkeypatch.setattr(goal_service, "refresh_goal_proposals", _noop)
    monkeypatch.setattr(
        workout_center_module,
        "get_or_create_user_split",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        workout_center_module, "get_active_workout", AsyncMock(return_value=None)
    )

    stored_phase_name = "Разгрузка" if is_system else "Alex Recovery Phase"
    phase = SimpleNamespace(
        phase_number=1,
        name=stored_phase_name,
        effort_tier="deload",
    )
    mesocycle = SimpleNamespace(
        id=uuid4(),
        author_id=None if is_system else 73,
        code="linear_4w" if is_system else "alex_recovery",
        name="Линейный 4-недельный" if is_system else "Alex Mesocycle",
        description="stored description",
        phases_in_cycle=1,
        phases=[phase],
    )
    active_meso = SimpleNamespace(
        mesocycle=mesocycle,
        current_phase=1,
        microcycle_length=7,
    )
    active_block = _block(stored_phase_name) if with_block else None
    if active_block is not None:
        active_block.mesocycle_id = mesocycle.id
    monkeypatch.setattr(
        periodization_repository,
        "get_active_block",
        AsyncMock(return_value=active_block),
    )

    session = _SequenceSession(
        [],
        [mesocycle],
        active_meso,
        [],
        [],
    )
    response = await workout_center_module.build_context(
        session,
        SimpleNamespace(id=73, _request_language=language),
    )
    return response, phase, active_block


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["ru", "en"])
@pytest.mark.parametrize("with_block", [False, True])
async def test_workout_center_localizes_global_phase_with_and_without_block(
    monkeypatch, language, with_block
):
    response, source_phase, source_block = await _workout_center_context(
        monkeypatch,
        language=language,
        is_system=True,
        with_block=with_block,
    )
    expected = phase_name("deload", language)

    assert response.selected_periodization_phase_name == expected
    if with_block:
        assert response.active_block.phase_name == expected
    assert source_phase.name == "Разгрузка"
    if source_block is not None:
        assert source_block.phases[0]["name"] == "Разгрузка"


@pytest.mark.asyncio
@pytest.mark.parametrize("with_block", [False, True])
async def test_workout_center_preserves_custom_phase_and_source(
    monkeypatch, with_block
):
    response, source_phase, source_block = await _workout_center_context(
        monkeypatch,
        language="en",
        is_system=False,
        with_block=with_block,
    )

    assert response.selected_periodization_phase_name == "Alex Recovery Phase"
    if with_block:
        assert response.active_block.phase_name == "Alex Recovery Phase"
    assert source_phase.name == "Alex Recovery Phase"
    if source_block is not None:
        assert source_block.phases[0]["name"] == "Alex Recovery Phase"


def _partial_coverage_inputs():
    day = GeneratedDayOut(
        day_tag="push",
        day_name="Push",
        warnings=[],
        coverage={"chest": {"target": 4, "filled": 0}},
        exercises=[
            GeneratedExerciseOut(
                exercise_id=1,
                name="Push-up",
                target_sets=0,
                order_index=1,
                fatigue_tier=2,
                primary_muscle="Грудь",
            )
        ],
    )
    pool = [
        SimpleNamespace(
            id=1,
            name="Bench press",
            action=ExerciseAction.push,
            fatigue_tier=2,
            equipment_needed=["barbell"],
            main_muscle_group="Грудь",
        )
    ]
    return day, pool


@pytest.mark.parametrize(
    (
        "language",
        "context_label",
        "library_label",
    ),
    [
        ("ru", "Проверить условия генерации", "Исправить день"),
        ("en", "Review generation conditions", "Fix the day"),
    ],
)
def test_partial_coverage_ctas_keep_distinct_keys_types_and_copy(
    language, context_label, library_label
):
    day, pool = _partial_coverage_inputs()
    context_issues = explain_day(
        day,
        {"chest": 4},
        pool,
        {"bodyweight"},
        [],
        None,
        None,
        language=language,
    )
    library_issues = explain_day(
        day,
        {"chest": 4},
        [],
        None,
        [],
        None,
        None,
        language=language,
    )
    context = next(
        issue
        for issue in context_issues
        if issue.code == "muscle_target_partially_covered"
    )
    library = next(
        issue
        for issue in library_issues
        if issue.code == "muscle_target_partially_covered"
    )

    assert context.action.type == "review_limitations"
    assert context.body_key == (
        "generation.muscle_target_partially_covered.context_body"
    )
    assert context.action.label_key == (
        "generation.muscle_target_partially_covered.context_action"
    )
    assert context.action.label == context_label
    assert library.action.type == "edit_day"
    assert library.body_key == (
        "generation.muscle_target_partially_covered.library_body"
    )
    assert library.action.label_key == (
        "generation.muscle_target_partially_covered.library_action"
    )
    assert library.action.label == library_label
