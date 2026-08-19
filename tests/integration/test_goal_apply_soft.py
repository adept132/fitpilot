"""Мягкие рычаги: применение, снимок, выборочность (P0-12, Задача 8).

apply_goal_decision НЕ коммитит сессию сама (ревью Задачи 8, Critical 1) —
контракт совпадает с volume.apply_volume_decision: только flush(). Реальный
вызывающий (periodization.service.apply_decision, Задача 10) проставляет
proposal.status/decided_action/client_uuid/decided_at и коммитит ОДИН раз,
атомарно, вместе с записями рычагов. Двухблочный стиль тестов ниже
(`async with SessionLocal() as db: ...`) — деталь харнесса, а не то, что
диктует семантику сервиса, поэтому каждый блок, вызывающий
apply_goal_decision и желающий увидеть эффект в следующем блоке, коммитит
явно — ровно так, как это будет делать настоящий вызывающий.
"""
import uuid
from datetime import date, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from api.services.goal.service import apply_goal_decision
from api.services.models import (
    AppUserProfile,
    Exercise,
    PeriodizationProposal,
    UserCalendarDay,
    UserExercisePreference,
    UserExerciseRepOverride,
    WorkoutPlan,
    WorkoutPlanExercise,
)
from api.services.periodization import params as periodization_params
from app.database import SessionLocal

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_soft_lever_writes(test_user):
    """Снести преференции/оверрайды, которые тест навешал на fresh_exercise.

    Без этого teardown test_user падает ForeignKeyViolationError: он сносит
    Exercise до того, как что-то удалит ссылающиеся на неё
    UserExercisePreference/UserExerciseRepOverride (у их exercise_id нет
    ON DELETE CASCADE). Фикстура завязана на test_user, поэтому её teardown
    по LIFO гарантированно отрабатывает раньше teardown'а test_user.
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


@pytest_asyncio.fixture
async def active_block(test_user):
    from api.services.models import TrainingBlock
    async with SessionLocal() as db:
        block = TrainingBlock(
            app_user_id=test_user.id, block_index=1, phases=[],
            microcycle_length=7, start_date=date.today(),
            planned_end_date=date.today() + timedelta(days=28), status="active",
        )
        db.add(block)
        await db.commit()
        await db.refresh(block)
        yield block


async def _exercise_with_plan(test_user, target_sets: int = 3):
    """Упражнение на грудь ("chest" — резолвится to_system_key напрямую, см.
    api/services/muscle_keys.py) + план, в котором оно стоит. Нужно тестам
    рычага LEVER_SETS (финальное ревью, Critical 2): _apply_sets теперь
    трогает ТОЛЬКО дни, чей UserCalendarDay.plan_id ведёт на план с ЭТИМ
    упражнением (join через WorkoutPlanExercise) — день без плана вообще не
    попадёт в выборку. fresh_exercise (conftest.py) не годится: её
    main_muscle_group == "back" не резолвится ни одним ключом landmarks
    (см. api/services/muscle_keys.py — "back" нет ни в _SYSTEM_KEYS, ни в
    RU_TO_KEY, ни в _EXTRA_TO_KEY), и headroom всегда был бы 0.
    """
    marker = uuid.uuid4().hex[:8]
    async with SessionLocal() as db:
        ex = Exercise(
            name=f"Жим для sets-рычага {marker}",
            category="base",
            main_muscle_group="chest",
            difficulty="beginner",
            equipment_needed=[],
            source="custom",
            app_user_id=test_user.id,
        )
        db.add(ex)
        await db.flush()

        plan = WorkoutPlan(
            app_user_id=test_user.id, name=f"План {marker}",
            day_tag="push", micro_tag="medium", meso_tag="medium",
        )
        db.add(plan)
        await db.flush()
        db.add(WorkoutPlanExercise(
            plan_id=plan.id, exercise_id=ex.id, target_sets=target_sets, order_index=0,
        ))
        await db.commit()
        await db.refresh(ex)
        await db.refresh(plan)
        return ex, plan


_DEFAULT_LEVERS = [
    {"index": 0, "kind": "ensure_present", "reason_code": "lift_missing",
     "effect_slope": 0.2, "effect_days": 10, "detail": {}},
    {"index": 1, "kind": "rep_range", "reason_code": "pace_behind",
     "effect_slope": 0.1, "effect_days": 5,
     "detail": {"rep_min": 2, "rep_max": 5}},
]


async def _proposal(
    user_id: int, block_id: int, exercise_id: int, levers: list[dict] | None = None
) -> int:
    async with SessionLocal() as db:
        row = PeriodizationProposal(
            app_user_id=user_id, block_id=block_id,
            kind=periodization_params.KIND_GOAL_PLAN,
            reason_code="pace_behind",
            payload={
                "goal_id": 1, "exercise_id": exercise_id,
                "levers": levers if levers is not None else _DEFAULT_LEVERS,
                "applied_snapshot": None,
            },
            status=periodization_params.STATUS_PENDING,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.id


async def test_only_accepted_levers_are_applied(test_user, fresh_exercise, active_block):
    pid = await _proposal(test_user.id, active_block.id, fresh_exercise.id)
    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, pid)
        result = await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0]},
        )
        # apply_goal_decision больше не коммитит сама (Critical 1) — коммитим
        # здесь явно, ровно как это сделает настоящий вызывающий (Задача 10).
        await db.commit()
    assert result["applied"] == [0]

    async with SessionLocal() as db:
        prefs = (await db.execute(
            select(UserExercisePreference).where(
                UserExercisePreference.app_user_id == test_user.id
            )
        )).scalars().all()
        overrides = (await db.execute(
            select(UserExerciseRepOverride).where(
                UserExerciseRepOverride.app_user_id == test_user.id
            )
        )).scalars().all()
    assert len(prefs) == 1
    assert overrides == []


async def test_empty_accepted_applies_nothing(test_user, fresh_exercise, active_block):
    pid = await _proposal(test_user.id, active_block.id, fresh_exercise.id)
    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, pid)
        result = await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": []},
        )
        await db.commit()
    assert result["status"] == "declined"
    assert result["applied"] == []


async def test_snapshot_records_previous_state(test_user, fresh_exercise, active_block):
    pid = await _proposal(test_user.id, active_block.id, fresh_exercise.id)
    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, pid)
        await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0, 1]},
        )
        await db.commit()

    async with SessionLocal() as db:
        row = await db.get(PeriodizationProposal, pid)
    snapshot = row.payload["applied_snapshot"]
    assert snapshot is not None
    assert snapshot["preference"] is None      # преференции не было
    assert snapshot["rep_override"] is None    # оверрайда не было


async def test_snapshot_captures_preexisting_preference_and_rep_override(
    test_user, fresh_exercise, active_block
):
    """Ветка «оверрайд/преференция уже существовали» — до этой правки в
    тестах исполнялась только ветка «ничего не было» (снимок None), см.
    докстринг test_snapshot_records_previous_state выше."""
    async with SessionLocal() as db:
        db.add(UserExercisePreference(
            app_user_id=test_user.id, exercise_id=fresh_exercise.id,
            exercise_name=fresh_exercise.name, preference="disliked",
        ))
        db.add(UserExerciseRepOverride(
            app_user_id=test_user.id, exercise_id=fresh_exercise.id,
            rep_min=6, rep_max=10,
        ))
        await db.commit()

    pid = await _proposal(test_user.id, active_block.id, fresh_exercise.id)
    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, pid)
        result = await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0, 1]},
        )
        await db.commit()
    assert result["applied"] == [0, 1]

    async with SessionLocal() as db:
        row = await db.get(PeriodizationProposal, pid)
        pref = (await db.execute(
            select(UserExercisePreference).where(
                UserExercisePreference.app_user_id == test_user.id,
                UserExercisePreference.exercise_id == fresh_exercise.id,
            )
        )).scalar_one()
        override = (await db.execute(
            select(UserExerciseRepOverride).where(
                UserExerciseRepOverride.app_user_id == test_user.id,
                UserExerciseRepOverride.exercise_id == fresh_exercise.id,
            )
        )).scalar_one()
    snapshot = row.payload["applied_snapshot"]
    assert snapshot["preference"] == "disliked"                     # снимок ДО перезаписи
    assert snapshot["rep_override"] == {"rep_min": 6, "rep_max": 10}
    assert pref.preference == "favorite"                            # перезаписано
    assert (override.rep_min, override.rep_max) == (2, 5)


async def test_scheme_lever_writes_override_and_snapshots_none(
    test_user, fresh_exercise, active_block
):
    async with SessionLocal() as db:
        db.add(AppUserProfile(app_user_id=test_user.id))
        await db.commit()

    levers = [
        {"index": 0, "kind": "scheme", "reason_code": "pace_behind",
         "effect_slope": 0.15, "effect_days": 7, "detail": {"to_scheme": "5x5"}},
    ]
    pid = await _proposal(test_user.id, active_block.id, fresh_exercise.id, levers)
    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, pid)
        result = await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0]},
        )
        await db.commit()
    assert result["applied"] == [0]

    async with SessionLocal() as db:
        profile = (await db.execute(
            select(AppUserProfile).where(AppUserProfile.app_user_id == test_user.id)
        )).scalar_one()
        row = await db.get(PeriodizationProposal, pid)
    assert profile.settings["progression"]["overrides"][str(fresh_exercise.id)] == "5x5"
    assert row.payload["applied_snapshot"]["scheme"] is None


async def test_scheme_lever_snapshots_existing_override(
    test_user, fresh_exercise, active_block
):
    async with SessionLocal() as db:
        db.add(AppUserProfile(
            app_user_id=test_user.id,
            settings={"progression": {"overrides": {str(fresh_exercise.id): "3x8"}}},
        ))
        await db.commit()

    levers = [
        {"index": 0, "kind": "scheme", "reason_code": "pace_behind",
         "effect_slope": 0.15, "effect_days": 7, "detail": {"to_scheme": "5x5"}},
    ]
    pid = await _proposal(test_user.id, active_block.id, fresh_exercise.id, levers)
    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, pid)
        await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0]},
        )
        await db.commit()

    async with SessionLocal() as db:
        profile = (await db.execute(
            select(AppUserProfile).where(AppUserProfile.app_user_id == test_user.id)
        )).scalar_one()
        row = await db.get(PeriodizationProposal, pid)
    assert profile.settings["progression"]["overrides"][str(fresh_exercise.id)] == "5x5"
    assert row.payload["applied_snapshot"]["scheme"] == "3x8"      # снимок СУЩЕСТВОВАВШЕГО значения


async def test_sets_lever_writes_adjustment_to_future_planned_days(
    test_user, active_block
):
    """Финальное ревью, Critical 2: день обязан НЕСТИ целевое упражнение
    (plan_id -> WorkoutPlanExercise), иначе _apply_sets его больше не
    трогает вовсе — см. её докстринг. microcycle_day_number=1 нужен, чтобы
    volume.repository.current_window нашёл окно на дату дня (тот же приём,
    что и в tests/integration/test_goal_repository.py::
    test_headroom_sets_runs_real_mrv_path_and_shrinks_with_prescribed_volume)."""
    ex, plan = await _exercise_with_plan(test_user, target_sets=3)
    future = date.today() + timedelta(days=3)
    async with SessionLocal() as db:
        day = UserCalendarDay(
            app_user_id=test_user.id, target_date=future,
            block_id=active_block.id, plan_id=plan.id,
            is_rest_day=False, is_blackout=False, microcycle_day_number=1,
            status="planned",
        )
        db.add(day)
        await db.commit()
        await db.refresh(day)
        day_id = day.id

    levers = [
        {"index": 0, "kind": "sets", "reason_code": "pace_behind",
         "effect_slope": 0.1, "effect_days": 14, "detail": {"delta_sets": 2}},
    ]
    pid = await _proposal(test_user.id, active_block.id, ex.id, levers)
    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, pid)
        result = await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0]},
        )
        await db.commit()
    assert result["applied"] == [0]
    assert result["skipped_days"] == 0

    async with SessionLocal() as db:
        day = await db.get(UserCalendarDay, day_id)
    # MRV груди для beginner (нет AppUserProfile -> level="beginner") — 14
    # (landmarks._TABLE), предписано 3 подхода -> запас 11, дельта 2 из
    # запаса умещается целиком.
    assert day.volume_adjustments == [
        {"exercise_id": ex.id, "delta_sets": 2, "proposal_id": pid}
    ]


async def test_sets_lever_ignores_day_without_the_lift(test_user, active_block):
    """Финальное ревью, Critical 2, требуемый тест: день БЕЗ целевого
    упражнения в плане (пустой день — plan_id=None, как день отдыха до
    первого сплита) не должен получить правку вовсе — раньше _apply_sets
    штамповал АБСОЛЮТНО ЛЮБОЙ будущий день блока."""
    ex, _plan = await _exercise_with_plan(test_user, target_sets=3)
    future = date.today() + timedelta(days=3)
    async with SessionLocal() as db:
        day = UserCalendarDay(
            app_user_id=test_user.id, target_date=future,
            block_id=active_block.id, plan_id=None,
            is_rest_day=False, is_blackout=False, microcycle_day_number=1,
            status="planned",
        )
        db.add(day)
        await db.commit()
        await db.refresh(day)
        day_id = day.id

    levers = [
        {"index": 0, "kind": "sets", "reason_code": "pace_behind",
         "effect_slope": 0.1, "effect_days": 14, "detail": {"delta_sets": 2}},
    ]
    pid = await _proposal(test_user.id, active_block.id, ex.id, levers)
    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, pid)
        result = await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0]},
        )
        await db.commit()
    # Ни одного дня, несущего лифт, не нашлось -> рычаг не применился вовсе.
    assert result["applied"] == []
    assert result["status"] == "declined"

    async with SessionLocal() as db:
        day = await db.get(UserCalendarDay, day_id)
    assert not (day.volume_adjustments or [])


async def test_sets_lever_ignores_rest_and_blackout_days(test_user, active_block):
    """Финальное ревью, Critical 2, требуемый тест: дни отдыха/blackout не
    получают правку, даже если по недосмотру несут plan_id с целевым
    упражнением (is_rest_day/is_blackout — фильтр ОТДЕЛЬНЫЙ от plan_id join,
    см. докстринг _apply_sets)."""
    ex, plan = await _exercise_with_plan(test_user, target_sets=3)
    future = date.today() + timedelta(days=3)
    async with SessionLocal() as db:
        rest_day = UserCalendarDay(
            app_user_id=test_user.id, target_date=future,
            block_id=active_block.id, plan_id=plan.id,
            is_rest_day=True, is_blackout=False, microcycle_day_number=1,
            status="planned",
        )
        blackout_day = UserCalendarDay(
            app_user_id=test_user.id, target_date=future + timedelta(days=1),
            block_id=active_block.id, plan_id=plan.id,
            is_rest_day=False, is_blackout=True,
            status="planned",
        )
        db.add_all([rest_day, blackout_day])
        await db.commit()
        await db.refresh(rest_day)
        await db.refresh(blackout_day)
        rest_id, blackout_id = rest_day.id, blackout_day.id

    levers = [
        {"index": 0, "kind": "sets", "reason_code": "pace_behind",
         "effect_slope": 0.1, "effect_days": 14, "detail": {"delta_sets": 2}},
    ]
    pid = await _proposal(test_user.id, active_block.id, ex.id, levers)
    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, pid)
        result = await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0]},
        )
        await db.commit()
    assert result["applied"] == []
    assert result["status"] == "declined"

    async with SessionLocal() as db:
        rest_day = await db.get(UserCalendarDay, rest_id)
        blackout_day = await db.get(UserCalendarDay, blackout_id)
    assert not (rest_day.volume_adjustments or [])
    assert not (blackout_day.volume_adjustments or [])


async def test_sets_lever_skips_non_planned_day_and_counts_it(
    test_user, active_block
):
    ex, plan = await _exercise_with_plan(test_user, target_sets=3)
    future = date.today() + timedelta(days=3)
    async with SessionLocal() as db:
        planned_day = UserCalendarDay(
            app_user_id=test_user.id, target_date=future,
            block_id=active_block.id, plan_id=plan.id,
            is_rest_day=False, is_blackout=False, microcycle_day_number=1,
            status="planned",
        )
        missed_day = UserCalendarDay(
            app_user_id=test_user.id, target_date=future + timedelta(days=1),
            block_id=active_block.id, plan_id=plan.id,
            is_rest_day=False, is_blackout=False,
            status="missed",
        )
        db.add_all([planned_day, missed_day])
        await db.commit()
        await db.refresh(planned_day)
        await db.refresh(missed_day)
        planned_id, missed_id = planned_day.id, missed_day.id

    levers = [
        {"index": 0, "kind": "sets", "reason_code": "pace_behind",
         "effect_slope": 0.1, "effect_days": 14, "detail": {"delta_sets": 2}},
    ]
    pid = await _proposal(test_user.id, active_block.id, ex.id, levers)
    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, pid)
        result = await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0]},
        )
        await db.commit()
    assert result["applied"] == [0]
    assert result["skipped_days"] == 1

    async with SessionLocal() as db:
        planned_day = await db.get(UserCalendarDay, planned_id)
        missed_day = await db.get(UserCalendarDay, missed_id)
    assert planned_day.volume_adjustments == [
        {"exercise_id": ex.id, "delta_sets": 2, "proposal_id": pid}
    ]
    assert not (missed_day.volume_adjustments or [])


async def test_sets_lever_caps_total_delta_within_window_headroom(
    test_user, active_block
):
    """Финальное ревью, Critical 2, требуемый тест: лифт стоит ТРИЖДЫ в
    одном микроцикле (одно окно — все три дня несут microcycle_day_number
    внутри диапазона первого маркера, см. current_window/_window_starts) —
    суммарная добавленная дельта по ВСЕМ трём дням не может превысить
    запас окна, даже если наивно применить delta_sets=2 к каждому вхождению
    дало бы 6 (втрое больше разрешённого — буквально формулировка Critical 2).

    Запас окна: MRV груди для beginner = 14 (landmarks._TABLE), предписано
    4 подхода/день * 3 дня = 12 -> headroom = 14 - 12 = 2. Headroom
    считается ОДИН РАЗ за вызов, на первом дне окна (см. докстринг
    _apply_sets про window_headroom_cache), и дальше только тратится: день 1
    получает min(delta=2, room=2-0=2) = 2 (весь остаток бюджета), дни 2 и 3
    получают min(2, room=2-2=0) = 0 каждый — бюджет уже исчерпан день 1-м.
    Итог: 2 + 0 + 0 = 2 == весь запас окна, ни граммом больше.
    """
    ex, plan = await _exercise_with_plan(test_user, target_sets=4)
    day1 = date.today() + timedelta(days=1)
    day2 = date.today() + timedelta(days=3)
    day3 = date.today() + timedelta(days=5)
    async with SessionLocal() as db:
        rows = [
            UserCalendarDay(
                app_user_id=test_user.id, target_date=d,
                block_id=active_block.id, plan_id=plan.id,
                is_rest_day=False, is_blackout=False,
                # Один маркер начала окна на самый ранний из трёх дней —
                # все три попадают в ОДНО и то же окно (следующего старта
                # микроцикла в этом блоке нет вовсе).
                microcycle_day_number=(1 if d == day1 else 2),
                status="planned",
            )
            for d in (day1, day2, day3)
        ]
        db.add_all(rows)
        await db.commit()
        for row in rows:
            await db.refresh(row)
        day_ids = [row.id for row in rows]

    levers = [
        {"index": 0, "kind": "sets", "reason_code": "pace_behind",
         "effect_slope": 0.1, "effect_days": 14, "detail": {"delta_sets": 2}},
    ]
    pid = await _proposal(test_user.id, active_block.id, ex.id, levers)
    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, pid)
        result = await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0]},
        )
        await db.commit()
    assert result["applied"] == [0]
    # Два из трёх дней недополучили ничего — бюджет окна кончился на первом.
    assert result["skipped_days"] == 2

    async with SessionLocal() as db:
        days = [await db.get(UserCalendarDay, day_id) for day_id in day_ids]
    grants = [
        sum(a.get("delta_sets", 0) for a in (day.volume_adjustments or []))
        for day in days
    ]
    assert sum(grants) <= 2, "суммарная дельта по окну не может превысить его запас"
    assert sum(grants) == 2
    assert grants == [2, 0, 0]


async def test_sets_lever_skipped_and_not_applied_when_headroom_gone_at_apply_time(
    test_user, active_block
):
    """Финальное ревью, Critical 2, требуемый тест: между предложением и
    применением headroom мог исчезнуть (например решение P0-09 приехало
    первым и уже забило окно под MRV). Рычаг обязан честно ничего не
    сделать и сообщить об этом (skipped_days, applied=[], status=declined)
    — а НЕ пробить потолок объёма (спека, решение 11)."""
    # target_sets=20 > MRV(14) для beginner -> headroom = 0 сразу.
    ex, plan = await _exercise_with_plan(test_user, target_sets=20)
    future = date.today() + timedelta(days=3)
    async with SessionLocal() as db:
        day = UserCalendarDay(
            app_user_id=test_user.id, target_date=future,
            block_id=active_block.id, plan_id=plan.id,
            is_rest_day=False, is_blackout=False, microcycle_day_number=1,
            status="planned",
        )
        db.add(day)
        await db.commit()
        await db.refresh(day)
        day_id = day.id

    levers = [
        {"index": 0, "kind": "sets", "reason_code": "pace_behind",
         "effect_slope": 0.1, "effect_days": 14, "detail": {"delta_sets": 2}},
    ]
    pid = await _proposal(test_user.id, active_block.id, ex.id, levers)
    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, pid)
        result = await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0]},
        )
        await db.commit()
    assert result["applied"] == []
    assert result["status"] == "declined"
    assert result["skipped_days"] == 1

    async with SessionLocal() as db:
        day = await db.get(UserCalendarDay, day_id)
    assert not (day.volume_adjustments or [])


async def test_replay_on_pending_proposal_keeps_original_snapshot_no_prior_carrier(
    test_user, fresh_exercise, active_block
):
    """Повтор apply_goal_decision на ещё pending предложении (офлайн-очередь,
    двойной тап) не должен переписывать snapshot — ветка «носителя не было».

    До исправления (ревью Задачи 8, Important) второй вызов читал уже
    применённое избранное первого вызова и записывал 'favorite' как будто
    это и было исходное состояние — см. докстринг apply_goal_decision."""
    pid = await _proposal(test_user.id, active_block.id, fresh_exercise.id)

    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, pid)
        await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0, 1]},
        )
        await db.commit()

    async with SessionLocal() as db:
        row = await db.get(PeriodizationProposal, pid)
    snapshot_after_first = row.payload["applied_snapshot"]
    assert snapshot_after_first["preference"] is None
    assert snapshot_after_first["rep_override"] is None

    # Предложение остаётся pending (Задача 10 её ещё не подключила) — повтор
    # того же decision имитирует ретрай из офлайн-очереди / двойной тап.
    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, pid)
        result_2 = await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0, 1]},
        )
        await db.commit()
    assert result_2["applied"] == [0, 1]

    async with SessionLocal() as db:
        row = await db.get(PeriodizationProposal, pid)
    snapshot_after_replay = row.payload["applied_snapshot"]
    assert snapshot_after_replay["preference"] is None      # НЕ 'favorite'
    assert snapshot_after_replay["rep_override"] is None    # НЕ перезаписан


async def test_replay_on_pending_proposal_keeps_original_snapshot_with_prior_carrier(
    test_user, fresh_exercise, active_block
):
    """Та же защита от повтора, но для ветки «носитель уже существовал» —
    снимок обязан удержать ИСХОДНОЕ значение, а не то, что записал первый
    вызов."""
    async with SessionLocal() as db:
        db.add(UserExercisePreference(
            app_user_id=test_user.id, exercise_id=fresh_exercise.id,
            exercise_name=fresh_exercise.name, preference="disliked",
        ))
        db.add(UserExerciseRepOverride(
            app_user_id=test_user.id, exercise_id=fresh_exercise.id,
            rep_min=6, rep_max=10,
        ))
        await db.commit()

    pid = await _proposal(test_user.id, active_block.id, fresh_exercise.id)

    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, pid)
        await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0, 1]},
        )
        await db.commit()

    async with SessionLocal() as db:
        row = await db.get(PeriodizationProposal, pid)
    snapshot_after_first = row.payload["applied_snapshot"]
    assert snapshot_after_first["preference"] == "disliked"
    assert snapshot_after_first["rep_override"] == {"rep_min": 6, "rep_max": 10}

    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, pid)
        result_2 = await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0, 1]},
        )
        await db.commit()
    assert result_2["applied"] == [0, 1]

    async with SessionLocal() as db:
        row = await db.get(PeriodizationProposal, pid)
    snapshot_after_replay = row.payload["applied_snapshot"]
    # Всё ещё исходное 'disliked' / (6, 10) — НЕ 'favorite' / (2, 5), которые
    # первый вызов записал в носители.
    assert snapshot_after_replay["preference"] == "disliked"
    assert snapshot_after_replay["rep_override"] == {"rep_min": 6, "rep_max": 10}


async def test_sets_lever_applied_twice_does_not_double(
    test_user, active_block
):
    ex, plan = await _exercise_with_plan(test_user, target_sets=3)
    future = date.today() + timedelta(days=3)
    async with SessionLocal() as db:
        day = UserCalendarDay(
            app_user_id=test_user.id, target_date=future,
            block_id=active_block.id, plan_id=plan.id,
            is_rest_day=False, is_blackout=False, microcycle_day_number=1,
            status="planned",
        )
        db.add(day)
        await db.commit()
        await db.refresh(day)
        day_id = day.id

    levers = [
        {"index": 0, "kind": "sets", "reason_code": "pace_behind",
         "effect_slope": 0.1, "effect_days": 14, "detail": {"delta_sets": 2}},
    ]
    pid = await _proposal(test_user.id, active_block.id, ex.id, levers)

    # Первое применение.
    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, pid)
        result_1 = await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0]},
        )
        await db.commit()
    assert result_1["applied"] == [0]

    # Повтор того же решения (двойной тап / повтор из офлайн-очереди) —
    # свежий объект proposal той же сессии реальной apply_decision (Задача
    # 10) не переставит статус в pending=False до истечения этого вызова,
    # но идемпотентность самой правки НЕ должна зависеть от этого — см.
    # докстринг _apply_sets.
    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, pid)
        result_2 = await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0]},
        )
        await db.commit()
    assert result_2["applied"] == [0]

    async with SessionLocal() as db:
        day = await db.get(UserCalendarDay, day_id)
    assert day.volume_adjustments == [
        {"exercise_id": ex.id, "delta_sets": 2, "proposal_id": pid}
    ]
