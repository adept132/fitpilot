"""Автопрогрессия: расчёт рекомендуемого веса/повторений на основе последней тренировки.

Логика (эталон):
1. Берём подходы последней завершённой тренировки с этим упражнением
   (без warmup/дропсетов, с непустыми весом и повторениями).
2. Целевое значение (ЦЗ) подхода:
     - Базовое (compound):  e1rm = weight * (1 + (reps + rir) / 30)   # Эпли с учётом RIR
     - Изолирующее:         volume = weight * (reps + rir)
   Берём максимум по подходам -> base_value.
3. Модифицированное ЦЗ: mod = base_value * factor (коэффициент прогрессии).
4. Для каждого r в целевом интервале повторений (или для выбранного r в свободной
   тренировке), с целевым rir_t, ищем вес W под mod.
5. Округляем W по оборудованию, пересчитываем новое ЦЗ.
6. Возвращаем (W, r) с минимальным |new_value - mod|.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.services.models import (
    WorkoutSession,
    WorkoutSessionExercise,
)

KGS_IN_LBS = 2.20462
FREE_WEIGHT_STEP_KG = 2.5

# effort_level -> RIR. Принимаем оба варианта warmup (фронт: "warmup", бэкенд-схема: "warmup_effort").
EFFORT_TO_RIR = {
    "warmup": 4,
    "warmup_effort": 4,
    "light": 3,
    "easy": 3,
    "medium": 2,
    "prefailure": 1,
    "failure": 0,
}
DEFAULT_RIR = 2  # если усилие не указано -> medium

# Дефолт коэффициента прогрессии по уровню пользователя.
DEFAULT_FACTOR_BY_LEVEL = {
    "beginner": 1.03,
    "intermediate": 1.02,
    "advanced": 1.01,
}
FALLBACK_FACTOR = 1.03

# Оборудование, для которого вес округляется до 10 lb (в кг). Остальное -> до 2.5 кг.
ROUND_10LB_EQUIPMENT = {"Тренажер", "Кроссовер"}


def effort_to_rir(effort_level: Optional[str]) -> int:
    if not effort_level:
        return DEFAULT_RIR
    return EFFORT_TO_RIR.get(effort_level.strip().lower(), DEFAULT_RIR)


def is_compound(category: Optional[str]) -> bool:
    """True -> базовое (e1rm), False -> изолирующее (объём). По умолчанию базовое."""
    if not category:
        return True
    c = category.strip().lower()
    if "изол" in c or "isol" in c:
        return False
    return True


def set_target_value(compound: bool, weight: float, reps: int, rir: int) -> float:
    if compound:
        return weight * (1 + (reps + rir) / 30)
    return weight * (reps + rir)


def weight_for_target(compound: bool, target_value: float, reps: int, rir: int) -> float:
    if compound:
        denom = 1 + (reps + rir) / 30
    else:
        denom = reps + rir
    if denom <= 0:
        return 0.0
    return target_value / denom


def round_weight_for_equipment(weight: float, equipment: Optional[list]) -> float:
    equipment = equipment or []
    use_10lb = any(e in ROUND_10LB_EQUIPMENT for e in equipment)

    if use_10lb:
        target_lbs = weight * KGS_IN_LBS
        rounded_lbs = round(target_lbs / 10) * 10
        final_lbs = max(10, rounded_lbs)
        return round(final_lbs / KGS_IN_LBS, 1)

    rounded_kg = round(weight / FREE_WEIGHT_STEP_KG) * FREE_WEIGHT_STEP_KG
    return round(max(FREE_WEIGHT_STEP_KG, rounded_kg), 1)


def progression_factor_for(
    experience_level: Optional[str],
    settings: Optional[dict],
) -> float:
    if settings:
        raw = settings.get("progression_factor")
        if raw is not None:
            try:
                val = float(raw)
                if val > 0:
                    return val
            except (TypeError, ValueError):
                pass
    level = (experience_level or "beginner").strip().lower()
    return DEFAULT_FACTOR_BY_LEVEL.get(level, FALLBACK_FACTOR)


async def get_last_performance_basis_sets(
    session: AsyncSession,
    app_user_id: int,
    exercise_id: int,
) -> list["WorkoutSessionSet"]:
    """Подходы последней завершённой тренировки, пригодные как база прогрессии."""
    stmt = (
        select(WorkoutSession)
        .join(
            WorkoutSessionExercise,
            WorkoutSessionExercise.workout_session_id == WorkoutSession.id,
        )
        .where(
            WorkoutSession.app_user_id == app_user_id,
            WorkoutSession.status == "finished",
            WorkoutSessionExercise.exercise_id == exercise_id,
        )
        .options(
            selectinload(WorkoutSession.exercises).selectinload(
                WorkoutSessionExercise.sets
            ),
        )
        .order_by(WorkoutSession.finished_at.desc())
    )

    result = await session.execute(stmt)
    workout = result.scalars().first()
    if workout is None:
        return []

    session_exercise = next(
        (e for e in workout.exercises if e.exercise_id == exercise_id),
        None,
    )
    if session_exercise is None:
        return []

    basis = []
    for s in session_exercise.sets:
        if not s.is_completed:
            continue
        if s.set_type in ("warmup", "drop"):
            continue
        if s.parent_set_id is not None:
            continue
        if s.weight is None or s.reps is None:
            continue
        if float(s.weight) <= 0 or int(s.reps) <= 0:
            continue
        # Аномальные подходы не участвуют в автопрогрессии (спека §5.3):
        # один жим «700 кг» иначе задрал бы e1RM и рекомендованный вес.
        if s.is_anomalous:
            continue
        basis.append(s)

    return basis


async def compute_autoprogression(
    session: AsyncSession,
    session_exercise: WorkoutSessionExercise,
    app_user_id: int,
    experience_level: Optional[str],
    settings: Optional[dict],
    target_reps: Optional[int] = None,
    target_effort: Optional[str] = None,
) -> dict:
    """Возвращает dict под AutoprogressionResponse.

    target_reps/target_effort передаются только в свободной тренировке (пользователь
    выбрал их вручную). В плановой тренировке берём recommended_* из session_exercise.
    """
    exercise = session_exercise.exercise
    compound = is_compound(getattr(exercise, "category", None))
    equipment = getattr(exercise, "equipment_needed", None) or []
    metric = "e1rm" if compound else "volume"

    basis_sets = await get_last_performance_basis_sets(
        session, app_user_id, exercise.id
    )
    if not basis_sets:
        return {
            "has_basis": False,
            "metric": None,
            "target_weight": None,
            "target_reps": None,
            "modified_target": None,
        }

    base_value = max(
        set_target_value(
            compound, float(s.weight), int(s.reps), effort_to_rir(s.effort_level)
        )
        for s in basis_sets
    )
    factor = progression_factor_for(experience_level, settings)
    mod = base_value * factor

    # Целевой RIR и набор кандидатов по повторениям.
    if target_reps is not None:
        # Свободная тренировка: пользователь выбрал конкретные повторения/усилие.
        if target_effort is not None:
            rir_t = effort_to_rir(target_effort)
        elif session_exercise.recommended_rir is not None:
            rir_t = session_exercise.recommended_rir
        else:
            rir_t = DEFAULT_RIR
        rep_candidates = [int(target_reps)]
    else:
        rir_t = (
            session_exercise.recommended_rir
            if session_exercise.recommended_rir is not None
            else DEFAULT_RIR
        )
        rmin = session_exercise.recommended_rep_min
        rmax = session_exercise.recommended_rep_max
        if rmin and rmax and rmax >= rmin:
            rep_candidates = list(range(int(rmin), int(rmax) + 1))
        elif rmin:
            rep_candidates = [int(rmin)]
        elif rmax:
            rep_candidates = [int(rmax)]
        else:
            rep_candidates = []

    if not rep_candidates:
        # Есть база, но целевые повторения не заданы (напр. свободная тренировка
        # до выбора пикеров) -> сигнал фронту показать выбор.
        return {
            "has_basis": True,
            "metric": metric,
            "target_weight": None,
            "target_reps": None,
            "modified_target": round(mod, 2),
        }

    best = None  # (abs_diff, weight, reps)
    for r in rep_candidates:
        raw_w = weight_for_target(compound, mod, r, rir_t)
        w_r = round_weight_for_equipment(raw_w, equipment)
        new_val = set_target_value(compound, w_r, r, rir_t)
        diff = abs(new_val - mod)
        if best is None or diff < best[0]:
            best = (diff, w_r, r)

    _, best_weight, best_reps = best
    return {
        "has_basis": True,
        "metric": metric,
        "target_weight": best_weight,
        "target_reps": best_reps,
        "modified_target": round(mod, 2),
    }
