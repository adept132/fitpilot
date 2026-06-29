import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession


from api.services.models import (
    AppUserMesocycle, AppUserMicrocycle, WorkoutPlan, Mesocycle, WorkoutPlanExercise
)
from api.services.resolvers import StrategicEffortTier, DayTacticalType, resolve_rir, resolve_rep_range


async def calculate_exercise_recommendations(
        session: AsyncSession,
        app_user_id: int,
        plan_id: Optional[int] = None,
        single_exercise_id: Optional[int] = None,
        single_fatigue_tier: Optional[int] = None
):
    # =====================================================================
    # СЛОЙ 1: МЕЗОЦИКЛ (Получаем StrategicEffortTier для текущей недели)
    # =====================================================================
    active_meso_stmt = (
        select(AppUserMesocycle)
        .options(selectinload(AppUserMesocycle.mesocycle).selectinload(Mesocycle.phases))
        .where(AppUserMesocycle.app_user_id == app_user_id, AppUserMesocycle.is_active == True)
    )
    meso_res = await session.execute(active_meso_stmt)
    active_meso = meso_res.scalar_one_or_none()

    effort_tier = StrategicEffortTier.medium

    if active_meso and active_meso.mesocycle:
        current_phase = active_meso.current_phase
        phase_data = next((p for p in active_meso.mesocycle.phases if p.phase_number == current_phase), None)
        if phase_data and hasattr(phase_data, "effort_tier"):
            effort_tier = StrategicEffortTier(phase_data.effort_tier)

    # =====================================================================
    # СЛОЙ 2: МИКРОЦИКЛ (Получаем DayTacticalType для текущего дня)
    # =====================================================================
    active_micro_stmt = select(AppUserMicrocycle).where(
        AppUserMicrocycle.app_user_id == app_user_id,
        AppUserMicrocycle.is_active == True
    )
    micro_res = await session.execute(active_micro_stmt)
    active_micro = micro_res.scalar_one_or_none()

    day_type = DayTacticalType.medium

    if active_micro and active_micro.days_mapping:
        current_day_index = 1
        day_info = active_micro.days_mapping.get(str(current_day_index), {})
        raw_type = day_info.get("type", "medium")
        day_type = DayTacticalType(raw_type)

    compiled_exercises = []

    # =====================================================================
    # СЛОЙ 3: ПЛАН ИЛИ ОДИНОЧНОЕ УПРАЖНЕНИЕ (Синтезируем данные)
    # =====================================================================

    # ВЕТКА А: Распаковка целого плана
    if plan_id:
        plan_stmt = (
            select(WorkoutPlan)
            .options(
                selectinload(WorkoutPlan.exercises)
                .selectinload(WorkoutPlanExercise.exercise)
            )
            .where(WorkoutPlan.id == plan_id)
        )
        plan_res = await session.execute(plan_stmt)
        plan = plan_res.scalar_one_or_none()

        superset_mapping = {}

        if plan:
            for idx, plan_ex in enumerate(plan.exercises):
                fatigue_tier = getattr(plan_ex.exercise, "fatigue_tier", 2) if plan_ex.exercise else 2

                calculated_rir = resolve_rir(fatigue_tier, effort_tier)
                base_rep_min, base_rep_max = resolve_rep_range(fatigue_tier, day_type)

                final_rir = plan_ex.override_rir if plan_ex.override_rir is not None else calculated_rir
                final_rep_min = base_rep_min
                final_rep_max = base_rep_max

                if plan_ex.override_reps:
                    parts = plan_ex.override_reps.split("-")
                    if len(parts) == 2:
                        final_rep_min, final_rep_max = int(parts[0].strip()), int(parts[1].strip())
                    elif len(parts) == 1:
                        final_rep_min = final_rep_max = int(parts[0].strip())

                sg_uuid = plan_ex.superset_group_id
                session_superset_str = None

                if sg_uuid:
                    if sg_uuid not in superset_mapping:
                        superset_mapping[sg_uuid] = str(uuid.uuid4())
                    session_superset_str = superset_mapping[sg_uuid]

                compiled_exercises.append({
                    "exercise_id": plan_ex.exercise_id,
                    "order_index": idx + 1,
                    "target_sets": plan_ex.target_sets,
                    "superset_group": session_superset_str,
                    "recommended_rir": final_rir,
                    "recommended_rep_min": final_rep_min,
                    "recommended_rep_max": final_rep_max
                })

    # ВЕТКА Б: Добавление одиночного упражнения на лету
    elif single_exercise_id:
        fatigue_tier = single_fatigue_tier if single_fatigue_tier is not None else 2

        calculated_rir = resolve_rir(fatigue_tier, effort_tier)
        base_rep_min, base_rep_max = resolve_rep_range(fatigue_tier, day_type)

        compiled_exercises.append({
            "exercise_id": single_exercise_id,
            "order_index": 999,  # По умолчанию кидаем в конец, роутер может перезаписать
            "target_sets": 0,  # Дефолтное количество подходов для кастомного упр-я
            "superset_group": None,
            "recommended_rir": calculated_rir,
            "recommended_rep_min": base_rep_min,
            "recommended_rep_max": base_rep_max
        })

    return compiled_exercises