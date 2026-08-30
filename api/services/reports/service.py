"""Материализация снапшотов отчётов.

Вызывается из двух мест: роутера /reports и фоновой проекции доменных
событий. Идемпотентна по (пользователь, тип, начало периода) — воркер и
клиент могут разойтись в оценке текущей даты на границе суток, и это
сдвигает лишь МОМЕНТ создания, но не содержимое и не количество записей.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.i18n import SupportedLanguage, resolve_language, tr
from api.services.exercise_localization import localized_names
from api.services.models import (
    AppUserProfile,
    Exercise,
    PeriodizationProposal,
    PeriodReport,
)
from api.services.progression.params import DEFAULT_RIR
from api.services.reports.metrics import ReportMetrics, compute_metrics, has_activity
from api.services.reports.periods import PERIOD_TYPES, closed_periods
from api.services.reports.rules import RULES_VERSION, Action, RuleContext, build_actions

REPORT_SHAPE_VERSION = 2


def _missing_record_localization(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    names = record.get("localized_names")
    return not (
        isinstance(names, dict)
        and any(
            key in {"ru", "en"} and isinstance(value, str) and value
            for key, value in names.items()
        )
    )


def _record_exercise_id(record: dict) -> int | None:
    raw = record.get("exercise_id")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int) and raw > 0:
        return raw
    if isinstance(raw, str) and raw.isdecimal() and int(raw) > 0:
        return int(raw)
    return None


async def enrich_report_record_localizations(
    session: AsyncSession, metrics: dict
) -> dict:
    """Return a localized response copy of durable report metrics.

    Historical snapshots remain immutable. Missing record maps are hydrated in
    one bounded exercise query; deleted IDs retain their durable Russian name.
    """
    enriched = deepcopy(metrics)
    records = enriched.get("records") if isinstance(enriched, dict) else None
    if not isinstance(records, list):
        return enriched

    missing = [
        record
        for record in records
        if _missing_record_localization(record)
        and _record_exercise_id(record) is not None
    ]
    exercise_ids = {_record_exercise_id(record) for record in missing}
    if not exercise_ids:
        return enriched

    exercises = list((await session.execute(
        select(Exercise).where(Exercise.id.in_(exercise_ids))
    )).scalars().all())
    exercises_by_id = {exercise.id: exercise for exercise in exercises}
    for record in missing:
        exercise_id = _record_exercise_id(record)
        exercise = exercises_by_id.get(exercise_id)
        names = localized_names(exercise) if exercise is not None else {}
        if not names:
            legacy_name = record.get("exercise_name")
            if isinstance(legacy_name, str) and legacy_name:
                names = {"ru": legacy_name}
        if names:
            record["localized_names"] = names
    return enriched

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
