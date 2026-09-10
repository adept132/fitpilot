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

УДАЛЕНО (P0-12, обрезка лестницы): рычаги LEVER_SETS и LEVER_REP_RANGE — вместе
с ними ушли тесты, гонявшие носители UserCalendarDay.volume_adjustments и
UserExerciseRepOverride через автопилот (разбор причины — в decide()/
simulate.py: ни одна схема прогрессии не читает число подходов, а «дожатие»
диапазона повторов истинно по конструкции синтетического исполнителя).
"""
from datetime import date, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from api.services.goal.service import apply_goal_decision
from api.services.models import (
    AppUserProfile,
    PeriodizationProposal,
    UserExercisePreference,
)
from api.services.periodization import params as periodization_params
from app.database import SessionLocal

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_soft_lever_writes(test_user):
    """Снести преференции, которые тест навешал на fresh_exercise.

    Без этого teardown test_user падает ForeignKeyViolationError: он сносит
    Exercise до того, как что-то удалит ссылающуюся на неё
    UserExercisePreference (у её exercise_id нет ON DELETE CASCADE).
    Фикстура завязана на test_user, поэтому её teardown по LIFO гарантированно
    отрабатывает раньше teardown'а test_user.
    """
    yield
    async with SessionLocal() as db:
        await db.execute(
            delete(UserExercisePreference).where(
                UserExercisePreference.app_user_id == test_user.id
            )
        )
        await db.commit()


@pytest_asyncio.fixture
async def active_block(test_user):
    from api.services.models import TrainingBlock
    async with SessionLocal() as db:
        block = TrainingBlock(
            phase_snapshot_trusted=True,
            app_user_id=test_user.id, block_index=1, phases=[],
            microcycle_length=7, start_date=date.today(),
            planned_end_date=date.today() + timedelta(days=28), status="active",
        )
        db.add(block)
        await db.commit()
        await db.refresh(block)
        yield block


_DEFAULT_LEVERS = [
    {"index": 0, "kind": "ensure_present", "reason_code": "lift_missing",
     "effect_slope": 0.2, "effect_days": 10, "detail": {}},
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
    """Селективность: из двух рычагов принят только один — носитель второго
    (профильные overrides схемы) остаётся нетронутым."""
    async with SessionLocal() as db:
        db.add(AppUserProfile(app_user_id=test_user.id))
        await db.commit()

    levers = _DEFAULT_LEVERS + [
        {"index": 1, "kind": "scheme", "reason_code": "pace_behind",
         "effect_slope": 0.15, "effect_days": 7, "detail": {"to_scheme": "5x5"}},
    ]
    pid = await _proposal(test_user.id, active_block.id, fresh_exercise.id, levers)
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
        profile = (await db.execute(
            select(AppUserProfile).where(AppUserProfile.app_user_id == test_user.id)
        )).scalar_one()
    assert len(prefs) == 1
    # Схемный рычаг (индекс 1) не был принят — overrides не тронуты.
    assert not profile.settings.get("progression", {}).get("overrides")


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
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0]},
        )
        await db.commit()

    async with SessionLocal() as db:
        row = await db.get(PeriodizationProposal, pid)
    snapshot = row.payload["applied_snapshot"]
    assert snapshot is not None
    assert snapshot["preference"] is None      # преференции не было


async def test_snapshot_captures_preexisting_preference(
    test_user, fresh_exercise, active_block
):
    """Ветка «преференция уже существовала» — до этой правки в тестах
    исполнялась только ветка «ничего не было» (снимок None), см. докстринг
    test_snapshot_records_previous_state выше."""
    async with SessionLocal() as db:
        db.add(UserExercisePreference(
            app_user_id=test_user.id, exercise_id=fresh_exercise.id,
            exercise_name=fresh_exercise.name, preference="disliked",
        ))
        await db.commit()

    pid = await _proposal(test_user.id, active_block.id, fresh_exercise.id)
    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, pid)
        result = await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0]},
        )
        await db.commit()
    assert result["applied"] == [0]

    async with SessionLocal() as db:
        row = await db.get(PeriodizationProposal, pid)
        pref = (await db.execute(
            select(UserExercisePreference).where(
                UserExercisePreference.app_user_id == test_user.id,
                UserExercisePreference.exercise_id == fresh_exercise.id,
            )
        )).scalar_one()
    snapshot = row.payload["applied_snapshot"]
    assert snapshot["preference"] == "disliked"    # снимок ДО перезаписи
    assert pref.preference == "favorite"           # перезаписано


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
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0]},
        )
        await db.commit()

    async with SessionLocal() as db:
        row = await db.get(PeriodizationProposal, pid)
    snapshot_after_first = row.payload["applied_snapshot"]
    assert snapshot_after_first["preference"] is None

    # Предложение остаётся pending (Задача 10 её ещё не подключила) — повтор
    # того же decision имитирует ретрай из офлайн-очереди / двойной тап.
    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, pid)
        result_2 = await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0]},
        )
        await db.commit()
    assert result_2["applied"] == [0]

    async with SessionLocal() as db:
        row = await db.get(PeriodizationProposal, pid)
    snapshot_after_replay = row.payload["applied_snapshot"]
    assert snapshot_after_replay["preference"] is None      # НЕ 'favorite'


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
        await db.commit()

    pid = await _proposal(test_user.id, active_block.id, fresh_exercise.id)

    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, pid)
        await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0]},
        )
        await db.commit()

    async with SessionLocal() as db:
        row = await db.get(PeriodizationProposal, pid)
    snapshot_after_first = row.payload["applied_snapshot"]
    assert snapshot_after_first["preference"] == "disliked"

    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, pid)
        result_2 = await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0]},
        )
        await db.commit()
    assert result_2["applied"] == [0]

    async with SessionLocal() as db:
        row = await db.get(PeriodizationProposal, pid)
    snapshot_after_replay = row.payload["applied_snapshot"]
    # Всё ещё исходное 'disliked' — НЕ 'favorite', которое первый вызов
    # записал в носитель.
    assert snapshot_after_replay["preference"] == "disliked"
