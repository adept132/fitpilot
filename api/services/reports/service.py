"""Материализация снапшотов отчётов.

Вызывается из двух мест: роутера /reports и фоновой проекции доменных
событий. Идемпотентна по (пользователь, тип, начало периода) — воркер и
клиент могут разойтись в оценке текущей даты на границе суток, и это
сдвигает лишь МОМЕНТ создания, но не содержимое и не количество записей.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.i18n import SupportedLanguage, resolve_language, tr
from api.services.models import AppUserProfile, PeriodizationProposal, PeriodReport
from api.services.progression.params import DEFAULT_RIR
from api.services.reports.metrics import ReportMetrics, compute_metrics, has_activity
from api.services.reports.periods import PERIOD_TYPES, closed_periods
from api.services.reports.rules import RULES_VERSION, Action, RuleContext, build_actions

REPORT_SHAPE_VERSION = 2

def build_payload(metrics: ReportMetrics, actions: list[Action]) -> dict:
    """JSON-представление снапшота. Даты — ISO-строки: payload переживает
    и БД, и офлайн-кэш клиента."""
    data = asdict(metrics)
    data["period_start"] = metrics.period_start.isoformat()
    data["period_end"] = metrics.period_end.isoformat()
    for record in data["records"]:
        record["achieved_on"] = record["achieved_on"].isoformat()
    return {
        "shape_version": REPORT_SHAPE_VERSION,
        "rules_version": RULES_VERSION,
        "metrics": data,
        "actions": [asdict(action) for action in actions],
    }


async def _rule_context(session: AsyncSession, app_user_id: int) -> RuleContext:
    proposal = (await session.execute(
        select(PeriodizationProposal)
        .where(
            PeriodizationProposal.app_user_id == app_user_id,
            PeriodizationProposal.status == "pending",
        )
        .order_by(PeriodizationProposal.id.desc())
    )).scalars().first()
    return RuleContext(
        pending_proposal_id=proposal.id if proposal else None,
        pending_proposal_kind=proposal.kind if proposal else None,
        target_rir=DEFAULT_RIR,
    )


async def ensure_reports(
    session: AsyncSession,
    app_user_id: int,
    local_date: date,
    language: SupportedLanguage | None = None,
) -> int:
    """Создать недостающие отчёты за закрытые периоды. Возвращает счётчик."""
    from api.services.notification_service import create_notification

    profile = (await session.execute(
        select(AppUserProfile).where(AppUserProfile.app_user_id == app_user_id)
    )).scalar_one_or_none()
    level = profile.experience_level if profile else None
    language = language or resolve_language(
        None, profile.settings if profile else None
    )
    context = await _rule_context(session, app_user_id)

    created = 0
    for period_type in PERIOD_TYPES:
        for start, end in closed_periods(period_type, local_date):
            exists = (await session.execute(
                select(PeriodReport.id).where(
                    PeriodReport.app_user_id == app_user_id,
                    PeriodReport.period_type == period_type,
                    PeriodReport.period_start == start,
                )
            )).scalar_one_or_none()
            if exists is not None:
                continue

            metrics = await compute_metrics(
                session, app_user_id, period_type, start, end, level
            )
            if not has_activity(metrics):
                continue

            actions = build_actions(metrics, context, language)
            statement = (
                insert(PeriodReport)
                .values(
                    app_user_id=app_user_id,
                    period_type=period_type,
                    period_start=start,
                    period_end=end,
                    payload=build_payload(metrics, actions),
                    rules_version=RULES_VERSION,
                    shape_version=REPORT_SHAPE_VERSION,
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
            report_id = (await session.execute(statement)).scalar_one_or_none()
            if report_id is None:
                # Другой процесс успел первым — уведомление он же и создал.
                continue

            created += 1
            await create_notification(
                session,
                app_user_id=app_user_id,
                event_type="period_report",
                entity_type="period_report",
                entity_id=report_id,
                title=tr(language, f"report.notification.{period_type}.title"),
                body=tr(language, "report.notification.body"),
                message_key=f"notification.period_report.{period_type}",
                message_params={},
                payload={"route": f"/reports/{period_type}/{start.isoformat()}"},
                dedupe_key=f"period_report:{period_type}:{start.isoformat()}",
            )
    return created
