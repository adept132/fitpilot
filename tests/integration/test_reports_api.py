from datetime import date

import pytest

from api.services.models import UserCalendarDay


@pytest.fixture
def local_monday() -> str:
    return "2026-08-17"


async def _planned_week(db, user_id):
    db.add(UserCalendarDay(
        app_user_id=user_id, target_date=date(2026, 8, 11), status="completed",
        is_rest_day=False, is_blackout=False,
    ))
    await db.commit()


@pytest.mark.asyncio
async def test_list_materializes_and_returns_cards(client, db, test_user, local_monday):
    await _planned_week(db, test_user.id)

    response = await client.get(f"/reports?local_date={local_monday}")

    assert response.status_code == 200
    items = response.json()["items"]
    week = next(item for item in items if item["period_type"] == "week")
    assert week["period_start"] == "2026-08-10"
    assert week["seen"] is False
    assert len(week["headline"]) >= 1


@pytest.mark.asyncio
async def test_detail_returns_metrics_and_shape_version(client, db, test_user, local_monday):
    await _planned_week(db, test_user.id)
    await client.get(f"/reports?local_date={local_monday}")

    response = await client.get("/reports/week/2026-08-10")

    assert response.status_code == 200
    body = response.json()
    assert body["shape_version"] >= 1
    assert body["metrics"]["adherence"]["completed_days"] == 1
    assert isinstance(body["actions"], list)


@pytest.mark.asyncio
async def test_detail_of_missing_report_is_404(client, test_user):
    response = await client.get("/reports/week/2020-01-06")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_seen_marks_report_and_is_idempotent(client, db, test_user, local_monday):
    await _planned_week(db, test_user.id)
    await client.get(f"/reports?local_date={local_monday}")

    first = await client.post("/reports/week/2026-08-10/seen")
    second = await client.post("/reports/week/2026-08-10/seen")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["seen_at"] == second.json()["seen_at"]

    listing = await client.get(f"/reports?local_date={local_monday}")
    week = next(
        item for item in listing.json()["items"] if item["period_type"] == "week"
    )
    assert week["seen"] is True


@pytest.mark.asyncio
async def test_bad_period_type_is_422(client):
    response = await client.get("/reports/decade/2026-01-01")
    assert response.status_code == 422
