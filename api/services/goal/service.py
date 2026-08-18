"""Автопилот цели: создание предложений и сборка контекста экрана (P0-12).

Лениво, на существующих точках вызова — планировщика в проекте нет и
заводить его не нужно (то же решение, что в volume.service).

ОТКЛОНЕНИЕ ОТ БРИФА (см. отчёт Задачи 6): бриф в разделе "Interfaces" называет
`build_context(session, app_user_id, goal, today) -> dict` как продукт этой
задачи ("тело эндпоинта Task 11"), но ни один шаг брифа не даёт для неё ни
кода, ни формы возвращаемого словаря, ни теста. Постановщик Задачи 6 эту
функцию тоже не упоминает. Реализовывать экранный контракт без единого
заданного поля значило бы гадать форму API, которую использует другая,
ещё не начатая задача — build_context сюда не добавлен, это осознанный
пробел, а не забывчивость (см. "Сомнения" в отчёте).
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.forecast_service import WEEKLY_GROWTH_CAP_PCT, _DEFAULT_CAP_PCT
from api.services.goal import decide, repository, simulate
from api.services.goal.types import DecisionInput, Rates
from api.services.models import (
    AppUserProfile,
    PeriodizationProposal,
    TrainingBlock,
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
