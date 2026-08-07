"""Склейка контура объёма: закрытие окна, предложение, применение решений."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from api.services.models import (
    AppUserProfile,
    PeriodizationProposal,
    UserCalendarDay,
    WorkoutPlanExercise,
)
from api.services.periodization import params as periodization_params
from api.services.volume import params, repository
from api.services.volume.decide import (
    Adjustment,
    DecisionInput,
    MuscleState,
    decide,
    headline_reason,
)
from api.services.volume.landmarks import Landmarks, landmarks_for

logger = logging.getLogger(__name__)

# [КОНФИГ] Сколько закрытых окон читаем ради предыстории. Нужен ровно один
# предыдущий плюс окна для истории adherence.
HISTORY_WINDOWS = 3

# [КОНФИГ] Сколько окон подряд можно тихо закрыть без предложения, догоняя
# визит пользователя, пропустившего несколько микроциклов подряд. По
# аналогии с periodization.params.MAX_CHAIN_ROLLOVERS: бесконечный откат
# назад по редко используемому аккаунту не нужен, а разумная глубина есть.
MAX_BACKLOG_WINDOWS = 6


def targets_from_budget(profile: Optional[AppUserProfile]) -> dict[str, float]:
    if profile is None or not profile.volume_budget:
        return {}
    weekly = profile.volume_budget.get("weekly_targets") or {}
    return {
        muscle: float((data or {}).get("target_sets") or 0)
        for muscle, data in weekly.items()
    }


def _states_from_snapshot(snapshot) -> dict[str, MuscleState]:
    """Снимок окна -> вход решателя. Границы берутся ИЗ СНИМКА, а не из
    текущей таблицы: решение обязано остаться объяснимым после калибровки."""
    states: dict[str, MuscleState] = {}
    for muscle, row in (snapshot.muscles or {}).items():
        lm_row = (snapshot.landmarks or {}).get(muscle)
        if lm_row is None:
            continue
        states[muscle] = MuscleState(
            target=float(row.get("target") or 0),
            prescribed=float(row.get("prescribed") or 0),
            performed_direct=float(row.get("performed_direct") or 0),
            performed_indirect=float(row.get("performed_indirect") or 0),
            landmarks=Landmarks(
                mev=lm_row["mev"], mav=lm_row["mav"], mrv=lm_row["mrv"],
                mev_direct=lm_row["mev_direct"], mrv_direct=lm_row["mrv_direct"],
            ),
        )
    return states


def _adherence_ratio(snapshot) -> float:
    data = snapshot.adherence or {}
    planned = int(data.get("planned_days") or 0)
    if planned <= 0:
        return 1.0
    return int(data.get("completed_days") or 0) / planned


async def _pick_day_for(
    session: AsyncSession, app_user_id: int, window, muscle: str
) -> tuple[Optional[int], Optional[int]]:
    """Найти день и упражнение будущего окна, к которым привязать правку.

    Берём первый день окна, где есть упражнение с этой главной мышцей:
    правка должна называть конкретное упражнение в конкретный день, иначе
    совет «добавь два подхода на широчайшие» некуда применить.
    """
    from api.services.models import Exercise
    from api.services.muscle_keys import to_system_key

    days = (await session.execute(
        select(UserCalendarDay)
        .where(
            UserCalendarDay.app_user_id == app_user_id,
            UserCalendarDay.target_date >= window.start_date,
            UserCalendarDay.target_date <= window.end_date,
            UserCalendarDay.plan_id.is_not(None),
        )
        .order_by(UserCalendarDay.target_date)
    )).scalars().all()

    for day in days:
        rows = (await session.execute(
            select(WorkoutPlanExercise.exercise_id, Exercise.main_muscle_group)
            .join(Exercise, WorkoutPlanExercise.exercise_id == Exercise.id)
            .where(WorkoutPlanExercise.plan_id == day.plan_id)
            .order_by(WorkoutPlanExercise.order_index)
        )).all()
        for exercise_id, main in rows:
            if to_system_key(main) == muscle:
                return day.id, exercise_id
    return None, None


async def _close_backlog(
    session: AsyncSession,
    app_user_id: int,
    finished,
    target_by_muscle: dict[str, float],
    level: Optional[str],
) -> None:
    """Тихо закрыть окна СТАРШЕ `finished`, если у них ещё нет снимка.

    `close_window` идемпотентна, поэтому повторный вызов на уже закрытом
    окне — no-op. Решение и предложение здесь не создаются: высказаться
    стоит только по самому свежему окну, а более старые нужны лишь как
    материал для «previous» в decide().
    """
    chain = []
    cursor = finished
    for _ in range(MAX_BACKLOG_WINDOWS):
        earlier = await repository.current_window(
            session, app_user_id, cursor.start_date - timedelta(days=1)
        )
        if earlier is None or earlier.start_date >= cursor.start_date:
            break
        chain.append(earlier)
        cursor = earlier

    for window in reversed(chain):
        await repository.close_window(
            session, app_user_id, window, target_by_muscle, level
        )


async def refresh_volume_proposals(
    session: AsyncSession, app_user_id: int, today: date
) -> Optional[PeriodizationProposal]:
    """Закрыть отработанное окно и, если есть что сказать, создать предложение.

    Лениво, на существующих точках (завершение сессии и запрос контекста) —
    планировщика в проекте нет и заводить его не нужно.
    """
    open_window = await repository.current_window(session, app_user_id, today)
    if open_window is None:
        return None

    # Закрываем ПРЕДЫДУЩЕЕ окно: текущее ещё идёт.
    previous_day = open_window.start_date - timedelta(days=1)
    finished = await repository.current_window(session, app_user_id, previous_day)
    # Сравниваем ДАТЫ НАЧАЛА, а не window_index: индекс окна нумеруется
    # внутри блока, и на границе блоков «окно 1 нового» имело бы индекс
    # МЕНЬШЕ, чем «окно 3 предыдущего». Сравнение индексов тогда решило бы,
    # что закрывать нечего, и последнее окно каждого блока не закрывалось бы
    # никогда.
    if finished is None or finished.start_date >= open_window.start_date:
        return None

    profile = (await session.execute(
        select(AppUserProfile).where(AppUserProfile.app_user_id == app_user_id)
    )).scalar_one_or_none()
    level = profile.experience_level if profile else None
    targets = targets_from_budget(profile)

    # Тихо догоняем окна СТАРШЕ finished, если они ещё не закрыты. Эта
    # функция закрывает ровно ОДНО (самое свежее) окно за вызов — если
    # пользователь пропустил визит и предыдущее окно ("finished" здесь)
    # оказалось не первым непросмотренным, окно перед ним так и осталось бы
    # без снимка навсегда. Без снимка «пол» решателя никогда не сработает:
    # он требует подтверждения ИМЕННО ПРЕДЫДУЩИМ окном
    # (params.FLOOR_REQUIRES_PREVIOUS_WINDOW), а предыдущим для decide()
    # ниже считается снимок в VolumeWindow, а не пересчитанное на лету
    # состояние. Backlog закрывается БЕЗ решения и БЕЗ предложения — по
    # аналогии с MAX_CHAIN_ROLLOVERS в periodization.repository: непрерывная
    # история снимков нужна вся, а высказаться пользователю есть смысл
    # только по самому свежему окну.
    await _close_backlog(session, app_user_id, finished, targets, level)

    snapshot = await repository.close_window(
        session, app_user_id, finished, targets, level
    )
    if snapshot is None:
        return None

    existing = (await session.execute(
        select(PeriodizationProposal).where(
            PeriodizationProposal.app_user_id == app_user_id,
            PeriodizationProposal.kind == periodization_params.KIND_VOLUME_REVIEW,
            PeriodizationProposal.payload["window_id"].astext == str(snapshot.id),
        )
    )).scalar_one_or_none()
    if existing is not None:
        return existing

    history = await repository.closed_windows(session, app_user_id, HISTORY_WINDOWS)

    # Правка подхода задним числом пересчитывает снимок последнего закрытого
    # окна (спека §7). Уже созданное по нему предложение НЕ отменяется:
    # решение принималось по числам, которые были видны в тот момент.
    if history:
        await repository.recompute_stale_window(session, app_user_id, history[0])

    # ВАЖНО: не сравнивать по window_index — `history` не скоупится блоком
    # (repository.closed_windows читает по всем блокам пользователя), а
    # индекс нумеруется ВНУТРИ БЛОКА (см. докстринг Window/_window_from в
    # repository.py). На границе блоков «окно 1 нового блока» имело бы
    # индекс МЕНЬШЕ, чем «окно 3 предыдущего», хотя хронологически именно
    # окно 3 предыдущего блока — то самое предыдущее. `history` уже
    # отсортирована по end_date по убыванию (closed_windows), а `snapshot` —
    # самый свежий закрытый снимок из всех, поэтому первый элемент `history`
    # с другим id — это и есть хронологически предыдущее окно.
    previous_snapshot = next(
        (w for w in history if w.id != snapshot.id), None
    )

    next_prescribed_raw = await repository.prescribed_for(
        session, app_user_id, open_window
    )
    next_prescribed = {m: c.effective for m, c in next_prescribed_raw.items()}

    adjustments = decide(DecisionInput(
        closed=_states_from_snapshot(snapshot),
        previous=_states_from_snapshot(previous_snapshot) if previous_snapshot else None,
        next_prescribed=next_prescribed,
        adherence_ratios=[_adherence_ratio(w) for w in history],
        is_deload=finished.is_deload,
    ))
    if not adjustments:
        return None

    payload_items = []
    for index, adjustment in enumerate(adjustments):
        item = {
            "index": index,
            "kind": adjustment.kind,
            "muscle": adjustment.muscle,
            "reason_code": adjustment.reason_code,
            "delta_sets": adjustment.delta_sets,
            "detail": adjustment.detail,
            "day_id": None,
            "exercise_id": None,
        }
        if adjustment.kind in (
            params.KIND_PRESCRIPTION_ADD, params.KIND_PRESCRIPTION_CUT
        ) and adjustment.muscle:
            day_id, exercise_id = await _pick_day_for(
                session, app_user_id, open_window, adjustment.muscle
            )
            item["day_id"] = day_id
            item["exercise_id"] = exercise_id
        payload_items.append(item)

    proposal = PeriodizationProposal(
        app_user_id=app_user_id,
        block_id=finished.block_id,
        kind=periodization_params.KIND_VOLUME_REVIEW,
        reason_code=headline_reason(adjustments),
        payload={"window_id": snapshot.id, "adjustments": payload_items},
        status=periodization_params.STATUS_PENDING,
    )
    session.add(proposal)
    await session.flush()
    return proposal


async def apply_volume_decision(
    session: AsyncSession,
    app_user_id: int,
    proposal: PeriodizationProposal,
    action: str,
    options: dict,
) -> dict:
    """Применить отмеченные пользователем правки.

    `options["accepted"]` — список индексов правок из payload. Пустой список
    означает «ничего не применять»: экран не блокирующий, и бездействие
    равно «продолжаем по плану».
    """
    accepted = set(options.get("accepted") or [])
    items = (proposal.payload or {}).get("adjustments") or []

    applied: list[int] = []
    for item in items:
        if item["index"] not in accepted:
            continue
        kind = item["kind"]
        if kind in (params.KIND_BUDGET_TO_RANGE, params.KIND_BUDGET_TO_FREQUENCY):
            if await _apply_budget(session, app_user_id, item):
                applied.append(item["index"])
        elif kind in (params.KIND_PRESCRIPTION_ADD, params.KIND_PRESCRIPTION_CUT):
            if await _apply_prescription(session, app_user_id, proposal.id, item):
                applied.append(item["index"])

    return {"status": "applied", "proposal_id": proposal.id, "applied": applied}


async def _apply_budget(
    session: AsyncSession, app_user_id: int, item: dict
) -> bool:
    profile = (await session.execute(
        select(AppUserProfile).where(AppUserProfile.app_user_id == app_user_id)
    )).scalar_one_or_none()
    if profile is None or not profile.volume_budget:
        return False

    budget = dict(profile.volume_budget)
    weekly = dict(budget.get("weekly_targets") or {})
    muscle = item.get("muscle")
    if not muscle or muscle not in weekly:
        return False

    lm = landmarks_for(muscle, profile.experience_level)
    if lm is None:
        return False

    row = dict(weekly[muscle])
    updated = int(row.get("target_sets") or 0) + int(item.get("delta_sets") or 0)
    # Итог всё равно клампится в диапазон: правка не имеет права вынести
    # цель за физиологические границы, ради которых она и делается.
    row["target_sets"] = max(lm.mev, min(lm.mrv, updated))
    weekly[muscle] = row
    budget["weekly_targets"] = weekly
    profile.volume_budget = budget
    flag_modified(profile, "volume_budget")
    return True


async def _apply_prescription(
    session: AsyncSession, app_user_id: int, proposal_id: int, item: dict
) -> bool:
    day_id = item.get("day_id")
    exercise_id = item.get("exercise_id")
    if not day_id or not exercise_id:
        return False

    day = (await session.execute(
        select(UserCalendarDay).where(
            UserCalendarDay.id == day_id,
            UserCalendarDay.app_user_id == app_user_id,
        )
    )).scalar_one_or_none()
    if day is None or day.status != "planned":
        # День уже отработан или пропущен — править его предписание поздно.
        return False

    existing = list(day.volume_adjustments or [])
    existing.append({
        "exercise_id": exercise_id,
        "delta_sets": int(item.get("delta_sets") or 0),
        "proposal_id": proposal_id,
    })
    day.volume_adjustments = existing
    flag_modified(day, "volume_adjustments")
    return True
