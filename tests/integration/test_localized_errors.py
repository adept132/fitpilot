import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request
from httpx import ASGITransport, AsyncClient

from api.errors import LocalizedHTTPException, localized_http_exception_handler
from api.deps import get_db
from api.routers.exercises import router as exercises_router
from api.services.app_user_service import get_current_app_user, set_request_language
from api.services.plan_generation_insights import missing_requested_day_issue
from api.services.reports.metrics import (
    AdherenceMetric,
    EffortMetric,
    IntensityMetric,
    ReportMetrics,
    TimeMetric,
    VolumeMetric,
)
from api.services.reports.rules import RuleContext, build_actions
from api.services.structure.mesocycle_presets import localized_preset
from api.services.structure.microcycle_profiles import localized_profile
from api.services.structure.split_catalog import localized_split


@pytest.fixture(autouse=True)
def _db_lifecycle():
    """This module is intentionally DB-free; Postgres evidence is run separately."""
    yield


def _request(language: str) -> Request:
    request = Request({"type": "http", "method": "GET", "path": "/"})
    request.state.language = language
    return request


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "detail"),
    [("ru", "Упражнение не найдено"), ("en", "Exercise not found")],
)
async def test_localized_error_keeps_string_detail(language, detail):
    response = await localized_http_exception_handler(
        _request(language),
        LocalizedHTTPException(404, "exercise.not_found"),
    )

    assert response.status_code == 404
    assert response.body.decode() == (
        '{"detail":"' + detail + '","error":{"code":"exercise.not_found","params":{}}}'
    )


@pytest.mark.asyncio
async def test_localized_error_keeps_canonical_parameter_values_as_data():
    response = await localized_http_exception_handler(
        _request("en"),
        LocalizedHTTPException(
            404, "superset.target_exercise_not_found", {"exercise_id": 912}
        ),
    )

    assert response.status_code == 404
    assert json.loads(response.body) == {
        "detail": "Target exercise 912 not found",
        "error": {
            "code": "superset.target_exercise_not_found",
            "params": {"exercise_id": 912},
        },
    }


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _FakeSession:
    def __init__(self, value=None):
        self.value = value
        self.execute_calls = 0

    async def execute(self, _statement):
        self.execute_calls += 1
        return _ScalarResult(self.value)


@pytest.mark.asyncio
async def test_profile_language_precedes_accept_language_in_request_state():
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"accept-language", b"en-US")],
        }
    )
    session = _FakeSession({"language": "ru"})

    await set_request_language(request, session, SimpleNamespace(id=7))

    assert request.state.language == "ru"
    assert session.execute_calls == 1


@pytest.mark.asyncio
async def test_header_language_is_fallback_when_profile_language_is_invalid():
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"accept-language", b"en-US")],
        }
    )

    await set_request_language(
        request, _FakeSession({"language": "de"}), SimpleNamespace(id=7)
    )

    assert request.state.language == "en"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "detail"),
    [("ru", "Упражнение не найдено"), ("en", "Exercise not found")],
)
async def test_exercise_route_uses_localized_error_contract(language, detail):
    local_app = FastAPI()
    local_app.include_router(exercises_router)
    local_app.add_exception_handler(
        LocalizedHTTPException, localized_http_exception_handler
    )

    async def missing_db():
        yield _FakeSession()

    async def current_user(request: Request):
        request.state.language = language
        return SimpleNamespace(id=1)

    local_app.dependency_overrides[get_db] = missing_db
    local_app.dependency_overrides[get_current_app_user] = current_user

    async with AsyncClient(
        transport=ASGITransport(app=local_app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/exercises/999999", headers={"Accept-Language": language}
        )

    assert response.status_code == 404
    assert response.json() == {
        "detail": detail,
        "error": {"code": "exercise.not_found", "params": {}},
    }


@pytest.mark.asyncio
async def test_machine_object_http_exception_bypasses_localized_handler():
    local_app = FastAPI()
    local_app.add_exception_handler(
        LocalizedHTTPException, localized_http_exception_handler
    )

    @local_app.get("/machine-error")
    async def machine_error():
        raise HTTPException(
            status_code=409,
            detail={"status": "conflict", "current_version": 7},
        )

    async with AsyncClient(
        transport=ASGITransport(app=local_app), base_url="http://test"
    ) as client:
        response = await client.get("/machine-error")

    assert response.status_code == 409
    assert response.json() == {
        "detail": {"status": "conflict", "current_version": 7}
    }


def _metrics() -> ReportMetrics:
    return ReportMetrics(
        period_start=__import__("datetime").date(2026, 8, 1),
        period_end=__import__("datetime").date(2026, 8, 7),
        period_type="week",
        adherence=AdherenceMetric(
            planned_days=3, completed_days=1, missed_days=2, rate=1 / 3
        ),
        volume=VolumeMetric(work_sets=0, tonnage_kg=0, by_muscle={}),
        intensity=IntensityMetric(avg_relative=None, heavy_set_share=None),
        effort=EffortMetric(avg_rir=2.0, labeled_share=1.0),
        time=TimeMetric(
            total_minutes=0,
            sessions=1,
            avg_session_minutes=None,
            sets_per_hour=None,
        ),
        records=[],
    )


def test_generated_report_action_exposes_keys_params_and_localized_copy():
    action = build_actions(
        _metrics(),
        RuleContext(
            pending_proposal_id=None,
            pending_proposal_kind=None,
            target_rir=2,
        ),
        language="en",
    )[0]

    assert action.title_key == "report.action.adherence_low.title"
    assert action.body_key == "report.action.adherence_low.body"
    assert action.params == {"completed_days": 1, "planned_days": 3}
    assert action.title == "The plan does not fit your week"
    assert action.reason == "Completed 1 of 3 planned days"


def test_generated_plan_issue_exposes_keys_and_preserves_user_day_name():
    issue = missing_requested_day_issue("Push by Alex", language="en")

    assert issue.title_key == "generation.requested_day_not_found.title"
    assert issue.body_key == "generation.requested_day_not_found.body"
    assert issue.params == {"day_name": "Push by Alex"}
    assert issue.title == "Training day not found in split"
    assert issue.reason == "The selected day is missing from the current split."
    assert issue.day_tag == "Push by Alex"


@pytest.mark.parametrize(
    ("language", "split_name", "profile_name", "preset_name"),
    [
        ("ru", "Верх / низ (2 дня)", "Равномерный", "Линейный 4-недельный"),
        ("en", "Upper / lower (2 days)", "Even", "Linear 4-week"),
    ],
)
def test_system_structure_templates_render_in_requested_language(
    language, split_name, profile_name, preset_name
):
    assert localized_split("upper_lower_2", language).name == split_name
    assert localized_profile("even", language).name == profile_name
    assert localized_preset("linear_4w", language).name == preset_name


def test_localizing_templates_does_not_modify_user_instance_copy():
    user_instance = SimpleNamespace(name="Alex custom split")

    localized_split("upper_lower_2", "ru")

    assert user_instance.name == "Alex custom split"
