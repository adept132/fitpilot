"""Сборка CSV-экспорта тренировок в формате «Eurith CSV v1» (см. csv_format)."""

import csv
import io
from datetime import date as date_type
from typing import Dict, Iterator, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.services.csv_format import (
    COL_COMPLETED,
    COL_DATE,
    COL_DISTANCE,
    COL_DURATION,
    COL_EFFORT,
    COL_EXERCISE_ID,
    COL_EXERCISE_NAME,
    COL_MESO_PHASE,
    COL_MICRO_DAY,
    COL_NOTES,
    COL_PARENT_SET_ORDER,
    COL_REPS,
    COL_RPE,
    COL_SECONDS,
    COL_SET_ORDER,
    COL_SET_TYPE,
    COL_SUPERSET_GROUP,
    COL_SUPERSET_ROUND,
    COL_WEIGHT,
    COL_WEIGHT_UNIT,
    COL_WORKOUT_NAME,
    COL_WORKOUT_NOTES,
    DATE_FORMAT,
    DEFAULT_WORKOUT_NAME,
    FULL_COLUMNS,
    STRONG_COLUMNS,
    effort_to_rpe,
    format_duration,
    kg_to_unit,
    utc_to_wall_clock,
)
from api.services.models import (
    UserCalendarDay,
    WorkoutPlan,
    WorkoutSession,
    WorkoutSessionExercise,
)

# Excel не распознаёт UTF-8 без BOM и ломает кириллицу в названиях упражнений.
UTF8_BOM = "﻿"


def _text(value: Optional[object]) -> str:
    return "" if value is None else str(value)


def _number(value: Optional[float]) -> str:
    """Число без хвостового .0, точка как разделитель (локаль-независимо)."""
    if value is None:
        return ""
    if float(value) == int(float(value)):
        return str(int(float(value)))
    return str(value)


async def _load_plan_names(session: AsyncSession, plan_ids: List[int]) -> Dict[int, str]:
    if not plan_ids:
        return {}
    result = await session.execute(
        select(WorkoutPlan.id, WorkoutPlan.name).where(WorkoutPlan.id.in_(plan_ids))
    )
    return {row[0]: row[1] for row in result.all()}


async def _load_micro_days(
    session: AsyncSession, app_user_id: int
) -> Dict[date_type, Optional[int]]:
    """Дата -> номер дня микроцикла (контекст периодизации для колонки Micro Day)."""
    result = await session.execute(
        select(UserCalendarDay.target_date, UserCalendarDay.microcycle_day_number).where(
            UserCalendarDay.app_user_id == app_user_id
        )
    )
    return {row[0]: row[1] for row in result.all()}


async def collect_export_rows(
    session: AsyncSession, app_user_id: int, unit: str, tz_name: Optional[str] = None
) -> List[Dict[str, str]]:
    """Полные строки экспорта (все колонки). Экспортируем только завершённые
    тренировки — активная сессия это не история.

    tz_name — часовой пояс пользователя: дату пишем его настенным временем,
    как это делает Strong (в файле смещения нет).
    """
    stmt = (
        select(WorkoutSession)
        .options(
            selectinload(WorkoutSession.exercises).selectinload(
                WorkoutSessionExercise.sets
            ),
            selectinload(WorkoutSession.exercises).selectinload(
                WorkoutSessionExercise.exercise
            ),
        )
        .where(
            WorkoutSession.app_user_id == app_user_id,
            WorkoutSession.status == "finished",
        )
        .order_by(WorkoutSession.started_at)
    )
    sessions = list((await session.execute(stmt)).scalars().all())

    plan_names = await _load_plan_names(
        session, [s.plan_id for s in sessions if s.plan_id]
    )
    micro_days = await _load_micro_days(session, app_user_id)

    rows: List[Dict[str, str]] = []

    for ws in sessions:
        # Длительность считаем по исходным моментам (до конверсии в местное
        # время) — разница от часового пояса не зависит.
        duration_seconds = None
        if ws.finished_at and ws.started_at:
            duration_seconds = int((ws.finished_at - ws.started_at).total_seconds())

        started = utc_to_wall_clock(ws.started_at, tz_name) if ws.started_at else None

        workout_name = plan_names.get(ws.plan_id) or DEFAULT_WORKOUT_NAME
        micro_day = micro_days.get(started.date()) if started else None

        for ws_ex in ws.exercises:
            # id подхода -> его set_number, чтобы выразить дропсет-родителя
            # через Set Order, а не через внутренний id базы.
            set_number_by_id = {s.id: s.set_number for s in ws_ex.sets}
            exercise_name = ws_ex.exercise.name if ws_ex.exercise else ""

            for s in ws_ex.sets:
                # Плановые строки, которые пользователь так и не заполнил
                # (target_sets создаёт их пустыми), — это не история. Strong
                # такие не экспортирует, и наш импорт их всё равно отбрасывает:
                # пропускаем здесь, чтобы экспорт и импорт были симметричны.
                if s.weight is None and s.reps is None:
                    continue

                rows.append(
                    {
                        COL_DATE: started.strftime(DATE_FORMAT) if started else "",
                        COL_WORKOUT_NAME: workout_name,
                        COL_DURATION: format_duration(duration_seconds),
                        COL_EXERCISE_NAME: exercise_name,
                        COL_SET_ORDER: _text(s.set_number),
                        COL_WEIGHT: _number(kg_to_unit(s.weight, unit)),
                        COL_REPS: _text(s.reps),
                        COL_DISTANCE: "0",
                        COL_SECONDS: "0",
                        COL_NOTES: _text(s.notes),
                        COL_WORKOUT_NOTES: _text(ws.notes),
                        COL_RPE: _number(effort_to_rpe(s.effort_level)),
                        # --- расширения Eurith ---
                        COL_WEIGHT_UNIT: unit,
                        COL_SET_TYPE: _text(s.set_type),
                        COL_EFFORT: _text(s.effort_level),
                        COL_COMPLETED: "true" if s.is_completed else "false",
                        COL_PARENT_SET_ORDER: _text(
                            set_number_by_id.get(s.parent_set_id)
                            if s.parent_set_id
                            else None
                        ),
                        COL_SUPERSET_GROUP: _text(ws_ex.superset_group),
                        COL_SUPERSET_ROUND: _text(s.superset_round),
                        COL_EXERCISE_ID: _text(ws_ex.exercise_id),
                        COL_MESO_PHASE: _text(ws.mesocycle_phase),
                        COL_MICRO_DAY: _text(micro_day),
                    }
                )

    return rows


def iter_csv(rows: List[Dict[str, str]], columns: List[str]) -> Iterator[str]:
    """Генерирует CSV построчно. extrasaction='ignore' позволяет одному набору
    строк отдаваться и в формате 'strong' (12 колонок), и в 'full'."""
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=columns, extrasaction="ignore", lineterminator="\r\n"
    )

    def flush() -> str:
        chunk = buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        return chunk

    writer.writeheader()
    yield UTF8_BOM + flush()

    for row in rows:
        writer.writerow(row)
        yield flush()


def columns_for(export_format: str) -> List[str]:
    return STRONG_COLUMNS if export_format == "strong" else FULL_COLUMNS
