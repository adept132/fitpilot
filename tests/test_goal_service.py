"""Тесты единого расчёта статуса цели (композиция/частота) на синтетике.

Источники истории замоканы — проверяем чистую логику: направление, прогресс,
ETA, реалистичность для разных типов целей независимо от состояния БД.
"""

import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from api.services import goal_service

_D0 = date(2026, 1, 1)


def _series(values, step_days=7):
    return [(_D0 + timedelta(days=i * step_days), float(v)) for i, v in enumerate(values)]


def _goal(**kw):
    base = dict(
        goal_type="bodyweight",
        app_user_id=1,
        exercise_id=None,
        target_value=80.0,
        target_reps=None,
        metric_key=None,
        deadline=None,
        created_at=datetime(2026, 1, 1),
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _run(goal, series, level="beginner"):
    async def fake_metric_series(session, user_id, metric_key):
        return series

    orig = goal_service.get_metric_series
    goal_service.get_metric_series = fake_metric_series
    try:
        return asyncio.run(goal_service.compute_goal_status(None, goal, level, None))
    finally:
        goal_service.get_metric_series = orig


def test_bodyweight_loss_progress_and_direction():
    # 90 -> 86 за 4 недели, цель 80. Базлайн (на created) = 90.
    st = _run(_goal(target_value=80), _series([90, 88.5, 87, 86]))
    assert st["has_data"]
    assert st["direction"] == "down"
    # прогресс: (86-90)/(80-90) = 40%
    assert st["progress_percentage"] == 40.0
    assert st["current_value"] == 86.0
    assert st["target_display"] == 80.0
    # тренд вниз и цель вниз -> движемся правильно
    assert st["realism"] in ("on_track", "ambitious")
    assert st["eta_date"] is not None


def test_bodyweight_gain_direction_up():
    st = _run(_goal(target_value=95), _series([80, 82, 84]))
    assert st["direction"] == "up"
    assert st["progress_percentage"] > 0


def test_wrong_way_when_trend_opposes_goal():
    # цель похудеть до 80, но вес растёт -> wrong_way
    st = _run(_goal(target_value=80), _series([84, 85, 86, 87]))
    assert st["direction"] == "down"
    assert st["realism"] == "wrong_way"


def test_body_fat_goal():
    st = _run(
        _goal(goal_type="body_fat", target_value=15),
        _series([22, 21, 20, 19]),
    )
    assert st["direction"] == "down"
    assert st["has_data"]
    assert st["current_value"] == 19.0


def test_measurement_goal_uses_metric_key():
    captured = {}

    async def fake_series(session, user_id, metric_key):
        captured["key"] = metric_key
        return _series([90, 88, 86])

    orig = goal_service.get_metric_series
    goal_service.get_metric_series = fake_series
    try:
        st = asyncio.run(
            goal_service.compute_goal_status(
                None,
                _goal(goal_type="measurement", metric_key="waist", target_value=80),
                "beginner",
                None,
            )
        )
    finally:
        goal_service.get_metric_series = orig
    assert captured["key"] == "waist"
    assert st["direction"] == "down"


def test_no_history_returns_empty():
    st = _run(_goal(), [])
    assert st["has_data"] is False
    assert st["progress_percentage"] == 0.0


def test_achieved_when_target_reached():
    # цель 80, уже 79 -> достигнуто
    st = _run(_goal(target_value=80), _series([85, 82, 80, 79]))
    assert st["realism"] == "achieved"


def test_single_day_has_data_but_insufficient_for_forecast():
    # Две записи, но за ОДИН день -> прогресс есть, тренд/ETA нет.
    same_day = [(_D0, 93.0), (_D0, 94.0)]
    st = _run(_goal(goal_type="bodyweight", target_value=98), same_day)
    assert st["has_data"] is True
    assert st["progress_percentage"] > 0  # прогресс-бар работает
    assert st["realism"] == "insufficient"  # но прогноз недоступен
    assert st["eta_date"] is None


def test_two_days_enables_forecast():
    # Те же значения, но в РАЗНЫЕ дни -> появляется тренд и оценка.
    two_days = [(_D0, 93.0), (_D0 + timedelta(days=7), 94.0)]
    st = _run(_goal(goal_type="bodyweight", target_value=98), two_days)
    assert st["realism"] != "insufficient"
    assert st["eta_date"] is not None


def test_deadline_affects_realism():
    # худеем медленно (0.5/нед), цель −10 за 4 недели -> нужно быстрее
    goal = _goal(target_value=80, deadline=date(2026, 1, 29))  # ~4 недели от последней точки
    st = _run(goal, _series([90, 89.5, 89, 88.5]))
    assert st["direction"] == "down"
    # требуется ~2/нед, фактически 0.5/нед -> амбициозно/нереалистично
    assert st["realism"] in ("ambitious", "unrealistic")
