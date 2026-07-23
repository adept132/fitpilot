"""Тесты сервисного слоя усталостной модели (compute_readiness).

Обращения к БД мокаются: подменяем service._load, чтобы гонять чистую
логику confidence/cold-start без Postgres. Реальный запрос к БД проверяется
интеграционно на уровне эндпоинта (T12).
"""

from datetime import datetime, timedelta, timezone

import pytest

from api.services.fatigue import service
from api.services.fatigue.core import SetImpulse
from api.services.fatigue.params import DEFAULT_PARAMS as P

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)


def _impulse(days_ago: float) -> SetImpulse:
    return SetImpulse(
        at=NOW - timedelta(days=days_ago),
        weight_kg=100.0,
        reps=8,
        rir=2,
        main_muscle="chest",
        secondary_muscles=("triceps",),
        fatigue_tier=2,
    )


def _patch_load(monkeypatch, loaded: service._Loaded):
    async def fake_load(db, app_user_id, now, p):
        return loaded

    monkeypatch.setattr(service, "_load", fake_load)


async def test_short_history_reports_cold_start(monkeypatch):
    # Новичок: тренировки только за последние 3 дня, но сессии есть (не 0).
    # Ряд для z обязан быть коротким (< cold_start_days), z не выдаётся, а
    # confidence падает в cold_start. До фикса ряд всегда был длиной window_days
    # (35), guard не срабатывал и юзер получал ложный z при confidence=normal.
    loaded = service._Loaded(
        impulses=[_impulse(0), _impulse(1), _impulse(2)],
        labeled=3,
        total=3,
        imported_sessions=0,
        total_sessions=1,
    )
    _patch_load(monkeypatch, loaded)

    report = await service.compute_readiness(db=None, app_user_id=1, now=NOW, p=P)

    assert report.confidence == service.CONFIDENCE_COLD_START
    assert report.systemic.z is None
    assert report.systemic.band == "unknown"


async def test_no_sessions_reports_cold_start(monkeypatch):
    _patch_load(monkeypatch, service._Loaded())
    report = await service.compute_readiness(db=None, app_user_id=1, now=NOW, p=P)
    assert report.confidence == service.CONFIDENCE_COLD_START


async def test_long_history_emits_real_z(monkeypatch):
    # Достаточная история (импульсы разбросаны на весь диапазон окна): ряд
    # длиннее cold_start_days, z считается, confidence уже не cold_start.
    impulses = [_impulse(d) for d in range(0, P.window_days, 2)]
    loaded = service._Loaded(
        impulses=impulses,
        labeled=len(impulses),
        total=len(impulses),
        imported_sessions=0,
        total_sessions=len(impulses),
    )
    _patch_load(monkeypatch, loaded)

    report = await service.compute_readiness(db=None, app_user_id=1, now=NOW, p=P)

    assert report.confidence != service.CONFIDENCE_COLD_START
    assert report.systemic.z is not None


async def test_low_effort_labeling_downgrades_confidence(monkeypatch):
    # Длинная история, но усилие размечено меньше порога -> confidence low.
    impulses = [_impulse(d) for d in range(0, P.window_days, 2)]
    loaded = service._Loaded(
        impulses=impulses,
        labeled=1,  # почти ничего не размечено
        total=len(impulses),
        imported_sessions=0,
        total_sessions=len(impulses),
    )
    _patch_load(monkeypatch, loaded)

    report = await service.compute_readiness(db=None, app_user_id=1, now=NOW, p=P)

    assert report.confidence == service.CONFIDENCE_LOW
    assert report.effort_labeled_pct < service.LOW_CONFIDENCE_LABELED_PCT


async def test_imported_fraction_is_reported(monkeypatch):
    impulses = [_impulse(d) for d in range(0, P.window_days, 2)]
    loaded = service._Loaded(
        impulses=impulses,
        labeled=len(impulses),
        total=len(impulses),
        imported_sessions=4,
        total_sessions=len(impulses),
    )
    _patch_load(monkeypatch, loaded)

    report = await service.compute_readiness(db=None, app_user_id=1, now=NOW, p=P)

    assert report.imported_pct == pytest.approx(
        4 / len(impulses) * 100.0, abs=0.1
    )
    assert report.model_version == service.MODEL_VERSION
