from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from api.errors import LocalizedHTTPException
from api.i18n import SupportedLanguage, tr
from api.schemas.reports import (
    PeriodType,
    ReportCardRead,
    ReportHeadlineRead,
    ReportListRead,
    ReportRead,
)
from api.services.app_user_service import get_current_app_user
from api.services.models import PeriodReport
from api.services.reports.service import (
    enrich_report_record_localizations,
    ensure_reports,
)

router = APIRouter(tags=["reports"])


def _headline(
    payload: dict, language: SupportedLanguage
) -> list[ReportHeadlineRead]:
    """Две-три цифры для карточки. Живут на сервере, чтобы лента и пуш
    говорили об отчёте одинаково независимо от версии клиента."""
    metrics = payload.get("metrics", {})
    adherence = metrics.get("adherence", {})
    volume = metrics.get("volume", {})
    records = metrics.get("records", [])
    items = [
        ReportHeadlineRead(
            label=tr(language, "report.headline.adherence.label"),
            value=tr(
                language,
                "report.headline.adherence.value",
                completed_days=adherence.get("completed_days", 0),
                planned_days=adherence.get("planned_days", 0),
            ),
        ),
        ReportHeadlineRead(
            label=tr(language, "report.headline.sets.label"),
            value=str(volume.get("work_sets", 0)),
        ),
    ]
    if records:
        items.append(
            ReportHeadlineRead(
                label=tr(language, "report.headline.records.label"),
                value=str(len(records)),
            )
        )
    return items


@router.get("/reports", response_model=ReportListRead)
async def list_reports(
    request: Request,
    local_date: date | None = None,
    limit: int = Query(20, ge=1, le=100),
    current_user=Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
) -> ReportListRead:
    await ensure_reports(
        db,
        current_user.id,
        local_date or date.today(),
        language=request.state.language,
    )
    await db.commit()

    rows = (await db.execute(
        select(PeriodReport)
        .where(PeriodReport.app_user_id == current_user.id)
        .order_by(PeriodReport.generated_at.desc(), PeriodReport.id.desc())
        .limit(limit)
    )).scalars().all()

    items = [
        ReportCardRead(
            period_type=row.period_type,
            period_start=row.period_start,
            period_end=row.period_end,
            generated_at=row.generated_at,
            seen=row.seen_at is not None,
            headline=_headline(row.payload, request.state.language),
        )
        for row in rows
    ]

    # Счётчик непрочитанных — по ВСЕМ отчётам пользователя, а не по странице:
    # иначе бейдж расходится с уведомлениями, как только непрочитанных больше
    # limit (неделя даёт 20 строк примерно за пять месяцев — это не редкий
    # случай).
    unseen_count = (await db.execute(
        select(func.count()).select_from(PeriodReport).where(
            PeriodReport.app_user_id == current_user.id,
            PeriodReport.seen_at.is_(None),
        )
    )).scalar_one()

    return ReportListRead(items=items, unseen_count=unseen_count)


async def _load(db: AsyncSession, app_user_id: int, period_type: str,
                period_start: date) -> PeriodReport:
    report = (await db.execute(
        select(PeriodReport).where(
            PeriodReport.app_user_id == app_user_id,
            PeriodReport.period_type == period_type,
            PeriodReport.period_start == period_start,
        )
    )).scalar_one_or_none()
    if report is None:
        raise LocalizedHTTPException(404, "report.not_found")
    return report


def _localized_actions(
    actions: list[dict], language: SupportedLanguage
) -> list[dict]:
    localized = []
    for stored_action in actions:
        action = dict(stored_action)
        title_key = action.get("title_key")
        body_key = action.get("body_key")
        params = action.get("params") or {}
        if title_key:
            action["title"] = tr(language, title_key, **params)
        if body_key:
            action["reason"] = tr(language, body_key, **params)
        localized.append(action)
    return localized


def _to_read(
    report: PeriodReport,
    language: SupportedLanguage,
    *,
    metrics: dict | None = None,
) -> ReportRead:
    return ReportRead(
        shape_version=report.shape_version,
        rules_version=report.rules_version,
        period_type=report.period_type,
        period_start=report.period_start,
        period_end=report.period_end,
        generated_at=report.generated_at,
        seen_at=report.seen_at,
        metrics=report.payload.get("metrics", {}) if metrics is None else metrics,
        actions=_localized_actions(report.payload.get("actions", []), language),
    )


@router.get("/reports/{period_type}/{period_start}", response_model=ReportRead)
async def get_report(
    request: Request,
    period_type: PeriodType,
    period_start: date,
    current_user=Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
) -> ReportRead:
    report = await _load(db, current_user.id, period_type, period_start)
    metrics = await enrich_report_record_localizations(
        db, report.payload.get("metrics", {})
    )
    return _to_read(report, request.state.language, metrics=metrics)


@router.post("/reports/{period_type}/{period_start}/seen", response_model=ReportRead)
async def mark_report_seen(
    request: Request,
    period_type: PeriodType,
    period_start: date,
    current_user=Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
) -> ReportRead:
    report = await _load(db, current_user.id, period_type, period_start)
    # Повторная отметка не двигает время: пользователь увидел отчёт один раз,
    # и клиент вправе слать это событие идемпотентно.
    if report.seen_at is None:
        report.seen_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(report)
    metrics = await enrich_report_record_localizations(
        db, report.payload.get("metrics", {})
    )
    return _to_read(report, request.state.language, metrics=metrics)
