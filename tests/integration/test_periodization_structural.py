"""Структурная правка: сдвиг диапазона повторов как персональное правило."""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import select

from api.services.calculate_exercise_recommendation import calculate_exercise_recommendations
from api.services.models import (
    AppUser,
    AppUserMesocycle,
    AppUserMicrocycle,
    Exercise,
    Mesocycle,
    MesocyclePhase,
    PeriodizationProposal,
    UserExerciseRepOverride,
    WorkoutPlan,
    WorkoutPlanExercise,
)
from api.services.periodization import params
from api.services.periodization.repository import ensure_active_block
from api.services.periodization.service import apply_decision
from api.services.progression import params as progression_params


async def _seed(db, user_id: int):
    meso = Mesocycle(author_id=user_id, name="Т", code=f"t_{uuid.uuid4().hex[:8]}", phases_in_cycle=1)
    db.add(meso)
    await db.flush()
    db.add(MesocyclePhase(mesocycle_id=meso.id, phase_number=1, name="Средняя", effort_tier="medium"))
    db.add(AppUserMesocycle(
        app_user_id=user_id, mesocycle_id=meso.id, is_active=True, microcycle_length=7, current_phase=1
    ))
    db.add(AppUserMicrocycle(
        app_user_id=user_id, name="М", length_days=7,
        days_mapping={"1": {"type": "hard", "tag": "full"}}, is_active=True,
    ))
    await db.commit()
    return await ensure_active_block(db, user_id, date.today())


async def _make_exercise(db, user_id: int) -> Exercise:
    marker = uuid.uuid4().hex[:8]
    ex = Exercise(
        name=f"Тестовое упражнение структурной правки {marker}",
        category="base",
        main_muscle_group="chest",
        difficulty="beginner",
        equipment_needed=[],
        source="custom",
        app_user_id=user_id,
    )
    db.add(ex)
    await db.commit()
    await db.refresh(ex)
    return ex


@pytest.mark.asyncio
async def test_shift_reps_writes_a_personal_override(db, test_user: AppUser):
    block = await _seed(db, test_user.id)
    proposal = PeriodizationProposal(
        app_user_id=test_user.id, block_id=block.id, kind=params.KIND_STRUCTURAL,
        reason_code=params.REASON_STALLED_AFTER_DELOAD,
        payload={"exercise_id": 1, "options": ["shift_reps", "replace", "keep"]},
        status=params.STATUS_PENDING,
    )
    db.add(proposal)
    await db.commit()

    result = await apply_decision(
        db, test_user.id, proposal.id, "shift_reps",
        client_uuid=uuid.uuid4().hex,
    )

    assert result["status"] == "applied"
    override = (
        await db.execute(
            select(UserExerciseRepOverride).where(
                UserExerciseRepOverride.app_user_id == test_user.id,
                UserExerciseRepOverride.exercise_id == 1,
            )
        )
    ).scalars().first()
    assert override is not None
    assert override.rep_max < override.rep_min + 6, "диапазон остаётся диапазоном"

    await db.delete(override)
    await db.commit()


@pytest.mark.asyncio
async def test_keep_writes_nothing(db, test_user: AppUser):
    block = await _seed(db, test_user.id)
    proposal = PeriodizationProposal(
        app_user_id=test_user.id, block_id=block.id, kind=params.KIND_STRUCTURAL,
        reason_code=params.REASON_STALLED_AFTER_DELOAD,
        payload={"exercise_id": 2}, status=params.STATUS_PENDING,
    )
    db.add(proposal)
    await db.commit()

    await apply_decision(db, test_user.id, proposal.id, "keep", client_uuid=uuid.uuid4().hex)

    override = (
        await db.execute(
            select(UserExerciseRepOverride).where(
                UserExerciseRepOverride.app_user_id == test_user.id,
                UserExerciseRepOverride.exercise_id == 2,
            )
        )
    ).scalars().first()
    assert override is None


def test_user_source_is_named():
    assert progression_params.REP_SOURCE_USER == "user_override"


@pytest.mark.asyncio
async def test_shift_reps_retried_with_same_client_uuid_applies_once(db, test_user: AppUser):
    """Повторная доставка того же решения (двойной тап / офлайн-очередь с
    повтором) не должна сдвигать диапазон дважды: apply_decision отвечает
    already_applied на второй вызов с тем же client_uuid, а override, который
    видит движок, не меняется дальше."""
    block = await _seed(db, test_user.id)
    proposal = PeriodizationProposal(
        app_user_id=test_user.id, block_id=block.id, kind=params.KIND_STRUCTURAL,
        reason_code=params.REASON_STALLED_AFTER_DELOAD,
        payload={"exercise_id": 3, "options": ["shift_reps", "replace", "keep"]},
        status=params.STATUS_PENDING,
    )
    db.add(proposal)
    await db.commit()

    client_uuid = uuid.uuid4().hex
    first = await apply_decision(db, test_user.id, proposal.id, "shift_reps", client_uuid=client_uuid)
    assert first["status"] == "applied"

    override_after_first = (
        await db.execute(
            select(UserExerciseRepOverride).where(
                UserExerciseRepOverride.app_user_id == test_user.id,
                UserExerciseRepOverride.exercise_id == 3,
            )
        )
    ).scalars().first()
    assert override_after_first is not None
    first_min, first_max = override_after_first.rep_min, override_after_first.rep_max

    second = await apply_decision(db, test_user.id, proposal.id, "shift_reps", client_uuid=client_uuid)
    assert second["status"] == "already_applied"

    await db.refresh(override_after_first)
    assert (override_after_first.rep_min, override_after_first.rep_max) == (first_min, first_max), (
        "повторная доставка того же решения не должна сдвигать диапазон второй раз"
    )

    await db.delete(override_after_first)
    await db.commit()


@pytest.mark.asyncio
async def test_shift_reps_applied_across_two_stalls_does_not_drift_below_floor(db, test_user: AppUser):
    """Два РАЗНЫХ предложения по одному упражнению (упражнение встало снова
    после того, как уже был один сдвиг) сдвигают диапазон дважды, но
    REP_SHIFT_MIN не даёт ему уйти в отрицательные/бессмысленные повторы —
    вторая правка сходится к полу, а не улетает вниз бесконечно."""
    block = await _seed(db, test_user.id)

    def _make_proposal():
        p = PeriodizationProposal(
            app_user_id=test_user.id, block_id=block.id, kind=params.KIND_STRUCTURAL,
            reason_code=params.REASON_STALLED_AFTER_DELOAD,
            payload={"exercise_id": 4, "options": ["shift_reps", "replace", "keep"]},
            status=params.STATUS_PENDING,
        )
        return p

    proposal_a = _make_proposal()
    db.add(proposal_a)
    await db.commit()
    await apply_decision(db, test_user.id, proposal_a.id, "shift_reps", client_uuid=uuid.uuid4().hex)

    override = (
        await db.execute(
            select(UserExerciseRepOverride).where(
                UserExerciseRepOverride.app_user_id == test_user.id,
                UserExerciseRepOverride.exercise_id == 4,
            )
        )
    ).scalars().first()
    assert override is not None
    first_min = override.rep_min

    proposal_b = _make_proposal()
    db.add(proposal_b)
    await db.commit()
    await apply_decision(db, test_user.id, proposal_b.id, "shift_reps", client_uuid=uuid.uuid4().hex)

    await db.refresh(override)
    assert override.rep_min <= first_min, "второй сдвиг двигает диапазон дальше вниз (или держит на полу)"
    assert override.rep_min >= progression_params.REP_SHIFT_MIN, "ниже пола REP_SHIFT_MIN опускаться нельзя"
    assert override.rep_max > override.rep_min, "диапазон остаётся диапазоном и после второго сдвига"

    await db.delete(override)
    await db.commit()


@pytest.mark.asyncio
async def test_recommendation_prefers_user_override_over_microcycle(db, test_user: AppUser):
    """Персональное правило перебивает микроцикл: при активном микроцикле
    (day_type=hard, tier=2 -> матрица дала бы 6-8) персональный override
    должен победить."""
    await _seed(db, test_user.id)
    ex = await _make_exercise(db, test_user.id)
    ex.fatigue_tier = 2
    db.add(ex)
    override = UserExerciseRepOverride(
        app_user_id=test_user.id, exercise_id=ex.id, rep_min=20, rep_max=25,
    )
    db.add(override)
    await db.commit()

    try:
        recs = await calculate_exercise_recommendations(
            db, test_user.id, single_exercise_id=ex.id, single_fatigue_tier=2, current_day_index=1,
        )
        assert len(recs) == 1
        rec = recs[0]
        assert (rec["recommended_rep_min"], rec["recommended_rep_max"]) == (20, 25)
        assert rec["rep_range_source"] == progression_params.REP_SOURCE_USER
    finally:
        await db.delete(override)
        await db.commit()


@pytest.mark.asyncio
async def test_recommendation_plan_override_wins_over_user_override(db, test_user: AppUser):
    """План остаётся последним словом: даже при наличии персонального
    override, override_reps плана побеждает."""
    await _seed(db, test_user.id)
    ex = await _make_exercise(db, test_user.id)
    ex.fatigue_tier = 2

    plan = WorkoutPlan(
        app_user_id=test_user.id, name="Тестовый план структурной правки",
        day_tag="push", micro_tag="medium", meso_tag="medium",
    )
    db.add(plan)
    await db.flush()

    plan_ex = WorkoutPlanExercise(
        plan_id=plan.id, exercise_id=ex.id, order_index=0, target_sets=3,
        override_reps="6-8",
    )
    db.add(plan_ex)

    override = UserExerciseRepOverride(
        app_user_id=test_user.id, exercise_id=ex.id, rep_min=20, rep_max=25,
    )
    db.add(override)
    await db.commit()

    try:
        recs = await calculate_exercise_recommendations(db, test_user.id, plan_id=plan.id)
        assert len(recs) == 1
        rec = recs[0]
        assert (rec["recommended_rep_min"], rec["recommended_rep_max"]) == (6, 8)
        assert rec["rep_range_source"] == progression_params.REP_SOURCE_PLAN
    finally:
        await db.delete(override)
        await db.commit()
