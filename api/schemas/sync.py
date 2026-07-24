from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field

from api.schemas.workouts import (
    WorkoutSessionDetailResponse,
    WorkoutSource,
    WorkoutStatus,
    WorkoutSetType,
)


class SyncSetSnapshot(BaseModel):
    """Снимок одного подхода из локального стора клиента."""
    client_uuid: str
    # Серверный id для адаптации ранее созданных (без client_uuid) строк:
    # если по client_uuid не нашли, матчим по server_id и проставляем client_uuid.
    server_id: int | None = None
    set_number: int
    set_type: WorkoutSetType = "normal"
    weight: Decimal | None = Field(default=None, ge=0, le=2000)
    reps: int | None = Field(default=None, ge=0, le=1000)
    # effort_level принимаем свободной строкой: набор значений на клиенте и
    # бэкенде исторически расходится ("warmup" vs "warmup_effort"), а синк не
    # место для 422 из-за расхождения справочников.
    effort_level: Optional[str] = None
    notes: str | None = None
    # Родитель (для дропсетов) задаётся клиентским uuid — разрешается в
    # серверный parent_set_id во втором проходе.
    parent_client_uuid: str | None = None
    superset_round: int | None = None
    is_completed: bool = True
    # Клиент выставляет true, когда пользователь подтвердил подозрительное значение.
    anomaly_confirmed: bool = False
    deleted: bool = False
    # Время последней локальной правки — клиент использует его для per-entity LWW
    # при merge после 409. Сервер только сохраняет/возвращает его.
    updated_at: datetime | None = None


class SyncExerciseSnapshot(BaseModel):
    """Снимок упражнения сессии со всеми подходами."""
    client_uuid: str
    server_id: int | None = None
    exercise_id: int = Field(gt=0)
    order_index: int
    superset_group: str | None = None
    notes: str | None = None
    recommended_rir: int | None = None
    recommended_weight: Decimal | None = Field(default=None, ge=0, le=2000)
    recommended_rep_min: int | None = None
    recommended_rep_max: int | None = None
    target_sets: int | None = None
    deleted: bool = False
    updated_at: datetime | None = None
    sets: list[SyncSetSnapshot] = []


class SyncWorkoutSnapshot(BaseModel):
    """Полный снимок тренировки для идемпотентного upsert по client_uuid."""
    client_uuid: str
    server_id: int | None = None
    # Версия, на которой основан снимок (оптимистичная блокировка). None = клиент
    # ещё ни разу не синхронизировал эту тренировку — проверку пропускаем.
    base_version: int | None = None
    source: WorkoutSource
    status: WorkoutStatus
    split_day_id: uuid.UUID | None = None
    plan_id: int | None = None
    notes: str | None = None
    # Субъективная тяжесть сессии по шкале Борга CR10 (0-10); заполняется позже,
    # после завершения тренировки, поэтому опциональна и в снапшоте.
    session_rpe: float | None = Field(default=None, ge=0, le=10)
    volume_targets: dict | None = None
    started_at: datetime
    finished_at: datetime | None = None
    deleted: bool = False
    exercises: list[SyncExerciseSnapshot] = []


class SyncWorkoutResponse(BaseModel):
    """Результат синка: серверное представление + карта client_uuid → server id."""
    deleted: bool = False
    # client_uuid → серверный integer id (для тренировки, упражнений и подходов)
    id_map: dict[str, int] = {}
    # Новая версия тренировки — клиент сохраняет её и пришлёт как base_version.
    sync_version: int = 0
    workout: WorkoutSessionDetailResponse | None = None


class SyncConflictResponse(BaseModel):
    """Тело 409: снимок основан на устаревшей версии.

    Клиент должен слить серверное состояние со своим (union по client_uuid,
    per-entity LWW по updated_at) и повторить пуш с новым base_version.

    Кладётся в поле detail HTTPException, поэтому на проводе выглядит как
    {"detail": {"error": "sync_conflict", "sync_version": ..., "workout": ...}}.
    """
    error: str = "sync_conflict"
    sync_version: int
    workout: WorkoutSessionDetailResponse


class SyncTombstoneItem(BaseModel):
    """Удалённая на другом устройстве сущность."""
    client_uuid: str
    server_id: int | None = None
    deleted_at: datetime


class SyncChangesResponse(BaseModel):
    """Дельта серверных изменений для восстановления/догона на устройстве."""
    workouts: list[WorkoutSessionDetailResponse] = []
    # str(серверный id тренировки) → её актуальная sync_version. Ключ именно id,
    # а не client_uuid: у тренировок, созданных легаси-эндпоинтами, client_uuid нет.
    versions: dict[str, int] = {}
    tombstones: list[SyncTombstoneItem] = []
    # Курсор для следующего запроса. Всегда серверное время — локальные часы
    # устройства не участвуют, иначе расхождение часов теряет изменения.
    server_time: datetime
    has_more: bool = False
