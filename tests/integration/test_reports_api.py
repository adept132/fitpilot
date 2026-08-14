import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import delete

from api.services.models import AppUser, PeriodReport, UserCalendarDay

# Минимально валидный payload для _headline/_to_read: оба читают metrics/actions
# через .get(...) с безопасными дефолтами, если ключей нет.
_BARE_PAYLOAD = {"shape_version": 1, "rules_version": 1, "metrics": {}, "actions": []}


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


@pytest.mark.asyncio
async def test_unseen_count_covers_all_reports_not_just_page(client, db, test_user):
    """unseen_count обязан считать ВСЕХ непрочитанных, а не только страницу:
    иначе бейдж расходится с уведомлениями, как только непрочитанных больше
    limit — а неделя даёт 20 строк примерно за пять месяцев, это не редкий
    случай."""
    for week in range(5):
        db.add(PeriodReport(
            app_user_id=test_user.id,
            period_type="week",
            period_start=date(2026, 1, 5) + timedelta(weeks=week),
            period_end=date(2026, 1, 11) + timedelta(weeks=week),
            payload=_BARE_PAYLOAD,
            rules_version=1,
            shape_version=1,
        ))
    await db.commit()

    response = await client.get("/reports?limit=2")

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 2
    assert body["unseen_count"] == 5
    assert body["unseen_count"] > len(body["items"])


@pytest.mark.asyncio
async def test_reports_are_scoped_to_current_user(client, db, test_user, local_monday):
    """_load и список обязаны фильтровать по app_user_id: отчёт другого
    пользователя не должен быть виден ни в деталях, ни в ленте."""
    marker = uuid.uuid4().hex[:12]
    other = AppUser(
        firebase_uid=f"test-other-{marker}",
        email=f"test-other-{marker}@example.com",
        display_name="Other User",
    )
    db.add(other)
    await db.flush()
    other_id = other.id

    db.add(PeriodReport(
        app_user_id=other_id,
        period_type="week",
        period_start=date(2026, 8, 10),
        period_end=date(2026, 8, 16),
        payload=_BARE_PAYLOAD,
        rules_version=1,
        shape_version=1,
    ))
    await db.commit()

    try:
        detail = await client.get("/reports/week/2026-08-10")
        assert detail.status_code == 404

        listing = await client.get(f"/reports?local_date={local_monday}")
        assert listing.status_code == 200
        assert all(
            not (item["period_type"] == "week" and item["period_start"] == "2026-08-10")
            for item in listing.json()["items"]
        )
    finally:
        await db.execute(delete(PeriodReport).where(PeriodReport.app_user_id == other_id))
        await db.execute(delete(AppUser).where(AppUser.id == other_id))
        await db.commit()
