from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException, Request
from httpx import ASGITransport, AsyncClient

import api.routers.reports as reports_module
import api.routers.sync as sync_module
from api.deps import get_db
from api.errors import LocalizedHTTPException, localized_http_exception_handler
from api.i18n import resolve_language
from api.routers.reports import _to_read
from api.routers.workout_supersets import router as supersets_router
from api.services.app_user_service import get_current_app_user
from api.services.workout_superset_service import WorkoutSupersetService


def _request(language: str) -> Request:
    request = Request({"type": "http", "method": "GET", "path": "/"})
    request.state.language = language
    return request


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalar_one(self):
        return self.value

    def scalars(self):
        return self

    def first(self):
        return self.value

    def all(self):
        return self.value


class _SupersetSession:
    def __init__(self, exercise=None):
        self.exercise = exercise
        self.statements = []
        self.commit_calls = 0
        self.refresh_calls = 0

    async def execute(self, statement):
        self.statements.append(statement)
        return _ScalarResult(self.exercise)

    async def commit(self):
        self.commit_calls += 1

    async def refresh(self, _value):
        self.refresh_calls += 1


def _supersets_app(session, current_user=None) -> FastAPI:
    app = FastAPI()
    app.include_router(supersets_router)
    app.add_exception_handler(
        LocalizedHTTPException, localized_http_exception_handler
    )

    async def db_override():
        yield session

    app.dependency_overrides[get_db] = db_override
    if current_user is not None:
        app.dependency_overrides[get_current_app_user] = current_user
    return app


@pytest.mark.asyncio
async def test_superset_start_rejects_unauthenticated_request():
    session = _SupersetSession()
    app = _supersets_app(session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/workout-supersets/session-exercises/41/start"
        )

    assert response.status_code == 401
    assert session.statements == []
    assert session.commit_calls == 0


@pytest.mark.asyncio
async def test_superset_start_hides_cross_user_row_and_uses_ru_header_fallback():
    session = _SupersetSession()

    async def current_user(request: Request):
        assert request.headers["Accept-Language"] == "ru"
        request.state.language = resolve_language(
            request.headers.get("Accept-Language"), None
        )
        return SimpleNamespace(id=73)

    app = _supersets_app(session, current_user)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/workout-supersets/session-exercises/41/start",
            headers={"Accept-Language": "ru"},
        )

    assert response.status_code == 404
    assert response.json() == {
        "detail": "Упражнение в сессии не найдено",
        "error": {"code": "exercise.session_not_found", "params": {}},
    }
    assert session.commit_calls == 0
    statement = session.statements[0]
    assert "workout_sessions" in str(statement)
    assert {41, 73}.issubset(set(statement.compile().params.values()))


@pytest.mark.asyncio
async def test_superset_start_owner_succeeds():
    exercise = SimpleNamespace(id=41, superset_group=None)
    session = _SupersetSession(exercise)

    async def current_user(request: Request):
        request.state.language = "en"
        return SimpleNamespace(id=73)

    app = _supersets_app(session, current_user)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/workout-supersets/session-exercises/41/start"
        )

    assert response.status_code == 200
    assert response.json()["session_exercise_id"] == 41
    assert response.json()["superset_group"] == exercise.superset_group
    assert exercise.superset_group
    assert session.commit_calls == 1
    assert session.refresh_calls == 1


def _report(actions):
    now = datetime(2026, 8, 30, tzinfo=timezone.utc)
    return SimpleNamespace(
        shape_version=2,
        rules_version=2,
        period_type="week",
        period_start=date(2026, 8, 17),
        period_end=date(2026, 8, 23),
        generated_at=now,
        seen_at=None,
        payload={"metrics": {}, "actions": actions},
    )


def test_report_read_rerenders_keyed_actions_and_preserves_legacy_rows():
    report = _report([
        {
            "id": "adherence_low",
            "title": "The plan does not fit your week",
            "reason": "Completed 1 of 3 planned days",
            "title_key": "report.action.adherence_low.title",
            "body_key": "report.action.adherence_low.body",
            "params": {"completed_days": 1, "planned_days": 3},
            "route": "/settings/training",
        },
        {
            "id": "legacy_action",
            "title": "Сохранённый заголовок",
            "reason": "Сохранённая причина",
            "route": "/progress",
        },
    ])

    english = _to_read(report, "en")
    russian = _to_read(report, "ru")

    assert english.actions[0].title == "The plan does not fit your week"
    assert russian.actions[0].title == "План не влезает в неделю"
    assert russian.actions[0].reason == "Выполнено 1 из 3 запланированных дней"
    assert russian.actions[0].title_key == "report.action.adherence_low.title"
    assert russian.actions[0].params == {"completed_days": 1, "planned_days": 3}
    assert russian.actions[1].title == "Сохранённый заголовок"
    assert russian.actions[1].reason == "Сохранённая причина"
    assert report.payload["actions"][0]["title"] == (
        "The plan does not fit your week"
    )
    assert report.payload["actions"][0]["params"] == {
        "completed_days": 1,
        "planned_days": 3,
    }


class _ReportsSession:
    def __init__(self):
        self.results = [_ScalarResult([]), _ScalarResult(0)]

    async def execute(self, _statement):
        return self.results.pop(0)

    async def commit(self):
        return None


@pytest.mark.asyncio
async def test_report_list_passes_resolved_header_fallback_to_materialization(
    monkeypatch,
):
    ensure = AsyncMock(return_value=0)
    monkeypatch.setattr(reports_module, "ensure_reports", ensure)
    session = _ReportsSession()

    response = await reports_module.list_reports(
        request=_request("ru"),
        local_date=date(2026, 8, 30),
        limit=20,
        current_user=SimpleNamespace(id=73),
        db=session,
    )

    assert response.unseen_count == 0
    ensure.assert_awaited_once_with(
        session, 73, date(2026, 8, 30), language="ru"
    )


@pytest.mark.asyncio
async def test_sync_workout_passes_already_resolved_request_language(monkeypatch):
    expected = SimpleNamespace(sync_version=4)
    apply_snapshot = AsyncMock(return_value=expected)
    monkeypatch.setattr(sync_module, "_apply_snapshot", apply_snapshot)
    db = SimpleNamespace(rollback=AsyncMock())
    payload = SimpleNamespace(client_uuid="phone-workout")

    result = await sync_module.sync_workout(
        payload=payload,
        db=db,
        app_user=SimpleNamespace(id=73, _request_language="ru"),
    )

    assert result is expected
    apply_snapshot.assert_awaited_once_with(db, 73, payload, language="ru")


class _ConflictSession:
    def __init__(self, workout):
        self.workout = workout
        self.execute_calls = 0
        self.rollback = AsyncMock()
        self.commit = AsyncMock()

    async def execute(self, _statement, _params=None):
        self.execute_calls += 1
        if self.execute_calls == 1:
            return _ScalarResult(None)
        if self.execute_calls == 2:
            return _ScalarResult(self.workout)
        raise AssertionError("conflict path must not re-query profile language")


@pytest.mark.asyncio
async def test_sync_conflict_notification_uses_passed_request_language(monkeypatch):
    db = _ConflictSession(SimpleNamespace(id=41, sync_version=9))
    create_notification = AsyncMock()
    monkeypatch.setattr(sync_module, "create_notification", create_notification)
    monkeypatch.setattr(sync_module, "_load_detail", AsyncMock(return_value=object()))

    class _Detail:
        @classmethod
        def model_validate(cls, _value):
            return object()

    class _Conflict:
        def __init__(self, sync_version, workout):
            self.sync_version = sync_version
            self.workout = workout

        def model_dump(self, mode):
            assert mode == "json"
            return {"sync_version": self.sync_version, "workout": {}}

    monkeypatch.setattr(sync_module, "WorkoutSessionDetailResponse", _Detail)
    monkeypatch.setattr(sync_module, "SyncConflictResponse", _Conflict)
    payload = SimpleNamespace(
        client_uuid="phone-workout",
        server_id=None,
        base_version=8,
        deleted=False,
    )

    with pytest.raises(HTTPException) as raised:
        await sync_module._apply_snapshot(db, 73, payload, language="ru")

    assert raised.value.status_code == 409
    assert db.execute_calls == 2
    create_notification.assert_awaited_once()
    notification = create_notification.await_args.kwargs
    assert notification["title"] == "Нужно проверить синхронизацию"
    assert notification["body"] == "Тренировка была изменена на другом устройстве."
