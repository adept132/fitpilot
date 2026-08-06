"""Календарь запоминает факт: привязка сессии и закрытие дня."""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from api.services.models import UserCalendarDay, WorkoutSession

pytestmark = pytest.mark.asyncio


def _utc_today() -> date:
    # Дата-основа сервера — UTC (на Render сервер и так работает в UTC;
    # dev-машина в другом поясе — нет). workout.started_at пишется через
    # server_default=func.now() и приходит в UTC, поэтому календарный день
    # для теста тоже должен строиться на UTC-дате, а не на локальной
    # date.today() — иначе тест сравнивает даты из двух разных базисов.
    return datetime.now(timezone.utc).date()


async def _make_day(db, user_id, target_date, **kw):
    defaults = dict(
        app_user_id=user_id,
        target_date=target_date,
        day_tag="push",
        micro_tag="medium",
        meso_tag="medium",
        microcycle_day_number=1,
        is_rest_day=False,
        is_blackout=False,
        status="planned",
    )
    defaults.update(kw)
    day = UserCalendarDay(**defaults)
    db.add(day)
    await db.commit()
    await db.refresh(day)
    return day


async def test_start_writes_calendar_day_id_into_session(
    client, auth_headers, db, test_user
):
    day = await _make_day(db, test_user.id, _utc_today())

    resp = await client.post(
        "/workouts/start",
        headers=auth_headers,
        json={"source": "free", "calendar_day_id": day.id},
    )
    assert resp.status_code == 200
    workout_id = resp.json()["id"]

    workout = (
        await db.execute(select(WorkoutSession).where(WorkoutSession.id == workout_id))
    ).scalar_one()
    assert workout.calendar_day_id == day.id


async def test_finish_closes_the_planned_day(client, auth_headers, db, test_user):
    day = await _make_day(db, test_user.id, _utc_today())

    started = await client.post(
        "/workouts/start",
        headers=auth_headers,
        json={"source": "free", "calendar_day_id": day.id},
    )
    workout_id = started.json()["id"]

    resp = await client.post(f"/workouts/{workout_id}/finish", headers=auth_headers)
    assert resp.status_code == 200

    await db.refresh(day)
    assert day.status == "completed"
    assert day.actual_workout_session_id == workout_id


async def test_spontaneous_session_is_matched_to_the_day_by_date(
    client, auth_headers, db, test_user
):
    # Сессия без calendar_day_id: спонтанная, импортированная или
    # приехавшая из офлайн-очереди. Она обязана попадать в adherence.
    day = await _make_day(db, test_user.id, _utc_today())

    started = await client.post(
        "/workouts/start", headers=auth_headers, json={"source": "free"}
    )
    workout_id = started.json()["id"]
    await client.post(f"/workouts/{workout_id}/finish", headers=auth_headers)

    await db.refresh(day)
    assert day.status == "completed"
    assert day.actual_workout_session_id == workout_id


async def test_second_session_same_day_does_not_steal_the_closed_day(
    client, auth_headers, db, test_user
):
    day = await _make_day(db, test_user.id, _utc_today())

    first = await client.post(
        "/workouts/start", headers=auth_headers, json={"source": "free"}
    )
    first_id = first.json()["id"]
    await client.post(f"/workouts/{first_id}/finish", headers=auth_headers)

    second = await client.post(
        "/workouts/start", headers=auth_headers, json={"source": "free"}
    )
    second_id = second.json()["id"]
    await client.post(f"/workouts/{second_id}/finish", headers=auth_headers)

    await db.refresh(day)
    assert day.actual_workout_session_id == first_id


async def test_rest_day_is_not_closed_by_a_session(client, auth_headers, db, test_user):
    day = await _make_day(db, test_user.id, _utc_today(), is_rest_day=True)

    started = await client.post(
        "/workouts/start", headers=auth_headers, json={"source": "free"}
    )
    workout_id = started.json()["id"]
    await client.post(f"/workouts/{workout_id}/finish", headers=auth_headers)

    await db.refresh(day)
    assert day.status == "planned"
    assert day.actual_workout_session_id is None


async def test_explicit_link_wins_over_date_for_past_midnight_session(
    client, auth_headers, db, test_user
):
    # Тренировка начата вчера, закончена после полуночи: намерение из
    # старта важнее сегодняшней даты.
    yesterday = await _make_day(db, test_user.id, _utc_today() - timedelta(days=1))
    today = await _make_day(db, test_user.id, _utc_today())

    started = await client.post(
        "/workouts/start",
        headers=auth_headers,
        json={"source": "free", "calendar_day_id": yesterday.id},
    )
    workout_id = started.json()["id"]
    await client.post(f"/workouts/{workout_id}/finish", headers=auth_headers)

    await db.refresh(yesterday)
    await db.refresh(today)
    assert yesterday.actual_workout_session_id == workout_id
    assert today.actual_workout_session_id is None
