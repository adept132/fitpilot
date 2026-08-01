"""Экспорт/импорт данных пользователя в формате «Eurith CSV v1»."""

from datetime import date
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from api.schemas.data_io import (
    ExerciseSuggestion,
    ImportCommitRequest,
    ImportCommitResponse,
    ImportPreviewRequest,
    ImportPreviewResponse,
    SkippedRowOut,
    UnmatchedExercise,
)
from api.services.app_user_service import get_current_app_user
from api.services.csv_export import collect_export_rows, columns_for, iter_csv
from api.services.csv_format import UNIT_KG, normalize_unit
from api.services.csv_import import CsvParseError, ParseResult, parse_csv
from api.services.import_service import (
    ExerciseResolver,
    existing_import_keys,
    import_workouts,
    save_alias,
)
from api.services.models import AppUser, AppUserProfile
from api.services.strong_dictionary import split_equipment

# Разумный потолок: годы истории в Strong — это единицы мегабайт.
MAX_IMPORT_BYTES = 10 * 1024 * 1024
MAX_SKIPPED_ROWS_IN_RESPONSE = 50

router = APIRouter(prefix="/data", tags=["data-io"])


async def _load_profile(db: AsyncSession, app_user_id: int) -> Optional[AppUserProfile]:
    return (
        await db.execute(
            select(AppUserProfile).where(AppUserProfile.app_user_id == app_user_id)
        )
    ).scalars().first()


async def _resolve_export_unit(
    db: AsyncSession, app_user_id: int, requested: Optional[str]
) -> str:
    """Явно запрошенная единица > единица из профиля > кг."""
    explicit = normalize_unit(requested)
    if explicit:
        return explicit

    profile = await _load_profile(db, app_user_id)
    from_settings = normalize_unit((profile.settings or {}).get("weight_unit")) if profile else None
    return from_settings or UNIT_KG


async def _user_timezone(db: AsyncSession, app_user_id: int) -> str:
    """Часовой пояс пользователя — в файле дата пишется его настенным временем."""
    profile = await _load_profile(db, app_user_id)
    return (profile.timezone if profile and profile.timezone else "UTC")


@router.get("/export")
async def export_csv(
    export_format: str = Query("full", alias="format", pattern="^(full|strong)$"),
    unit: Optional[str] = Query(None, pattern="^(kg|lbs)$"),
    db: AsyncSession = Depends(get_db),
    current_app_user: AppUser = Depends(get_current_app_user),
):
    """CSV с историей тренировок.

    format=full   — Strong-колонки + расширения Eurith (round-trip без потерь)
    format=strong — только 12 колонок Strong (максимальная совместимость)
    """
    resolved_unit = await _resolve_export_unit(db, current_app_user.id, unit)
    tz_name = await _user_timezone(db, current_app_user.id)
    rows = await collect_export_rows(db, current_app_user.id, resolved_unit, tz_name)

    filename = f"eurith-export-{date.today().isoformat()}.csv"
    return StreamingResponse(
        iter_csv(rows, columns_for(export_format)),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- Импорт ---


def _prepared_csv(csv: str) -> str:
    if not csv or not csv.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Файл пустой")
    if len(csv.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"Файл больше {MAX_IMPORT_BYTES // (1024 * 1024)} МБ",
        )
    # Клиент читает файл текстом; BOM (его пишет Excel и наш экспорт) снимаем тут.
    return csv.lstrip("﻿")


def _parse_or_400(text: str, unit: Optional[str]) -> ParseResult:
    try:
        return parse_csv(text, default_unit=normalize_unit(unit) or UNIT_KG)
    except CsvParseError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


@router.post("/import/preview", response_model=ImportPreviewResponse)
async def import_preview(
    payload: ImportPreviewRequest,
    db: AsyncSession = Depends(get_db),
    current_app_user: AppUser = Depends(get_current_app_user),
):
    """Разбирает CSV и показывает, что произойдёт. НИЧЕГО не пишет в БД."""
    text = _prepared_csv(payload.csv)
    parsed = _parse_or_400(text, payload.unit)

    resolver = ExerciseResolver(db, current_app_user.id)
    await resolver.load()

    # Сколько подходов стоит за каждым несопоставленным именем — чтобы на
    # экране сопоставления было видно, что важно разобрать в первую очередь.
    sets_by_name: Dict[str, int] = {}
    for w in parsed.workouts:
        for e in w.exercises:
            sets_by_name[e.name] = sets_by_name.get(e.name, 0) + len(e.sets)

    matched = 0
    unmatched: List[UnmatchedExercise] = []
    for name in parsed.exercise_names:
        if await resolver.resolve(name):
            matched += 1
            continue
        unmatched.append(
            UnmatchedExercise(
                name=name,
                equipment=split_equipment(name)[1],
                sets_count=sets_by_name.get(name, 0),
                suggestions=[
                    ExerciseSuggestion(**s) for s in await resolver.suggestions(name)
                ],
            )
        )
    unmatched.sort(key=lambda u: u.sets_count, reverse=True)

    keys = [w.import_key for w in parsed.workouts]
    duplicates = await existing_import_keys(db, current_app_user.id, keys)

    return ImportPreviewResponse(
        detected_unit=parsed.file_unit,
        needs_unit_choice=parsed.file_unit is None,
        workouts_total=len(parsed.workouts),
        workouts_duplicate=len(duplicates),
        workouts_new=len(parsed.workouts) - len(duplicates),
        sets_total=sum(w.total_sets for w in parsed.workouts),
        matched_exercises=matched,
        unmatched=unmatched,
        skipped_rows=[
            SkippedRowOut(line=s.line, reason=s.reason)
            for s in parsed.skipped[:MAX_SKIPPED_ROWS_IN_RESPONSE]
        ],
        skipped_rows_total=len(parsed.skipped),
    )


@router.post("/import/commit", response_model=ImportCommitResponse)
async def import_commit(
    payload: ImportCommitRequest,
    db: AsyncSession = Depends(get_db),
    current_app_user: AppUser = Depends(get_current_app_user),
):
    """Пишет историю в БД. Дубликаты пропускает, в конце отдаёт отчёт."""
    text = _prepared_csv(payload.csv)
    parsed = _parse_or_400(text, payload.unit)

    resolved_unit = parsed.file_unit or normalize_unit(payload.unit) or UNIT_KG

    mapping_dict: Dict[str, int] = payload.mappings or {}

    resolver = ExerciseResolver(db, current_app_user.id)
    await resolver.load()

    # Ручной выбор пользователя запоминаем, чтобы следующий импорт не
    # переспрашивал то же самое.
    saved_aliases = 0
    for external_name, exercise_id in mapping_dict.items():
        if not isinstance(exercise_id, int):
            continue
        await save_alias(db, current_app_user.id, external_name, exercise_id)
        saved_aliases += 1
    if saved_aliases:
        await db.flush()
        await resolver.load()  # перечитываем алиасы, чтобы резолв их увидел

    keys = [w.import_key for w in parsed.workouts]
    skip_keys = await existing_import_keys(db, current_app_user.id, keys)

    tz_name = await _user_timezone(db, current_app_user.id)
    added, duplicates, unresolved = await import_workouts(
        db, current_app_user.id, parsed.workouts, resolver, skip_keys, tz_name
    )
    await db.commit()

    return ImportCommitResponse(
        added_workouts=added,
        skipped_duplicates=duplicates,
        skipped_exercises=unresolved,
        saved_aliases=saved_aliases,
        unit_used=resolved_unit,
    )
