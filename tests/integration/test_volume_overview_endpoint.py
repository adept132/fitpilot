"""Контракт GET /api/progress/volume-overview."""
from datetime import timedelta

import pytest

from api.services.models import AppUserProfile, UserCalendarDay
from api.services.volume.repository import utc_today

pytestmark = pytest.mark.asyncio


async def test_overview_returns_window_shape(
    client, auth_headers, db, test_user, active_block, seeded_plan
):
    db.add(AppUserProfile(
        app_user_id=test_user.id, experience_level="intermediate",
        volume_budget={"weekly_targets": {"chest": {"target_sets": 12, "min_floor": 6}}},
    ))
    start = utc_today() - timedelta(days=2)
    for offset in range(6):
        db.add(UserCalendarDay(
            app_user_id=test_user.id, target_date=start + timedelta(days=offset),
            block_id=active_block.id, plan_id=seeded_plan.id, day_tag="push",
            micro_tag="medium", meso_tag="medium", microcycle_day_number=offset + 1,
            is_rest_day=False, is_blackout=False, status="planned",
        ))
    await db.commit()

    resp = await client.get("/api/progress/volume-overview", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()

    assert body["shape_version"] == 2
    assert body["window"]["day"] == 3
    assert body["window"]["length"] == 6
    chest = body["muscles"]["chest"]
    assert chest["target"] == 12.0
    assert chest["mrv"] == 18
    assert chest["forecast"] >= chest["performed_direct"]


async def test_overview_without_calendar_returns_empty_window(
    client, auth_headers, db, test_user
):
    # Нет блока и календаря — контур молчит, но эндпоинт не падает.
    resp = await client.get("/api/progress/volume-overview", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["shape_version"] == 2
    assert body["window"] is None
    assert body["muscles"] == {}
