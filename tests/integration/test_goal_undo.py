"""Откат применённого предложения (P0-12, Задача 10)."""
from datetime import date, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.orm.attributes import flag_modified

from api.services.goal.service import apply_goal_decision, undo_goal_decision
from api.services.models import (
    AppUserProfile,
    PeriodizationProposal,
    UserCalendarDay,
    UserExercisePreference,
    UserExerciseRepOverride,
)
from api.services.periodization import params as periodization_params
from api.services.periodization.service import apply_decision
from app.database import SessionLocal

pytestmark = pytest.mark.asyncio

# active_block берётся из tests/integration/conftest.py (Minor 4, ревью
# Задачи 10): та фикстура несёт при себе минимальный активный сплит
# Push/Pull (UserSplit/SplitBlueprint), без которого SchedulingEngine.
# generate_block_days видит пустую slots_queue и не кладёт в календарь ни
# одной строки — прежняя ЛОКАЛЬНАЯ фикстура этого файла (бare TrainingBlock)
# была той же ловушкой, которую Задача 9 уже убрала из test_goal_apply_
# structural.py: тесты регенерации над ней работали бы над пустым
# диапазоном и ничего не проверяли бы по-настоящему.


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_soft_lever_writes(test_user):
    """Снести преференции/оверрайды, которые тест навешал на fresh_exercise.

    Без этого teardown test_user падает ForeignKeyViolationError: он сносит
    Exercise до того, как что-то удалит ссылающиеся на неё
    UserExercisePreference/UserExerciseRepOverride (у их exercise_id нет
    ON DELETE CASCADE). Фикстура завязана на test_user, поэтому её teardown
    по LIFO гарантированно отрабатывает раньше teardown'а test_user (тот же
    приём, что в test_goal_apply_soft.py, Задача 8).
    """
    yield
    async with SessionLocal() as db:
        await db.execute(
            delete(UserExercisePreference).where(
                UserExercisePreference.app_user_id == test_user.id
            )
        )
        await db.execute(
            delete(UserExerciseRepOverride).where(
                UserExerciseRepOverride.app_user_id == test_user.id
            )
        )
        await db.commit()


async def test_undo_restores_previous_preference(test_user, fresh_exercise, active_block):
    async with SessionLocal() as db:
        proposal = PeriodizationProposal(
            app_user_id=test_user.id, block_id=active_block.id,
            kind=periodization_params.KIND_GOAL_PLAN, reason_code="lift_missing",
            payload={
                "goal_id": 1, "exercise_id": fresh_exercise.id,
                "levers": [{"index": 0, "kind": "ensure_present",
                            "reason_code": "lift_missing", "effect_slope": 0.2,
                            "effect_days": 9, "detail": {}}],
                "applied_snapshot": None,
            },
            status=periodization_params.STATUS_PENDING,
        )
        db.add(proposal)
        await db.commit()
        await db.refresh(proposal)
        await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0]},
        )
        await db.commit()
        result = await undo_goal_decision(db, test_user.id, proposal)
        await db.commit()

    assert result["status"] == "undone"
    async with SessionLocal() as db:
        prefs = (await db.execute(
            select(UserExercisePreference).where(
                UserExercisePreference.app_user_id == test_user.id
            )
        )).scalars().all()
    assert prefs == []


async def test_undo_is_blocked_after_fact_appears(
    test_user, fresh_exercise, active_block
):
    async with SessionLocal() as db:
        db.add(UserCalendarDay(
            app_user_id=test_user.id, target_date=date.today() + timedelta(days=1),
            block_id=active_block.id, day_tag="push", micro_tag="medium",
            meso_tag="medium", is_rest_day=False, is_blackout=False,
            status="completed", actual_workout_session_id=1,
        ))
        proposal = PeriodizationProposal(
            app_user_id=test_user.id, block_id=active_block.id,
            kind=periodization_params.KIND_GOAL_PLAN, reason_code="pace_behind",
            payload={
                "goal_id": 1, "exercise_id": fresh_exercise.id,
                "levers": [],
                "applied_snapshot": {
                    "preference": None, "rep_override": None, "scheme": None,
                    "days": [{"target_date": (date.today() + timedelta(days=1)).isoformat(),
                              "plan_id": None, "day_tag": "push", "micro_tag": "medium",
                              "meso_tag": "medium", "mesocycle_phase_number": None,
                              "is_rest_day": False,
                              # touched=True: этот день числится РЕАЛЬНО тронутым
                              # регенерацией на момент применения — именно
                              # такой день и обязан блокировать откат, когда на
                              # нём позже появляется факт (Finding 2, ревью
                              # Задачи 10 — гейт теперь смотрит только на
                              # touched=True, см. undo_goal_decision).
                              "touched": True}],
                    "created_plan_ids": [],
                },
            },
            status=periodization_params.STATUS_ACCEPTED,
        )
        db.add(proposal)
        await db.commit()
        await db.refresh(proposal)
        result = await undo_goal_decision(db, test_user.id, proposal)

    assert result["status"] == "conflict"
    assert "факт" in result["reason"].lower()


async def test_undo_keeps_manual_changes(test_user, fresh_exercise, active_block):
    """Пользователь после применения сам сменил преференцию — откат её не трогает."""
    async with SessionLocal() as db:
        proposal = PeriodizationProposal(
            app_user_id=test_user.id, block_id=active_block.id,
            kind=periodization_params.KIND_GOAL_PLAN, reason_code="lift_missing",
            payload={
                "goal_id": 1, "exercise_id": fresh_exercise.id,
                "levers": [{"index": 0, "kind": "ensure_present",
                            "reason_code": "lift_missing", "effect_slope": 0.2,
                            "effect_days": 9, "detail": {}}],
                "applied_snapshot": None,
            },
            status=periodization_params.STATUS_PENDING,
        )
        db.add(proposal)
        await db.commit()
        await db.refresh(proposal)
        await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0]},
        )
        await db.commit()

        pref = (await db.execute(
            select(UserExercisePreference).where(
                UserExercisePreference.app_user_id == test_user.id
            )
        )).scalar_one()
        pref.preference = "disliked"
        await db.commit()

        result = await undo_goal_decision(db, test_user.id, proposal)
        await db.commit()

    assert "preference" in result["kept"]
    async with SessionLocal() as db:
        pref = (await db.execute(
            select(UserExercisePreference).where(
                UserExercisePreference.app_user_id == test_user.id
            )
        )).scalar_one()
    assert pref.preference == "disliked"


async def test_undo_keeps_manually_diverged_rep_range_and_scheme(
    test_user, fresh_exercise, active_block
):
    """Finding 3 (ревью Задачи 10, Important): §5.5 требует ту же дисциплину
    сравнения "текущее значение против того, что записал автопилот", что
    раньше была реализована только для преференции. Диапазон повторов и
    схема прогрессии восстанавливались БЕЗУСЛОВНО — правка, которую
    пользователь внёс сам ПОСЛЕ применения, молча терялась при откате.
    Автопилот записал rep 2-5 и схему "5x5" (см. levers ниже); пользователь
    правит оба носителя на другие значения — откат обязан оставить их как
    есть и назвать оба в kept, не восстанавливая поверх.
    """
    async with SessionLocal() as db:
        db.add(AppUserProfile(app_user_id=test_user.id))
        await db.commit()

    levers = [
        {"index": 0, "kind": "rep_range", "reason_code": "pace_behind",
         "effect_slope": 0.1, "effect_days": 5, "detail": {"rep_min": 2, "rep_max": 5}},
        {"index": 1, "kind": "scheme", "reason_code": "pace_behind",
         "effect_slope": 0.15, "effect_days": 7, "detail": {"to_scheme": "5x5"}},
    ]
    async with SessionLocal() as db:
        proposal = PeriodizationProposal(
            app_user_id=test_user.id, block_id=active_block.id,
            kind=periodization_params.KIND_GOAL_PLAN, reason_code="pace_behind",
            payload={
                "goal_id": 1, "exercise_id": fresh_exercise.id,
                "levers": levers, "applied_snapshot": None,
            },
            status=periodization_params.STATUS_PENDING,
        )
        db.add(proposal)
        await db.commit()
        await db.refresh(proposal)

        result = await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0, 1]},
        )
        await db.commit()
        pid = proposal.id
    assert result["applied"] == [0, 1]

    # Пользователь правит ОБА носителя ПОСЛЕ применения — теперь они
    # разошлись с тем, что записал автопилот (2-5 и "5x5").
    async with SessionLocal() as db:
        override = (await db.execute(
            select(UserExerciseRepOverride).where(
                UserExerciseRepOverride.app_user_id == test_user.id,
                UserExerciseRepOverride.exercise_id == fresh_exercise.id,
            )
        )).scalar_one()
        override.rep_min, override.rep_max = 6, 10

        profile = (await db.execute(
            select(AppUserProfile).where(AppUserProfile.app_user_id == test_user.id)
        )).scalar_one()
        settings = dict(profile.settings or {})
        settings["progression"]["overrides"][str(fresh_exercise.id)] = "3x8"
        profile.settings = settings
        flag_modified(profile, "settings")
        await db.commit()

    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, pid)
        undo_result = await undo_goal_decision(db, test_user.id, proposal)
        await db.commit()

    assert set(undo_result["kept"]) >= {"rep_override", "scheme"}

    async with SessionLocal() as db:
        override = (await db.execute(
            select(UserExerciseRepOverride).where(
                UserExerciseRepOverride.app_user_id == test_user.id,
                UserExerciseRepOverride.exercise_id == fresh_exercise.id,
            )
        )).scalar_one()
        profile = (await db.execute(
            select(AppUserProfile).where(AppUserProfile.app_user_id == test_user.id)
        )).scalar_one()
    assert (override.rep_min, override.rep_max) == (6, 10)
    assert profile.settings["progression"]["overrides"][str(fresh_exercise.id)] == "3x8"


async def test_undo_removes_only_own_volume_adjustment_keeps_volume_review(
    test_user, fresh_exercise, active_block
):
    """Обязательный тест из спеки — сосуществование с P0-09: откат обязан
    снять ТОЛЬКО свою запись в volume_adjustments дня и оставить нетронутой
    запись, которую туда положило РЕШЕНИЕ ОБЗОРА ОБЪЁМА (volume_review) —
    тот же формат {exercise_id, delta_sets, proposal_id}, что кладёт
    api/services/volume/service (см. её _apply_frozen_or_pick)."""
    future = date.today() + timedelta(days=3)
    async with SessionLocal() as db:
        volume_review_proposal = PeriodizationProposal(
            app_user_id=test_user.id, block_id=active_block.id,
            kind=periodization_params.KIND_VOLUME_REVIEW, reason_code="over_mrv",
            payload={"adjustments": [], "window_id": 1},
            status=periodization_params.STATUS_ACCEPTED,
        )
        db.add(volume_review_proposal)

        goal_proposal = PeriodizationProposal(
            app_user_id=test_user.id, block_id=active_block.id,
            kind=periodization_params.KIND_GOAL_PLAN, reason_code="pace_behind",
            payload={
                "goal_id": 1, "exercise_id": fresh_exercise.id,
                "levers": [{"index": 0, "kind": "sets", "reason_code": "pace_behind",
                            "effect_slope": 0.1, "effect_days": 14,
                            "detail": {"delta_sets": 2}}],
                "applied_snapshot": None,
            },
            status=periodization_params.STATUS_PENDING,
        )
        db.add(goal_proposal)

        day = UserCalendarDay(
            app_user_id=test_user.id, target_date=future,
            block_id=active_block.id, status="planned",
        )
        db.add(day)
        await db.commit()
        await db.refresh(volume_review_proposal)
        await db.refresh(goal_proposal)
        await db.refresh(day)
        volume_review_pid = volume_review_proposal.id
        goal_pid = goal_proposal.id
        day_id = day.id

        result = await apply_goal_decision(
            db, test_user.id, goal_proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0]},
        )
        await db.commit()
    assert result["applied"] == [0]

    # Волюм-ревью P0-09 кладёт СВОЮ запись на тот же день, тем же форматом,
    # что и goal-автопилот, но со своим proposal_id.
    async with SessionLocal() as db:
        day = await db.get(UserCalendarDay, day_id)
        adjustments = list(day.volume_adjustments or [])
        adjustments.append({
            "exercise_id": fresh_exercise.id, "delta_sets": 1,
            "proposal_id": volume_review_pid,
        })
        day.volume_adjustments = adjustments
        flag_modified(day, "volume_adjustments")
        await db.commit()

    async with SessionLocal() as db:
        goal_proposal = await db.get(PeriodizationProposal, goal_pid)
        undo_result = await undo_goal_decision(db, test_user.id, goal_proposal)
        await db.commit()
    assert undo_result["status"] == "undone"

    async with SessionLocal() as db:
        day = await db.get(UserCalendarDay, day_id)
    assert day.volume_adjustments == [
        {"exercise_id": fresh_exercise.id, "delta_sets": 1, "proposal_id": volume_review_pid}
    ]


async def test_dispatcher_applies_structural_lever_and_allows_undo(
    test_user, fresh_exercise, active_block
):
    """Обязательный тест из спеки — сквозной путь через диспетчер
    (periodization.service.apply_decision), а не прямые вызовы
    apply_goal_decision/undo_goal_decision. Это ровно то, что поймало бы
    Finding 1: раньше decided-поля (status/decided_action/client_uuid/
    decided_at) выставлялись ТОЛЬКО ПОСЛЕ return apply_goal_decision, хотя
    структурный рычаг доходит до SchedulingEngine.generate_block_days,
    которая коммитит сессию сама (см. докстринг _apply_structural)."""
    async with SessionLocal() as db:
        proposal = PeriodizationProposal(
            app_user_id=test_user.id, block_id=active_block.id,
            kind=periodization_params.KIND_GOAL_PLAN, reason_code="pace_behind",
            payload={
                "goal_id": 1, "exercise_id": fresh_exercise.id,
                "levers": [{"index": 0, "kind": "lift_frequency",
                            "reason_code": "pace_behind", "effect_slope": 0.3,
                            "effect_days": 12, "detail": {"delta_sessions": 1}}],
                "applied_snapshot": None,
            },
            status=periodization_params.STATUS_PENDING,
        )
        db.add(proposal)
        await db.commit()
        await db.refresh(proposal)
        pid = proposal.id

        apply_result = await apply_decision(
            db, test_user.id, pid, periodization_params.ACTION_APPLY_GOAL,
            client_uuid="apply-1", options={"accepted": [0]},
        )
    assert apply_result["status"] == "applied"
    assert apply_result["applied"] == [0]

    async with SessionLocal() as db:
        row = await db.get(PeriodizationProposal, pid)
    assert row.status == periodization_params.STATUS_ACCEPTED
    assert row.decided_action == periodization_params.ACTION_APPLY_GOAL
    assert row.client_uuid == "apply-1"
    assert row.decided_at is not None
    assert row.payload["applied_snapshot"]["structural_applied"] is True

    async with SessionLocal() as db:
        undo_result = await apply_decision(
            db, test_user.id, pid, periodization_params.ACTION_UNDO_GOAL,
            client_uuid="undo-1",
        )
    assert undo_result["status"] == "undone"

    async with SessionLocal() as db:
        row = await db.get(PeriodizationProposal, pid)
    assert row.status == periodization_params.STATUS_UNDONE
    assert row.decided_action == periodization_params.ACTION_UNDO_GOAL


async def test_undo_not_blocked_by_untouched_completed_day(
    test_user, fresh_exercise, active_block
):
    """Finding 2 (ревью Задачи 10, Critical 2), воспроизведённое ревью
    буквально: пять посеянных будущих дней, один уже completed —
    регенерация его щадит (_wipe_future_calendar), _apply_structural
    записывает для него touched=False в снимке. Откат остальных, РЕАЛЬНО
    тронутых регенерацией дней, обязан пройти — а не блокироваться
    навсегда тем, что этот ОДИН нетронутый день несёт факт."""
    async with SessionLocal() as db:
        for i in range(5):
            db.add(UserCalendarDay(
                app_user_id=test_user.id,
                target_date=date.today() + timedelta(days=i + 1),
                block_id=active_block.id, day_tag="push",
                micro_tag="medium", meso_tag="medium",
                is_rest_day=False, is_blackout=False,
                status="completed" if i == 2 else "planned",
            ))
        proposal = PeriodizationProposal(
            app_user_id=test_user.id, block_id=active_block.id,
            kind=periodization_params.KIND_GOAL_PLAN, reason_code="pace_behind",
            payload={
                "goal_id": 1, "exercise_id": fresh_exercise.id,
                "levers": [{"index": 0, "kind": "lift_frequency",
                            "reason_code": "pace_behind", "effect_slope": 0.3,
                            "effect_days": 12, "detail": {"delta_sessions": 1}}],
                "applied_snapshot": None,
            },
            status=periodization_params.STATUS_PENDING,
        )
        db.add(proposal)
        await db.commit()
        await db.refresh(proposal)

        result = await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0]},
        )
        await db.commit()
        pid = proposal.id
    assert result["applied"] == [0]
    assert result["skipped_days"] >= 1

    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, pid)
        snapshot = proposal.payload["applied_snapshot"]
    # Ровно одна запись снимка помечена нетронутой — тот самый completed день.
    assert sum(1 for d in snapshot["days"] if not d["touched"]) == 1

    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, pid)
        undo_result = await undo_goal_decision(db, test_user.id, proposal)
        await db.commit()

    assert undo_result["status"] == "undone"


async def test_undo_restores_full_calendar_including_recreated_day(
    test_user, fresh_exercise, active_block
):
    """Обязательный тест из спеки — полное восстановление: применяем
    структурный рычаг, откатываем, координаты возвращаются к исходным —
    включая день, который регенерация УДАЛИЛА и не пересоздала (день за
    planned_end_date блока: _wipe_future_calendar сносит диапазон БЕЗ
    верхней границы, а _generate_future_calendar кладёт дни только ДО
    planned_end_date, см. их докстринги в periodization/service.py).
    test_undo_is_blocked_after_fact_appears выше до кода восстановления
    (шаг 5 undo_goal_decision) вообще не доходит — возвращается через
    ранний conflict; здесь он действительно исполняется."""
    original = {
        "day_tag": "push", "micro_tag": "medium", "meso_tag": "medium",
        "is_rest_day": False, "plan_id": None, "mesocycle_phase_number": None,
    }
    near_dates = [date.today() + timedelta(days=i) for i in range(1, 6)]
    far_date = date.today() + timedelta(days=30)  # заведомо за planned_end_date блока
    all_dates = near_dates + [far_date]

    async with SessionLocal() as db:
        for d in all_dates:
            db.add(UserCalendarDay(
                app_user_id=test_user.id, target_date=d,
                block_id=active_block.id, status="planned", is_blackout=False,
                **original,
            ))
        proposal = PeriodizationProposal(
            app_user_id=test_user.id, block_id=active_block.id,
            kind=periodization_params.KIND_GOAL_PLAN, reason_code="pace_behind",
            payload={
                "goal_id": 1, "exercise_id": fresh_exercise.id,
                "levers": [{"index": 0, "kind": "lift_frequency",
                            "reason_code": "pace_behind", "effect_slope": 0.3,
                            "effect_days": 12, "detail": {"delta_sessions": 1}}],
                "applied_snapshot": None,
            },
            status=periodization_params.STATUS_PENDING,
        )
        db.add(proposal)
        await db.commit()
        await db.refresh(proposal)

        result = await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0]},
        )
        await db.commit()
        pid = proposal.id
    assert result["applied"] == [0]

    # Регенерация действительно прошла: far_date исчез вовсе (за
    # planned_end_date регенерация уже не кладёт дней).
    async with SessionLocal() as db:
        far_day = (await db.execute(
            select(UserCalendarDay).where(
                UserCalendarDay.app_user_id == test_user.id,
                UserCalendarDay.target_date == far_date,
            )
        )).scalar_one_or_none()
    assert far_day is None, "день за planned_end_date должен был исчезнуть при wipe"

    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, pid)
        undo_result = await undo_goal_decision(db, test_user.id, proposal)
        await db.commit()
    assert undo_result["status"] == "undone"

    async with SessionLocal() as db:
        rows = (await db.execute(
            select(UserCalendarDay).where(
                UserCalendarDay.app_user_id == test_user.id,
                UserCalendarDay.target_date.in_(all_dates),
            )
        )).scalars().all()
    by_date = {r.target_date: r for r in rows}
    assert set(by_date) == set(all_dates), "все прежние дни, включая удалённый, обязаны вернуться"
    for d in all_dates:
        row = by_date[d]
        assert row.day_tag == original["day_tag"]
        assert row.micro_tag == original["micro_tag"]
        assert row.meso_tag == original["meso_tag"]
        assert row.is_rest_day == original["is_rest_day"]
        assert row.plan_id == original["plan_id"]
        assert row.mesocycle_phase_number == original["mesocycle_phase_number"]


async def test_undo_leaves_untouched_scheme_alone_even_if_value_matches_lever(
    test_user, fresh_exercise, active_block
):
    """Finding 1 (ревью Задачи 10 Task-10, Critical): владение носителем
    решает ПРИСУТСТВИЕ ключа в applied_snapshot, а не совпадение текущего
    значения с тем, что предложил бы рычаг. Ревью воспроизвело буквально: в
    payload["levers"] лежат ОБА кандидата — схемный (decide.py всегда
    предлагает "percent_1rm" для тяжёлых базовых, детерминированно) и
    диапазон повторов, — но пользователь принял только диапазон (accepted=
    [1]); схемный рычаг НИКОГДА не применялся, и applied_snapshot несёт
    ключ "rep_override", но не несёт ключа "scheme". Оверрайд схемы, который
    уже стоял на "percent_1rm" (поставлен СОВСЕМ ДРУГИМ актором — не этим
    предложением), совпадает со значением, которое предложил бы схемный
    рычаг. Старый код сравнивал именно это значение и удалял оверрайд как
    «свой» — откат обязан оставить его нетронутым."""
    async with SessionLocal() as db:
        db.add(AppUserProfile(
            app_user_id=test_user.id,
            settings={"progression": {"overrides": {str(fresh_exercise.id): "percent_1rm"}}},
        ))
        await db.commit()

    levers = [
        {"index": 0, "kind": "scheme", "reason_code": "pace_behind",
         "effect_slope": 0.15, "effect_days": 7, "detail": {"to_scheme": "percent_1rm"}},
        {"index": 1, "kind": "rep_range", "reason_code": "pace_behind",
         "effect_slope": 0.1, "effect_days": 5, "detail": {"rep_min": 2, "rep_max": 5}},
    ]
    async with SessionLocal() as db:
        proposal = PeriodizationProposal(
            app_user_id=test_user.id, block_id=active_block.id,
            kind=periodization_params.KIND_GOAL_PLAN, reason_code="pace_behind",
            payload={
                "goal_id": 1, "exercise_id": fresh_exercise.id,
                "levers": levers, "applied_snapshot": None,
            },
            status=periodization_params.STATUS_PENDING,
        )
        db.add(proposal)
        await db.commit()
        await db.refresh(proposal)
        pid = proposal.id

        # Принят ТОЛЬКО диапазон повторов (индекс 1) — схемный рычаг
        # (индекс 0) в accepted нет и никогда не применялся.
        result = await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [1]},
        )
        await db.commit()
    assert result["applied"] == [1]

    async with SessionLocal() as db:
        row = await db.get(PeriodizationProposal, pid)
        snapshot = row.payload["applied_snapshot"]
    assert "rep_override" in snapshot
    assert "scheme" not in snapshot, "схемный рычаг не применялся — снимок не должен нести его ключ"

    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, pid)
        undo_result = await undo_goal_decision(db, test_user.id, proposal)
        await db.commit()
    assert undo_result["status"] == "undone"
    assert "scheme" not in undo_result["kept"], "непринятый рычаг откат вообще не должен упоминать"

    async with SessionLocal() as db:
        profile = (await db.execute(
            select(AppUserProfile).where(AppUserProfile.app_user_id == test_user.id)
        )).scalar_one()
    assert profile.settings["progression"]["overrides"][str(fresh_exercise.id)] == "percent_1rm", (
        "оверрайд, поставленный не этим предложением, откат не имеет права снимать"
    )


async def test_undo_removes_freshly_created_rep_override_and_scheme(
    test_user, fresh_exercise, active_block
):
    """Ключ в snapshot записан как None (носителя не было до применения) —
    откат обязан удалить и строку оверрайда диапазона повторов, и запись
    схемы в overrides, а не просто оставить их."""
    async with SessionLocal() as db:
        db.add(AppUserProfile(app_user_id=test_user.id))
        await db.commit()

    levers = [
        {"index": 0, "kind": "rep_range", "reason_code": "pace_behind",
         "effect_slope": 0.1, "effect_days": 5, "detail": {"rep_min": 2, "rep_max": 5}},
        {"index": 1, "kind": "scheme", "reason_code": "pace_behind",
         "effect_slope": 0.15, "effect_days": 7, "detail": {"to_scheme": "5x5"}},
    ]
    async with SessionLocal() as db:
        proposal = PeriodizationProposal(
            app_user_id=test_user.id, block_id=active_block.id,
            kind=periodization_params.KIND_GOAL_PLAN, reason_code="pace_behind",
            payload={
                "goal_id": 1, "exercise_id": fresh_exercise.id,
                "levers": levers, "applied_snapshot": None,
            },
            status=periodization_params.STATUS_PENDING,
        )
        db.add(proposal)
        await db.commit()
        await db.refresh(proposal)
        pid = proposal.id

        result = await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0, 1]},
        )
        await db.commit()
    assert result["applied"] == [0, 1]

    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, pid)
        undo_result = await undo_goal_decision(db, test_user.id, proposal)
        await db.commit()
    assert undo_result["status"] == "undone"
    assert undo_result["kept"] == []

    async with SessionLocal() as db:
        override = (await db.execute(
            select(UserExerciseRepOverride).where(
                UserExerciseRepOverride.app_user_id == test_user.id,
                UserExerciseRepOverride.exercise_id == fresh_exercise.id,
            )
        )).scalar_one_or_none()
        profile = (await db.execute(
            select(AppUserProfile).where(AppUserProfile.app_user_id == test_user.id)
        )).scalar_one()
    assert override is None
    assert str(fresh_exercise.id) not in profile.settings.get("progression", {}).get("overrides", {})


async def test_undo_restores_preexisting_rep_override_and_scheme_values(
    test_user, fresh_exercise, active_block
):
    """Ключ в snapshot записан НЕ как None (носитель уже нёс значение до
    применения) — откат обязан вернуть именно это прежнее значение, а не
    просто удалить носителя."""
    async with SessionLocal() as db:
        db.add(AppUserProfile(
            app_user_id=test_user.id,
            settings={"progression": {"overrides": {str(fresh_exercise.id): "3x8"}}},
        ))
        db.add(UserExerciseRepOverride(
            app_user_id=test_user.id, exercise_id=fresh_exercise.id,
            rep_min=6, rep_max=10,
        ))
        await db.commit()

    levers = [
        {"index": 0, "kind": "rep_range", "reason_code": "pace_behind",
         "effect_slope": 0.1, "effect_days": 5, "detail": {"rep_min": 2, "rep_max": 5}},
        {"index": 1, "kind": "scheme", "reason_code": "pace_behind",
         "effect_slope": 0.15, "effect_days": 7, "detail": {"to_scheme": "5x5"}},
    ]
    async with SessionLocal() as db:
        proposal = PeriodizationProposal(
            app_user_id=test_user.id, block_id=active_block.id,
            kind=periodization_params.KIND_GOAL_PLAN, reason_code="pace_behind",
            payload={
                "goal_id": 1, "exercise_id": fresh_exercise.id,
                "levers": levers, "applied_snapshot": None,
            },
            status=periodization_params.STATUS_PENDING,
        )
        db.add(proposal)
        await db.commit()
        await db.refresh(proposal)
        pid = proposal.id

        result = await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0, 1]},
        )
        await db.commit()
    assert result["applied"] == [0, 1]

    # Применение действительно переписало носителей на новые значения.
    async with SessionLocal() as db:
        override = (await db.execute(
            select(UserExerciseRepOverride).where(
                UserExerciseRepOverride.app_user_id == test_user.id,
                UserExerciseRepOverride.exercise_id == fresh_exercise.id,
            )
        )).scalar_one()
        profile = (await db.execute(
            select(AppUserProfile).where(AppUserProfile.app_user_id == test_user.id)
        )).scalar_one()
    assert (override.rep_min, override.rep_max) == (2, 5)
    assert profile.settings["progression"]["overrides"][str(fresh_exercise.id)] == "5x5"

    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, pid)
        undo_result = await undo_goal_decision(db, test_user.id, proposal)
        await db.commit()
    assert undo_result["status"] == "undone"
    assert undo_result["kept"] == []

    async with SessionLocal() as db:
        override = (await db.execute(
            select(UserExerciseRepOverride).where(
                UserExerciseRepOverride.app_user_id == test_user.id,
                UserExerciseRepOverride.exercise_id == fresh_exercise.id,
            )
        )).scalar_one()
        profile = (await db.execute(
            select(AppUserProfile).where(AppUserProfile.app_user_id == test_user.id)
        )).scalar_one()
    assert (override.rep_min, override.rep_max) == (6, 10)
    assert profile.settings["progression"]["overrides"][str(fresh_exercise.id)] == "3x8"


async def test_undo_retry_same_client_uuid_returns_already_applied(
    test_user, fresh_exercise, active_block
):
    """Finding 2 (ревью Задачи 10 Task-10, Important): контракт модуля
    обещает, что повтор с тем же client_uuid возвращает результат первого
    решения, а мобильный клиент офлайн-первый с очередью повторов. Ни
    ветка диспетчера, ни undo_goal_decision раньше не проставляли
    proposal.client_uuid при отмене — после apply -> undo -> повтор ТОЙ ЖЕ
    отмены строка всё ещё несла client_uuid от apply, и верхний гейт
    apply_decision отвечал conflict вместо already_applied."""
    async with SessionLocal() as db:
        proposal = PeriodizationProposal(
            app_user_id=test_user.id, block_id=active_block.id,
            kind=periodization_params.KIND_GOAL_PLAN, reason_code="lift_missing",
            payload={
                "goal_id": 1, "exercise_id": fresh_exercise.id,
                "levers": [{"index": 0, "kind": "ensure_present",
                            "reason_code": "lift_missing", "effect_slope": 0.2,
                            "effect_days": 9, "detail": {}}],
                "applied_snapshot": None,
            },
            status=periodization_params.STATUS_PENDING,
        )
        db.add(proposal)
        await db.commit()
        await db.refresh(proposal)
        pid = proposal.id

        await apply_decision(
            db, test_user.id, pid, periodization_params.ACTION_APPLY_GOAL,
            client_uuid="apply-1", options={"accepted": [0]},
        )

    async with SessionLocal() as db:
        first_undo = await apply_decision(
            db, test_user.id, pid, periodization_params.ACTION_UNDO_GOAL,
            client_uuid="undo-1",
        )
    assert first_undo["status"] == "undone"

    async with SessionLocal() as db:
        row = await db.get(PeriodizationProposal, pid)
    assert row.client_uuid == "undo-1"
    assert row.status == periodization_params.STATUS_UNDONE

    # Повтор ТОЙ ЖЕ отмены (тот же client_uuid) — already_applied, не conflict.
    async with SessionLocal() as db:
        retry = await apply_decision(
            db, test_user.id, pid, periodization_params.ACTION_UNDO_GOAL,
            client_uuid="undo-1",
        )
    assert retry["status"] == "already_applied"
    assert retry["proposal_id"] == pid

    # Иной client_uuid на уже решённом (undone) предложении — по-прежнему
    # conflict: гейт не должен слабеть до "любой undo на undone проходит".
    async with SessionLocal() as db:
        different = await apply_decision(
            db, test_user.id, pid, periodization_params.ACTION_UNDO_GOAL,
            client_uuid="undo-2",
        )
    assert different["status"] == "conflict"
