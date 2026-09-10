"""Склейка контура объёма: закрытие окна, предложение, применение решений."""

from __future__ import annotations

import logging
import math
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from api.services.models import (
    AppUserProfile,
    PeriodizationProposal,
    UserCalendarDay,
    VolumeWindow,
    WorkoutPlanExercise,
)
from api.services.periodization import params as periodization_params
from api.services.volume import params, repository
from api.services.volume.decide import (
    DecisionInput,
    MuscleState,
    decide,
    headline_reason,
)
from api.services.volume.landmarks import Landmarks, landmarks_for, scale_landmarks

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

    P0-09 I1: `status == "planned"` и `target_date >= utc_today()` —
    `_apply_prescription` отказывает на любом дне, который уже не "planned"
    (отработан или пропущен), а без фильтра здесь эта функция с готовностью
    называла именно такой день, если он оказывался первым в окне по дате.
    Пользователь принимал совет на дне 3, а замороженный `day_id` указывал
    на уже прошедший день 1 — правка молча не применялась, хотя `decide()`
    предлагал её с расчётом на реальное применение.
    """
    from api.services.models import Exercise
    from api.services.muscle_keys import to_system_key

    days = (await session.execute(
        select(UserCalendarDay)
        .where(
            UserCalendarDay.app_user_id == app_user_id,
            UserCalendarDay.target_date >= window.start_date,
            UserCalendarDay.target_date <= window.end_date,
            UserCalendarDay.target_date >= repository.utc_today(),
            UserCalendarDay.status == "planned",
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

    ВАЖНО (ревью Задачи 11, Important 1): `refresh_volume_proposals` зовётся
    на КАЖДОМ `build_context` — самом горячем эндпоинте приложения. В
    steady state (пользователь заходит хотя бы раз в микроцикл) окно,
    непосредственно предшествующее `finished`, уже закрыто предыдущим
    вызовом, и полный обратный обход не находит ни одного разрыва — но
    честно тратит на это до `MAX_BACKLOG_WINDOWS` итераций по три запроса
    каждая, вечно, впустую. Поэтому здесь короткое замыкание: одним
    запросом проверяем снимок непосредственно предыдущего окна и, если он
    уже есть, выходим — по индукции это означает, что вся история старше
    него тоже уже закрыта (она была бы закрыта тем же способом на каком-то
    предыдущем вызове). Полный обход остаётся только для настоящего
    разрыва — пользователь пропустил визит на 2+ микроцикла.
    """
    earlier = await repository.current_window(
        session, app_user_id, finished.start_date - timedelta(days=1)
    )
    if earlier is None or earlier.start_date >= finished.start_date:
        return

    existing = (await session.execute(
        select(VolumeWindow.id).where(
            VolumeWindow.app_user_id == app_user_id,
            VolumeWindow.block_id == earlier.block_id,
            VolumeWindow.window_index == earlier.window_index,
        )
    )).scalar_one_or_none()
    if existing is not None:
        return

    chain = [earlier]
    cursor = earlier
    for _ in range(MAX_BACKLOG_WINDOWS - 1):
        nxt = await repository.current_window(
            session, app_user_id, cursor.start_date - timedelta(days=1)
        )
        if nxt is None or nxt.start_date >= cursor.start_date:
            break
        chain.append(nxt)
        cursor = nxt

    for window in reversed(chain):
        # Снимок пишется с ТЕКУЩИМИ (на момент вызова) target_by_muscle и
        # level пользователя, а не с теми, что были актуальны в момент,
        # когда это окно реально шло — истории профиля нет (то же
        # ограничение, что и у close_window для самого `finished`, см. его
        # докстринг). При многонедельном догоняющем закрытии разрыв между
        # «уровнем тогда» и «уровнем сейчас» шире, чем для одного окна —
        # это известная фикция снимка, а не баг.
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
    #
    # Ревью Задачи 11, Minor: `w.id != snapshot.id` («самое свежее ДРУГОЕ
    # окно») — не то же самое, что «непосредственно предшествующее». Если
    # промежуточное окно не получило снимка (отвергнуто ratio-guard'ом в
    # close_window — доля дней с предписанием ниже MIN_PRESCRIBED_DAY_RATIO),
    # «самое свежее другое» окно в history может оказаться НЕ смежным со
    # `snapshot`, а «пол» решателя (FLOOR_REQUIRES_PREVIOUS_WINDOW) обязан
    # подтверждаться именно смежным предыдущим окном. Выражаем условие
    # смежности напрямую через даты, а не через позицию в списке.
    previous_snapshot = next(
        (w for w in history if w.end_date < snapshot.start_date), None
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

    if finished.block_id is None:
        # Ревью Задачи 11, Important 3: PeriodizationProposal.block_id — NOT
        # NULL, а Window.block_id для до-P0-08 календарей (дни, у которых
        # ещё не был проставлен block_id) намеренно nullable. Для такого
        # пользователя `PeriodizationProposal(block_id=None)` упал бы на
        # flush() нарушением NOT NULL; guarded() в build_context эту ошибку
        # глотает и логирует, так что обзор объёма молча никогда бы не
        # материализовался, а исключение тихо копилось бы в логах на КАЖДОМ
        # обращении. Обзору объёма физически некуда повеситься без блока —
        # это деградация легаси-календаря, а не ошибка: снимок окна
        # (`snapshot`, уже записан выше через close_window) остаётся в
        # истории, теряется только карточка предложения для пользователя.
        logger.info(
            "P0-09: обзор объёма пропущен для app_user_id=%s — окно %s без "
            "block_id (легаси-календарь до P0-08)",
            app_user_id, snapshot.id,
        )
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

    # P0-09 C2: uq_periodization_proposals_pending уникален по (block_id,
    # kind, COALESCE(payload->>'exercise_id','')) — для volume_review это
    # (block_id, 'volume_review', ''), потому что у этого kind нет
    # exercise_id в payload вовсе. Окно N+1 закрывается ВОЗМОЖНО, пока
    # карточка окна N ещё pending: экран необязывающий, дожидание решения —
    # штатное состояние (см. докстринг apply_volume_decision). Строка выше
    # (`existing is not None: return existing`) уже отсекла случай «карточка
    # для ЭТОГО ЖЕ окна уже есть» — значит любая ещё pending volume_review
    # карточка данного блока, до которой мы дошли здесь, обязана быть по
    # СТАРШЕМУ окну. Без явного истечения вставка ниже валит flush()
    # IntegrityError'ом, guarded() откатывает ВЕСЬ SAVEPOINT и вместе с ним
    # только что записанный close_window-снимок — пользователь навсегда
    # застревает с исключением на каждом /workout-center/context. Совет
    # устаревшего окна и так неактуален (свежее уже посчитано), поэтому
    # истечение — не костыль вокруг индекса, а более честный UX.
    await session.execute(
        sa_update(PeriodizationProposal)
        .where(
            PeriodizationProposal.app_user_id == app_user_id,
            PeriodizationProposal.block_id == finished.block_id,
            PeriodizationProposal.kind == periodization_params.KIND_VOLUME_REVIEW,
            PeriodizationProposal.status == periodization_params.STATUS_PENDING,
        )
        .values(status=periodization_params.STATUS_EXPIRED)
    )

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
    # Ревью Задачи 11, Minor: `options` приходит из тела HTTP-запроса и не
    # типизировано на границе — `{"accepted": 5}` дошёл бы до `set(5)` и
    # упал бы TypeError'ом уже внутри транзакции (500 вместо вежливого
    # отказа). Нечисловой/не-list `accepted` трактуем как пустой — то же
    # осознанное «ничего не применять», что и для явно пустого списка.
    raw_accepted = options.get("accepted")
    accepted = set(raw_accepted) if isinstance(raw_accepted, list) else set()
    items = (proposal.payload or {}).get("adjustments") or []

    applied: list[int] = []
    for item in items:
        if item["index"] not in accepted:
            continue
        kind = item["kind"]
        if kind == params.KIND_BUDGET_TO_RANGE:
            if await _apply_budget(session, app_user_id, item, proposal.block_id):
                applied.append(item["index"])
        elif kind == params.KIND_BUDGET_TO_FREQUENCY:
            if await _apply_frequency(session, app_user_id, item, proposal.block_id):
                applied.append(item["index"])
        elif kind in (params.KIND_PRESCRIPTION_ADD, params.KIND_PRESCRIPTION_CUT):
            if await _apply_prescription(session, app_user_id, proposal.id, item):
                applied.append(item["index"])

    # Ревью Задачи 11, Minor: раньше здесь всегда стоял "applied", даже
    # когда `applied` пуст и вызывающая apply_decision пишет
    # proposal.status = STATUS_DECLINED — ответ и записанная строка
    # расходились. Синхронизируем прямо здесь, а не в вызывающей стороне,
    # чтобы расхождение не завелось снова в новом вызывающем коде.
    return {
        "status": "applied" if applied else "declined",
        "proposal_id": proposal.id,
        "applied": applied,
    }


async def _apply_budget(
    session: AsyncSession, app_user_id: int, item: dict, block_id: Optional[int] = None
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
    # P0-09 I2: target_sets в бюджете уже смасштабирован под ФАКТИЧЕСКУЮ
    # длину микроцикла блока (volume_calculator.clamp_target). Клампить его
    # против СЫРЫХ (за 7 дней) lm.mev/lm.mrv значило бы судить десятидневную
    # цель семидневным потолком — легитимная цель схлопывалась бы при
    # каждом принятии рычага. Масштабируем границы той же формулой, что и
    # close_window при заморозке снимка (см. landmarks.scale_landmarks).
    cycle_multiplier = await repository.block_microcycle_length(session, block_id) / 7.0
    scaled_lm = scale_landmarks(lm, cycle_multiplier)

    row = dict(weekly[muscle])
    updated = int(row.get("target_sets") or 0) + int(item.get("delta_sets") or 0)
    # Итог всё равно клампится в диапазон: правка не имеет права вынести
    # цель за физиологические границы, ради которых она и делается.
    row["target_sets"] = max(scaled_lm.mev, min(scaled_lm.mrv, updated))
    weekly[muscle] = row
    budget["weekly_targets"] = weekly
    profile.volume_budget = budget
    flag_modified(profile, "volume_budget")
    return True


async def _apply_frequency(
    session: AsyncSession, app_user_id: int, item: dict, block_id: Optional[int] = None
) -> bool:
    """Привести весь бюджет к реально достижимой частоте.

    Рычаг не про мышцу, а про расписание целиком: если из шести
    предписанных дней стабильно выходит четыре, цель по КАЖДОЙ мышце
    завышена в одной и той же пропорции. Масштабируем все цели на
    наблюдаемую исполняемость и снова клампим каждую в её диапазон —
    правка не имеет права вынести цель за границы, ради которых делается.

    Ревью Задачи 11, Important 2: до этой функции `budget_to_frequency`
    эмитился decide() с `muscle=None`, а `apply_volume_decision` маршрутил
    его в `_apply_budget`, которая немедленно отказывает на `not muscle`.
    Рычаг «привести цель к реальной частоте» существовал только в тексте
    карточки — принять его пользователь не мог: `applied` оставался пустым,
    предложение уходило в declined, а бюджет не менялся.
    """
    ratios = [float(r) for r in (item.get("detail") or {}).get("ratios") or []]
    if not ratios:
        return False
    observed = sum(ratios) / len(ratios)
    if observed <= 0:
        return False

    profile = (await session.execute(
        select(AppUserProfile).where(AppUserProfile.app_user_id == app_user_id)
    )).scalar_one_or_none()
    if profile is None or not profile.volume_budget:
        return False

    # P0-09 I2: та же логика, что и в _apply_budget — клампим против границ,
    # смасштабированных под фактическую длину микроцикла блока, а не против
    # сырой (за 7 дней) таблицы.
    cycle_multiplier = await repository.block_microcycle_length(session, block_id) / 7.0

    budget = dict(profile.volume_budget)
    weekly = dict(budget.get("weekly_targets") or {})
    changed = False
    for muscle, row in weekly.items():
        lm = landmarks_for(muscle, profile.experience_level)
        current = int((row or {}).get("target_sets") or 0)
        if lm is None or current <= 0:
            continue
        scaled_lm = scale_landmarks(lm, cycle_multiplier)
        scaled_target = max(
            scaled_lm.mev, min(scaled_lm.mrv, int(math.floor(current * observed)))
        )
        if scaled_target != current:
            patched = dict(row)
            patched["target_sets"] = scaled_target
            weekly[muscle] = patched
            changed = True

    if not changed:
        return False

    budget["weekly_targets"] = weekly
    profile.volume_budget = budget
    flag_modified(profile, "volume_budget")
    return True


async def _apply_prescription(
    session: AsyncSession, app_user_id: int, proposal_id: int, item: dict
) -> bool:
    """Записать правку предписания в день, разрешённый ПРЯМО СЕЙЧАС.

    P0-09 I1: `day_id`/`exercise_id` в payload заморожены в момент, когда
    `_pick_day_for` строила предложение, — карточка не блокирующая и может
    провисеть pending часы или дни. Если за это время замороженный день
    перестал быть "planned" (пользователь его отработал или он пропущен),
    слепое доверие старому `day_id` тихо хоронит правку отказом, хотя в
    окне вполне может найтись другой, ещё не пройденный день с той же
    главной мышцей. Поэтому день переразрешается здесь: замороженные
    значения используются, только если день всё ещё валиден; иначе —
    свежий поиск через `_pick_day_for` по текущему окну.
    """
    day_id = item.get("day_id")
    exercise_id = item.get("exercise_id")
    muscle = item.get("muscle")

    day = None
    if day_id:
        day = (await session.execute(
            select(UserCalendarDay).where(
                UserCalendarDay.id == day_id,
                UserCalendarDay.app_user_id == app_user_id,
            )
        )).scalar_one_or_none()

    frozen_valid = (
        day is not None
        and day.status == "planned"
        and day.target_date >= repository.utc_today()
        and exercise_id is not None
    )

    if not frozen_valid:
        if not muscle:
            return False
        window = await repository.current_window(
            session, app_user_id, repository.utc_today()
        )
        if window is None:
            return False
        day_id, exercise_id = await _pick_day_for(session, app_user_id, window, muscle)
        if day_id is None or exercise_id is None:
            # Ни одного валидного дня для этой мышцы не нашлось — правку
            # честно не применяем, а не молча привязываем к устаревшему дню.
            return False
        day = (await session.execute(
            select(UserCalendarDay).where(
                UserCalendarDay.id == day_id,
                UserCalendarDay.app_user_id == app_user_id,
            )
        )).scalar_one_or_none()
        if day is None:
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
