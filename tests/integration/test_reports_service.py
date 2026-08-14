from datetime import date, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from api.services.models import (
    AppNotification,
    PeriodReport,
    UserCalendarDay,
)
from api.services.reports.service import ensure_reports


async def _planned_day(db, user_id, day: date, status: str = "completed"):
    db.add(UserCalendarDay(
        app_user_id=user_id, target_date=day, status=status,
        is_rest_day=False, is_blackout=False,
    ))


@pytest.mark.asyncio
async def test_creates_report_for_closed_week(db, test_user):
    await _planned_day(db, test_user.id, date(2026, 8, 11))
    await db.commit()

    created = await ensure_reports(db, test_user.id, date(2026, 8, 17))
    await db.commit()

    reports = (await db.execute(
        select(PeriodReport).where(
            PeriodReport.app_user_id == test_user.id,
            PeriodReport.period_type == "week",
        )
    )).scalars().all()
    assert created >= 1
    assert [r.period_start for r in reports] == [date(2026, 8, 10)]
    assert reports[0].payload["actions"] == []
    assert reports[0].shape_version >= 1


@pytest.mark.asyncio
async def test_is_idempotent(db, test_user):
    await _planned_day(db, test_user.id, date(2026, 8, 11))
    await db.commit()

    await ensure_reports(db, test_user.id, date(2026, 8, 17))
    await db.commit()
    await ensure_reports(db, test_user.id, date(2026, 8, 17))
    await db.commit()

    count = (await db.execute(
        select(PeriodReport).where(PeriodReport.app_user_id == test_user.id)
    )).scalars().all()
    weeks = [r for r in count if r.period_type == "week"]
    assert len(weeks) == 1


@pytest.mark.asyncio
async def test_empty_period_produces_no_report(db, test_user):
    created = await ensure_reports(db, test_user.id, date(2026, 8, 17))
    await db.commit()

    reports = (await db.execute(
        select(PeriodReport).where(PeriodReport.app_user_id == test_user.id)
    )).scalars().all()
    assert created == 0
    assert reports == []


@pytest.mark.asyncio
async def test_zero_adherence_period_still_produces_report(db, test_user):
    """Пропущенная неделя — самое ценное сообщение, прятать его нельзя."""
    await _planned_day(db, test_user.id, date(2026, 8, 11), status="missed")
    await db.commit()

    await ensure_reports(db, test_user.id, date(2026, 8, 17))
    await db.commit()

    report = (await db.execute(
        select(PeriodReport).where(
            PeriodReport.app_user_id == test_user.id,
            PeriodReport.period_type == "week",
        )
    )).scalar_one()
    assert report.payload["metrics"]["adherence"]["completed_days"] == 0
    assert report.payload["actions"][0]["id"] == "adherence_low"


@pytest.mark.asyncio
async def test_creates_notification_with_route_to_the_report(db, test_user):
    await _planned_day(db, test_user.id, date(2026, 8, 11))
    await db.commit()

    await ensure_reports(db, test_user.id, date(2026, 8, 17))
    await db.commit()

    notification = (await db.execute(
        select(AppNotification).where(
            AppNotification.app_user_id == test_user.id,
            AppNotification.event_type == "period_report",
        )
    )).scalars().first()
    assert notification is not None
    assert notification.payload["route"] == "/reports/week/2026-08-10"
    # Тексты пушей обезличены: ни чисел, ни имён упражнений.
    assert "%" not in notification.body


@pytest.mark.asyncio
async def test_catchup_depth_limits_old_periods(db, test_user):
    """Полгода отсутствия не должны обернуться пачкой отчётов."""
    for offset in range(1, 30):
        await _planned_day(db, test_user.id, date(2026, 8, 17) - timedelta(weeks=offset))
    await db.commit()

    await ensure_reports(db, test_user.id, date(2026, 8, 17))
    await db.commit()

    weeks = (await db.execute(
        select(PeriodReport).where(
            PeriodReport.app_user_id == test_user.id,
            PeriodReport.period_type == "week",
        )
    )).scalars().all()
    assert len(weeks) == 4


def _insert_report_statement(app_user_id: int, period_start: date):
    """Тот же самый statement, что строит ensure_reports (service.py)."""
    return (
        insert(PeriodReport)
        .values(
            app_user_id=app_user_id,
            period_type="week",
            period_start=period_start,
            period_end=period_start + timedelta(days=6),
            payload={},
            rules_version=1,
            shape_version=1,
        )
        .on_conflict_do_nothing(
            index_elements=[
                PeriodReport.app_user_id,
                PeriodReport.period_type,
                PeriodReport.period_start,
            ]
        )
        .returning(PeriodReport.id)
    )


@pytest.mark.asyncio
async def test_conflict_insert_discriminates_the_winning_writer(db, test_user):
    """Проверяет не ветку ensure_reports (она недостижима в один процесс:
    pre-check SELECT отсекает повтор раньше, чем строится этот statement),
    а сам механизм ON CONFLICT ... RETURNING, на чью семантику эта ветка
    опирается: первая вставка возвращает id, повторная — None, и в таблице
    остаётся ровно одна строка. Именно по этому признаку ensure_reports
    решает, какой процесс создаёт уведомление."""
    period_start = date(2026, 8, 10)

    first_id = (await db.execute(
        _insert_report_statement(test_user.id, period_start)
    )).scalar_one_or_none()
    await db.commit()

    second_id = (await db.execute(
        _insert_report_statement(test_user.id, period_start)
    )).scalar_one_or_none()
    await db.commit()

    assert first_id is not None
    assert second_id is None

    rows = (await db.execute(
        select(PeriodReport).where(
            PeriodReport.app_user_id == test_user.id,
            PeriodReport.period_type == "week",
            PeriodReport.period_start == period_start,
        )
    )).scalars().all()
    assert len(rows) == 1
