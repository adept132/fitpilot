"""Материализация обзора объёма и применение трёх рычагов."""
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from api.services.models import (
    AppUserProfile,
    Exercise,
    PeriodizationProposal,
    TrainingBlock,
    UserCalendarDay,
    VolumeWindow,
    WorkoutPlan,
    WorkoutPlanExercise,
)
from api.services.periodization import params as periodization_params
from api.services.periodization.service import apply_decision
from api.services.volume.repository import guarded, utc_today
from api.services.volume.service import apply_volume_decision, refresh_volume_proposals
from app.database import SessionLocal

pytestmark = pytest.mark.asyncio


async def _seed_two_windows(db, user_id, block_id, plan_id, first_start):
    """Два подряд закрытых микроцикла по 4 дня, все дни выполнены."""
    for window in range(2):
        for offset in range(4):
            db.add(UserCalendarDay(
                app_user_id=user_id,
                target_date=first_start + timedelta(days=window * 4 + offset),
                block_id=block_id, plan_id=plan_id, day_tag="push",
                micro_tag="medium", meso_tag="medium",
                microcycle_day_number=offset + 1,
                is_rest_day=False, is_blackout=False, status="completed",
            ))
    # Следующее окно — будущее, ещё открытое.
    for offset in range(4):
        db.add(UserCalendarDay(
            app_user_id=user_id,
            target_date=first_start + timedelta(days=8 + offset),
            block_id=block_id, plan_id=plan_id, day_tag="push",
            micro_tag="medium", meso_tag="medium",
            microcycle_day_number=offset + 1,
            is_rest_day=False, is_blackout=False, status="planned",
        ))
    await db.commit()


async def test_refresh_creates_single_proposal_for_closed_window(
    db, test_user, active_block, seeded_plan
):
    profile = AppUserProfile(
        app_user_id=test_user.id, experience_level="intermediate",
        volume_budget={"weekly_targets": {"chest": {"target_sets": 12, "min_floor": 6}}},
    )
    db.add(profile)
    first_start = utc_today() - timedelta(days=9)
    await _seed_two_windows(db, test_user.id, active_block.id, seeded_plan.id, first_start)

    proposal = await refresh_volume_proposals(db, test_user.id, utc_today())
    await db.commit()

    assert proposal is not None
    assert proposal.kind == periodization_params.KIND_VOLUME_REVIEW
    assert proposal.status == periodization_params.STATUS_PENDING
    assert proposal.payload["adjustments"], "правки должны быть перечислены в payload"
    assert "window_id" in proposal.payload


async def test_second_pending_volume_review_supersedes_first_and_keeps_snapshot(
    db, test_user, active_block
):
    """P0-09 C2 (Critical): окно N+1 закрывается, пока карточка окна N ещё
    pending, — штатное недожидание решения (экран необязывающий, спека).

    До фикса вторая вставка pending volume_review валила flush()
    IntegrityError'ом: uq_periodization_proposals_pending уникален по
    (block_id, kind, COALESCE(payload->>'exercise_id','')), а у
    volume_review нет exercise_id в payload вовсе — ключ один и тот же для
    любого окна одного блока. guarded() ловил исключение, но откатывал ВЕСЬ
    SAVEPOINT — вместе с уже записанным close_window-снимком ВТОРОГО окна,
    хотя сам снимок был совершенно валиден. Пользователь навсегда застревал
    с исключением на каждом /workout-center/context.

    План намеренно НЕ бьёт по "chest" (единственной мышце с целью в
    бюджете): упражнение здесь на "quads", поэтому предписание следующего
    окна по груди всегда 0, и разрыв цели (`_prescription_gap` в decide.py)
    срабатывает на КАЖДОМ закрытии окна независимо от предыдущего —
    ровно то, что нужно, чтобы у ПЕРВОГО же закрытого окна была своя
    карточка (а не только у второго, как в остальных тестах файла, где
    план совпадает с целью бюджета и первое окно молчит).
    """
    profile = AppUserProfile(
        app_user_id=test_user.id, experience_level="intermediate",
        volume_budget={"weekly_targets": {"chest": {"target_sets": 12, "min_floor": 6}}},
    )
    db.add(profile)

    off_target_exercise = Exercise(
        name="C2 quads exercise", category="base", main_muscle_group="quads",
        difficulty="beginner", equipment_needed=[], source="custom",
        app_user_id=test_user.id,
    )
    db.add(off_target_exercise)
    await db.flush()
    plan = WorkoutPlan(
        app_user_id=test_user.id, name="C2 plan", day_tag="legs",
        micro_tag="medium", meso_tag="medium",
    )
    db.add(plan)
    await db.flush()
    db.add(WorkoutPlanExercise(
        plan_id=plan.id, exercise_id=off_target_exercise.id, order_index=0, target_sets=3,
    ))
    await db.flush()

    first_start = utc_today() - timedelta(days=11)
    await _seed_two_windows(db, test_user.id, active_block.id, plan.id, first_start)

    # Шаг 1: закрыть окно 0 (в этот момент открыто окно 1) — карточка A.
    proposal_a = await refresh_volume_proposals(
        db, test_user.id, first_start + timedelta(days=4)
    )
    await db.commit()
    assert proposal_a is not None
    assert proposal_a.status == periodization_params.STATUS_PENDING

    # Шаг 2: НЕ решаем по карточке A. Открывается будущее окно, закрывается
    # окно 1 — вторая pending-карточка volume_review для того же блока.
    # Оборачиваем в guarded() так же, как это делает build_context, — иначе
    # тест проверял бы не тот путь, на котором нашлась находка.
    proposal_b = await guarded(
        db,
        "test C2: refresh_volume_proposals — второе окно без решения по первому",
        refresh_volume_proposals(db, test_user.id, first_start + timedelta(days=8)),
    )
    await db.commit()

    assert proposal_b is not None, (
        "снимок и предложение второго окна не должны теряться из-за "
        "ещё не решённой карточки первого окна"
    )
    assert proposal_b.id != proposal_a.id
    assert proposal_b.status == periodization_params.STATUS_PENDING

    rows = (await db.execute(
        select(PeriodizationProposal).where(
            PeriodizationProposal.app_user_id == test_user.id,
            PeriodizationProposal.kind == periodization_params.KIND_VOLUME_REVIEW,
        )
    )).scalars().all()
    pending = [r for r in rows if r.status == periodization_params.STATUS_PENDING]
    assert len(pending) == 1
    assert pending[0].id == proposal_b.id

    refreshed_a = await db.get(PeriodizationProposal, proposal_a.id)
    assert refreshed_a.status == periodization_params.STATUS_EXPIRED

    # Снимок ВТОРОГО окна обязан пережить SAVEPOINT — это и есть регрессия:
    # до фикса guarded() откатывал его вместе с провалившейся вставкой карточки.
    snapshot_count = await db.scalar(
        select(func.count()).select_from(VolumeWindow).where(
            VolumeWindow.app_user_id == test_user.id,
            VolumeWindow.block_id == active_block.id,
        )
    )
    assert snapshot_count == 2


async def test_refresh_is_idempotent_for_the_same_window(
    db, test_user, active_block, seeded_plan
):
    db.add(AppUserProfile(
        app_user_id=test_user.id, experience_level="intermediate",
        volume_budget={"weekly_targets": {"chest": {"target_sets": 12, "min_floor": 6}}},
    ))
    first_start = utc_today() - timedelta(days=9)
    await _seed_two_windows(db, test_user.id, active_block.id, seeded_plan.id, first_start)

    await refresh_volume_proposals(db, test_user.id, utc_today())
    await db.commit()
    await refresh_volume_proposals(db, test_user.id, utc_today())
    await db.commit()

    rows = (await db.execute(
        select(PeriodizationProposal).where(
            PeriodizationProposal.app_user_id == test_user.id,
            PeriodizationProposal.kind == periodization_params.KIND_VOLUME_REVIEW,
        )
    )).scalars().all()
    assert len(rows) == 1


async def test_apply_budget_lever_moves_weekly_target(db, test_user, active_block):
    profile = AppUserProfile(
        app_user_id=test_user.id, experience_level="intermediate",
        volume_budget={"weekly_targets": {"chest": {"target_sets": 25, "min_floor": 6}}},
    )
    db.add(profile)
    proposal = PeriodizationProposal(
        app_user_id=test_user.id, block_id=active_block.id,
        kind=periodization_params.KIND_VOLUME_REVIEW,
        reason_code="above_mrv",
        payload={"window_id": None, "adjustments": [
            {"index": 0, "kind": "budget_to_range", "muscle": "chest",
             "reason_code": "above_mrv", "delta_sets": -7},
        ]},
        status=periodization_params.STATUS_PENDING,
    )
    db.add(proposal)
    await db.commit()

    result = await apply_volume_decision(
        db, test_user.id, proposal, "apply_volume", {"accepted": [0]}
    )
    await db.commit()
    await db.refresh(profile)

    assert result["status"] == "applied"
    assert profile.volume_budget["weekly_targets"]["chest"]["target_sets"] == 18


async def test_apply_budget_lever_does_not_collapse_target_on_long_microcycle(
    db, test_user
):
    """P0-09 I2 (Important): landmarks._TABLE — за 7 ДНЕЙ, а target_sets в
    бюджете уже смасштабирован под ФАКТИЧЕСКУЮ длину микроцикла блока
    (volume_calculator.clamp_target использует тот же cycle_multiplier). До
    фикса `_apply_budget` клампила результат против СЫРОГО lm.mrv — на
    десятидневном микроцикле легитимная цель схлопывалась бы до
    семидневного потолка при каждом принятии рычага.

    Chest/intermediate: raw mrv=18. На микроцикле 10 дней масштабированный
    потолок floor(18 * 10/7) = 25. Цель 24 (легитимно выше 18, но внутри
    масштабированного потолка) обязана пережить принятие рычага без
    изменений — рычаг здесь ничего не двигает (delta_sets=0), только
    клампит, и именно кламп — то, что проверяется.
    """
    block = TrainingBlock(
        phase_snapshot_trusted=True,
        app_user_id=test_user.id, block_index=1,
        phases=[{"phase_number": 1, "name": "medium", "effort_tier": "medium", "length_days": 10}],
        microcycle_length=10,
        start_date=utc_today() - timedelta(days=3),
        planned_end_date=utc_today() + timedelta(days=20),
        status="active",
    )
    db.add(block)
    profile = AppUserProfile(
        app_user_id=test_user.id, experience_level="intermediate",
        volume_budget={"weekly_targets": {"chest": {"target_sets": 24, "min_floor": 6}}},
    )
    db.add(profile)
    await db.commit()
    await db.refresh(block)

    proposal = PeriodizationProposal(
        app_user_id=test_user.id, block_id=block.id,
        kind=periodization_params.KIND_VOLUME_REVIEW,
        reason_code="above_mrv",
        payload={"window_id": None, "adjustments": [
            {"index": 0, "kind": "budget_to_range", "muscle": "chest",
             "reason_code": "above_mrv", "delta_sets": 0},
        ]},
        status=periodization_params.STATUS_PENDING,
    )
    db.add(proposal)
    await db.commit()

    result = await apply_volume_decision(
        db, test_user.id, proposal, "apply_volume", {"accepted": [0]}
    )
    await db.commit()
    await db.refresh(profile)

    assert result["status"] == "applied"
    target = profile.volume_budget["weekly_targets"]["chest"]["target_sets"]
    # До фикса кламп шёл против сырого mrv=18 и обрезал бы цель до 18.
    assert target == 24
    assert target > 18


async def test_apply_frequency_lever_scales_all_weekly_targets(
    db, test_user, active_block
):
    """Ревью Задачи 11, Important 2: до фикса `budget_to_frequency`
    (muscle=None, delta_sets=0) маршрутился в `_apply_budget`, которая
    отказывает немедленно на `not muscle` — рычаг «привести цель к реальной
    частоте» был непроходим НИ ПРИ КАКИХ данных. Проверяем, что теперь он
    маршрутится в `_apply_frequency` и реально масштабирует ВСЕ мышцы
    бюджета на наблюдаемую исполняемость (а не только названную в
    payload — payload у этого рычага мышцу не называет вовсе)."""
    profile = AppUserProfile(
        app_user_id=test_user.id, experience_level="intermediate",
        volume_budget={"weekly_targets": {
            "chest": {"target_sets": 20, "min_floor": 6},
            "lats": {"target_sets": 20, "min_floor": 6},
        }},
    )
    db.add(profile)
    proposal = PeriodizationProposal(
        app_user_id=test_user.id, block_id=active_block.id,
        kind=periodization_params.KIND_VOLUME_REVIEW,
        reason_code="adherence_gap",
        payload={"window_id": None, "adjustments": [
            {"index": 0, "kind": "budget_to_frequency", "muscle": None,
             "reason_code": "adherence_gap", "delta_sets": 0,
             "detail": {"ratios": [0.5, 0.6]}},
        ]},
        status=periodization_params.STATUS_PENDING,
    )
    db.add(proposal)
    await db.commit()

    result = await apply_volume_decision(
        db, test_user.id, proposal, "apply_volume", {"accepted": [0]}
    )
    await db.commit()
    await db.refresh(profile)

    # До фикса: applied == [] независимо от данных — это и была находка.
    assert result["applied"] == [0]
    assert result["status"] == "applied"
    weekly = profile.volume_budget["weekly_targets"]
    # observed = (0.5 + 0.6) / 2 = 0.55; floor(20 * 0.55) = 11, что попадает
    # в диапазон MEV..MRV для chest/lats на intermediate — клампа не видно.
    assert weekly["chest"]["target_sets"] == 11
    assert weekly["lats"]["target_sets"] == 11


async def test_apply_prescription_lever_writes_day_adjustments(
    db, test_user, active_block, seeded_plan, seeded_history
):
    db.add(AppUserProfile(
        app_user_id=test_user.id, experience_level="intermediate",
        volume_budget={"weekly_targets": {"chest": {"target_sets": 12, "min_floor": 6}}},
    ))
    future = utc_today() + timedelta(days=1)
    day = UserCalendarDay(
        app_user_id=test_user.id, target_date=future, block_id=active_block.id,
        plan_id=seeded_plan.id, day_tag="push", micro_tag="medium",
        meso_tag="medium", microcycle_day_number=1,
        is_rest_day=False, is_blackout=False, status="planned",
    )
    db.add(day)
    proposal = PeriodizationProposal(
        app_user_id=test_user.id, block_id=active_block.id,
        kind=periodization_params.KIND_VOLUME_REVIEW,
        reason_code="below_mev",
        payload={"window_id": None, "adjustments": [
            {"index": 0, "kind": "prescription_add", "muscle": "chest",
             "reason_code": "below_mev", "delta_sets": 2,
             "exercise_id": seeded_history.id, "day_id": None},
        ]},
        status=periodization_params.STATUS_PENDING,
    )
    db.add(proposal)
    await db.commit()
    await db.refresh(day)
    proposal.payload["adjustments"][0]["day_id"] = day.id

    await apply_volume_decision(
        db, test_user.id, proposal, "apply_volume", {"accepted": [0]}
    )
    await db.commit()
    await db.refresh(day)

    assert day.volume_adjustments == [
        {"exercise_id": seeded_history.id, "delta_sets": 2, "proposal_id": proposal.id}
    ]


async def test_apply_prescription_reresolves_when_frozen_day_already_completed(
    db, test_user, active_block, seeded_plan, seeded_history
):
    """P0-09 I1 (Important): `_pick_day_for` строила предложение по первому
    дню окна с нужной мышцей, не глядя на статус, — `_apply_prescription`
    же требует `status == "planned"`. Карточка не блокирующая: пользователь
    вполне может отработать день 1 (тот самый замороженный `day_id`) и
    только потом решить по предложению, назвавшему именно его. До фикса это
    значило тихий отказ ("declined"), хотя днём позже в том же окне лежит
    валидный день с той же главной мышцей. Проверяем, что правка теперь
    переразрешается на этот более поздний день, а не просто молча гибнет.
    """
    db.add(AppUserProfile(
        app_user_id=test_user.id, experience_level="intermediate",
        volume_budget={"weekly_targets": {"chest": {"target_sets": 12, "min_floor": 6}}},
    ))
    today = utc_today()
    # Маркер начала окна — нужен только current_window(), чтобы вообще
    # найти окно при переразрешении на apply-time. Без plan_id намеренно:
    # _pick_day_for отбирает дни с plan_id IS NOT NULL, и если бы у маркера
    # тоже был seeded_plan (chest), он сам оказался бы «более ранним
    # валидным днём» и замаскировал бы то, что проверяет этот тест.
    marker_day = UserCalendarDay(
        app_user_id=test_user.id, target_date=today, block_id=active_block.id,
        plan_id=None, day_tag="push", micro_tag="medium",
        meso_tag="medium", microcycle_day_number=1,
        is_rest_day=False, is_blackout=False, status="planned",
    )
    # Замороженный день — на момент постройки предложения был "planned",
    # но к моменту решения пользователь его уже отработал.
    frozen_day = UserCalendarDay(
        app_user_id=test_user.id, target_date=today + timedelta(days=1),
        block_id=active_block.id, plan_id=seeded_plan.id, day_tag="push",
        micro_tag="medium", meso_tag="medium", microcycle_day_number=2,
        is_rest_day=False, is_blackout=False, status="completed",
    )
    # Более поздний, всё ещё валидный день с той же главной мышцей (тот же
    # план — seeded_plan бьёт на "chest").
    later_day = UserCalendarDay(
        app_user_id=test_user.id, target_date=today + timedelta(days=2),
        block_id=active_block.id, plan_id=seeded_plan.id, day_tag="push",
        micro_tag="medium", meso_tag="medium", microcycle_day_number=3,
        is_rest_day=False, is_blackout=False, status="planned",
    )
    db.add_all([marker_day, frozen_day, later_day])
    await db.commit()
    await db.refresh(frozen_day)
    await db.refresh(later_day)

    proposal = PeriodizationProposal(
        app_user_id=test_user.id, block_id=active_block.id,
        kind=periodization_params.KIND_VOLUME_REVIEW,
        reason_code="below_mev",
        payload={"window_id": None, "adjustments": [
            {"index": 0, "kind": "prescription_add", "muscle": "chest",
             "reason_code": "below_mev", "delta_sets": 2,
             "exercise_id": seeded_history.id, "day_id": frozen_day.id},
        ]},
        status=periodization_params.STATUS_PENDING,
    )
    db.add(proposal)
    await db.commit()

    result = await apply_volume_decision(
        db, test_user.id, proposal, "apply_volume", {"accepted": [0]}
    )
    await db.commit()
    await db.refresh(frozen_day)
    await db.refresh(later_day)

    assert result["applied"] == [0]
    assert result["status"] == "applied"
    # Отработанный день не тронут — правка сама переехала на более поздний.
    assert frozen_day.volume_adjustments in (None, [])
    assert later_day.volume_adjustments == [
        {"exercise_id": seeded_history.id, "delta_sets": 2, "proposal_id": proposal.id}
    ]


async def test_apply_prescription_declines_honestly_when_no_valid_day_left(
    db, test_user, active_block, seeded_plan, seeded_history
):
    """Тот же сценарий устаревшего day_id, но БЕЗ более позднего валидного
    дня в окне — переразрешение обязано честно вернуть "declined", а не
    привязать правку к отработанному дню и не упасть."""
    db.add(AppUserProfile(
        app_user_id=test_user.id, experience_level="intermediate",
        volume_budget={"weekly_targets": {"chest": {"target_sets": 12, "min_floor": 6}}},
    ))
    today = utc_today()
    marker_day = UserCalendarDay(
        app_user_id=test_user.id, target_date=today, block_id=active_block.id,
        plan_id=seeded_plan.id, day_tag="push", micro_tag="medium",
        meso_tag="medium", microcycle_day_number=1,
        is_rest_day=False, is_blackout=False, status="completed",
    )
    frozen_day = UserCalendarDay(
        app_user_id=test_user.id, target_date=today + timedelta(days=1),
        block_id=active_block.id, plan_id=seeded_plan.id, day_tag="push",
        micro_tag="medium", meso_tag="medium", microcycle_day_number=2,
        is_rest_day=False, is_blackout=False, status="completed",
    )
    db.add_all([marker_day, frozen_day])
    await db.commit()
    await db.refresh(frozen_day)

    proposal = PeriodizationProposal(
        app_user_id=test_user.id, block_id=active_block.id,
        kind=periodization_params.KIND_VOLUME_REVIEW,
        reason_code="below_mev",
        payload={"window_id": None, "adjustments": [
            {"index": 0, "kind": "prescription_add", "muscle": "chest",
             "reason_code": "below_mev", "delta_sets": 2,
             "exercise_id": seeded_history.id, "day_id": frozen_day.id},
        ]},
        status=periodization_params.STATUS_PENDING,
    )
    db.add(proposal)
    await db.commit()

    result = await apply_volume_decision(
        db, test_user.id, proposal, "apply_volume", {"accepted": [0]}
    )
    await db.commit()
    await db.refresh(frozen_day)

    assert result["applied"] == []
    assert result["status"] == "declined"
    assert frozen_day.volume_adjustments in (None, [])


async def test_unaccepted_adjustments_are_not_applied(db, test_user, active_block):
    profile = AppUserProfile(
        app_user_id=test_user.id, experience_level="intermediate",
        volume_budget={"weekly_targets": {"chest": {"target_sets": 25, "min_floor": 6}}},
    )
    db.add(profile)
    proposal = PeriodizationProposal(
        app_user_id=test_user.id, block_id=active_block.id,
        kind=periodization_params.KIND_VOLUME_REVIEW, reason_code="above_mrv",
        payload={"window_id": None, "adjustments": [
            {"index": 0, "kind": "budget_to_range", "muscle": "chest",
             "reason_code": "above_mrv", "delta_sets": -7},
        ]},
        status=periodization_params.STATUS_PENDING,
    )
    db.add(proposal)
    await db.commit()

    await apply_volume_decision(
        db, test_user.id, proposal, "apply_volume", {"accepted": []}
    )
    await db.commit()
    await db.refresh(profile)

    assert profile.volume_budget["weekly_targets"]["chest"]["target_sets"] == 25


async def test_apply_decision_declines_when_nothing_accepted(
    db, test_user, active_block
):
    """Ревью Задачи 11, Important 4: путь через apply_decision (не напрямую
    через apply_volume_decision) — тот самый путь, которым реально ходит
    роутер, и тот самый, где обнаружился пропавший commit(). Все пять
    тестов выше коммитят сами и бьют в apply_volume_decision напрямую, так
    что этот commit() для них не мог провалиться незаметно."""
    profile = AppUserProfile(
        app_user_id=test_user.id, experience_level="intermediate",
        volume_budget={"weekly_targets": {"chest": {"target_sets": 25, "min_floor": 6}}},
    )
    db.add(profile)
    proposal = PeriodizationProposal(
        app_user_id=test_user.id, block_id=active_block.id,
        kind=periodization_params.KIND_VOLUME_REVIEW, reason_code="above_mrv",
        payload={"window_id": None, "adjustments": [
            {"index": 0, "kind": "budget_to_range", "muscle": "chest",
             "reason_code": "above_mrv", "delta_sets": -7},
        ]},
        status=periodization_params.STATUS_PENDING,
    )
    db.add(proposal)
    await db.commit()
    await db.refresh(proposal)
    proposal_id = proposal.id

    result = await apply_decision(
        db, test_user.id, proposal_id,
        periodization_params.ACTION_APPLY_VOLUME,
        options={"accepted": []},
    )
    assert result["applied"] == []
    assert result["status"] == "declined"

    # Ключевая проверка: читаем из СОВЕРШЕННО ДРУГОЙ сессии, не из `db`.
    # `db` держит тот же Python-объект `proposal` в identity map — его
    # .status уже выставлен в памяти вызовом apply_decision независимо от
    # того, добрался ли этот commit() реально до БД. Если бы явный
    # session.commit() внутри ветки KIND_VOLUME_REVIEW пропал (это и был
    # реальный баг ревью первого прохода), `db.refresh(proposal)` увидел бы
    # тот же pending, который откатился бы при закрытии сессии эндпоинтом,
    # а этот тест остался бы зелёным. Свежая сессия этого не прощает.
    async with SessionLocal() as fresh:
        reread = await fresh.get(PeriodizationProposal, proposal_id)
        assert reread is not None
        assert reread.status == periodization_params.STATUS_DECLINED
        assert reread.decided_action == periodization_params.ACTION_APPLY_VOLUME


async def test_apply_decision_persists_budget_mutation_via_fresh_session(
    db, test_user, active_block
):
    """Ревью Задачи 11, Important 4: непустой accepted на budget_to_range
    через apply_decision — и мутация профиля, и статус предложения должны
    пережить закрытие исходной сессии, не только остаться в identity map
    `db`."""
    profile = AppUserProfile(
        app_user_id=test_user.id, experience_level="intermediate",
        volume_budget={"weekly_targets": {"chest": {"target_sets": 25, "min_floor": 6}}},
    )
    db.add(profile)
    proposal = PeriodizationProposal(
        app_user_id=test_user.id, block_id=active_block.id,
        kind=periodization_params.KIND_VOLUME_REVIEW, reason_code="above_mrv",
        payload={"window_id": None, "adjustments": [
            {"index": 0, "kind": "budget_to_range", "muscle": "chest",
             "reason_code": "above_mrv", "delta_sets": -7},
        ]},
        status=periodization_params.STATUS_PENDING,
    )
    db.add(proposal)
    await db.commit()
    await db.refresh(proposal)
    proposal_id = proposal.id
    app_user_id = test_user.id

    result = await apply_decision(
        db, test_user.id, proposal_id,
        periodization_params.ACTION_APPLY_VOLUME,
        options={"accepted": [0]},
    )
    assert result["applied"] == [0]
    assert result["status"] == "applied"

    # Свежая сессия — та же логика, что и в тесте выше: identity map `db`
    # не доказывает, что commit() реально случился.
    async with SessionLocal() as fresh:
        reread_proposal = await fresh.get(PeriodizationProposal, proposal_id)
        assert reread_proposal.status == periodization_params.STATUS_ACCEPTED
        # AppUserProfile.id — суррогатный PK, app_user_id — лишь уникальный
        # FK; fresh.get() адресует по PK, поэтому здесь select(), а не get().
        reread_profile = (await fresh.execute(
            select(AppUserProfile).where(AppUserProfile.app_user_id == app_user_id)
        )).scalar_one()
        assert (
            reread_profile.volume_budget["weekly_targets"]["chest"]["target_sets"]
            == 18
        )
