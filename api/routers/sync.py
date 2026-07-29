from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import exists, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.deps import get_db
from api.schemas.sync import (
    SyncChangesResponse,
    SyncConflictResponse,
    SyncTombstoneItem,
    SyncWorkoutResponse,
    SyncWorkoutSnapshot,
)
from api.schemas.workouts import WorkoutSessionDetailResponse
from api.services.anomaly_guard import check_set, resolve_is_anomalous
from api.services.anomaly_stats import load_exercise_stats
from api.services.app_user_service import get_current_app_user
from api.services.models import (
    AppUser,
    SyncTombstone,
    UserExerciseProgressionState,
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)

router = APIRouter(tags=["sync"])

# Глубина первой (безкурсорной) выгрузки на новом устройстве.
INITIAL_PULL_WINDOW = timedelta(days=30)


def _detail_options():
    return (
        selectinload(WorkoutSession.exercises).selectinload(
            WorkoutSessionExercise.exercise
        ),
        selectinload(WorkoutSession.exercises).selectinload(
            WorkoutSessionExercise.sets
        ),
    )


async def _load_detail(db: AsyncSession, workout_id: int) -> WorkoutSession:
    """Перечитать полное серверное представление тренировки.

    expunge_all() обязателен: иначе select вернёт объекты из identity map,
    созданные/изменённые выше, у которых связи не прогружены — и Pydantic уйдёт
    в lazy-load уже вне greenlet-контекста (MissingGreenlet).
    """
    db.expunge_all()
    return (
        await db.execute(
            select(WorkoutSession)
            .where(WorkoutSession.id == workout_id)
            .options(*_detail_options())
        )
    ).scalar_one()


@router.post("/sync/workouts", response_model=SyncWorkoutResponse)
async def sync_workout(
    payload: SyncWorkoutSnapshot,
    db: AsyncSession = Depends(get_db),
    app_user: AppUser = Depends(get_current_app_user),
):
    """Идемпотентный upsert полного снимка тренировки по client_uuid.

    Клиент-авторитетный (LWW) с оптимистичной блокировкой: если снимок основан на
    устаревшей версии (base_version != текущей sync_version), возвращаем 409 с
    актуальным состоянием — клиент сливает его со своим и повторяет пуш.

    Упражнения и подходы сопоставляются по client_uuid; удаляются ТОЛЬКО по
    явному флагу deleted. Отсутствие сущности в снимке удалением не считается:
    после merge клиент может прислать частичный набор, и молчаливое удаление
    «недостающего» уничтожило бы данные другого устройства.

    Повторная отправка того же снимка не создаёт дублей: конкурентные пуши одной
    тренировки сериализуются advisory-локом, а уникальные индексы по client_uuid
    (см. init_db) остаются жёсткой гарантией.
    """
    app_user_id = app_user.id
    try:
        return await _apply_snapshot(db, app_user_id, payload)
    except IntegrityError:
        # Гонка проскочила мимо advisory-лока (например, запросы ушли в разные
        # соединения через внешний пулер). Уникальный индекс отработал — второй
        # проход уже найдёт строку, созданную конкурентом, и обновит её.
        await db.rollback()
        return await _apply_snapshot(db, app_user_id, payload)


async def _apply_snapshot(
    db: AsyncSession,
    app_user_id: int,
    payload: SyncWorkoutSnapshot,
) -> SyncWorkoutResponse:
    # 0. Сериализуем конкурентные синки ОДНОЙ тренировки: лок держится до конца
    # транзакции, поэтому второй пуш дождётся коммита первого и увидит его строку
    # вместо того, чтобы вставить дубль.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"{app_user_id}:{payload.client_uuid}"},
    )

    # 1. Существующая тренировка этого пользователя по client_uuid.
    existing_stmt = (
        select(WorkoutSession)
        .where(
            WorkoutSession.app_user_id == app_user_id,
            WorkoutSession.client_uuid == payload.client_uuid,
        )
        .options(
            selectinload(WorkoutSession.exercises).selectinload(
                WorkoutSessionExercise.sets
            )
        )
    )
    workout = (await db.execute(existing_stmt)).scalars().first()

    # Адаптация: тренировка создана ранее без client_uuid — матчим по server_id
    # и проставляем client_uuid, чтобы дальнейшие синки шли по нему без дублей.
    if workout is None and payload.server_id is not None:
        adopt_stmt = (
            select(WorkoutSession)
            .where(
                WorkoutSession.app_user_id == app_user_id,
                WorkoutSession.id == payload.server_id,
            )
            .options(
                selectinload(WorkoutSession.exercises).selectinload(
                    WorkoutSessionExercise.sets
                )
            )
        )
        workout = (await db.execute(adopt_stmt)).scalars().first()
        if workout is not None:
            workout.client_uuid = payload.client_uuid

    # 2. Проверка версии ДО любых мутаций. base_version=None означает «клиент эту
    # тренировку ещё не синхронизировал» — проверять нечего.
    if (
        workout is not None
        and payload.base_version is not None
        and (workout.sync_version or 0) != payload.base_version
    ):
        current_version = workout.sync_version or 0
        workout_id = workout.id
        await db.rollback()  # снимаем advisory-лок, ничего не записав
        detail = await _load_detail(db, workout_id)
        conflict = SyncConflictResponse(
            sync_version=current_version,
            workout=WorkoutSessionDetailResponse.model_validate(detail),
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=conflict.model_dump(mode="json"),
        )

    # 3. Удаление тренировки (tombstone).
    if payload.deleted:
        if workout is not None:
            # Надгробие — чтобы удаление доехало до других устройств через
            # GET /sync/changes: хард-delete сам по себе следа не оставляет.
            db.add(
                SyncTombstone(
                    app_user_id=app_user_id,
                    entity_type="workout",
                    client_uuid=payload.client_uuid,
                    server_id=workout.id,
                )
            )
            await db.delete(workout)
            await db.commit()
        return SyncWorkoutResponse(deleted=True, id_map={}, sync_version=0, workout=None)

    is_new = workout is None
    if is_new:
        workout = WorkoutSession(
            app_user_id=app_user_id,
            client_uuid=payload.client_uuid,
        )
        db.add(workout)

    # 4. Скалярные поля (клиент-авторитетно).
    workout.source = payload.source
    workout.status = payload.status
    workout.split_day_id = payload.split_day_id
    workout.plan_id = payload.plan_id
    workout.notes = payload.notes
    # sRPE проставляется клиентом отдельно от остальных скалярных полей: стампим
    # session_rpe_at только когда значение реально пришло и отличается от
    # сохранённого, чтобы не тереть метку времени повторной синхронизацией.
    if payload.session_rpe is not None and workout.session_rpe != payload.session_rpe:
        workout.session_rpe = payload.session_rpe
        workout.session_rpe_at = datetime.now(timezone.utc)
    workout.volume_targets = payload.volume_targets
    workout.started_at = payload.started_at
    workout.finished_at = payload.finished_at
    await db.flush()

    id_map: dict[str, int] = {payload.client_uuid: workout.id}

    # У НОВОЙ тренировки связей нет — обращаться к workout.exercises нельзя:
    # в async-режиме это ленивая загрузка вне greenlet-контекста (MissingGreenlet).
    # У существующей связи уже подтянуты selectinload'ом выше.
    loaded_exercises = [] if is_new else list(workout.exercises)

    existing_exercises = {
        e.client_uuid: e for e in loaded_exercises if e.client_uuid is not None
    }
    existing_exercises_by_id = {e.id: e for e in loaded_exercises}

    for ex_snap in payload.exercises:
        exercise = existing_exercises.get(ex_snap.client_uuid)
        ex_is_new = False
        if exercise is None and ex_snap.server_id is not None:
            exercise = existing_exercises_by_id.get(ex_snap.server_id)
            if exercise is not None:
                exercise.client_uuid = ex_snap.client_uuid

        if ex_snap.deleted:
            if exercise is not None:
                await db.delete(exercise)
            continue

        if exercise is None:
            exercise = WorkoutSessionExercise(
                workout_session_id=workout.id,
                client_uuid=ex_snap.client_uuid,
            )
            db.add(exercise)
            ex_is_new = True

        exercise.exercise_id = ex_snap.exercise_id
        exercise.order_index = ex_snap.order_index
        exercise.superset_group = ex_snap.superset_group
        exercise.notes = ex_snap.notes
        exercise.recommended_rir = ex_snap.recommended_rir
        exercise.recommended_weight = ex_snap.recommended_weight
        exercise.recommended_rep_min = ex_snap.recommended_rep_min
        exercise.recommended_rep_max = ex_snap.recommended_rep_max
        exercise.target_sets = ex_snap.target_sets
        # Write-once: серверное предписание — то, что пользователь уже видел.
        # Клиентское принимаем только когда своего нет (упражнение добавлено
        # офлайн и предписание пришло из локального кэша).
        if not exercise.prescription and ex_snap.prescription:
            exercise.prescription = ex_snap.prescription
        await db.flush()
        id_map[ex_snap.client_uuid] = exercise.id

        # Аналогично: у нового упражнения подходов нет — не трогаем связь.
        loaded_sets = [] if ex_is_new else list(exercise.sets)
        existing_sets = {
            s.client_uuid: s for s in loaded_sets if s.client_uuid is not None
        }
        existing_sets_by_id = {s.id: s for s in loaded_sets}
        set_by_client: dict[str, WorkoutSessionSet] = {}

        # Статистика по упражнению считается один раз на всю пачку подходов
        # снапшота, а не на каждый подход — иначе на большом снимке это N
        # лишних запросов к БД.
        exercise_stats = await load_exercise_stats(
            db, app_user_id, exercise.exercise_id
        )

        # Первый проход: создать/обновить подходы без разрешения parent.
        for set_snap in ex_snap.sets:
            workout_set = existing_sets.get(set_snap.client_uuid)
            if workout_set is None and set_snap.server_id is not None:
                workout_set = existing_sets_by_id.get(set_snap.server_id)
                if workout_set is not None:
                    workout_set.client_uuid = set_snap.client_uuid

            if set_snap.deleted:
                if workout_set is not None:
                    await db.delete(workout_set)
                continue

            if workout_set is None:
                workout_set = WorkoutSessionSet(
                    workout_session_exercise_id=exercise.id,
                    client_uuid=set_snap.client_uuid,
                )
                db.add(workout_set)

            workout_set.set_number = set_snap.set_number
            workout_set.set_type = set_snap.set_type
            workout_set.weight = set_snap.weight
            workout_set.reps = set_snap.reps
            workout_set.effort_level = set_snap.effort_level
            workout_set.notes = set_snap.notes
            workout_set.superset_round = set_snap.superset_round
            workout_set.is_completed = set_snap.is_completed
            workout_set.parent_set_id = None  # разрешим во втором проходе

            # Синк только помечает аномалию — не поднимаем HTTPException ни при
            # каком вердикте, иначе один кривой подход развалит весь снимок
            # тренировки (см. api/schemas/sync.py:28).
            verdict = check_set(
                float(set_snap.weight) if set_snap.weight is not None else None,
                set_snap.reps,
                set_snap.set_type or "normal",
                exercise_stats,
            )
            workout_set.is_anomalous = resolve_is_anomalous(
                verdict, set_snap.anomaly_confirmed
            )

            await db.flush()
            id_map[set_snap.client_uuid] = workout_set.id
            set_by_client[set_snap.client_uuid] = workout_set

        # Второй проход: связать дропсеты с родителями по client_uuid.
        for set_snap in ex_snap.sets:
            if set_snap.deleted or not set_snap.parent_client_uuid:
                continue
            child = set_by_client.get(set_snap.client_uuid)
            if child is None:
                continue
            child.parent_set_id = id_map.get(set_snap.parent_client_uuid)

    # 5. Новая версия — её клиент сохранит и пришлёт как base_version.
    workout.sync_version = (workout.sync_version or 0) + 1
    new_version = workout.sync_version

    await db.commit()
    workout_id = workout.id

    detail = await _load_detail(db, workout_id)
    return SyncWorkoutResponse(
        deleted=False, id_map=id_map, sync_version=new_version, workout=detail
    )


@router.get("/sync/changes", response_model=SyncChangesResponse)
async def sync_changes(
    since: datetime | None = Query(
        default=None,
        description=(
            "Курсор из предыдущего ответа (server_time). Пусто = первичная "
            "выгрузка: активная сессия + последние 30 дней."
        ),
    ),
    limit: int = Query(default=100, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    app_user: AppUser = Depends(get_current_app_user),
):
    """Дельта серверных изменений — восстановление и догон на устройстве.

    Отдаёт тренировки, изменившиеся после `since`, вместе с их версиями, плюс
    надгробия удалённых. Клиент двигает курсор на `server_time` только после
    полной успешной обработки страницы.
    """
    app_user_id = app_user.id
    now = (await db.execute(select(func.now()))).scalar_one()

    stmt = select(WorkoutSession).where(WorkoutSession.app_user_id == app_user_id)

    if since is None:
        # Первый pull (новое устройство / переустановка). Всю историю тянуть
        # нельзя: локальный репозиторий сериализуется целиком на каждый
        # введённый подход, и сотни тренировок сделали бы запись подхода
        # ощутимо медленной. История и так читается с сервера напрямую —
        # локально нужны незавершённая сессия и недавний контекст.
        stmt = stmt.where(
            or_(
                WorkoutSession.status == "active",
                WorkoutSession.updated_at > now - INITIAL_PULL_WINDOW,
            )
        )
    else:
        # Учитываем изменения ДЕТЕЙ: легаси-эндпоинты (workouts.py,
        # workout_supersets.py) правят подходы, не трогая строку тренировки, —
        # по одному лишь WorkoutSession.updated_at такие правки в дельту не попадут.
        child_changed = (
            select(WorkoutSessionExercise.id)
            .where(WorkoutSessionExercise.workout_session_id == WorkoutSession.id)
            .where(
                or_(
                    WorkoutSessionExercise.updated_at > since,
                    exists(
                        select(WorkoutSessionSet.id).where(
                            WorkoutSessionSet.workout_session_exercise_id
                            == WorkoutSessionExercise.id,
                            WorkoutSessionSet.updated_at > since,
                        )
                    ),
                )
            )
        )
        stmt = stmt.where(
            or_(WorkoutSession.updated_at > since, exists(child_changed))
        )

    # limit + 1 — чтобы отличить «есть ещё» без отдельного count.
    rows = (
        (
            await db.execute(
                stmt.order_by(WorkoutSession.updated_at.asc())
                .limit(limit + 1)
                .options(*_detail_options())
            )
        )
        .scalars()
        .all()
    )

    has_more = len(rows) > limit
    rows = list(rows[:limit])

    # Курсор: пока страницы не кончились, двигаем его на updated_at последней
    # отданной тренировки, а не на now() — иначе хвост дельты был бы потерян.
    # Значение консервативное (updated_at родителя может быть старше правки
    # ребёнка), поэтому в худшем случае тренировка приедет повторно — идемпотентно.
    cursor = rows[-1].updated_at if has_more and rows else now

    tomb_stmt = select(SyncTombstone).where(
        SyncTombstone.app_user_id == app_user_id,
        SyncTombstone.entity_type == "workout",
    )
    if since is not None:
        tomb_stmt = tomb_stmt.where(SyncTombstone.deleted_at > since)
    tombs = (await db.execute(tomb_stmt.limit(500))).scalars().all()

    # Предварительные предписания по всем упражнениям пользователя. Их немного
    # (по одному на упражнение с историей), и они нужны целиком: локальный
    # кэш обслуживает добавление любого упражнения офлайн.
    state_rows = (
        (
            await db.execute(
                select(UserExerciseProgressionState).where(
                    UserExerciseProgressionState.app_user_id == app_user_id,
                    UserExerciseProgressionState.next_prescription.isnot(None),
                )
            )
        )
        .scalars()
        .all()
    )
    prescriptions = {
        str(row.exercise_id): row.next_prescription for row in state_rows
    }

    return SyncChangesResponse(
        workouts=rows,
        # Ключ — серверный id тренировки: он есть всегда, в отличие от client_uuid
        # (у созданных легаси-эндпоинтами тренировок его нет).
        versions={str(w.id): (w.sync_version or 0) for w in rows},
        tombstones=[
            SyncTombstoneItem(
                client_uuid=t.client_uuid,
                server_id=t.server_id,
                deleted_at=t.deleted_at,
            )
            for t in tombs
        ],
        prescriptions=prescriptions,
        server_time=cursor,
        has_more=has_more,
    )
