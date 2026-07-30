"""Единственное место пакета готовности, знающее про БД.

UserObservation — append-only журнал: правок и удалений нет, побеждает
последнее наблюдение по паре (kind, subject) в окне. Отсюда бесплатно
получается жизненный цикл боли: "прошло" пишет новую запись value=0,
а история травмы остаётся — ровно то, ради чего таблица заводилась
в P0-01.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.models import UserObservation
from api.services.muscle_keys import to_system_key
from api.services.readiness import params
from api.services.readiness.types import CheckinSignals, ExerciseTarget


def checkin_enabled(settings: Optional[dict[str, Any]]) -> bool:
    """Включён ли контур сбора (спека §10).

    Выключен — значит вердикт всегда None, движок ведёт себя как P0-06,
    и вопрос о боли в итогах тренировки тоже не показывается.

    settings — свободный JSONB: туда мог попасть мусор от старых сборок,
    поэтому нестрогая проверка типа, а не settings["readiness"]["..."].
    """
    block = (settings or {}).get("readiness")
    if not isinstance(block, dict):
        return True
    value = block.get("checkin_enabled")
    if not isinstance(value, bool):
        return True
    return value


def build_exercise_target(exercise: Any) -> ExerciseTarget:
    """Строка БД -> чистый ExerciseTarget с нормализованными ключами.

    main_muscle_group хранится по-русски, secondary_muscle_groups — JSONB
    со смесью кодировок. to_system_key — единственный мост между ними
    и EN system keys, которыми оперирует вердикт.
    """
    action = getattr(exercise, "action", None)
    action_value = getattr(action, "value", action) or "unknown"

    secondary = []
    for raw in getattr(exercise, "secondary_muscle_groups", None) or []:
        key = to_system_key(raw)
        if key and key not in secondary:
            secondary.append(key)

    return ExerciseTarget(
        exercise_id=getattr(exercise, "id", 0),
        main_muscle=to_system_key(getattr(exercise, "main_muscle_group", None)),
        secondary_muscles=tuple(secondary),
        action=str(action_value),
    )


def _rows_for(
    signals: CheckinSignals, observed_at: datetime
) -> list[tuple[str, float, Optional[str]]]:
    """(kind, value, subject) на каждый непустой ответ."""
    rows: list[tuple[str, float, Optional[str]]] = []
    if signals.sleep is not None:
        rows.append((params.KIND_SLEEP, float(signals.sleep), None))
    if signals.stress is not None:
        rows.append((params.KIND_STRESS, float(signals.stress), None))
    for muscle, value in sorted((signals.soreness or {}).items()):
        if value is not None:
            rows.append((params.KIND_SORENESS, float(value), muscle))
    for place, value in sorted((signals.pain or {}).items()):
        if value is not None:
            rows.append((params.KIND_PAIN, float(value), place))
    return rows


async def save_signals(
    session: AsyncSession,
    app_user_id: int,
    signals: CheckinSignals,
    source: str,
    client_uuid: Optional[str],
) -> list[UserObservation]:
    """Записать ответы чек-ина. Идемпотентно по client_uuid.

    Идемпотентность нужна офлайн-очереди: повторная отправка того же
    чек-ина не должна множить наблюдения. Проверка на уровне приложения —
    init_db не создаёт индексов и констрейнтов на ALTER-колонках.
    """
    if client_uuid:
        existing = await session.execute(
            select(UserObservation.id).where(
                UserObservation.app_user_id == app_user_id,
                UserObservation.client_uuid == client_uuid,
            ).limit(1)
        )
        if existing.scalars().first() is not None:
            return []

    observed_at = signals.observed_at or datetime.now(timezone.utc)
    created: list[UserObservation] = []
    for kind, value, subject in _rows_for(signals, observed_at):
        row = UserObservation(
            app_user_id=app_user_id,
            kind=kind,
            value=value,
            subject=subject,
            source=source,
            observed_at=observed_at,
            client_uuid=client_uuid,
        )
        session.add(row)
        created.append(row)
    return created


async def load_signals(
    session: AsyncSession, app_user_id: int, checkin_client_uuid: str
) -> CheckinSignals:
    """Поднять сигналы одного чек-ина по его uuid.

    Привязка по uuid, а не по временному окну: вердикт принадлежит
    сессии, которую породил, и угадывать "чек-ин за последние N часов,
    наверное, про эту тренировку" не нужно (спека §6.6).
    """
    rows = (
        await session.execute(
            select(UserObservation).where(
                UserObservation.app_user_id == app_user_id,
                UserObservation.client_uuid == checkin_client_uuid,
            )
        )
    ).scalars().all()

    sleep = stress = None
    soreness: dict[str, int] = {}
    pain: dict[str, int] = {}
    observed_at: Optional[datetime] = None

    for row in rows:
        observed_at = observed_at or row.observed_at
        if row.kind == params.KIND_SLEEP:
            sleep = int(row.value)
        elif row.kind == params.KIND_STRESS:
            stress = int(row.value)
        elif row.kind == params.KIND_SORENESS and row.subject:
            soreness[row.subject] = int(row.value)
        elif row.kind == params.KIND_PAIN and row.subject:
            pain[row.subject] = int(row.value)

    return CheckinSignals(
        sleep=sleep,
        stress=stress,
        soreness=soreness,
        pain=pain,
        observed_at=observed_at,
    )


async def verdict_for_checkin(
    session: AsyncSession,
    app_user_id: int,
    checkin_client_uuid: Optional[str],
    settings: Optional[dict[str, Any]] = None,
):
    """Вердикт по uuid чек-ина. None — вердикта нет, движок как P0-06.

    None возвращается в трёх случаях: uuid не прислали, чек-ины выключены
    в настройках, наблюдений по этому uuid нет (например, офлайн-чек-ин
    ещё не доехал синхронизацией — это штатная ситуация, а не ошибка).
    """
    from api.services.readiness.verdict import build_verdict

    if not checkin_client_uuid:
        return None
    if not checkin_enabled(settings):
        return None
    signals = await load_signals(session, app_user_id, checkin_client_uuid)
    return build_verdict(signals)


async def active_pain(
    session: AsyncSession, app_user_id: int, now: Optional[datetime] = None
) -> dict[str, int]:
    """Места, которые болят прямо сейчас: место -> сила.

    Окно PAIN_ACTIVE_DAYS отсекает устаревшее само, поэтому отдельной
    чистки журнала не нужно — в том числе после того, как пользователь
    надолго выключал чек-ины и снова включил.
    """
    moment = now or datetime.now(timezone.utc)
    since = moment - timedelta(days=params.PAIN_ACTIVE_DAYS)

    rows = (
        await session.execute(
            select(UserObservation)
            .where(
                UserObservation.app_user_id == app_user_id,
                UserObservation.kind == params.KIND_PAIN,
                UserObservation.observed_at >= since,
            )
            .order_by(UserObservation.observed_at.asc(), UserObservation.id.asc())
        )
    ).scalars().all()

    # Побеждает последнее наблюдение по subject: более поздние строки
    # затирают более ранние, включая обнуление "прошло".
    latest: dict[str, int] = {}
    for row in rows:
        if row.subject:
            latest[row.subject] = int(row.value)

    return {place: value for place, value in latest.items() if value > params.PAIN_MIN}
