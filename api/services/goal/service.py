"""Автопилот цели: создание предложений и сборка контекста экрана (P0-12).

Лениво, на существующих точках вызова — планировщика в проекте нет и
заводить его не нужно (то же решение, что в volume.service).

build_context (Задача 11) — тело GET /goals/{id}/autopilot: всё, что нужно
экрану цели, одним вызовом поверх evaluate(). Молчание всегда с причиной
(см. её докстринг).
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import or_ as sa_or
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from api.services.forecast_service import WEEKLY_GROWTH_CAP_PCT, _DEFAULT_CAP_PCT
from api.services.goal import decide, params, repository, simulate
from api.services.goal.types import DecisionInput, Rates
from api.services.models import (
    AppUserProfile,
    Exercise,
    PeriodizationProposal,
    TrainingBlock,
    UserCalendarDay,
    UserExercisePreference,
    UserGoal,
)
from api.services.periodization import params as periodization_params

_HORIZON_TAIL_DAYS = 120  # запас за дедлайном, чтобы увидеть «не успеваем»


async def _primary_goal(session: AsyncSession, app_user_id: int) -> Optional[UserGoal]:
    return (await session.execute(
        select(UserGoal).where(
            UserGoal.app_user_id == app_user_id,
            UserGoal.is_primary.is_(True),
            UserGoal.is_completed.is_(False),
        )
    )).scalar_one_or_none()


def _goal_target_e1rm(goal: UserGoal) -> float:
    """Целевой e1RM цели — та же формула Эпли, что и в evaluate()/simulate."""
    reps = goal.target_reps or 1
    return float(goal.target_value) * (1.0 + reps / 30.0)


async def _retire_finished_primary_goal(
    session: AsyncSession, app_user_id: int, goal: UserGoal, today: date
) -> bool:
    """Снять is_primary с достигнутой или просроченной ведущей цели.

    ДОПОЛНИТЕЛЬНОЕ ТРЕБОВАНИЕ СВЕРХ БРИФА (спека §7, долг задачи не покрыт
    ни одной из Задач 1-5): без этого завершённая цель молча удерживает
    единственный слот ведущей (uq_user_goals_primary), и пользователь
    никогда не получает предложения выбрать следующую. Достигнутая — когда
    текущий e1RM дошёл до целевого; просроченная — когда дедлайн уже прошёл.
    В обоих случаях гасим и активное goal_plan-предложение: рычаги по
    закрытой цели больше не актуальны.
    """
    overdue = goal.deadline is not None and goal.deadline < today

    achieved = False
    if goal.exercise_id is not None:
        current = await repository.current_e1rm(session, app_user_id, goal.exercise_id)
        if current is not None and current >= _goal_target_e1rm(goal):
            achieved = True

    if not overdue and not achieved:
        return False

    goal.is_primary = False
    await session.execute(
        sa_update(PeriodizationProposal)
        .where(
            PeriodizationProposal.app_user_id == app_user_id,
            PeriodizationProposal.kind == periodization_params.KIND_GOAL_PLAN,
            PeriodizationProposal.status == periodization_params.STATUS_PENDING,
            # ИСПРАВЛЕНО (ревью Задачи 6, Minor 3): гасим только предложения
            # ИМЕННО этой цели, а не все goal_plan-предложения пользователя —
            # у пользователя может быть больше одной цели за её жизнь (смена
            # ведущей), и путь создания уже скопирован по goal_id (см. ниже).
            PeriodizationProposal.payload["goal_id"].astext == str(goal.id),
        )
        .values(status=periodization_params.STATUS_EXPIRED)
    )
    await session.commit()
    return True


def _inputs_hash(profile: Optional[AppUserProfile], goal: UserGoal) -> str:
    """Отпечаток условий: расписание, оборудование, ограничения, цель.

    Пользователь, дважды сохранивший профиль без изменений, не должен
    получить два предложения.
    """
    settings = (profile.settings or {}) if profile else {}
    payload = {
        "frequency": getattr(profile, "training_frequency", None),
        "microcycle": getattr(profile, "microcycle_length", None),
        "locations": sorted(settings.get("locations") or []),
        "prehab": sorted(settings.get("prehab_flags") or []),
        "budget": (profile.volume_budget or {}) if profile else {},
        "deadline": goal.deadline.isoformat() if goal.deadline else None,
        "target": float(goal.target_value),
        "reps": goal.target_reps,
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


async def evaluate(
    session: AsyncSession, app_user_id: int, goal: UserGoal, today: date
) -> Optional[dict]:
    """Полный расчёт по ведущей цели: обе даты, темпы, рычаги.

    None — цели нельзя дать честный прогноз (нет истории лифта). Молчание
    здесь правильнее любого числа.
    """
    if goal.exercise_id is None or goal.deadline is None:
        return None

    current = await repository.current_e1rm(session, app_user_id, goal.exercise_id)
    if current is None or current <= 0:
        return None

    profile = (await session.execute(
        select(AppUserProfile).where(AppUserProfile.app_user_id == app_user_id)
    )).scalar_one_or_none()
    level = (profile.experience_level if profile else None) or "beginner"
    cap_pct = WEEKLY_GROWTH_CAP_PCT.get(level.strip().lower(), _DEFAULT_CAP_PCT)

    target_e1rm = _goal_target_e1rm(goal)

    until = goal.deadline + timedelta(days=_HORIZON_TAIL_DAYS)
    sessions = await repository.future_sessions(
        session, app_user_id, goal.exercise_id, today, until
    )
    # Ритм для достройки горизонта за календарь (спека §5.3, §7,
    # simulate.project_sessions): длина микроцикла активного блока. Нет
    # активного блока — нет ритма, simulate.run() ниже просто не достраивает
    # (microcycle_length=None), горизонт остаётся materialized как раньше.
    microcycle_length = (await session.execute(
        select(TrainingBlock.microcycle_length).where(
            TrainingBlock.app_user_id == app_user_id,
            TrainingBlock.status == "active",
        )
    )).scalar_one_or_none()
    lift_sessions, success_rate = await repository.lift_stats(
        session, app_user_id, goal.exercise_id
    )
    factor = simulate.calibration_factor(
        await repository.adherence_ratios(session, app_user_id),
        lift_sessions,
        success_rate,
    )
    # ФИКС I4 (финальное ревью P0-12): фактический тренд лифта — из истории,
    # а не из симулированного планового темпа (см. докстринг repository.
    # historical_trend_slope про то, почему это была мёртвая ветка).
    trend_slope = await repository.historical_trend_slope(
        session, app_user_id, goal.exercise_id
    )

    # КРИТИЧЕСКАЯ ПОПРАВКА К БРИФУ (см. поправки постановщика Задачи 6):
    # simulate.run принимает SchemeContext ПЕРВЫМ аргументом, а не голый
    # start_e1rm — прокрутка идёт через настоящий движок прогрессии
    # (progression.engine.plan_exercise), а не по арифметике "шаг × частота".
    ctx = await repository.scheme_context(session, app_user_id, goal.exercise_id, profile)
    if ctx is None:
        return None

    result = simulate.run(
        ctx,
        target_e1rm=target_e1rm,
        sessions=sessions,
        cap_pct=cap_pct,
        factor=factor,
        microcycle_length=microcycle_length,
        until=until,
    )

    weeks_left = max((goal.deadline - today).days / 7.0, 1e-9)
    rates = Rates(
        required=max((target_e1rm - current) / weeks_left, 0.0),
        plan=result.plan_slope,
        ceiling=current * cap_pct,
    )
    eta = result.calibrated_date or result.nominal_date

    return {
        "current_e1rm": current,
        "target_e1rm": target_e1rm,
        "simulation": result,
        "rates": rates,
        "eta": eta,
        "lift_in_plan": bool(sessions),
        "inputs_hash": _inputs_hash(profile, goal),
        "level": level,
        "trend_slope": trend_slope,
        # ФИКС C1: ctx/sessions/cap_pct/factor/горизонт — то же самое, чем
        # только что воспользовался baseline-прогон run() выше, — наружу для
        # refresh_goal_proposals: ей нужно ЭТО ЖЕ (ctx, sessions), чтобы
        # собрать simulate_with и пересимулировать план С рычагом, не тратя
        # второй раунд запросов к БД на то, что уже загружено здесь.
        "ctx": ctx,
        "sessions": sessions,
        "cap_pct": cap_pct,
        "factor": factor,
        "horizon_start": today + timedelta(days=1),
        "horizon_until": until,
        # Контекст рычагов: параметры упражнения (схема, тяжесть базы) для
        # применимости LEVER_SCHEME — из БД через repository.
        "exercise": await repository.exercise_context(
            session, app_user_id, goal.exercise_id
        ),
    }


UNAVAILABLE_NOT_STRENGTH = "Эта цель вне контура плана: её ведёт питание, а не тренировки"
UNAVAILABLE_NO_DEADLINE = "У цели нет срока — успевать не к чему"
UNAVAILABLE_NO_HISTORY = "Нужно несколько тренировок с этим упражнением, чтобы построить прогноз"
UNAVAILABLE_NO_BLOCK = "Автопилоту нужен план: разверните блок"


def _touched_dates(snapshot: dict) -> list[date]:
    """Даты дней снимка, которые перегенерация РЕАЛЬНО тронула (touched=True).

    Общая точка правды для undo_goal_decision (сами ворота отката) и
    build_context (can_undo экрана, Задача 11) — оба обязаны видеть одно и
    то же множество дней, иначе экран разойдётся с тем, что реально
    разрешает POST /goals/proposals/{id}/decision (undo_goal). Дни, которые
    регенерация пощадила (уже несли факт или принятую правку объёма —
    touched=False, см. докстринг _apply_structural), не входят: факт,
    появившийся на них позже, не имеет отношения к тому, что применил
    автопилот, и не должен блокировать откат.
    """
    return [
        date.fromisoformat(d["target_date"])
        for d in (snapshot.get("days") or [])
        if d.get("touched")
    ]


async def _blocked_by_calendar_fact(
    session: AsyncSession, app_user_id: int, dates: list[date]
) -> bool:
    """Есть ли среди дат уже факт, который откат переписал бы.

    Общая проверка для undo_goal_decision и can_undo экрана автопилота (см.
    докстринг _touched_dates) — то же самое условие, тем же запросом.
    """
    if not dates:
        return False
    blocked = (await session.execute(
        select(UserCalendarDay.id).where(
            UserCalendarDay.app_user_id == app_user_id,
            UserCalendarDay.target_date.in_(dates),
            sa_or(
                UserCalendarDay.status != "planned",
                UserCalendarDay.actual_workout_session_id.isnot(None),
            ),
        )
    )).scalars().first()
    return blocked is not None


async def _undo_blocked_reason(
    session: AsyncSession, app_user_id: int, proposal: PeriodizationProposal
) -> Optional[str]:
    """Причина, по которой /undo этого предложения сейчас откажет, либо None.

    Использует ТУ ЖЕ логику отбора дат (_touched_dates), что и реальные
    ворота undo_goal_decision, — иначе can_undo экрана может разойтись с
    тем, что действительно разрешает откат (см. её докстринг, ревью
    Задачи 10, Critical 2: гейт по ВСЕМ дням снимка блокировал бы откат там,
    где сервер его разрешает).

    ИНВАРИАНТ (ревью Задачи 11, Minor — эти два места обязаны сходиться в
    трактовке пустого снимка): undo_goal_decision (см. её первую проверку
    ниже по файлу) отвечает явным conflict «нечего отменять», если
    applied_snapshot отсутствует или пуст, — это НЕ «ничего не блокирует».
    Раньше эта функция трактовала то же самое состояние как can_undo=True,
    и экран пообещал бы отмену, которую сервер тут же завернул бы. Сегодня
    это недостижимо (диспетчер понижает принятое предложение до declined,
    если ничего не применилось, — applied_snapshot пишется только когда
    что-то реально применилось), но раз обе функции читают одно и то же
    поле, они не имеют права трактовать его состояния по-разному.
    """
    snapshot = (proposal.payload or {}).get("applied_snapshot")
    if not snapshot:
        return "Нечего отменять: предложение не применялось"
    dates = _touched_dates(snapshot)
    blocked = await _blocked_by_calendar_fact(session, app_user_id, dates)
    return (
        "По изменённым дням уже есть выполненная тренировка"
        if blocked else None
    )


_HISTORY_WEEKS_BACK = 16  # ~4 месяца недельных точек факта, не вся история


def _history_weekly_points(
    history_points: list[tuple[date, float]], today: date,
) -> list[tuple[date, float]]:
    """Факт e1RM по календарным неделям ВКЛЮЧАЯ сегодня (P0-12, Задача
    19/20-фикс, §6.2): столько недель назад, сколько задаёт
    `_HISTORY_WEEKS_BACK` — несколько месяцев, а не вся история тренировок.

    Веха-факт живёт в неделе [week_start, week_start+7), последнее значение
    недели, если тренировок несколько, — та же честность, что раньше несла
    _milestones_with_fact: неделя без факта просто отсутствует в результате
    (дырка в точках, а не точка на нуле). Недельная сетка привязана не к
    `today`, а к `today + 1 день` — тренировка, завершённая СЕГОДНЯ, тоже
    факт и обязана попасть в последнюю неделю, а не потеряться на границе;
    сама граница (today+1) при этом не пропускает ничего датированного
    будущим — history_points по построению (e1rm_history_points читает
    только завершённые сессии) в будущем и не бывает, это лишь защита
    формулы, а не ожидаемый в проде случай. Вехи симуляции по-прежнему
    начинаются не раньше today+1 (см. repository.future_sessions: `target_
    date > today`), так что общей недели у двух половин оси нет (history_
    points отсортированы по возрастанию даты вызывающим кодом).
    """
    boundary = today + timedelta(days=1)
    result: list[tuple[date, float]] = []
    for i in range(_HISTORY_WEEKS_BACK, 0, -1):
        week_start = boundary - timedelta(days=7 * i)
        week_end = week_start + timedelta(days=7)
        in_week = [v for d, v in history_points if week_start <= d < week_end]
        if in_week:
            result.append((week_start, in_week[-1]))
    return result


def _timeline_milestones(
    milestones: list, history_points: list[tuple[date, float]], today: date,
) -> list[dict]:
    """Вехи симуляции против факта на одной недельной оси (P0-12, Задача
    19/20-фикс, §6.2).

    Раньше вехи (`milestones`, из simulate.run) начинались только с первой
    БУДУЩЕЙ сессии и шли вперёд — факт по определению существует только в
    прошлом, и потому в единственную неделю симуляции ни разу не попадал:
    actual_e1rm был вечным None на живом эндпоинте (см. отчёт Задачи 19).
    Чинится не подгонкой сравнения, а вторым источником точек: прошлое —
    из истории лифта (_history_weekly_points, та же history_points, что уже
    читает historical_trend_slope — второй, расходящийся запрос не заводим),
    будущее — из симуляции, как и раньше. Швом служит `today`: история
    строго до него, вехи симуляции строго от первой будущей сессии (то есть
    от today+1 или позже, см. simulate.run/repository.future_sessions) — у
    двух половин нет общей недели, вторая линия не задваивается.

    Оба поля теперь опциональны и заполняются РОВНО одной из половин:
    у точки факта нет expected_e1rm (симуляция для прошлого не считается),
    у вехи плана нет actual_e1rm (факта будущего не существует по
    определению — поле, которое не может быть заполнено НИКОГДА, здесь не
    пишется вовсе, а не остаётся вечным null, см. отчёт задачи).
    """
    past = [
        {"week_start": w.isoformat(), "actual_e1rm": v}
        for w, v in _history_weekly_points(history_points, today)
    ]
    future = [
        {"week_start": m.week_start.isoformat(), "expected_e1rm": m.expected_e1rm}
        for m in milestones
    ]
    return past + future


async def build_context(
    session: AsyncSession, app_user_id: int, goal: UserGoal, today: date
) -> dict:
    """Тело GET /goals/{id}/autopilot: обе даты ETA, темпы, вехи, план,
    активное предложение и состояние отмены последнего применённого.

    Молчание всегда с причиной (см. UNAVAILABLE_* выше) — экран не имеет
    права показать пустой автопилот без объяснения, почему он выключен.
    """
    empty = {
        "available": False, "unavailable_reason": None,
        "eta": {}, "rates": {}, "milestones": [], "plan_ahead": {},
        "proposal": None, "last_applied": None,
    }

    if goal.goal_type != "strength":
        return {**empty, "unavailable_reason": UNAVAILABLE_NOT_STRENGTH}
    if goal.deadline is None:
        return {**empty, "unavailable_reason": UNAVAILABLE_NO_DEADLINE}

    state = await evaluate(session, app_user_id, goal, today)
    if state is None:
        return {**empty, "unavailable_reason": UNAVAILABLE_NO_HISTORY}

    block = (await session.execute(
        select(TrainingBlock).where(
            TrainingBlock.app_user_id == app_user_id,
            TrainingBlock.status == "active",
        )
    )).scalar_one_or_none()
    if block is None:
        return {**empty, "unavailable_reason": UNAVAILABLE_NO_BLOCK}

    sim = state["simulation"]
    sessions = await repository.future_sessions(
        session, app_user_id, goal.exercise_id, today,
        goal.deadline + timedelta(days=_HORIZON_TAIL_DAYS),
    )
    # ИСПРАВЛЕНО (ревью Задачи 11, Critical 1 — предложение чужой цели
    # утекало на этот экран): без фильтра по payload["goal_id"] обе выборки
    # ниже брали САМОЕ СВЕЖЕЕ goal_plan-предложение ПОЛЬЗОВАТЕЛЯ, а не этой
    # цели, — при двух целях одного типа второй экран показывал предложение
    # (и, того хуже, last_applied/proposal_id) первой. Тот же второй ключ,
    # что уже применяется в refresh_goal_proposals/_retire_finished_primary_
    # goal — payload несёт goal_id именно чтобы это различать.
    pending = (await session.execute(
        select(PeriodizationProposal).where(
            PeriodizationProposal.app_user_id == app_user_id,
            PeriodizationProposal.kind == periodization_params.KIND_GOAL_PLAN,
            PeriodizationProposal.status == periodization_params.STATUS_PENDING,
            PeriodizationProposal.payload["goal_id"].astext == str(goal.id),
        ).order_by(PeriodizationProposal.created_at.desc())
    )).scalars().first()
    applied = (await session.execute(
        select(PeriodizationProposal).where(
            PeriodizationProposal.app_user_id == app_user_id,
            PeriodizationProposal.kind == periodization_params.KIND_GOAL_PLAN,
            PeriodizationProposal.status == periodization_params.STATUS_ACCEPTED,
            PeriodizationProposal.payload["goal_id"].astext == str(goal.id),
        ).order_by(PeriodizationProposal.decided_at.desc())
    )).scalars().first()

    # Веха vs факт (P0-12, Задача 19, §6.2): та же история, что уже
    # прочитана для тренда (repository.historical_trend_slope) — второй,
    # расходящийся запрос за фактом лифта не заводим.
    history_points = sorted(
        await repository.e1rm_history_points(session, app_user_id, goal.exercise_id)
    )

    last_applied = None
    if applied is not None:
        blocked_reason = await _undo_blocked_reason(session, app_user_id, applied)
        last_applied = {
            "proposal_id": applied.id,
            "applied_at": applied.decided_at.isoformat() if applied.decided_at else None,
            "can_undo": blocked_reason is None,
            "undo_blocked_reason": blocked_reason,
        }

    return {
        "available": True,
        "unavailable_reason": None,
        "eta": {
            "nominal": sim.nominal_date.isoformat() if sim.nominal_date else None,
            "calibrated": sim.calibrated_date.isoformat() if sim.calibrated_date else None,
            "factor": sim.factor,
            "horizon": sim.horizon,
            "calibration_available": sim.calibration_available,
        },
        "rates": {
            "required": round(state["rates"].required, 3),
            "plan": round(state["rates"].plan, 3),
            "ceiling": round(state["rates"].ceiling, 3),
        },
        "milestones": _timeline_milestones(sim.milestones, history_points, today),
        "plan_ahead": {
            "target_lift_sessions": len(sessions),
            "sets_per_window": sum(s.prescription_sets for s in sessions[:4]),
            "effort": sessions[0].phase_effort_tier if sessions else None,
            "next_session_date": sessions[0].date.isoformat() if sessions else None,
        },
        "proposal": (
            {"id": pending.id, "kind": pending.kind,
             "reason_code": pending.reason_code, "payload": pending.payload}
            if pending else None
        ),
        "last_applied": last_applied,
    }


def _lever_session_counts(
    levers: list, state: dict, block: TrainingBlock, exercise_id: int,
) -> tuple[int, int]:
    """Сколько сессий целевого лифта в горизонте — сейчас и после ВСЕХ
    рычагов лестницы (P0-12, Задача 18: превью структурного рычага, §6.2).

    «После» получаем ТОЙ ЖЕ функцией simulate.apply_lever, которой
    simulate_with (см. ниже) уже посчитал эффект каждого рычага на темп —
    не выводим число вторым, независимым способом (тот же принцип, каким
    simulate_with уже пользуется для темпа). Раз simulate_with уже прогонял
    apply_lever кумулятивно поверх applied+candidate на каждом шаге
    лестницы, применение здесь того же списка levers с нуля даёт то самое
    состояние сессий, на котором decide() остановился, приняв последний
    рычаг.
    """
    before = len(state["sessions"])
    cur_sessions, cur_ctx = state["sessions"], state["ctx"]
    for lever in levers:
        cur_sessions, cur_ctx = simulate.apply_lever(
            lever.kind, lever.detail, cur_sessions, cur_ctx,
            exercise_id=exercise_id,
            microcycle_length=block.microcycle_length,
            start=state["horizon_start"], until=state["horizon_until"],
        )
    return before, len(cur_sessions)


async def refresh_goal_proposals(
    session: AsyncSession, app_user_id: int, today: date
) -> Optional[PeriodizationProposal]:
    """Создать предложение автопилота, если есть что сказать.

    Первым делом проверяет, не завершилась ли ведущая цель (достигнута или
    просрочена) — тогда снимает is_primary и гасит активное предложение,
    новое не создаётся вовсе (см. _retire_finished_primary_goal).

    ДИСЦИПЛИНА КОММИТА (финальное ревью, Important 9): эта функция коммитит
    сессию сама на каждом пути, где что-то записала, — В ОТЛИЧИЕ от
    volume.service.refresh_volume_proposals, которая только flush()-ит и
    полагается на коммит вызывающей стороны. Это осознанное расхождение, а
    не забытая копия чужой дисциплины:

      - у ДВУХ ИЗ ТРЁХ точек вызова (api/routers/goals.py,
        api/routers/profile.py) вызывающая сторона коммитит СВОЮ часть
        работы ДО guarded(..., refresh_goal_proposals(...)) и ни разу не
        коммитит ПОСЛЕ — без собственного commit() здесь всё, что эта
        функция запишет (новое предложение, is_primary, истечение старых
        строк), осталось бы только в памяти сессии и исчезло бы при её
        закрытии;
      - третья точка (api/routers/workout_center.py, build_context) устроена
        похоже, но не тождественно: сам build_context не коммитит ни разу ни
        до, ни после (единственный коммит внутри него — этот, из
        refresh_goal_proposals). Но обработчик GET /workout-center/context,
        который зовёт build_context, ДО него делает свой собственный
        commit() — после ensure_structure (см. докстринг обработчика,
        ревью Задачи 8, P1-03 ч.1); это отдельная, более ранняя транзакция,
        закрытая до того, как начинается цепочка mark_missed_days ->
        refresh_volume_proposals -> refresh_goal_proposals внутри
        build_context. Для дисциплины build_context это ничего не меняет:
        эта функция по-прежнему ВЫЗЫВАЕТСЯ ПОСЛЕДНЕЙ в цепочке
        guarded()-обёрнутых лениво-материализующихся шагов, и её
        собственный commit() ЯВНО и ОСОЗНАННО делает шаги ЭТОЙ цепочки
        долговечными — ровно тот же приём, что и в
        periodization.service.refresh_proposals (`if created: await
        session.commit()`), которая тоже коммитит себя сама для тех же
        голых GET-путей. Это не побочный эффект, который надо скрывать, а
        необходимость: без единого коммита в конце цепочки ни пропуски дней,
        ни обзор объёма, ни автопилот цели не пережили бы закрытие сессии на
        обычном чтении контекста.

    guarded() (api/services/volume/repository.py) готов к этому по
    конструкции: он открывает session.begin_nested() БЕЗ `async with` именно
    потому, что контекст-менеджерная форма падает, если обёрнутая работа
    закоммитила сессию сама (см. её докстринг) — ручная форма проверяет
    nested.is_active перед commit()/rollback() и корректно не пытается
    закоммитить/откатить уже неактивный SAVEPOINT. Менять здесь нечего:
    savepoint-дисциплина guarded() уже безопасна для самокоммитящей работы.

    Каждый вызывающий несёт короткий комментарий на месте guarded(...) с
    отсылкой сюда — см. api/routers/goals.py, api/routers/profile.py,
    api/routers/workout_center.py.
    """
    goal = await _primary_goal(session, app_user_id)
    if goal is None:
        return None

    if await _retire_finished_primary_goal(session, app_user_id, goal, today):
        return None

    state = await evaluate(session, app_user_id, goal, today)
    if state is None:
        return None

    block = (await session.execute(
        select(TrainingBlock).where(
            TrainingBlock.app_user_id == app_user_id,
            TrainingBlock.status == "active",
        )
    )).scalar_one_or_none()
    if block is None:
        return None  # автопилоту нечего править: блока нет

    microcycles_left = max(
        int((goal.deadline - today).days // max(block.microcycle_length, 1)), 0
    )

    # ФИКС C1 (финальное ревью P0-12): simulate_with — единственный источник
    # эффекта рычага для decide(). `applied` — уже ПРИНЯТЫЕ decide() рычаги
    # ЭТОГО же прохода лестницы (пустой кортеж на первом кандидате); каждый
    # вызов пересобирает план С НУЛЯ, применяя applied по порядку и кандидат
    # (kind, detail) поверх них, — состояние не копится в замыкании между
    # вызовами, поэтому один и тот же набор аргументов всегда даёт один и
    # тот же ответ (decide() полагается на это как на чистую функцию).
    # cap_pct/factor берутся ИЗ baseline-прогона evaluate() — потолок роста
    # и калибровка исполнения остаются в силе для КАЖДОЙ пробной симуляции,
    # ни один рычаг не может обещать темп быстрее биологического потолка.
    def simulate_with(applied, kind, detail):
        cur_sessions, cur_ctx = state["sessions"], state["ctx"]
        for lever in applied:
            cur_sessions, cur_ctx = simulate.apply_lever(
                lever.kind, lever.detail, cur_sessions, cur_ctx,
                exercise_id=goal.exercise_id,
                microcycle_length=block.microcycle_length,
                start=state["horizon_start"], until=state["horizon_until"],
            )
        cur_sessions, cur_ctx = simulate.apply_lever(
            kind, detail, cur_sessions, cur_ctx,
            exercise_id=goal.exercise_id,
            microcycle_length=block.microcycle_length,
            start=state["horizon_start"], until=state["horizon_until"],
        )
        probe = simulate.run(
            cur_ctx, target_e1rm=state["target_e1rm"], sessions=cur_sessions,
            cap_pct=state["cap_pct"], factor=state["factor"],
        )
        return probe.plan_slope, (probe.calibrated_date or probe.nominal_date)

    levers, reason = decide.decide(DecisionInput(
        rates=state["rates"],
        deadline=goal.deadline,
        eta=state["eta"],
        lift_in_plan=state["lift_in_plan"],
        trend_slope=state["trend_slope"],
        microcycles_left=microcycles_left,
        scheme=state["exercise"]["scheme"],
        is_heavy_compound=state["exercise"]["is_heavy_compound"],
    ), simulate_with)
    if not levers and reason not in params.LEVERLESS_PROPOSAL_REASONS:
        # ИСПРАВЛЕНО (ревью Задачи 6, Important 2): это УДАВШАЯСЯ переоценка
        # (evaluate() что-то посчитал, активный блок нашёлся) с содержательным
        # выводом «сейчас предлагать нечего» — пользователь мог нагнать темп
        # (reason == "") или тренд уйти в минус (REASON_TREND_DOWN, спека
        # отдаёт его P0-07/P0-08, автопилот здесь молчит сознательно, см.
        # params.LEVERLESS_PROPOSAL_REASONS). Прежнее pending-предложение по
        # ЭТОЙ цели после такого вывода несёт устаревшие цифры и совет,
        # который уже неверен — гасим его здесь же.
        # ФИКС (Задача 17): REASON_ABOVE_CEILING/REASON_NO_LEVER_LEFT сюда
        # больше не попадают — у них тоже нет рычагов, но есть честный совет
        # «сдвиньте срок» (§5.4, решение 13), и они проваливаются дальше по
        # функции, к тому же дедупу/созданию, что и предложения с рычагами.
        # Важна граница: этот блок недостижим, если evaluate() вернул None или
        # активного блока нет (оба return выше) — временная невозможность
        # посчитать НЕ повод гасить предложение, которое пользователь как раз
        # мог собираться применить.
        await session.execute(
            sa_update(PeriodizationProposal)
            .where(
                PeriodizationProposal.app_user_id == app_user_id,
                PeriodizationProposal.kind == periodization_params.KIND_GOAL_PLAN,
                PeriodizationProposal.status == periodization_params.STATUS_PENDING,
                PeriodizationProposal.payload["goal_id"].astext == str(goal.id),
            )
            .values(status=periodization_params.STATUS_EXPIRED)
        )
        await session.commit()
        return None

    existing = (await session.execute(
        select(PeriodizationProposal).where(
            PeriodizationProposal.app_user_id == app_user_id,
            PeriodizationProposal.kind == periodization_params.KIND_GOAL_PLAN,
            PeriodizationProposal.status == periodization_params.STATUS_PENDING,
        )
    )).scalars().all()

    # ОТКЛОНЕНИЕ ОТ БРИФА (обоснование в отчёте Задачи 6): постановщик
    # требует "двухключевой" дедуп по образцу volume.service, но volume'овский
    # второй ключ (payload["window_id"]) не имеет аналога в домене цели — тут
    # нет окон. Естественный второй ключ здесь — goal_id: без него совпадение
    # inputs_hash у пользователя, пересоздавшего цель с теми же параметрами
    # (другой goal_id, тот же target/deadline), вернуло бы СТАРОЕ предложение
    # с payload["goal_id"] от уже удалённой цели — Задачи 8-10 применяют и
    # откатывают рычаги именно по этому полю.
    # ИСПРАВЛЕНО (финальное ревью, Deferred Minor 6): истечение прогоняется
    # ПОЛНЫМ отдельным проходом по ВСЕМ строкам ДО решения о совпадении, а не
    # одним смешанным циклом с ранним `return`. У прежней версии было два
    # независимых дефекта одной природы:
    #   (а) строка, помеченная expired ДО того, как цикл наткнулся на
    #       совпадение (порядок scalars().all() ничем не гарантирован —
    #       порядок строк из БД не обязан совпадать с порядком вставки),
    #       никогда не коммитилась: `return row` обрывал функцию раньше
    #       ЕДИНСТВЕННОГО commit(), который был ниже по телу (см. финальный
    #       commit() у создания нового предложения) — при закрытии сессии
    #       чужой (по другой цели) pending-предложение так и оставалось
    #       pending навсегда, ссылаясь на уже неактуальный план;
    #   (b) строки ПОСЛЕ совпадения в этом же порядке вообще не посещались
    #       телом цикла — early return останавливал итерацию целиком, а не
    #       только пропускал коммит.
    # Оба чинятся одним и тем же изменением: сначала просмотреть ВСЕ строки
    # и разметить чужие/устаревшие, потом отдельно решить, есть ли совпадение.
    match: Optional[PeriodizationProposal] = None
    for row in existing:
        payload = row.payload or {}
        if payload.get("goal_id") == goal.id and payload.get("inputs_hash") == state["inputs_hash"]:
            match = row
        else:
            # Другая цель или изменившиеся условия: прежнее предложение больше не про этот план.
            row.status = periodization_params.STATUS_EXPIRED
    if match is not None:
        # Коммитим ЗДЕСЬ же (см. докстринг функции про её commit-дисциплину,
        # Important 9) — без этого истечение строк выше осталось бы только в
        # памяти сессии: ни один из вызывающих не коммитит после guarded(...).
        await session.commit()
        return match  # те же условия той же цели — то же предложение, второго не надо

    sim = state["simulation"]
    # ФИКС (Задача 17, §5.4 решение 13): рычагов нет (above_ceiling/
    # no_lever_left) -> единственное честное предложение — сдвинуть срок.
    # Дата берётся ТОЙ ЖЕ функцией пересечения, что и nominal_date/
    # calibrated_date внутри этой же симуляции (simulate.run) — второй,
    # независимо посчитанной даты не заводим. Темп — потолок из ЭТОЙ ЖЕ
    # rates (state["rates"].ceiling = current_e1rm * cap_pct), а не заново
    # выведенное число.
    suggested_deadline = (
        simulate.crossing_date_at_rate(
            today, state["current_e1rm"], state["target_e1rm"], state["rates"].ceiling,
        )
        if not levers else None
    )

    # Превью структурного рычага (P0-12, Задача 18, §6.2): GeneratorComparisonSheet
    # экрана-генератора здесь не годится — построить настоящий GenerationComparison
    # значит прогнать SchedulingEngine.generate_block_days, а она пишет в БД и
    # коммитит (см. её докстринг) ради предпросмотра предложения, которое
    # пользователь ещё не принял. Вместо dry-run — то, что система и так уже
    # знает, не трогая календарь:
    #   affected_dates/protected_dates — тот же предикат, каким живая
    #     _wipe_future_calendar удаляет дни (repository.structural_calendar_preview),
    #     посчитанный превентивно, ДО применения;
    #   sessions_before/sessions_after — сколько сессий целевого лифта несёт
    #     горизонт сейчас и после лестницы; sessions_after не считается заново
    #     отдельной формулой, а взят из той же apply_lever-прокрутки, которой
    #     simulate_with уже пользовался, чтобы получить эффект каждого рычага
    #     (_lever_session_counts выше).
    # Заполняется только когда лестница реально дошла до структурной ступени
    # (LEVER_LIFT_FREQUENCY/LEVER_STRUCTURAL, params.STRUCTURAL_LEVERS) — на
    # ensure_present/scheme календарь не трогается, превью нечего показывать.
    structural = None
    if any(lever.kind in params.STRUCTURAL_LEVERS for lever in levers):
        affected_dates, protected_dates = await repository.structural_calendar_preview(
            session, app_user_id, block.id, today,
        )
        sessions_before, sessions_after = _lever_session_counts(
            levers, state, block, goal.exercise_id,
        )
        structural = {
            "affected_dates": affected_dates,
            "protected_dates": protected_dates,
            "sessions_before": sessions_before,
            "sessions_after": sessions_after,
        }

    proposal = PeriodizationProposal(
        app_user_id=app_user_id,
        block_id=block.id,
        kind=periodization_params.KIND_GOAL_PLAN,
        reason_code=reason,
        payload={
            "goal_id": goal.id,
            # Задачи 8–10 применяют и откатывают рычаги по этому полю:
            # цель к моменту решения может быть уже удалена, а откат обязан
            # знать, к какому упражнению относились правки.
            "exercise_id": goal.exercise_id,
            "deadline": goal.deadline.isoformat(),
            "target_e1rm": round(state["target_e1rm"], 1),
            "inputs_hash": state["inputs_hash"],
            # Предложение сдвинуть срок (Задача 17): правка ЦЕЛИ, а не
            # плана — применяет её пользователь через PATCH /goals своей
            # рукой (спека, решение 13), не decision-эндпоинт предложений.
            "suggested_deadline": (
                suggested_deadline.isoformat() if suggested_deadline else None
            ),
            "eta": {
                "nominal": sim.nominal_date.isoformat() if sim.nominal_date else None,
                "calibrated": sim.calibrated_date.isoformat() if sim.calibrated_date else None,
                "factor": sim.factor,
                "horizon": sim.horizon,
                "calibration_available": sim.calibration_available,
            },
            "rates": {
                "required": round(state["rates"].required, 3),
                "plan": round(state["rates"].plan, 3),
                "ceiling": round(state["rates"].ceiling, 3),
            },
            "levers": [
                {
                    "index": l.index, "kind": l.kind, "reason_code": l.reason_code,
                    "effect_slope": l.effect_slope, "effect_days": l.effect_days,
                    "detail": l.detail,
                }
                for l in levers
            ],
            "structural": structural,
            "applied_snapshot": None,
        },
        status=periodization_params.STATUS_PENDING,
    )
    session.add(proposal)
    await session.commit()
    await session.refresh(proposal)
    return proposal


async def apply_goal_decision(
    session: AsyncSession,
    app_user_id: int,
    proposal: PeriodizationProposal,
    action: str,
    options: dict,
) -> dict:
    """Применить отмеченные пользователем рычаги.

    Контракт совпадает с volume.apply_volume_decision: options["accepted"] —
    список индексов рычагов, пустой означает «ничего не применять». Экран не
    блокирующий, и бездействие равно «идём по плану».

    Нетипизированный accepted (тело HTTP-запроса приходит как есть)
    трактуем как пустой — то же осознанное «ничего», а не 500.

    Порядок применения — порядок принятых рычагов как есть (совпадает с
    порядком индексов лестницы decide.py: ensure_present -> scheme ->
    lift_frequency -> structural). УДАЛЕНИЕ (P0-12, обрезка лестницы):
    раньше здесь был отдельный порядок ФАЗ (структурный рычаг — первым,
    sets — последним), потому что sets писала в UserCalendarDay.
    volume_adjustments и могла столкнуться со структурной перегенерацией
    того же дня. Носителей ensure_present/scheme (UserExercisePreference,
    profile.settings) календарный день не касается вовсе, так что порядок
    между всеми оставшимися рычагами теперь не имеет значения. `applied` в
    ответе — всегда список индексов ПО ВОЗРАСТАНИЮ, независимо от порядка
    фактического исполнения.

    НЕ КОММИТИТ СЕССИЮ (ревью Задачи 8, Critical 1) — по тому же контракту,
    что и volume.apply_volume_decision: только flush(). Записи рычагов и
    решение по предложению (proposal.status/decided_action/client_uuid/
    decided_at) обязаны попасть в ОДИН commit() вызывающей стороны
    (periodization.service.apply_decision, Задача 10) — см. подробный разбор
    в её докстринге у Critical 3: коммит здесь расщепил бы транзакцию на
    два, и обрыв процесса между ними оставил бы рычаги применёнными, а
    предложение — всё ещё pending; повтор из офлайн-очереди мобильного
    клиента прошёл бы проверку status == pending заново и применил бы
    рычаги ВТОРОЙ раз (см. идемпотентность _apply_structural по маркеру
    structural_applied ниже — это подстраховка на случай именно такого
    повтора, а не замена атомарности).

    skipped_days — счётчик дней, которые структурный рычаг НЕ тронул, хотя
    формально мог бы: дни, которые _wipe_future_calendar пощадила (факт или
    принятая правка объёма, см. докстринг _apply_structural). Мягкие рычаги
    (ensure_present/scheme) дней не касаются вовсе и в этот счётчик не
    попадают.
    """
    raw = options.get("accepted")
    accepted = set(raw) if isinstance(raw, list) else set()
    payload = dict(proposal.payload or {})
    levers = payload.get("levers") or []
    exercise_id = payload.get("exercise_id")

    # ИСПРАВЛЕНО (ревью Задачи 8, Important): снимок обязан фиксировать
    # состояние ДО того, как это предложение хоть раз что-то применило, и
    # не должен переписываться повторно. Повтор apply_goal_decision на ещё
    # pending предложении (офлайн-очередь мобильного клиента, двойной тап)
    # раньше строил snapshot заново на каждый вызов — вторая попытка читала
    # текущее состояние носителя, которое уже несло правку ПЕРВОЙ попытки,
    # и записывала её как «то, что было до» (ревью воспроизвело буквально:
    # после первого вызова snapshot держал {'preference': None}, после
    # повтора — {'preference': 'favorite'}; последующий откат Задачи 10
    # тогда навсегда оставлял лифт избранным, как будто это выбрал сам
    # пользователь). Чиним начиная не с пустого «с нуля» на каждый вызов, а
    # с уже сохранённого снимка, если он есть.
    #
    # Пустой dict, а не заполненный None-ами: None — валидное записанное
    # значение («носителя не было»), и хелперам ниже нужно различать «ключ
    # ещё не записан» от «ключ записан как None» — на заполненном словаре
    # оба случая неотличимы, и проверка «уже записано?» ничего не защищает.
    #
    # Это ровно то же двухуровневое правило, на которое опирается
    # undo_goal_decision (см. её докстринг): ПРИСУТСТВИЕ ключа в снимке —
    # единственный источник истины о том, писало ли ЭТО предложение данный
    # носитель (preference/scheme), а само записанное значение
    # (включая None) — это то, что было ДО записи, по которому откат либо
    # удаляет носителя (None), либо восстанавливает прежнее значение.
    existing_snapshot = payload.get("applied_snapshot")
    snapshot: dict = dict(existing_snapshot) if isinstance(existing_snapshot, dict) else {}
    snapshot.setdefault("days", [])
    snapshot.setdefault("created_plan_ids", [])
    applied: list[int] = []
    skipped_days = 0

    accepted_levers = [lever for lever in levers if lever.get("index") in accepted]

    for lever in accepted_levers:
        kind = lever["kind"]
        if kind == params.LEVER_ENSURE_PRESENT:
            if await _apply_favorite(session, app_user_id, exercise_id, snapshot):
                applied.append(lever["index"])
        elif kind == params.LEVER_SCHEME:
            if await _apply_scheme(session, app_user_id, exercise_id, lever, snapshot):
                applied.append(lever["index"])
        elif kind in params.STRUCTURAL_LEVERS:
            done, skipped = await _apply_structural(
                session, app_user_id, proposal, snapshot
            )
            skipped_days += skipped
            if done:
                applied.append(lever["index"])

    # Список индексов наружу — по возрастанию, а не в порядке accepted_levers
    # (порядок accepted как список пришёл в запросе не гарантирован).
    applied.sort()

    if applied:
        # ИСПРАВЛЕНО (ревью Задачи 9, следствие Critical 1): читаем
        # proposal.payload ЗАНОВО, а не берём `payload`, захваченный в
        # начале функции (строка выше цикла рычагов) — если среди applied
        # был структурный рычаг, _apply_structural уже записал и
        # закоммитил(-flush'ил) applied_snapshot в САМ proposal.payload
        # (см. её докстринг). Запись поверх устаревшего `payload` из начала
        # функции откатила бы это до состояния "как было до цикла" на
        # ключах, которых тот словарь ещё не видел. `snapshot` — тот же
        # объект, что и внутри _apply_structural, поэтому переприсвоение
        # applied_snapshot здесь идемпотентно, а не второй, отдельный факт.
        payload = dict(proposal.payload or {})
        payload["applied_snapshot"] = snapshot
        proposal.payload = payload
        flag_modified(proposal, "payload")
        await session.flush()

    return {
        "status": "applied" if applied else "declined",
        "proposal_id": proposal.id,
        "applied": applied,
        "skipped_days": skipped_days,
    }


async def _apply_favorite(
    session: AsyncSession, app_user_id: int, exercise_id: Optional[int], snapshot: dict
) -> bool:
    """Пометить целевой лифт избранным, чтобы умная замена его не вытесняла."""
    if exercise_id is None:
        return False
    existing = (await session.execute(
        select(UserExercisePreference).where(
            UserExercisePreference.app_user_id == app_user_id,
            UserExercisePreference.exercise_id == exercise_id,
        )
    )).scalar_one_or_none()
    # Пишем ТОЛЬКО если ключ ещё не записан (см. докстринг apply_goal_
    # decision про повтор) — на повторном проходе `existing` уже несёт
    # правку первого вызова, и её нельзя принять за состояние «до».
    if "preference" not in snapshot:
        snapshot["preference"] = existing.preference if existing else None
    if existing is None:
        # exercise_name обязателен в модели (см. api/routers/exercises.py,
        # set_exercise_preference) — тянем его из Exercise; если упражнения
        # уже нет, ставить нечего.
        exercise_name = (await session.execute(
            select(Exercise.name).where(Exercise.id == exercise_id)
        )).scalar_one_or_none()
        if exercise_name is None:
            return False
        session.add(UserExercisePreference(
            app_user_id=app_user_id, exercise_id=exercise_id,
            exercise_name=exercise_name, preference="favorite",
        ))
    else:
        existing.preference = "favorite"
    await session.flush()
    return True


async def _apply_scheme(
    session: AsyncSession, app_user_id: int, exercise_id: Optional[int],
    lever: dict, snapshot: dict,
) -> bool:
    """Схема живёт в settings, а не в кэше состояния: выбор пользователя
    (а теперь и принятое им предложение) пересчётом чиниться не должен."""
    if exercise_id is None:
        return False
    to_scheme = (lever.get("detail") or {}).get("to_scheme")
    if not to_scheme:
        return False
    profile = (await session.execute(
        select(AppUserProfile).where(AppUserProfile.app_user_id == app_user_id)
    )).scalar_one_or_none()
    if profile is None:
        return False
    settings = dict(profile.settings or {})
    progression = dict(settings.get("progression") or {})
    overrides = dict(progression.get("overrides") or {})
    # Пишем ТОЛЬКО если ключ ещё не записан — та же причина, что и в
    # _apply_favorite (см. её комментарий и докстринг apply_goal_decision).
    if "scheme" not in snapshot:
        snapshot["scheme"] = overrides.get(str(exercise_id))
    overrides[str(exercise_id)] = to_scheme
    progression["overrides"] = overrides
    settings["progression"] = progression
    profile.settings = settings
    flag_modified(profile, "settings")
    await session.flush()
    return True


async def _apply_structural(
    session: AsyncSession, app_user_id: int, proposal: PeriodizationProposal,
    snapshot: dict,
) -> tuple[bool, int]:
    """Перегенерировать будущие дни блока (LEVER_LIFT_FREQUENCY / LEVER_STRUCTURAL).

    Идём через _wipe_future_calendar/_generate_future_calendar периодизации,
    а не своим DELETE: там уже закрыт долг P0-08 — дни со status != 'planned'
    и дни с принятыми правками объёма не удаляются (см. её докстринг). Второй
    такой обход заводить нельзя: он неизбежно разойдётся с первым, и тогда
    правка автопилота начнёт стирать факт.

    Дисциплина снимка (фикс-проход Задачи 8, см. докстринг apply_goal_
    decision): snapshot["days"]/["created_plan_ids"] уже пришли сюда из
    setdefault выше пустыми СПИСКАМИ, а не отсутствующими ключами — этим они
    отличаются от "preference"/"scheme", где отсутствие ключа
    и есть сигнал "ещё не применялось". Пустой список после setdefault
    неотличим от "структурный рычаг уже применялся, и будущих дней тогда не
    нашлось" — проверка "если список пуст, пишем" защитила бы неправильный
    случай. Поэтому используем отдельный маркер structural_applied: второй
    заход в эту функцию (два структурных рычага в одном accepted, повтор
    apply_goal_decision из офлайн-очереди) не имеет права переписать уже
    сохранённые координаты "прежних" дней данными, которые сам же и создал
    предыдущим запуском.

    created_plan_ids сознательно НЕ заполняется (Задача 9): регенерация не
    создаёт новых WorkoutPlan — SchedulingEngine._score_and_find_best_plan
    выбирает id из уже существующих планов пользователя (см. её тело:
    `plans = list((await session.execute(select(WorkoutPlan)...))`, ни
    одного `session.add(WorkoutPlan(...))` в generate_block_days нет), она
    лишь перепривязывает UserCalendarDay.plan_id к уже существующему
    шаблону. Откату (Задача 10) нечего было бы удалять по этому ключу —
    восстановить "прежний" plan_id для отката можно прямо из
    snapshot["days"][i]["plan_id"], который уже сохранён ниже.

    ИСПРАВЛЕНО (ревью Задачи 9, Critical 1 — снимок мог потеряться
    безвозвратно): эта функция идёт через _generate_future_calendar ->
    SchedulingEngine.generate_block_days, а та КОММИТИТ СЕССИЮ САМА (см. её
    докстринг и последнюю строку тела) — это не в нашей власти и не
    предмет этой задачи. Раньше snapshot оставался только в локальном
    словаре до конца ВСЕГО цикла рычагов в apply_goal_decision, и попадал
    в proposal.payload только её финальным flush() — уже ПОСЛЕ того как
    wipe+регенерация были необратимо закоммичены тем внутренним commit'ом.
    Обрыв процесса в этом окне удалял старый календарь безвозвратно, не
    оставляя от него ни строки в БД, ни снимка — Задаче 10 нечего было бы
    восстанавливать, а status предложения снаружи всё ещё выглядел бы
    pending, никак не сигналя о повреждении.
    Чиним, переставляя запись: снимок (включая маркер structural_applied)
    пишется в proposal.payload и flush()-ится ЗДЕСЬ, ДО вызова
    _wipe_future_calendar/_generate_future_calendar. Поскольку flush() не
    открывает отдельную транзакцию, а лишь готовит УЖЕ ОТКРЫТУЮ, наш снимок
    едет в ТОЙ ЖЕ транзакции, что и сам wipe — когда generate_block_days
    вызовет commit(), закоммитятся оба разом. Если процесс оборвётся ДО
    этого внутреннего commit'а — не закоммитится ничего из этой транзакции
    вовсе (ни снимок, ни удаление), и повтор начнёт с нетронутых дней.
    Если оборвётся ПОСЛЕ — календарь и снимок, описывающий его прежнее
    состояние, окажутся в БД вместе, консистентно.
    Отдельно (СДЕЛАНО ревью Задачи 10, Critical 1 — см. periodization.service.
    apply_decision, ветку KIND_GOAL_PLAN/ACTION_APPLY_GOAL): вызывающий
    проставляет proposal.status/decided_action/client_uuid/decided_at ДО
    apply_goal_decision, тем же порядком, что и _BLOCK_MUTATING_ACTIONS
    там же (её "Critical 3" у вызова _perform) — то же самое обещание "не
    коммитит сессию сама" здесь не держится. Это НЕ то же самое, что риск
    потери snapshot, который эта правка (Critical 2 предыдущего ревью)
    закрывает: снимок переживает обрыв благодаря flush() выше ДО wipe/
    regenerate; proposal.status/decided_action вне payload переживает обрыв
    благодаря тому, что вызывающий пишет их ДО, а не после, этого вызова.
    """
    if snapshot.get("structural_applied"):
        return False, 0

    from api.services.periodization.service import (
        _generate_future_calendar,
        _wipe_future_calendar,
    )

    block = await session.get(TrainingBlock, proposal.block_id)
    if block is None:
        return False, 0

    first_future = date.today() + timedelta(days=1)
    days = (await session.execute(
        select(UserCalendarDay).where(
            UserCalendarDay.app_user_id == app_user_id,
            UserCalendarDay.block_id == block.id,
            UserCalendarDay.target_date >= first_future,
        ).order_by(UserCalendarDay.target_date)
    )).scalars().all()

    # ИСПРАВЛЕНО (ревью Задачи 10, Critical 2 — ворота отката блокировали
    # безопасный откат навсегда): каждая запись снимка ниже несёт булево
    # поле "touched" — было ли это КОНКРЕТНО тот день, который регенерация
    # реально тронула бы, а не просто день, существовавший на момент снимка.
    # Предикат ОБЯЗАН зеркалить условие, по которому _wipe_future_calendar
    # щадит день (см. её докстринг): status == 'planned' и пустой
    # volume_adjustments — тронутый; иначе (уже есть факт или принятая
    # правка объёма) — день, который автопилот и не собирался трогать.
    # undo_goal_decision (см. её докстринг и гейт ниже по файлу) смотрит
    # ТОЛЬКО на даты с touched=True: день, который автопилот не трогал, не
    # имеет права блокировать откат тем, что позже обзавёлся фактом, — этот
    # факт никак не связан с применением рычага.
    touched_flags = [d.status == "planned" and not d.volume_adjustments for d in days]

    # Дыра в диапазоне [first_future, block.planned_end_date], для которой
    # ВООБЩЕ НЕТ строки UserCalendarDay, — SchedulingEngine.generate_block_days
    # заполняет ВЕСЬ этот диапазон и пропускает только даты, для которых
    # строка УЖЕ есть (см. её `existing_dates`), так что дыра означает, что
    # генерация реально создаст новые дни, даже если ни один из УЖЕ
    # существующих (`days` выше) не тронут регенерацией. Именно так устроен
    # test_dispatcher_applies_structural_lever_and_allows_undo: календарь
    # пуст вовсе (`days` пуст, touched_flags == []), и весь эффект рычага —
    # это заполнение пустоты с нуля.
    expected_span = (block.planned_end_date - first_future).days + 1
    has_gap = len(days) < max(expected_span, 0)

    # ИСПРАВЛЕНО (финальное ревью, Critical 3, часть 1 — «применено» без
    # единого применения): раньше функция возвращала True БЕЗУСЛОВНО, даже
    # когда ни один СУЩЕСТВУЮЩИЙ день не тронут (touched_flags целиком False)
    # И дыр в диапазоне нет — то есть у блока либо вообще нет будущих дней в
    # его собственном диапазоне (`expected_span <= 0`, блок уже кончился),
    # либо КАЖДЫЙ день диапазона уже материализован и несёт факт или
    # принятую правку объёма (spare-условие _wipe_future_calendar, см. её
    # докстринг, — то же самое условие, что и здесь). В этом случае
    # _wipe_future_calendar не удалит ни одной строки, а generate_block_days
    # не создаст ни одной новой (дыр нет), так что вызов ниже был бы пустым
    # проходом. Внешний ответ тем не менее утверждал «структурный рычаг
    # применён»: periodization.service.apply_decision держал бы
    # proposal.status == accepted, снимок нёс бы structural_applied=True с
    # пустым/нетронутым "days", can_undo экрана включался бы, а undo_goal_
    # decision молча ничего не восстанавливал бы (спека §5.5, "отмена
    # возвращает только то, что сделал сам" — здесь автопилот не сделал
    # ничего вовсе). Честное «не применилось» — ДО того как что-либо
    # запишется в payload, и без единого обращения к wipe/generate: snapshot
    # остаётся ровно тем, что было до этого вызова.
    if not any(touched_flags) and not has_gap:
        return False, len(touched_flags)

    snapshot["days"] = [
        {
            "target_date": d.target_date.isoformat(),
            "plan_id": d.plan_id,
            "day_tag": d.day_tag,
            "micro_tag": d.micro_tag,
            "meso_tag": d.meso_tag,
            "mesocycle_phase_number": d.mesocycle_phase_number,
            "is_rest_day": d.is_rest_day,
            "touched": touched,
        }
        for d, touched in zip(days, touched_flags)
    ]
    # ИСПРАВЛЕНО (ревью Задачи 15, Critical 1 — undo оставлял «фантомные» дни
    # календаря): snapshot["days"] выше несёт координаты дней, которые СУЩЕСТВОВАЛИ
    # на момент применения. _generate_future_calendar ниже (через
    # SchedulingEngine.generate_block_days) заполняет КАЖДУЮ дату от first_future
    # до block.planned_end_date включительно, вставляя НОВЫЕ строки для дат, у
    # которых прежде вообще не было записи в UserCalendarDay, — эти даты в
    # snapshot["days"] не попадают (их там просто не с чем зафиксировать: строки
    # не было). undo_goal_decision обязана знать границу, откуда веерная
    # регенерация могла что-то придумать с нуля, — фиксируем её здесь же, в
    # той же транзакции, что и сам снимок (см. разбор про flush() ДО wipe/
    # regenerate выше в докстринге).
    snapshot["first_future_date"] = first_future.isoformat()
    # skipped_days считает дни СРЕДИ БУДУЩИХ дней блока, которые перегенерация
    # не тронула, потому что они уже несут факт или принятую правку — то же
    # самое множество, что молча обходит _wipe_future_calendar (см. её
    # докстринг); здесь просто считаем его явно для ответа наружу, из того же
    # touched_flags, что и снимок выше, — одно и то же условие не должно жить
    # в коде дважды и рисковать разойтись.
    skipped = sum(1 for touched in touched_flags if not touched)

    # Маркер выставляем ДО разрушения календаря — см. разбор в докстринге
    # выше: он обязан уехать в БД в ТОЙ ЖЕ транзакции, что и сам wipe, иначе
    # повтор после обрыва посреди регенерации увидит "ещё не применялось" и
    # пересчитает snapshot["days"] уже по НОВЫМ, только что созданным дням.
    snapshot["structural_applied"] = True
    current_payload = dict(proposal.payload or {})
    current_payload["applied_snapshot"] = snapshot
    proposal.payload = current_payload
    flag_modified(proposal, "payload")
    await session.flush()

    await _wipe_future_calendar(session, app_user_id, block, first_future)
    await _generate_future_calendar(session, app_user_id, block, first_future)
    await session.flush()

    return True, skipped


async def undo_goal_decision(
    session: AsyncSession, app_user_id: int, proposal: PeriodizationProposal,
    client_uuid: Optional[str] = None,
) -> dict:
    """Вернуть то, что автопилот сделал, — и только то.

    Ворота: ни один день, который автопилот РЕАЛЬНО тронул (touched=True в
    снимке, см. докстринг _apply_structural), не сменил статус и не получил
    привязанной сессии. После появления факта на ТАКОМ дне откат означал бы
    переписывание истории — вместо него честнее новое предложение.

    Шаг 4 ниже (ревью Задачи 15, Critical 1) сносит ФАНТОМНЫЕ дни — строки
    UserCalendarDay, которых не было вовсе до применения и которые
    придумала регенерация (SchedulingEngine.generate_block_days заполняет
    ВЕСЬ диапазон [first_future, block.planned_end_date], а не только даты,
    отражённые в snapshot["days"]). Точное правило удаления и почему оно
    безопасно — в комментарии над самим шагом 4.

    ИСПРАВЛЕНО (ревью Задачи 10, Critical 2 — ворота блокировали безопасный
    откат навсегда): snapshot["days"] несёт КАЖДЫЙ будущий день блока на
    момент применения, включая дни, которые _wipe_future_calendar щадит по
    определению (уже нёсшие факт или принятую правку объёма — автопилот их
    не трогал вовсе). Раньше гейт ниже смотрел на ВСЕ даты снимка без
    разбора: блок с одним таким пощажённым, но уже completed днём навсегда
    блокировал откат, хотя ни один РЕАЛЬНО регенерированный день факта не
    получил. Смотрим только на даты с touched=True — то множество, что
    зеркалит days-запрос _apply_structural и её же touched_flags.

    ИСПРАВЛЕНО (ревью Задачи 10 Task-10, Critical 1 — откат мог снести
    носителя, который это предложение никогда не писало): для preference/
    scheme ниже действует ДВУХУРОВНЕВОЕ правило, зеркальное тому, что
    описано в докстринге apply_goal_decision про запись снимка:
      1. ПРИСУТСТВИЕ ключа в snapshot решает ВЛАДЕНИЕ. Ключ отсутствует —
         это предложение соответствующего рычага не применяло (не был
         принят пользователем среди payload["levers"], или applied_snapshot
         вообще пуст) — носитель не трогаем НИКАК, даже не заносим в kept:
         его текущее значение к этому предложению отношения не имеет.
         Раньше владение решалось равенством ТЕКУЩЕГО значения носителя
         detail'у рычага из payload["levers"] — но тот список несёт ВСЕ
         предложенные рычаги, а не только принятые, а decide.py вдобавок
         детерминирован (схемный рычаг всегда предлагает percent_1rm,
         диапазон повторов считается из неизменного target_reps) — так что
         значение, записанное СОВСЕМ ДРУГИМ актором (пользователем вручную
         или другим предложением), могло случайно совпасть с тем, что
         предложил бы рычаг, и удалялось как «наше».
      2. Ключ ПРИСУТСТВУЕТ (в т.ч. как None) — это предложение носителя
         коснулось. None означает «носителя не было до нас» — откат его
         удаляет; записанное значение — восстанавливаем его. Сравнение с
         текущим значением остаётся, но ТОЛЬКО после этой проверки
         присутствия и только чтобы поймать позднюю правку самого
         пользователя ПОСЛЕ применения — такую правку не восстанавливаем
         поверх, а называем в kept (см. ниже).
    """
    # ИНВАРИАНТ (ревью Задачи 11, Minor): _undo_blocked_reason (build_context,
    # can_undo экрана) обязана трактовать отсутствующий/пустой snapshot ТАК
    # ЖЕ — как блокирующую причину, а не как «ничего не мешает». Разошедшийся
    # can_undo=True обещал бы отмену, которую этот же conflict тут же завернёт.
    snapshot = (proposal.payload or {}).get("applied_snapshot")
    if not snapshot:
        return {"status": "conflict", "proposal_id": proposal.id,
                "reason": "Нечего отменять: предложение не применялось"}

    payload = proposal.payload or {}
    exercise_id = payload.get("exercise_id")
    levers = payload.get("levers") or []
    # Тот же отбор дат и тот же запрос, что и can_undo экрана автопилота
    # (build_context -> _undo_blocked_reason, Задача 11) — общие хелперы
    # _touched_dates/_blocked_by_calendar_fact, чтобы эти две точки не могли
    # разойтись в том, что считается заблокированным откатом.
    dates = _touched_dates(snapshot)
    if await _blocked_by_calendar_fact(session, app_user_id, dates):
        return {
            "status": "conflict", "proposal_id": proposal.id,
            "reason": "По изменённым дням уже есть факт — откат переписал бы историю",
        }

    kept: list[str] = []

    # 1. Преференция. Владение — по присутствию ключа "preference" в
    # snapshot (см. докстринг выше); значение constant'но ("favorite"), и
    # именно поэтому сравнение по значению БЕЗ проверки присутствия было
    # дырявым — совпадение с "favorite" ничего не говорит о том, кто его
    # поставил.
    if exercise_id is not None and "preference" in snapshot:
        pref = (await session.execute(
            select(UserExercisePreference).where(
                UserExercisePreference.app_user_id == app_user_id,
                UserExercisePreference.exercise_id == exercise_id,
            )
        )).scalar_one_or_none()
        if pref is not None:
            if pref.preference != "favorite":
                kept.append("preference")       # пользователь поменял сам
            elif snapshot.get("preference") is None:
                await session.delete(pref)
            else:
                pref.preference = snapshot["preference"]

    # 2. Схема прогрессии — та же дисциплина, что у преференции выше:
    # владение решает присутствие ключа "scheme" в snapshot (ИСПРАВЛЕНО,
    # ревью Задачи 10 Task-10, Critical 1 — раньше владение проверялось тем,
    # что exercise_id вообще ЕСТЬ в overrides сейчас, то есть текущим
    # наличием носителя, а не тем, писало ли его ЭТО предложение; схемный
    # рычаг к тому же детерминирован — decide.py всегда предлагает
    # "percent_1rm", — так что чужая правка на то же значение удалялась бы
    # как «наша», см. докстринг undo_goal_decision). Сравнение с
    # detail.to_scheme того lever'а, который его записал (ИСПРАВЛЕНО, ревью
    # Задачи 10, Important 3), идёт ПОСЛЕ подтверждения владения и ловит
    # только позднюю правку самого пользователя — восстанавливаем поверх
    # только если она не разошлась, иначе оставляем как есть и называем в
    # kept.
    profile = (await session.execute(
        select(AppUserProfile).where(AppUserProfile.app_user_id == app_user_id)
    )).scalar_one_or_none()
    if profile is not None and exercise_id is not None and "scheme" in snapshot:
        settings = dict(profile.settings or {})
        progression = dict(settings.get("progression") or {})
        overrides = dict(progression.get("overrides") or {})
        scheme_lever = next(
            (l for l in levers if l.get("kind") == params.LEVER_SCHEME), None
        )
        written_scheme = ((scheme_lever or {}).get("detail") or {}).get("to_scheme")
        current_scheme = overrides.get(str(exercise_id))
        if current_scheme != written_scheme:
            kept.append("scheme")    # пользователь сам поменял схему после применения
        else:
            if snapshot.get("scheme") is None:
                overrides.pop(str(exercise_id), None)
            else:
                overrides[str(exercise_id)] = snapshot["scheme"]
            progression["overrides"] = overrides
            settings["progression"] = progression
            profile.settings = settings
            flag_modified(profile, "settings")

    # 3. Дни календаря — вернуть снятые координаты.
    for row in snapshot.get("days") or []:
        target = date.fromisoformat(row["target_date"])
        day = (await session.execute(
            select(UserCalendarDay).where(
                UserCalendarDay.app_user_id == app_user_id,
                UserCalendarDay.target_date == target,
            )
        )).scalars().first()
        if day is None:
            day = UserCalendarDay(app_user_id=app_user_id, target_date=target)
            session.add(day)
        day.plan_id = row["plan_id"]
        day.day_tag = row["day_tag"]
        day.micro_tag = row["micro_tag"]
        day.meso_tag = row["meso_tag"]
        day.mesocycle_phase_number = row["mesocycle_phase_number"]
        day.is_rest_day = row["is_rest_day"]
        day.block_id = proposal.block_id

    # 4. Фантомные дни — снести то, что регенерация ПРИДУМАЛА, а снимок
    # никогда не видел (ревью Задачи 15, Critical 1). _apply_structural идёт
    # через _generate_future_calendar -> SchedulingEngine.generate_block_days,
    # а та заполняет КАЖДУЮ дату от snapshot["first_future_date"] до
    # block.planned_end_date включительно — в том числе даты, для которых до
    # применения не было ни одной строки UserCalendarDay вовсе. Такие даты не
    # попадают в snapshot["days"] (там нечего было зафиксировать), и шаг 3
    # выше их не трогает: get-or-create по snapshot["days"] создаёт только то,
    # что снимок ЗНАЕТ. Без этого шага откат оставлял бы позади ровно те дни,
    # которых не было ДО применения, — ревью воспроизвело буквально: 5
    # посеянных дней +1..+5, блок с планируемым концом +10, после apply+undo
    # календарь держал +1..+10.
    #
    # Правило удаления — намеренно консервативное, снести можно ТОЛЬКО день,
    # который одновременно:
    #   - принадлежит ИМЕННО этому блоку (proposal.block_id) — регенерация
    #     трогала только его дни, чужой блок за пределами этой операции;
    #   - датирован НЕ РАНЬШЕ snapshot["first_future_date"] — до этой границы
    #     регенерация не заходила вовсе (см. её докстринг в _apply_structural);
    #   - ОТСУТСТВУЕТ в snapshot["days"] — присутствие означало бы, что строка
    #     существовала до применения и уже обработана шагом 3 выше;
    #   - всё ещё "planned" и без actual_workout_session_id — день, на
    #     который уже успели записать тренировку (в т.ч. после отката соседних
    #     дней в рамках этого же вызова), несёт факт, а откат не имеет права
    #     переписывать историю (тот же принцип, что и ворота выше по функции);
    #   - без принятых правок объёма (volume_adjustments) — там лежит решение
    #     пользователя, а не пустое место, оставленное автопилотом.
    # Любой день, не прошедший все пять условий разом, остаётся как есть:
    # удаляются только строки, которые сам автопилот и создал из ничего.
    first_future_raw = snapshot.get("first_future_date")
    if first_future_raw is not None:
        first_future = date.fromisoformat(first_future_raw)
        known_dates = {
            date.fromisoformat(row["target_date"]) for row in snapshot.get("days") or []
        }
        candidates = (await session.execute(
            select(UserCalendarDay).where(
                UserCalendarDay.app_user_id == app_user_id,
                UserCalendarDay.block_id == proposal.block_id,
                UserCalendarDay.target_date >= first_future,
            )
        )).scalars().all()
        for day in candidates:
            if day.target_date in known_dates:
                continue
            if day.status != "planned":
                continue
            if day.actual_workout_session_id is not None:
                continue
            if day.volume_adjustments:
                continue
            await session.delete(day)

    proposal.status = periodization_params.STATUS_UNDONE
    proposal.decided_action = periodization_params.ACTION_UNDO_GOAL
    # ИСПРАВЛЕНО (ревью Задачи 10 Task-10, Important — откат не был
    # идемпотентен по client_uuid): без этой строки proposal.client_uuid
    # оставался тем, что записал apply_goal_decision (диспетчер выставляет
    # его до вызова apply, см. periodization.service.apply_decision), и
    # повтор ЭТОЙ ЖЕ отмены с тем же client_uuid из офлайн-очереди мобильного
    # клиента не совпадал с ним — верхний гейт apply_decision отвечал
    # conflict вместо already_applied, хотя предложение уже честно отменено.
    # Пишем именно здесь, вместе с status/decided_action/decided_at, а не в
    # диспетчере: на ранних return (нечего отменять / конфликт по факту)
    # decided-поля не меняются вовсе, и client_uuid не должен меняться тоже —
    # тот отказ ещё не решение, повтор с ЛЮБЫМ client_uuid обязан пройти
    # сюда снова (гейт диспетчера пропускает status == accepted без сверки
    # client_uuid, см. её докстринг).
    proposal.client_uuid = client_uuid
    proposal.decided_at = datetime.now(timezone.utc)
    await session.flush()
    return {"status": "undone", "proposal_id": proposal.id, "kept": kept}
