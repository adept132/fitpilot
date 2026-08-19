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
    UserExerciseRepOverride,
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
    lift_sessions, success_rate = await repository.lift_stats(
        session, app_user_id, goal.exercise_id
    )
    factor = simulate.calibration_factor(
        await repository.adherence_ratios(session, app_user_id),
        lift_sessions,
        success_rate,
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
        # Контекст рычагов: запас объёма до MRV и параметры упражнения, ОБА
        # берутся из БД через repository (поправка постановщика Задачи 6:
        # заготовка брифа местами хардкодила эти значения — без реального
        # headroom_sets вето по MRV не работало бы вовсе, спека §5.4/решение 11).
        "headroom_sets": await repository.headroom_sets(
            session, app_user_id, goal.exercise_id, level, today
        ),
        "exercise": await repository.exercise_context(
            session, app_user_id, goal.exercise_id
        ),
    }


UNAVAILABLE_NOT_STRENGTH = "Эта цель вне контура плана: её ведёт питание, а не тренировки"
UNAVAILABLE_NO_DEADLINE = "У цели нет срока — автопилоту нечему не успевать"
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
        "milestones": [
            {"week_start": m.week_start.isoformat(),
             "expected_e1rm": m.expected_e1rm, "actual_e1rm": None}
            for m in sim.milestones
        ],
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


async def refresh_goal_proposals(
    session: AsyncSession, app_user_id: int, today: date
) -> Optional[PeriodizationProposal]:
    """Создать предложение автопилота, если есть что сказать.

    Первым делом проверяет, не завершилась ли ведущая цель (достигнута или
    просрочена) — тогда снимает is_primary и гасит активное предложение,
    новое не создаётся вовсе (см. _retire_finished_primary_goal).
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
    levers, reason = decide.decide(DecisionInput(
        rates=state["rates"],
        deadline=goal.deadline,
        eta=state["eta"],
        lift_in_plan=state["lift_in_plan"],
        trend_slope=state["simulation"].nominal_slope,
        microcycles_left=microcycles_left,
        headroom_sets=state["headroom_sets"],
        scheme=state["exercise"]["scheme"],
        is_heavy_compound=state["exercise"]["is_heavy_compound"],
        rep_max=state["exercise"]["rep_max"],
        target_reps=goal.target_reps or 1,
    ))
    if not levers:
        # ИСПРАВЛЕНО (ревью Задачи 6, Important 2): это УДАВШАЯСЯ переоценка
        # (evaluate() что-то посчитал, активный блок нашёлся) с содержательным
        # выводом «сейчас предлагать нечего» — пользователь мог нагнать темп,
        # тренд уйти в минус или требуемый темп выйти выше потолка. Прежнее
        # pending-предложение по ЭТОЙ цели после такого вывода несёт устаревшие
        # цифры и совет, который уже неверен (ревью воспроизвело это буквально:
        # предложение по "лифта нет в плане" осталось висеть после того, как
        # ситуация стала ABOVE_CEILING) — гасим его здесь же.
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
    for row in existing:
        payload = row.payload or {}
        if payload.get("goal_id") == goal.id and payload.get("inputs_hash") == state["inputs_hash"]:
            return row  # те же условия той же цели — то же предложение, второго не надо
        # Другая цель или изменившиеся условия: прежнее предложение больше не про этот план.
        row.status = periodization_params.STATUS_EXPIRED

    sim = state["simulation"]
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
            "structural": None,
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

    Структурный рычаг (lift_frequency/structural) сюда не относится — его
    применение решает Задача 9; индекс такого рычага в accepted сейчас
    просто ничего не делает.

    НЕ КОММИТИТ СЕССИЮ (ревью Задачи 8, Critical 1) — по тому же контракту,
    что и volume.apply_volume_decision: только flush(). Записи рычагов и
    решение по предложению (proposal.status/decided_action/client_uuid/
    decided_at) обязаны попасть в ОДИН commit() вызывающей стороны
    (periodization.service.apply_decision, Задача 10) — см. подробный разбор
    в её докстринге у Critical 3: коммит здесь расщепил бы транзакцию на
    два, и обрыв процесса между ними оставил бы рычаги применёнными, а
    предложение — всё ещё pending; повтор из офлайн-очереди мобильного
    клиента прошёл бы проверку status == pending заново и применил бы
    рычаги ВТОРОЙ раз (см. идемпотентность _apply_sets по proposal_id ниже —
    это подстраховка на случай именно такого повтора, а не замена
    атомарности).

    skipped_days — счётчик СРЕДИ СТРОГО БУДУЩИХ дней блока (target_date >
    today), которые уже несут факт (status != "planned") и потому рычаг
    LEVER_SETS их не тронул. Сегодняшний день в этот счётчик НЕ входит — он
    не применяется и не считается пропущенным (см. докстринг _apply_sets).
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
    # носитель (preference/rep_override/scheme), а само записанное значение
    # (включая None) — это то, что было ДО записи, по которому откат либо
    # удаляет носителя (None), либо восстанавливает прежнее значение.
    existing_snapshot = payload.get("applied_snapshot")
    snapshot: dict = dict(existing_snapshot) if isinstance(existing_snapshot, dict) else {}
    snapshot.setdefault("days", [])
    snapshot.setdefault("created_plan_ids", [])
    applied: list[int] = []
    skipped_days = 0

    for lever in levers:
        if lever["index"] not in accepted:
            continue
        kind = lever["kind"]
        if kind == params.LEVER_ENSURE_PRESENT:
            if await _apply_favorite(session, app_user_id, exercise_id, snapshot):
                applied.append(lever["index"])
        elif kind == params.LEVER_REP_RANGE:
            if await _apply_rep_range(session, app_user_id, exercise_id, lever, snapshot):
                applied.append(lever["index"])
        elif kind == params.LEVER_SCHEME:
            if await _apply_scheme(session, app_user_id, exercise_id, lever, snapshot):
                applied.append(lever["index"])
        elif kind == params.LEVER_SETS:
            done, skipped = await _apply_sets(
                session, app_user_id, proposal, exercise_id, lever
            )
            skipped_days += skipped
            if done:
                applied.append(lever["index"])
        elif kind in params.STRUCTURAL_LEVERS:
            done, skipped = await _apply_structural(
                session, app_user_id, proposal, snapshot
            )
            skipped_days += skipped
            if done:
                applied.append(lever["index"])

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


async def _apply_rep_range(
    session: AsyncSession, app_user_id: int, exercise_id: Optional[int],
    lever: dict, snapshot: dict,
) -> bool:
    if exercise_id is None:
        return False
    detail = lever.get("detail") or {}
    rep_min, rep_max = detail.get("rep_min"), detail.get("rep_max")
    if rep_min is None or rep_max is None:
        return False
    existing = (await session.execute(
        select(UserExerciseRepOverride).where(
            UserExerciseRepOverride.app_user_id == app_user_id,
            UserExerciseRepOverride.exercise_id == exercise_id,
        )
    )).scalar_one_or_none()
    # Пишем ТОЛЬКО если ключ ещё не записан — та же причина, что и в
    # _apply_favorite (см. её комментарий и докстринг apply_goal_decision).
    if "rep_override" not in snapshot:
        snapshot["rep_override"] = (
            {"rep_min": existing.rep_min, "rep_max": existing.rep_max} if existing else None
        )
    if existing is None:
        session.add(UserExerciseRepOverride(
            app_user_id=app_user_id, exercise_id=exercise_id,
            rep_min=int(rep_min), rep_max=int(rep_max),
        ))
    else:
        existing.rep_min, existing.rep_max = int(rep_min), int(rep_max)
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


async def _apply_sets(
    session: AsyncSession, app_user_id: int, proposal: PeriodizationProposal,
    exercise_id: Optional[int], lever: dict,
) -> tuple[bool, int]:
    """+N подходов целевого лифта на будущих днях блока.

    Правка живёт НА ДНЕ (UserCalendarDay.volume_adjustments), а не в
    WorkoutPlanExercise: план переиспользуется на всех подходящих днях, и
    правка в нём изменила бы каждый такой день навсегда (см. докстринг поля).
    Дни, переставшие быть planned, пропускаются: там уже есть факт. Граница
    выборки — target_date > today: сегодняшний день не применяется и не
    считается пропущенным (см. докстринг apply_goal_decision про
    skipped_days).

    Идемпотентность по proposal_id (ревью Задачи 8, Critical 1): apply_
    goal_decision больше не коммитит сама (см. её докстринг) — решение и
    правка коммитятся ОДНИМ commit() вызывающей стороны (Задача 10), но до
    тех пор, пока это не подключено, а также на случай повтора из офлайн-
    очереди мобильного клиента ПОСЛЕ того как рычаг уже применился, но
    предложение ещё не успело перейти в decided-статус, — повторный проход
    по тому же дню не должен задвоить прибавку подходов. Если день уже
    несёт запись {exercise_id, proposal_id} от ЭТОГО ЖЕ предложения, вторую
    не добавляем, но день всё равно считаем затронутым (это не тот же
    случай, что «дня уже нет в planned» — правка НА НЁМ есть, просто уже
    ровно одна).
    """
    if exercise_id is None:
        return False, 0
    delta = int((lever.get("detail") or {}).get("delta_sets") or 0)
    if delta == 0:
        return False, 0

    today = date.today()
    days = (await session.execute(
        select(UserCalendarDay).where(
            UserCalendarDay.app_user_id == app_user_id,
            UserCalendarDay.block_id == proposal.block_id,
            UserCalendarDay.target_date > today,
        )
    )).scalars().all()

    touched, skipped = 0, 0
    for day in days:
        if day.status != "planned":
            skipped += 1
            continue
        adjustments = list(day.volume_adjustments or [])
        already_applied = any(
            a.get("exercise_id") == exercise_id and a.get("proposal_id") == proposal.id
            for a in adjustments
        )
        if already_applied:
            touched += 1
            continue
        adjustments.append({
            "exercise_id": exercise_id,
            "delta_sets": delta,
            "proposal_id": proposal.id,
        })
        day.volume_adjustments = adjustments
        flag_modified(day, "volume_adjustments")
        touched += 1

    await session.flush()
    return touched > 0, skipped


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
    отличаются от "preference"/"rep_override"/"scheme", где отсутствие ключа
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
    rep_override/scheme ниже действует ДВУХУРОВНЕВОЕ правило, зеркальное
    тому, что описано в докстринге apply_goal_decision про запись снимка:
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

    # 1. Подходы: адресно по proposal_id — чужие правки (volume_review)
    #    остаются на месте.
    days = (await session.execute(
        select(UserCalendarDay).where(
            UserCalendarDay.app_user_id == app_user_id,
            UserCalendarDay.volume_adjustments.isnot(None),
        )
    )).scalars().all()
    for day in days:
        remaining = [
            a for a in (day.volume_adjustments or [])
            if a.get("proposal_id") != proposal.id
        ]
        if len(remaining) != len(day.volume_adjustments or []):
            day.volume_adjustments = remaining
            flag_modified(day, "volume_adjustments")

    # 2. Преференция. Владение — по присутствию ключа "preference" в
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

    # 3. Диапазон повторов — та же дисциплина: владение решает присутствие
    # ключа "rep_override" в snapshot (ИСПРАВЛЕНО, ревью Задачи 10 Task-10,
    # Critical 1 — раньше владение проверялось равенством текущего значения
    # detail'у рычага из payload["levers"], а тот список несёт ВСЕ
    # предложенные рычаги, включая непринятые; decide.py к тому же считает
    # rep_min/rep_max детерминированно из неизменного target_reps, так что
    # значение, записанное СОВСЕМ ДРУГИМ актором, могло случайно совпасть и
    # удалялось как «наше» — см. докстринг undo_goal_decision). Восстанавливаем
    # ТОЛЬКО если текущее значение ещё совпадает с тем, что записал автопилот
    # (ИСПРАВЛЕНО, ревью Задачи 10, Important 3, спека §5.5: было безусловно).
    # Что именно записал автопилот, берём из proposal.payload["levers"] — тот
    # же rep_min/rep_max, что _apply_rep_range взяла из detail своего
    # lever'а; эта проверка ИДЁТ ПОСЛЕ подтверждения владения и ловит только
    # позднюю правку самого пользователя ПОСЛЕ применения — такую правку
    # откат не имеет права стирать молча: разошедшееся значение оставляем
    # как есть и называем в kept, а не восстанавливаем поверх.
    if exercise_id is not None and "rep_override" in snapshot:
        override = (await session.execute(
            select(UserExerciseRepOverride).where(
                UserExerciseRepOverride.app_user_id == app_user_id,
                UserExerciseRepOverride.exercise_id == exercise_id,
            )
        )).scalar_one_or_none()
        previous = snapshot.get("rep_override")
        if override is not None:
            rep_lever = next(
                (l for l in levers if l.get("kind") == params.LEVER_REP_RANGE), None
            )
            written = (rep_lever or {}).get("detail") or {}
            written_value = (written.get("rep_min"), written.get("rep_max"))
            if (override.rep_min, override.rep_max) != written_value:
                kept.append("rep_override")    # пользователь сам поменял диапазон после применения
            elif previous is None:
                await session.delete(override)
            else:
                override.rep_min, override.rep_max = previous["rep_min"], previous["rep_max"]

    # 4. Схема прогрессии — та же дисциплина, что у диапазона повторов выше:
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

    # 5. Дни календаря — вернуть снятые координаты.
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
