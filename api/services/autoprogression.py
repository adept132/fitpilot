"""Автопрогрессия: расчёт рекомендуемого веса/повторений на основе последней тренировки.

Логика (эталон):
1. Берём подходы последней завершённой тренировки с этим упражнением
   (без warmup/дропсетов, с непустыми весом и повторениями).
2. Целевое значение (ЦЗ) подхода — e1rm по Эпли с учётом RIR для всех типов
   упражнений (и базовых, и изолирующих): e1rm = weight * (1 + (reps + rir) / 30).
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

# Настраиваемые пороги/таблицы — единый источник в progression/params.py.
# С переходом compute_autoprogression на движок (P0-06) в этом модуле сами
# больше не используются — только реэкспорт для внешних импортов
# (DEFAULT_RIR тянет csv_format.py, DEFAULT_WEIGHT_STEPS/EFFORT_TO_RIR — fatigue/, тесты).
from api.services.progression.params import DEFAULT_RIR  # noqa: F401
from api.services.progression.params import DEFAULT_WEIGHT_STEPS, EFFORT_TO_RIR  # noqa: F401

# Формулы и округление переехали в api/services/progression/.
# Старые имена сохраняются на один релиз: их импортируют fatigue/,
# csv_format.py и tests/test_autoprogression.py.
from api.services.progression.metrics import effort_to_rir  # noqa: F401
from api.services.progression.metrics import e1rm as set_target_value  # noqa: F401
from api.services.progression.metrics import weight_for_e1rm as weight_for_target  # noqa: F401
from api.services.progression.rounding import KG_PER_LB, LB_PER_KG  # noqa: F401
from api.services.progression.rounding import round_to_step as round_weight_for_equipment  # noqa: F401

# Дефолт коэффициента прогрессии по уровню пользователя.
DEFAULT_FACTOR_BY_LEVEL = {
    "beginner": 1.03,
    "intermediate": 1.02,
    "advanced": 1.01,
}
FALLBACK_FACTOR = 1.03


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
    phase_effort_tier: str = "medium",
    provisional: bool = False,
) -> dict:
    """Совместимая обёртка над движком прогрессии (P0-06).

    Форма ответа сохранена ради AutoprogressionResponse и старых клиентов;
    добавлены prescription/scheme/reason_*.
    """
    from api.services.progression import repository
    from api.services.progression.engine import plan_exercise
    from api.services.progression.resolve import override_for

    ctx = await repository.build_context(
        session,
        session_exercise,
        app_user_id,
        experience_level,
        settings,
        phase_effort_tier=phase_effort_tier,
    )

    if target_reps is not None:
        # Свободная тренировка: пользователь выбрал повторы и усилие руками.
        from dataclasses import replace

        rir = (
            effort_to_rir(target_effort)
            if target_effort is not None
            else ctx.target_rir
        )
        ctx = replace(ctx, rep_min=int(target_reps), rep_max=int(target_reps), target_rir=rir)

    prescription = plan_exercise(
        ctx,
        override=override_for(settings, session_exercise.exercise_id),
        provisional=provisional,
    )

    first = prescription.sets[0] if prescription.sets else None
    return {
        "has_basis": bool(prescription.sets),
        "metric": "e1rm" if prescription.sets else None,
        "target_weight": first.weight_kg if first else None,
        "target_reps": first.rep_min if first else None,
        "modified_target": prescription.basis.get("modified_target"),
        "prescription": prescription.to_dict() if prescription.sets else None,
        "scheme": prescription.scheme,
        "reason_code": prescription.reason_code,
        "reason_text": prescription.reason_text,
    }
