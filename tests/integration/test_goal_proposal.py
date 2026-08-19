"""Создание предложения автопилота: пороги, дедуп, вытеснение (P0-12, Задача 6)."""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from api.services.goal import decide, repository
from api.services.goal import params as goal_params
from api.services.goal.service import refresh_goal_proposals
from api.services.goal.types import DecisionInput, Rates
from api.services.models import (
    PeriodizationProposal,
    TrainingBlock,
    UserExerciseProgressionState,
    UserGoal,
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)
from api.services.periodization import params as periodization_params
from app.database import SessionLocal

pytestmark = pytest.mark.asyncio


async def _primary_goal(
    user_id: int, exercise_id: int, target: float, deadline=None, target_reps: int = 3
) -> int:
    async with SessionLocal() as db:
        goal = UserGoal(
            app_user_id=user_id, goal_type="strength", target_value=target,
            exercise_id=exercise_id, target_reps=target_reps,
            deadline=deadline if deadline is not None else date.today() + timedelta(days=60),
            is_primary=True,
        )
        db.add(goal)
        await db.commit()
        await db.refresh(goal)
        return goal.id


async def _set_working_e1rm(user_id: int, exercise_id: int, working_e1rm: float) -> None:
    async with SessionLocal() as db:
        db.add(UserExerciseProgressionState(
            app_user_id=user_id, exercise_id=exercise_id, working_e1rm=working_e1rm,
        ))
        await db.commit()


async def test_no_primary_goal_means_no_proposal(test_user):
    async with SessionLocal() as db:
        assert await refresh_goal_proposals(db, test_user.id, date.today()) is None


async def test_goal_without_data_produces_nothing(test_user, fresh_exercise):
    await _primary_goal(test_user.id, fresh_exercise.id, 200.0)
    async with SessionLocal() as db:
        assert await refresh_goal_proposals(db, test_user.id, date.today()) is None


async def test_repeated_refresh_does_not_duplicate(test_user, seeded_history, active_block):
    """ИСПРАВЛЕНО (ревью Задачи 6, Critical 1): прежняя версия ни разу не
    засевала UserExerciseProgressionState.working_e1rm, которую repository.
    current_e1rm читает как единственный источник — evaluate() валился на
    первой же проверке, refresh_goal_proposals всегда возвращал None, и
    assert len(rows) <= 1 был правдив на 0 <= 1: ветка дедупа (existing/
    inputs_hash) не проверялась вовсе.

    Здесь working_e1rm=100, цель — target_reps=1, target_value=100.0
    (target_e1rm = 100 * (1 + 1/30) ≈ 103.33). У seeded_history нет плана на
    календаре -> lift_in_plan=False -> сработает ровно рычаг
    LEVER_ENSURE_PRESENT (см. decide._applicable), gap закрывается им целиком
    за один проход лестницы. required (~0.39 кг/нед) заведомо ниже ceiling
    (100 * 1 %/нед = 1.0) — до REASON_ABOVE_CEILING не доходит, предложение
    рождается.
    """
    await _primary_goal(test_user.id, seeded_history.id, 100.0, target_reps=1)
    await _set_working_e1rm(test_user.id, seeded_history.id, 100.0)

    async with SessionLocal() as db:
        first = await refresh_goal_proposals(db, test_user.id, date.today())
    assert first is not None, "при этих условиях рычаг ensure_present обязан сработать"

    async with SessionLocal() as db:
        second = await refresh_goal_proposals(db, test_user.id, date.today())
    assert second is not None
    # Суть находки: тот же goal_id + тот же inputs_hash -> та же строка,
    # а не новая. Проверяем тождество id, а не только счётчик.
    assert second.id == first.id

    async with SessionLocal() as db:
        rows = (await db.execute(
            select(PeriodizationProposal).where(
                PeriodizationProposal.app_user_id == test_user.id,
                PeriodizationProposal.kind == periodization_params.KIND_GOAL_PLAN,
                PeriodizationProposal.status == periodization_params.STATUS_PENDING,
            )
        )).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == first.id


async def test_stale_proposal_expiry_survives_hash_match_early_return(
    test_user, seeded_history, active_block
):
    """Деferred Minor 6 (финальное ревью): цикл дедупа мог пометить ЧУЖОЙ
    (по другой цели) pending-предложение expired ТОЛЬКО В ПАМЯТИ и тут же
    вернуться на совпадении inputs_hash+goal_id — `return row` обрывал
    функцию раньше единственного commit(), который стоял ниже по телу (у
    создания НОВОГО предложения). Экспирация чужой строки терялась при
    закрытии сессии. Воспроизводим буквально: сначала обычным путём
    материализуем предложение ЭТОЙ цели (как test_repeated_refresh_does_not_
    duplicate), затем руками подсаживаем pending-строку ДРУГОЙ (уже
    неактуальной) цели того же пользователя, третий refresh обязан и вернуть
    ТУ ЖЕ строку (тождество id), и — что и есть суть находки — реально
    закоммитить экспирацию чужой строки, а не просто пометить её в памяти
    сессии, которая тут же закрывается.
    """
    goal_id = await _primary_goal(test_user.id, seeded_history.id, 100.0, target_reps=1)
    await _set_working_e1rm(test_user.id, seeded_history.id, 100.0)

    async with SessionLocal() as db:
        first = await refresh_goal_proposals(db, test_user.id, date.today())
    assert first is not None

    async with SessionLocal() as db:
        stale = PeriodizationProposal(
            app_user_id=test_user.id,
            block_id=active_block.id,
            kind=periodization_params.KIND_GOAL_PLAN,
            reason_code="pace_behind",
            payload={
                "goal_id": goal_id + 10_000_000,  # заведомо чужая/несуществующая цель
                # ДРУГОЙ exercise_id, а не seeded_history.id: uq_periodization_
                # proposals_pending уникален по (block_id, kind,
                # COALESCE(payload->>'exercise_id', '')) — та же тройка, что и
                # у уже существующего pending-предложения этой цели (created
                # выше через refresh_goal_proposals), столкнулась бы с ним.
                "exercise_id": seeded_history.id + 10_000_000,
                "inputs_hash": "stale-from-another-goal",
                "applied_snapshot": None,
            },
            status=periodization_params.STATUS_PENDING,
        )
        db.add(stale)
        await db.commit()
        await db.refresh(stale)
        stale_id = stale.id

    async with SessionLocal() as db:
        second = await refresh_goal_proposals(db, test_user.id, date.today())
    assert second is not None
    assert second.id == first.id  # то же предложение той же цели, второго не создано

    # Суть находки: читаем ИЗ ФРЕШ-СЕССИИ (не той, что делала refresh) — если
    # бы экспирация осталась только в памяти упавшей на return сессии,
    # чужая строка здесь всё ещё была бы pending.
    async with SessionLocal() as db:
        stale_row = await db.get(PeriodizationProposal, stale_id)
    assert stale_row.status == periodization_params.STATUS_EXPIRED


# --- Сверх брифа: финиш ведущей цели снимает is_primary (спека §7) ---
#
# Бриф Задачи 6 этого не покрывает — требование добавлено постановщиком
# отдельно (см. p0-12-task-6-report.md, раздел "Сверх брифа"). Достигнутая
# или просроченная ведущая цель обязана освободить свой единственный слот,
# иначе пользователь никогда не получит предложения выбрать следующую.

async def test_achieved_goal_clears_primary_flag(test_user, fresh_exercise):
    """target_reps=3, target_value=100 -> target_e1rm = 100 * 1.1 = 110.
    working_e1rm=150, с явным запасом над целью — цель достигнута."""
    goal_id = await _primary_goal(test_user.id, fresh_exercise.id, 100.0, target_reps=3)
    await _set_working_e1rm(test_user.id, fresh_exercise.id, 150.0)

    async with SessionLocal() as db:
        result = await refresh_goal_proposals(db, test_user.id, date.today())
    assert result is None

    async with SessionLocal() as db:
        goal = await db.get(UserGoal, goal_id)
        assert goal.is_primary is False


async def test_overdue_goal_clears_primary_flag(test_user, fresh_exercise):
    """Дедлайн уже прошёл — цель просрочена, даже если e1RM не достигнут."""
    past_deadline = date.today() - timedelta(days=1)
    goal_id = await _primary_goal(
        test_user.id, fresh_exercise.id, 200.0, deadline=past_deadline
    )

    async with SessionLocal() as db:
        result = await refresh_goal_proposals(db, test_user.id, date.today())
    assert result is None

    async with SessionLocal() as db:
        goal = await db.get(UserGoal, goal_id)
        assert goal.is_primary is False


async def test_finished_goal_expires_its_active_proposal(test_user, fresh_exercise, active_block):
    """Активное goal_plan-предложение достигнутой цели уходит в expired, а не
    остаётся висеть pending навсегда."""
    goal_id = await _primary_goal(test_user.id, fresh_exercise.id, 100.0, target_reps=3)
    await _set_working_e1rm(test_user.id, fresh_exercise.id, 150.0)

    async with SessionLocal() as db:
        proposal = PeriodizationProposal(
            app_user_id=test_user.id,
            block_id=active_block.id,
            kind=periodization_params.KIND_GOAL_PLAN,
            reason_code="pace_behind",
            payload={"goal_id": goal_id, "exercise_id": fresh_exercise.id, "inputs_hash": "old"},
            status=periodization_params.STATUS_PENDING,
        )
        db.add(proposal)
        await db.commit()
        await db.refresh(proposal)
        proposal_id = proposal.id

    async with SessionLocal() as db:
        result = await refresh_goal_proposals(db, test_user.id, date.today())
    assert result is None

    async with SessionLocal() as db:
        refreshed = await db.get(PeriodizationProposal, proposal_id)
        assert refreshed.status == periodization_params.STATUS_EXPIRED


# --- Сверх брифа: осиротевшее pending-предложение при "рычагов больше нет" ---
#
# Ревью (Important 2) воспроизвело это напрямую: предложение, созданное для
# "лифта нет в плане", оставалось pending с устаревшим советом уже после того,
# как ситуация стала REASON_ABOVE_CEILING. refresh_goal_proposals обязан
# погасить его сам, не дожидаясь, пока пользователь заметит несоответствие.

async def test_no_levers_after_reeval_replaces_stale_pending_proposal(
    test_user, seeded_history, active_block
):
    """Удачная переоценка (working_e1rm есть, блок активен), но decide()
    возвращает пустой список рычагов — здесь REASON_ABOVE_CEILING: короткий
    дедлайн (10 дней) и большой разрыв (100 -> ~206.7) требуют темпа далеко
    выше недельного потолка роста. Прежнее pending-предложение по ЭТОЙ цели
    обязано уйти в expired.

    ФИКС (Задача 17): раньше "рычагов нет" означало "сказать нечего" целиком,
    и вторая часть этого теста проверяла result is None. Теперь
    REASON_ABOVE_CEILING — не молчание: рычагов действительно нет, но есть
    честный совет "сдвиньте срок" (§5.4, решение 13) — новое предложение
    обязано заменить устаревшее, а не просто погасить его в никуда."""
    goal_id = await _primary_goal(
        test_user.id, seeded_history.id, 200.0,
        deadline=date.today() + timedelta(days=10), target_reps=1,
    )
    await _set_working_e1rm(test_user.id, seeded_history.id, 100.0)

    async with SessionLocal() as db:
        proposal = PeriodizationProposal(
            app_user_id=test_user.id,
            block_id=active_block.id,
            kind=periodization_params.KIND_GOAL_PLAN,
            reason_code="lift_missing",
            payload={"goal_id": goal_id, "exercise_id": seeded_history.id, "inputs_hash": "stale"},
            status=periodization_params.STATUS_PENDING,
        )
        db.add(proposal)
        await db.commit()
        await db.refresh(proposal)
        proposal_id = proposal.id

    async with SessionLocal() as db:
        result = await refresh_goal_proposals(db, test_user.id, date.today())
    assert result is not None
    assert result.id != proposal_id
    assert result.reason_code == goal_params.REASON_ABOVE_CEILING
    assert result.payload["levers"] == []
    assert result.payload["suggested_deadline"] is not None

    async with SessionLocal() as db:
        refreshed = await db.get(PeriodizationProposal, proposal_id)
        assert refreshed.status == periodization_params.STATUS_EXPIRED


async def test_incomplete_evaluation_leaves_pending_proposal_untouched(
    test_user, seeded_history, db
):
    """Граница находки: если оценку вообще не удалось довести до конца
    (здесь — нет АКТИВНОГО блока, evaluate() досчитал бы, но
    refresh_goal_proposals бракует до decide()), это НЕ содержательный вывод
    "рычагов больше нет" — временная неспособность посчитать не повод гасить
    предложение, которое пользователь мог как раз собираться применить."""
    goal_id = await _primary_goal(test_user.id, seeded_history.id, 100.0, target_reps=1)
    await _set_working_e1rm(test_user.id, seeded_history.id, 100.0)

    # Блок есть, но НЕ активен -> запрос активного блока в refresh_goal_proposals
    # вернёт None, и функция обязана остановиться ДО блока expire-логики.
    block = TrainingBlock(
        app_user_id=test_user.id,
        block_index=1,
        phases=[{"phase_number": 1, "name": "medium", "effort_tier": "medium", "length_days": 7}],
        microcycle_length=7,
        start_date=date.today() - timedelta(days=30),
        planned_end_date=date.today() - timedelta(days=1),
        status="completed",
    )
    db.add(block)
    await db.flush()

    proposal = PeriodizationProposal(
        app_user_id=test_user.id,
        block_id=block.id,
        kind=periodization_params.KIND_GOAL_PLAN,
        reason_code="lift_missing",
        payload={"goal_id": goal_id, "exercise_id": seeded_history.id, "inputs_hash": "stale"},
        status=periodization_params.STATUS_PENDING,
    )
    db.add(proposal)
    await db.commit()
    await db.refresh(proposal)
    proposal_id = proposal.id

    async with SessionLocal() as fresh_db:
        result = await refresh_goal_proposals(fresh_db, test_user.id, date.today())
    assert result is None

    async with SessionLocal() as fresh_db:
        untouched = await fresh_db.get(PeriodizationProposal, proposal_id)
        assert untouched.status == periodization_params.STATUS_PENDING


# --- Финальное ревью P0-12, фикс I4: падающий тренд — через настоящую
# историю, а не через руками подставленную -0.4 ---
#
# Раньше DecisionInput.trend_slope в проде получал `state["simulation"].
# nominal_slope` — плановый ПРОГНОЗНЫЙ темп, который simulate.run строит
# неотрицательным по конструкции (`max(raw_slope, 0.0)`, см. её докстринг):
# REASON_TREND_DOWN не мог сработать НИКОГДА на реальном пути. Три теста
# ниже доказывают фикс на трёх уровнях одной и той же цепочки: сырое число
# из repository, решение decide() на этом числе, и молчание всего
# refresh_goal_proposals end-to-end.

async def _seed_declining_sessions(user_id: int, exercise_id: int) -> None:
    """Три тренировки со стабильно падающим рабочим весом (60 -> 52 -> 44 кг,
    раз в неделю) — тренд e1RM по ним обязан получиться отрицательным."""
    async with SessionLocal() as db:
        for i, weight in enumerate([60.0, 52.0, 44.0]):
            workout = WorkoutSession(
                app_user_id=user_id, source="free", status="finished",
                finished_at=datetime.now(timezone.utc) - timedelta(days=(3 - i) * 7),
            )
            db.add(workout)
            await db.flush()
            se = WorkoutSessionExercise(
                workout_session_id=workout.id, exercise_id=exercise_id, order_index=0,
            )
            db.add(se)
            await db.flush()
            for set_number in range(1, 4):
                db.add(WorkoutSessionSet(
                    workout_session_exercise_id=se.id, set_number=set_number,
                    set_type="normal", weight=weight, reps=8,
                    effort_level="medium", is_completed=True,
                ))
        await db.commit()


async def test_historical_trend_slope_is_negative_for_real_declining_history(
    test_user, fresh_exercise
):
    """Уровень 1: сырая функция repository.historical_trend_slope, которую
    теперь и читает service.evaluate, реально умеет вернуть отрицательное
    число на настоящей истории — раньше эта функция даже не существовала,
    а её место занимал симулированный плановый темп."""
    await _seed_declining_sessions(test_user.id, fresh_exercise.id)

    async with SessionLocal() as db:
        trend = await repository.historical_trend_slope(db, test_user.id, fresh_exercise.id)

    assert trend < 0


async def test_falling_trend_from_real_history_blocks_acceleration(
    test_user, fresh_exercise
):
    """Уровень 2: тот же реальный тренд, поданный в decide() без единого
    руками подставленного числа, обрывает лестницу ДО обращения к
    пересимуляции рычагов — падающий тренд не ускоряется (спека §5.4)."""
    await _seed_declining_sessions(test_user.id, fresh_exercise.id)

    async with SessionLocal() as db:
        trend = await repository.historical_trend_slope(db, test_user.id, fresh_exercise.id)
    assert trend < 0

    inp = DecisionInput(
        rates=Rates(required=1.5, plan=0.5, ceiling=2.0),
        deadline=date.today() + timedelta(days=60),
        eta=date.today() + timedelta(days=120),
        lift_in_plan=True,
        trend_slope=trend,
        microcycles_left=8,
        scheme="double",
        is_heavy_compound=True,
    )

    def _unreachable_simulate_with(applied, kind, detail):
        raise AssertionError(
            "падающий тренд обязан оборвать decide() ДО обращения к "
            "пересимуляции рычагов"
        )

    levers, reason = decide.decide(inp, _unreachable_simulate_with)
    assert levers == []
    assert reason == goal_params.REASON_TREND_DOWN


async def test_refresh_goal_proposals_stays_silent_on_falling_trend(
    test_user, fresh_exercise, active_block
):
    """Уровень 3: сквозь весь продакшн-путь — настоящая падающая история +
    ведущая цель + активный блок -> refresh_goal_proposals не создаёт
    предложение ускорения. До фикса I4 trend_slope на этом пути физически
    не мог быть отрицательным, и этот сценарий не мог провалиться иначе,
    чем на подставленной вручную константе."""
    await _seed_declining_sessions(test_user.id, fresh_exercise.id)
    await _set_working_e1rm(test_user.id, fresh_exercise.id, 45.0)
    await _primary_goal(test_user.id, fresh_exercise.id, 200.0, target_reps=3)

    async with SessionLocal() as db:
        result = await refresh_goal_proposals(db, test_user.id, date.today())
    assert result is None


# --- Задача 17: рычагов нет, но предложение сдвинуть срок — не тишина ---
#
# Спека §5.4, решение 13: "быстрее биологического потолка план не сделает.
# Единственное предложение — сдвинуть дедлайн или снизить целевой вес;
# применяет пользователь руками". До этой задачи decide() честно возвращал
# REASON_ABOVE_CEILING, а refresh_goal_proposals читал пустой список
# рычагов как "сказать нечего" и не создавал вообще ничего — пользователь
# видел ETA за дедлайном без единого объяснения (см. брифинг задачи).

async def test_above_ceiling_proposal_carries_reachable_suggested_date(
    test_user, seeded_history, active_block
):
    """Короткий дедлайн (10 дней) и большой разрыв (100 -> ~206.7 кг) требуют
    темпа далеко выше недельного потолка новичка (1 %/нед) — REASON_ABOVE_
    CEILING. Предложение обязано нести дату, ДОСТИЖИМУЮ на этом самом
    потолке (не выдуманную): пересчитывая (target - current) / темп из
    payload, получаем ceiling с точностью до округления дней/недель."""
    await _primary_goal(
        test_user.id, seeded_history.id, 200.0,
        deadline=date.today() + timedelta(days=10), target_reps=1,
    )
    await _set_working_e1rm(test_user.id, seeded_history.id, 100.0)

    async with SessionLocal() as db:
        proposal = await refresh_goal_proposals(db, test_user.id, date.today())

    assert proposal is not None
    assert proposal.reason_code == goal_params.REASON_ABOVE_CEILING
    assert proposal.payload["levers"] == []
    suggested = proposal.payload["suggested_deadline"]
    assert suggested is not None

    target = proposal.payload["target_e1rm"]
    ceiling = proposal.payload["rates"]["ceiling"]
    weeks = (date.fromisoformat(suggested) - date.today()).days / 7.0
    achieved_rate = (target - 100.0) / weeks
    assert achieved_rate == pytest.approx(ceiling, rel=0.05)


async def test_above_ceiling_does_not_duplicate_on_repeated_wakeup(
    test_user, seeded_history, active_block
):
    """Тот же дедуп (inputs_hash + goal_id), что и для предложений с
    рычагами (test_repeated_refresh_does_not_duplicate) — не должен зависеть
    от того, есть рычаги или нет: второй вызов на тех же условиях обязан
    вернуть ТУ ЖЕ строку, а не завести вторую, и не должен молча
    экспирировать первую без замены."""
    await _primary_goal(
        test_user.id, seeded_history.id, 200.0,
        deadline=date.today() + timedelta(days=10), target_reps=1,
    )
    await _set_working_e1rm(test_user.id, seeded_history.id, 100.0)

    async with SessionLocal() as db:
        first = await refresh_goal_proposals(db, test_user.id, date.today())
    assert first is not None
    assert first.reason_code == goal_params.REASON_ABOVE_CEILING

    async with SessionLocal() as db:
        second = await refresh_goal_proposals(db, test_user.id, date.today())
    assert second is not None
    assert second.id == first.id

    async with SessionLocal() as db:
        rows = (await db.execute(
            select(PeriodizationProposal).where(
                PeriodizationProposal.app_user_id == test_user.id,
                PeriodizationProposal.kind == periodization_params.KIND_GOAL_PLAN,
                PeriodizationProposal.status == periodization_params.STATUS_PENDING,
            )
        )).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == first.id


async def test_goal_within_ceiling_still_gets_ordinary_lever_proposal(
    test_user, seeded_history, active_block
):
    """Регрессия: ветка "рычагов нет" не должна перехватывать случай, где
    рычаги реально есть. Тот же сетап, что test_repeated_refresh_does_not_
    duplicate (required ~0.39 кг/нед заведомо ниже ceiling ~1.0 кг/нед у
    новичка) — рычаг ensure_present обязан появиться, а suggested_deadline
    остаётся пустым (сдвигать срок незачем, план и так справляется)."""
    await _primary_goal(test_user.id, seeded_history.id, 100.0, target_reps=1)
    await _set_working_e1rm(test_user.id, seeded_history.id, 100.0)

    async with SessionLocal() as db:
        proposal = await refresh_goal_proposals(db, test_user.id, date.today())

    assert proposal is not None
    assert proposal.reason_code != goal_params.REASON_ABOVE_CEILING
    assert proposal.payload["levers"] != []
    assert proposal.payload["suggested_deadline"] is None
