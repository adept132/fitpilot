from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import Request

import api.routers.reports as reports_module
import api.routers.splits as splits_module
import api.routers.workout_center as workout_center_module
from api.services.structure.mesocycle_presets import localized_preset
from api.services.structure.split_catalog import SPLITS, localized_split


def _request(language: str) -> Request:
    request = Request({"type": "http", "method": "GET", "path": "/"})
    request.state.language = language
    return request


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
        self.commit = AsyncMock()

    async def execute(self, _statement):
        if not self.values:
            raise AssertionError("unexpected database query")
        return _Result(self.values.pop(0))


def _report_payload() -> dict:
    return {
        "metrics": {
            "adherence": {"completed_days": 1, "planned_days": 3},
            "volume": {"work_sets": 8},
            "records": [{"kind": "e1rm"}],
        },
        "actions": [],
    }


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        (
            "ru",
            [
                ("Выполнено", "1 из 3"),
                ("Подходов", "8"),
                ("Рекордов", "1"),
            ],
        ),
        (
            "en",
            [
                ("Completed", "1 of 3"),
                ("Sets", "8"),
                ("Records", "1"),
            ],
        ),
    ],
)
def test_report_headline_renders_all_labels_and_values_from_request_locale(
    language, expected
):
    headline = reports_module._headline(_report_payload(), language)

    assert [(item.label, item.value) for item in headline] == expected


@pytest.mark.asyncio
async def test_report_list_threads_request_locale_into_existing_card(monkeypatch):
    now = datetime(2026, 8, 30, tzinfo=timezone.utc)
    report = SimpleNamespace(
        period_type="week",
        period_start=date(2026, 8, 17),
        period_end=date(2026, 8, 23),
        generated_at=now,
        seen_at=None,
        payload=_report_payload(),
    )
    session = _SequenceSession([report], 1)
    monkeypatch.setattr(reports_module, "ensure_reports", AsyncMock(return_value=0))

    response = await reports_module.list_reports(
        request=_request("en"),
        local_date=date(2026, 8, 30),
        limit=20,
        current_user=SimpleNamespace(id=73),
        db=session,
    )

    assert [(item.label, item.value) for item in response.items[0].headline] == [
        ("Completed", "1 of 3"),
        ("Sets", "8"),
        ("Records", "1"),
    ]


def _split_blueprint(*, system: bool, name: str):
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        author_id=None if system else 73,
        length_days=7,
        is_system=system,
        slots=[],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["ru", "en"])
async def test_active_split_localizes_system_name(language):
    stored = next(split for split in SPLITS if split.code == "upper_lower_2")
    blueprint = _split_blueprint(system=True, name=stored.name)
    active = SimpleNamespace(
        id=11,
        blueprint_id=blueprint.id,
        blueprint=blueprint,
        selected_plans={"blackout_weekdays": [6]},
        start_date=date(2026, 8, 24),
        current_day=2,
    )

    response = await splits_module.get_active_split(
        request=_request(language),
        session=_SequenceSession(active),
        current_user=SimpleNamespace(id=73),
    )

    assert response["blueprint_name"] == localized_split(stored.code, language).name
    assert blueprint.name == stored.name


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["ru", "en"])
async def test_split_detail_localizes_system_name(language):
    stored = next(split for split in SPLITS if split.code == "upper_lower_2")
    blueprint = _split_blueprint(system=True, name=stored.name)

    response = await splits_module.get_custom_split_details(
        request=_request(language),
        blueprint_id=blueprint.id,
        session=_SequenceSession(blueprint),
        current_user=SimpleNamespace(id=73),
    )

    assert response.name == localized_split(stored.code, language).name
    assert blueprint.name == stored.name


@pytest.mark.asyncio
async def test_split_detail_preserves_custom_name_byte_for_byte():
    blueprint = _split_blueprint(
        system=False,
        name="Верх / низ (2 дня) — Alex CUSTOM",
    )

    response = await splits_module.get_custom_split_details(
        request=_request("en"),
        blueprint_id=blueprint.id,
        session=_SequenceSession(blueprint),
        current_user=SimpleNamespace(id=73),
    )

    assert response.name == "Верх / низ (2 дня) — Alex CUSTOM"
    assert blueprint.name == "Верх / низ (2 дня) — Alex CUSTOM"


async def _discard_guarded(_session, _label, operation):
    operation.close()


async def _noop(*_args, **_kwargs):
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["ru", "en"])
async def test_workout_center_localizes_only_system_structure_names(
    monkeypatch, language
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
        periodization_repository, "get_active_block", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        workout_center_module, "get_active_workout", AsyncMock(return_value=None)
    )

    stored_split = next(split for split in SPLITS if split.code == "upper_lower_2")
    system_split = _split_blueprint(system=True, name=stored_split.name)
    custom_split = _split_blueprint(
        system=False, name="Верх / низ (2 дня) — Alex CUSTOM"
    )
    user_split = SimpleNamespace(
        blueprint=system_split,
        current_day=1,
        selected_plans={},
    )
    monkeypatch.setattr(
        workout_center_module,
        "get_or_create_user_split",
        AsyncMock(return_value=user_split),
    )

    system_meso = SimpleNamespace(
        id=uuid4(),
        author_id=None,
        code="linear_4w",
        name="Линейный 4-недельный",
        description="stored system description",
        phases_in_cycle=4,
        phases=[],
    )
    custom_meso = SimpleNamespace(
        id=uuid4(),
        author_id=73,
        code="linear_4w",
        name="Линейный 4-недельный — Alex CUSTOM",
        description="Alex custom description",
        phases_in_cycle=4,
        phases=[],
    )
    active_meso = SimpleNamespace(
        mesocycle=system_meso,
        current_phase=1,
        microcycle_length=7,
    )
    session = _SequenceSession(
        [system_split, custom_split],
        [system_meso, custom_meso],
        active_meso,
        [],
        [],
    )

    response = await workout_center_module.build_context(
        session,
        SimpleNamespace(id=73, _request_language=language),
    )

    expected_split = localized_split(stored_split.code, language).name
    expected_meso = localized_preset("linear_4w", language).name
    assert response.selected_split.name == expected_split
    assert [item.name for item in response.available_splits] == [
        expected_split,
        "Верх / низ (2 дня) — Alex CUSTOM",
    ]
    assert response.selected_periodization.name == expected_meso
    assert [item.name for item in response.available_mesocycles] == [
        expected_meso,
        "Линейный 4-недельный — Alex CUSTOM",
    ]
    assert system_split.name == stored_split.name
    assert custom_split.name == "Верх / низ (2 дня) — Alex CUSTOM"
    assert system_meso.name == "Линейный 4-недельный"
    assert custom_meso.name == "Линейный 4-недельный — Alex CUSTOM"
