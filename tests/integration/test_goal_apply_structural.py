"""Структурный рычаг не трогает дни с фактом (P0-12, Задача 9).

apply_goal_decision НЕ коммитит сессию сама (см. её докстринг и
tests/integration/test_goal_apply_soft.py) — каждый блок ниже, желающий
увидеть эффект в следующем блоке, коммитит явно, ровно как это сделает
настоящий вызывающий (periodization.service.apply_decision, Задача 10).

ИСПРАВЛЕНО (ревью Задачи 9, Important 2 — "зелёный на пустой выборке"):
раньше этот файл заводил СВОЙ локальный `active_block` без активного
UserSplit/SplitBlueprint. SchedulingEngine.generate_block_days на пустой
slots_queue делает `if not slots_queue: return 0` ДО своего внутреннего
commit() (см. её докстринг) — обе "перегенерации" в этом файле были
затычкой: ни одного нового дня календаря не появлялось, а тест-ассерты
были посчитаны по ДО-регенерационному запросу, поэтому проходили одинаково
и при настоящей регенерации, и при полном её отсутствии. Убираем локальную
фикстуру — модуль теперь получает `active_block` из tests/integration/
conftest.py, которая как раз несёт при себе минимальный сплит Push/Pull
(см. её докстринг) ровно для того, чтобы generate_block_days было чем
наполнить slots_queue.
"""
from datetime import date, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

from api.services.goal.service import apply_goal_decision
from api.services.models import PeriodizationProposal, UserCalendarDay
from api.services.periodization import params as periodization_params
from app.database import SessionLocal

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def seeded_future_days(test_user, active_block):
    """Пять будущих дней блока; один из них уже выполнен.

    day_tag="push" (нижний регистр) — сознательно НЕ совпадает ни с "Push",
    ни с "Pull" (day_tag реальных DayBlueprint из фикстуры active_block,
    см. conftest.py). Это разночтение — единственный надёжный способ
    отличить в тестах ниже "прежние" дни (эти) от НАСТОЯЩИХ дней, которые
    положит SchedulingEngine.generate_block_days при регенерации.
    """
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
        await db.commit()
    yield


async def test_completed_days_survive_regeneration(
    test_user, fresh_exercise, active_block, seeded_future_days
):
    """Один из будущих дней помечен completed — регенерация обязана его сохранить."""
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

        result = await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0]},
        )
        # apply_goal_decision больше не коммитит сама — коммитим здесь явно,
        # ровно как это сделает настоящий вызывающий (Задача 10).
        await db.commit()

    assert result["skipped_days"] >= 1

    async with SessionLocal() as db:
        surviving = (await db.execute(
            select(UserCalendarDay).where(
                UserCalendarDay.app_user_id == test_user.id,
                UserCalendarDay.status == "completed",
            )
        )).scalars().all()
    assert len(surviving) >= 1

    # ИСПРАВЛЕНО (ревью Задачи 9, Important 2): одних surviving/skipped_days
    # недостаточно — они верны и когда генерация вообще ничего не сделала
    # (см. докстринг модуля). Доказываем, что регенерация РЕАЛЬНО прошла:
    # day_tag "Push"/"Pull" может появиться только из настоящего
    # SchedulingEngine.generate_block_days по сплиту active_block, а не из
    # seeded_future_days (та кладёт только day_tag="push").
    async with SessionLocal() as db:
        regenerated = (await db.execute(
            select(UserCalendarDay).where(
                UserCalendarDay.app_user_id == test_user.id,
                UserCalendarDay.block_id == active_block.id,
                UserCalendarDay.status == "planned",
                UserCalendarDay.day_tag.in_(["Push", "Pull"]),
            )
        )).scalars().all()
    assert regenerated, (
        "регенерация должна была создать новые дни по сплиту Push/Pull, "
        "а не просто оставить старые нетронутыми"
    )


async def test_snapshot_keeps_previous_day_coordinates(
    test_user, fresh_exercise, active_block, seeded_future_days
):
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
        await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0]},
        )
        # apply_goal_decision больше не коммитит сама — коммитим здесь явно,
        # ровно как это сделает настоящий вызывающий (Задача 10).
        await db.commit()
        pid = proposal.id

    async with SessionLocal() as db:
        row = await db.get(PeriodizationProposal, pid)
    days = row.payload["applied_snapshot"]["days"]
    assert days
    assert all({"target_date", "plan_id", "day_tag"} <= set(d) for d in days)
    # Координаты в снимке — это ИМЕННО прежние дни (day_tag="push" из
    # seeded_future_days), а не то, чем регенерация их заменила
    # ("Push"/"Pull") — иначе снимок описывал бы уже новый календарь, и
    # откату (Задача 10) было бы нечего восстанавливать.
    assert {d["day_tag"] for d in days} == {"push"}


async def test_snapshot_persists_through_generate_block_days_internal_commit(
    test_user, fresh_exercise, active_block, seeded_future_days
):
    """Finding 1 (ревью Задачи 9, Critical 1): SchedulingEngine.
    generate_block_days коммитит сессию САМА, ДО того как apply_goal_
    decision успевает дописать snapshot в proposal.payload обычным путём.
    Раньше snapshot["days"]/["structural_applied"] жили только в локальном
    dict до самого конца цикла рычагов — обрыв процесса между внутренним
    commit'ом регенерации и финальным commit'ом вызывающего стирал их
    безвозвратно, при том что старый календарь уже был удалён.

    Проверяем это не косвенно, а имитацией именно такого обрыва: НЕ делаем
    финальный commit() вызывающего (как это сделал бы Задачи 10 apply_
    decision) вовсе, читаем результат из ДРУГОЙ, свежей сессии — если бы
    snapshot ехал в БД только вместе с финальным commit'ом вызывающего, его
    бы там не оказалось. Он там есть, потому что _apply_structural
    записывает и flush()-ит его ДО wipe/regenerate — тот же flush попадает
    в ТУ ЖЕ транзакцию, что и внутренний commit() generate_block_days.
    """
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

        result = await apply_goal_decision(
            db, test_user.id, proposal,
            periodization_params.ACTION_APPLY_GOAL, {"accepted": [0]},
        )
        pid = proposal.id
        # СОЗНАТЕЛЬНО не коммитим здесь: имитируем обрыв вызывающего сразу
        # после apply_goal_decision, ДО его собственного финального
        # commit(). Если snapshot переживёт это — он попал в БД благодаря
        # внутреннему commit'у generate_block_days, а не этому отсутствующему
        # финальному commit'у.

    assert result["status"] == "applied"

    async with SessionLocal() as db:
        row = await db.get(PeriodizationProposal, pid)
        assert row is not None
        snap = row.payload["applied_snapshot"]
        assert snap["structural_applied"] is True
        assert snap["days"], "снимок обязан содержать координаты прежних дней"
        # Снимок — это ПРЕЖНИЕ дни (day_tag="push"), не то, во что их
        # превратила регенерация.
        assert {d["day_tag"] for d in snap["days"]} == {"push"}

        # А календарь при этом УЖЕ несёт результат регенерации — значит,
        # внутренний commit() действительно произошёл и действительно унёс
        # snapshot с собой в одной транзакции.
        regenerated = (await db.execute(
            select(UserCalendarDay).where(
                UserCalendarDay.app_user_id == test_user.id,
                UserCalendarDay.block_id == active_block.id,
                UserCalendarDay.day_tag.in_(["Push", "Pull"]),
            )
        )).scalars().all()
    assert regenerated, "регенерация должна была создать дни по сплиту Push/Pull"
