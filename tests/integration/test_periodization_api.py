"""HTTP-контракт периодизации."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from api.services.day_template import DayTemplateType
from api.services.models import (
    AppUser,
    AppUserMesocycle,
    AppUserMicrocycle,
    DayBlueprint,
    DayMuscleTarget,
    Exercise,
    Mesocycle,
    MesocyclePhase,
    PeriodizationProposal,
    SplitBlueprint,
    SplitDaySlot,
    TrainingBlock,
    UserSplit,
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)
from api.services.periodization import params
from api.services.periodization.repository import ensure_active_block
from api.services.scheduling_engine import SchedulingEngine


async def _seed(db, user_id: int, start: date):
    meso = Mesocycle(author_id=user_id, name="Т", code=f"t_{uuid.uuid4().hex[:8]}", phases_in_cycle=2)
    db.add(meso)
    await db.flush()
    for number, tier in enumerate(["medium", "deload"], start=1):
        db.add(MesocyclePhase(mesocycle_id=meso.id, phase_number=number, name=tier, effort_tier=tier))
    db.add(AppUserMesocycle(
        app_user_id=user_id, mesocycle_id=meso.id, is_active=True,
        microcycle_length=7, current_phase=1,
    ))
    db.add(AppUserMicrocycle(
        app_user_id=user_id, name="М", length_days=7,
        days_mapping={"1": {"type": "hard", "tag": "full"}}, is_active=True,
    ))
    await db.commit()
    return await ensure_active_block(db, user_id, start)


@pytest.mark.asyncio
async def test_context_returns_coordinates(client, db, test_user: AppUser):
    await _seed(db, test_user.id, date.today())

    response = await client.get("/periodization/context")

    assert response.status_code == 200
    body = response.json()
    assert body["block"]["block_index"] == 1
    assert body["block"]["phase_ordinal"] == 1
    assert body["block"]["phases_total"] == 2
    assert body["block"]["effort_tier"] == "medium"
    assert body["proposals"] == []


@pytest.mark.asyncio
async def test_context_without_periodization_is_empty_not_an_error(client, test_user: AppUser):
    response = await client.get("/periodization/context")
    assert response.status_code == 200
    assert response.json() == {"block": None, "proposals": []}


@pytest.mark.asyncio
async def test_context_resolves_goal_plan_reason_text_from_goal_dictionary(
    client, db, test_user: AppUser
):
    """Ревью Задачи 15, Critical 2: _proposal_out резолвил reason_text ЛЮБОГО
    предложения через periodization.params.REASON_TEXTS — словарь кодов
    early_deload/postpone_deload/block_boundary/structural/volume_review, с
    кодами автопилота цели (goal.params.REASON_TEXTS: lift_missing,
    pace_behind, ...) не пересекающийся вовсе. goal_plan уходил на
    /periodization/context — тот самый эндпоинт, который читает карточка
    цели на Home-экране мобильного клиента, — с reason_text="" всегда.
    Проверяем прямо на HTTP-контракте эндпоинта, что теперь это не так и
    текст берётся из СВОЕГО словаря."""
    from api.services.goal import params as goal_params

    block = await _seed(db, test_user.id, date.today())

    proposal = PeriodizationProposal(
        app_user_id=test_user.id, block_id=block.id,
        kind=params.KIND_GOAL_PLAN, reason_code="pace_behind",
        payload={"goal_id": 1, "exercise_id": 1, "levers": []},
        status=params.STATUS_PENDING,
    )
    db.add(proposal)
    await db.commit()

    response = await client.get("/periodization/context")

    assert response.status_code == 200
    body = response.json()
    goal_rows = [p for p in body["proposals"] if p["kind"] == params.KIND_GOAL_PLAN]
    assert len(goal_rows) == 1
    assert goal_rows[0]["reason_code"] == "pace_behind"
    assert goal_rows[0]["reason_text"] == goal_params.REASON_TEXTS["pace_behind"]
    assert goal_rows[0]["reason_text"] != ""


@pytest.mark.asyncio
async def test_decision_endpoint_applies_and_repeats_safely(client, db, test_user: AppUser):
    block = await _seed(db, test_user.id, date.today())
    proposal = PeriodizationProposal(
        app_user_id=test_user.id, block_id=block.id,
        kind=params.KIND_EARLY_DELOAD, reason_code=params.REASON_FATIGUE_HIGH,
        payload={"after_phase_number": 1}, status=params.STATUS_PENDING,
    )
    db.add(proposal)
    await db.commit()
    marker = uuid.uuid4().hex

    first = await client.post(
        f"/periodization/proposals/{proposal.id}/decision",
        json={"action": "insert_deload", "client_uuid": marker},
    )
    second = await client.post(
        f"/periodization/proposals/{proposal.id}/decision",
        json={"action": "insert_deload", "client_uuid": marker},
    )

    assert first.status_code == 200
    assert first.json()["status"] == "applied"
    assert second.status_code == 200
    assert second.json()["status"] == "already_applied"


@pytest.mark.asyncio
async def test_conflicting_decision_returns_409(client, db, test_user: AppUser):
    block = await _seed(db, test_user.id, date.today())
    proposal = PeriodizationProposal(
        app_user_id=test_user.id, block_id=block.id,
        kind=params.KIND_EARLY_DELOAD, reason_code=params.REASON_FATIGUE_HIGH,
        payload={"after_phase_number": 1}, status=params.STATUS_PENDING,
    )
    db.add(proposal)
    await db.commit()

    await client.post(
        f"/periodization/proposals/{proposal.id}/decision",
        json={"action": "insert_deload", "client_uuid": uuid.uuid4().hex},
    )
    conflict = await client.post(
        f"/periodization/proposals/{proposal.id}/decision",
        json={"action": "decline", "client_uuid": uuid.uuid4().hex},
    )

    assert conflict.status_code == 409


@pytest.mark.asyncio
async def test_summary_reports_entry_and_exit(client, db, test_user: AppUser):
    block = await _seed(db, test_user.id, date.today())

    response = await client.get(f"/periodization/blocks/{block.id}/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["block_index"] == 1
    assert body["close_reason"] is None
    assert body["exercises"] == []


@pytest.mark.asyncio
async def test_summary_of_a_foreign_block_is_404(client, db, test_user: AppUser):
    response = await client.get("/periodization/blocks/999999/summary")
    assert response.status_code == 404


async def _make_second_user(db, marker: str) -> AppUser:
    """Второй реальный пользователь, созданный вручную (как в
    test_readiness_repository.test_save_signals_idempotency_scoped_per_user,
    около строки 227) — нужен, чтобы проверить именно межпользовательскую
    изоляцию, а не просто "несуществующий id"."""
    second_user = AppUser(
        firebase_uid=f"test-second-{marker}",
        email=f"test-second-{marker}@example.com",
        display_name="Second Test User",
    )
    db.add(second_user)
    await db.commit()
    await db.refresh(second_user)
    return second_user


async def _cleanup_second_user(db, second_user_id: int) -> None:
    from sqlalchemy import delete

    # TrainingBlock/PeriodizationProposal/Mesocycle/AppUserMesocycle/
    # AppUserMicrocycle все висят на app_users.id с ON DELETE CASCADE —
    # удаления самого AppUser достаточно, чтобы унести всё созданное _seed().
    await db.execute(delete(AppUser).where(AppUser.id == second_user_id))
    await db.commit()


@pytest.mark.asyncio
async def test_summary_of_another_users_block_is_404(client, db, test_user: AppUser):
    """Ревью, Находка 1: test_summary_of_a_foreign_block_is_404 запрашивает
    несуществующий id=999999 — такой тест прошёл бы, даже убери фильтр по
    app_user_id из запроса вовсе (он просто не найдётся ни для кого). Этот
    тест — прямая защита межпользовательской изоляции: блок РЕАЛЬНО
    существует, просто принадлежит ДРУГОМУ пользователю."""
    marker = uuid.uuid4().hex[:12]
    second_user = await _make_second_user(db, marker)
    try:
        foreign_block = await _seed(db, second_user.id, date.today())

        response = await client.get(f"/periodization/blocks/{foreign_block.id}/summary")

        assert response.status_code == 404
    finally:
        await _cleanup_second_user(db, second_user.id)


@pytest.mark.asyncio
async def test_decision_on_another_users_proposal_is_404(client, db, test_user: AppUser):
    """Симметричный случай для эндпоинта решения: чужое предложение
    недоступно. Проверяем не только код ответа, но и то, что запрос
    ДЕЙСТВИТЕЛЬНО ничего не сделал — предложение осталось pending, а не
    было по-тихому решено запросом от чужого имени."""
    marker = uuid.uuid4().hex[:12]
    second_user = await _make_second_user(db, marker)
    try:
        foreign_block = await _seed(db, second_user.id, date.today())
        foreign_proposal = PeriodizationProposal(
            app_user_id=second_user.id, block_id=foreign_block.id,
            kind=params.KIND_EARLY_DELOAD, reason_code=params.REASON_FATIGUE_HIGH,
            payload={"after_phase_number": 1}, status=params.STATUS_PENDING,
        )
        db.add(foreign_proposal)
        await db.commit()

        response = await client.post(
            f"/periodization/proposals/{foreign_proposal.id}/decision",
            json={"action": "insert_deload", "client_uuid": uuid.uuid4().hex},
        )

        assert response.status_code == 404

        refreshed = (
            await db.execute(
                select(PeriodizationProposal).where(PeriodizationProposal.id == foreign_proposal.id)
            )
        ).scalar_one()
        assert refreshed.status == params.STATUS_PENDING, (
            "запрос от чужого имени не должен был применить решение — "
            "предложение обязано остаться нетронутым"
        )
    finally:
        await _cleanup_second_user(db, second_user.id)


# --- Поправки к брифу Задачи 11 ------------------------------------------


async def _seed_with_split(db, user_id: int, start: date):
    """Как _seed(), но добавляет минимальный активный сплит из одного
    тренировочного (не-отдыхового) дня — нужен SchedulingEngine.generate_block_days,
    чтобы в календаре реально появились дни (поправка 2: workouts_to_deload
    считается по настоящим UserCalendarDay, а без сплита слот-очередь пуста и
    generate_block_days создаёт ноль строк)."""
    meso = Mesocycle(author_id=user_id, name="Т", code=f"t_{uuid.uuid4().hex[:8]}", phases_in_cycle=2)
    db.add(meso)
    await db.flush()
    for number, tier in enumerate(["medium", "deload"], start=1):
        db.add(MesocyclePhase(mesocycle_id=meso.id, phase_number=number, name=tier, effort_tier=tier))
    db.add(AppUserMesocycle(
        app_user_id=user_id, mesocycle_id=meso.id, is_active=True,
        microcycle_length=7, current_phase=1,
    ))
    db.add(AppUserMicrocycle(
        app_user_id=user_id, name="М", length_days=7,
        days_mapping={str(i): {"type": "hard", "tag": "full"} for i in range(1, 8)},
        is_active=True,
    ))

    blueprint = SplitBlueprint(name="Тестовый сплит", author_id=user_id, length_days=1, is_system=False)
    day = DayBlueprint(
        name="Full Body", author_id=user_id, template_type=DayTemplateType.FULL_BODY, is_system=False,
    )
    db.add_all([blueprint, day])
    await db.flush()
    db.add(DayMuscleTarget(day_id=day.id, muscle_group_id="full_body"))
    db.add(SplitDaySlot(blueprint_id=blueprint.id, day_id=day.id, day_order=0))
    db.add(UserSplit(
        app_user_id=user_id, blueprint_id=blueprint.id, is_active=True, current_day=1,
        selected_plans={},
    ))
    await db.commit()
    return await ensure_active_block(db, user_id, start)


@pytest.mark.asyncio
async def test_context_reports_real_workouts_to_deload(client, db, test_user: AppUser):
    """Поправка 2 брифа: workouts_to_deload не заглушка, а настоящий подсчёт
    не-выходных/не-заблокированных дней календаря до старта ближайшей
    разгрузки — той же логикой, что и repository.collect_decision_input."""
    today = date.today()
    block = await _seed_with_split(db, test_user.id, today)
    created = await SchedulingEngine.generate_block_days(
        db, test_user.id, block, from_date=today, until_date=block.planned_end_date
    )
    assert created > 0, "нужен реальный календарь, иначе тест ничего не проверяет"
    await db.commit()

    response = await client.get("/periodization/context")

    assert response.status_code == 200
    body = response.json()
    # Первая фаза — 7 дней "medium", вторая — "deload": разгрузка начинается
    # через 7 дней от старта блока (сегодня — 1-й день блока).
    assert body["block"]["days_to_deload"] == 7
    assert body["block"]["workouts_to_deload"] == 7, (
        "сплит из одного полнотельного дня без блэкаутов — все 7 дней "
        "до разгрузки рабочие, ни один не выходной и не заблокирован"
    )


@pytest.mark.asyncio
async def test_context_workouts_to_deload_is_null_without_upcoming_deload(client, db, test_user: AppUser):
    """Симметричный случай: если разгрузки впереди нет (days_to_deload is
    None), workouts_to_deload обязан быть null, а не 0 или заглушкой."""
    meso = Mesocycle(author_id=test_user.id, name="Т", code=f"t_{uuid.uuid4().hex[:8]}", phases_in_cycle=1)
    db.add(meso)
    await db.flush()
    db.add(MesocyclePhase(mesocycle_id=meso.id, phase_number=1, name="medium", effort_tier="medium"))
    db.add(AppUserMesocycle(
        app_user_id=test_user.id, mesocycle_id=meso.id, is_active=True,
        microcycle_length=7, current_phase=1,
    ))
    db.add(AppUserMicrocycle(
        app_user_id=test_user.id, name="М", length_days=7,
        days_mapping={"1": {"type": "hard", "tag": "full"}}, is_active=True,
    ))
    await db.commit()
    await ensure_active_block(db, test_user.id, date.today())

    response = await client.get("/periodization/context")

    assert response.status_code == 200
    body = response.json()
    assert body["block"]["days_to_deload"] is None
    assert body["block"]["workouts_to_deload"] is None


@pytest.mark.asyncio
async def test_decision_on_proposal_from_closed_block_returns_409_block_closed(
    client, db, test_user: AppUser
):
    """Поправка 1 брифа: apply_decision тоже отвечает конфликтом, когда
    блок-мутирующее действие (insert_deload/close_block/postpone) приходит
    по предложению от блока, который автопереход уже закрыл, пока
    пользователь не отвечал на карточку. Этот случай отличается от "чужого
    решения по уже решённому предложению" (proposal.status уже не pending) —
    здесь proposal ВСЁ ЕЩЁ pending, а закрылся именно блок. Тело ответа
    обязано нести reason=block_closed, чтобы клиент отличал этот случай от
    обычного конфликта решений."""
    start = date.today() - timedelta(days=20)
    block1 = await _seed(db, test_user.id, start)
    proposal = PeriodizationProposal(
        app_user_id=test_user.id, block_id=block1.id,
        kind=params.KIND_EARLY_DELOAD, reason_code=params.REASON_FATIGUE_HIGH,
        payload={"after_phase_number": 1}, status=params.STATUS_PENDING,
    )
    db.add(proposal)
    await db.commit()

    # Автопереход: реальное "сегодня" уже позже planned_end_date block1
    # (2 фазы по 7 дней = 14 дней, начало 20 дней назад) — закрывает block1 и
    # открывает block2.
    block2 = await ensure_active_block(db, test_user.id, date.today())
    assert block2.id != block1.id, "переход обязан был случиться для этого теста"
    await db.commit()

    response = await client.post(
        f"/periodization/proposals/{proposal.id}/decision",
        json={"action": "insert_deload", "client_uuid": uuid.uuid4().hex},
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["status"] == "conflict"
    assert detail["reason"] == "block_closed"


@pytest.mark.asyncio
async def test_summary_is_reachable_for_a_closed_block(client, db, test_user: AppUser):
    """Поправка 3 брифа: карточка итогов чаще всего звучит именно по блоку,
    который только что закрыл автопереход, — эндпоинт не должен требовать,
    чтобы блок был активным."""
    start = date.today() - timedelta(days=20)
    block1 = await _seed(db, test_user.id, start)
    block1_id = block1.id

    block2 = await ensure_active_block(db, test_user.id, date.today())
    assert block2.id != block1_id, "переход обязан был случиться для этого теста"
    await db.commit()

    closed = (
        await db.execute(select(TrainingBlock).where(TrainingBlock.id == block1_id))
    ).scalar_one()
    assert closed.status == "closed", "предпосылка теста: блок из запроса ниже обязан быть закрыт"

    response = await client.get(f"/periodization/blocks/{block1_id}/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "closed"


@pytest.mark.asyncio
async def test_context_extends_a_short_calendar_before_counting(client, db, test_user: AppUser):
    """Ревью, Находка 2: workouts_to_deload считается по УЖЕ существующим
    UserCalendarDay, а достраивает календарь только
    SchedulingEngine.ensure_horizon — раньше её звал ТОЛЬКО роутер календаря.
    Если горизонт короче расстояния до разгрузки, счётчик занижал бы число.

    Здесь календарь намеренно сгенерирован лишь на три дня вперёд, а разгрузка
    начинается через семь. Без вызова ensure_horizon внутри эндпоинта счётчик
    вернул бы 3 вместо 7.

    Заметьте: полностью ПУСТОЙ календарь здесь не проверяется, и это не
    упущение. ensure_horizon по построению только ПРОДЛЕВАЕТ существующее
    расписание (при пустом календаре она выходит сразу — достраивать нечего),
    а первичное разворачивание делает запуск сплита. Пустой календарь при
    настроенной периодизации означает, что сплит ещё не запускали, и ноль
    тренировок до разгрузки там честный ответ."""
    today = date.today()
    block = await _seed_with_split(db, test_user.id, today)
    created = await SchedulingEngine.generate_block_days(
        db, test_user.id, block, from_date=today, until_date=today + timedelta(days=2)
    )
    assert created == 3, "предпосылка теста: календарь короче, чем расстояние до разгрузки"
    await db.commit()

    response = await client.get("/periodization/context")

    assert response.status_code == 200
    body = response.json()
    assert body["block"]["days_to_deload"] == 7
    assert body["block"]["workouts_to_deload"] == 7, (
        "эндпоинт обязан сам достроить горизонт перед подсчётом: иначе "
        "пользователь увидит 3 тренировки до разгрузки вместо семи"
    )


@pytest.mark.asyncio
async def test_context_reports_zero_workouts_during_deload(client, db, test_user: AppUser):
    """Ревью, Находка 4: если текущая фаза блока — сама разгрузка,
    days_to_deload равен 0 (она уже идёт), и workouts_to_deload обязан быть
    РОВНО 0 — не null (это означало бы "разгрузки впереди нет" — неверно,
    она идёт прямо сейчас) и не отрицательным числом."""
    today = date.today()
    # Первая фаза "medium" длиной 7 дней уже прошла — блок стартовал 7 дней
    # назад, поэтому today приходится на 8-й день блока, первый день фазы
    # "deload" (см. _seed: фазы по 7 дней каждая, medium затем deload).
    start = today - timedelta(days=7)
    await _seed(db, test_user.id, start)

    response = await client.get("/periodization/context")

    assert response.status_code == 200
    body = response.json()
    assert body["block"]["effort_tier"] == "deload", "предпосылка теста: сейчас идёт разгрузка"
    assert body["block"]["days_to_deload"] == 0
    assert body["block"]["workouts_to_deload"] == 0, (
        "разгрузка уже идёт — тренировок ДО её начала осталось 0, а не null "
        "и не отрицательное число"
    )


# --- Разрыв "висящие предложения по закрытому блоку" --------------------


@pytest.mark.asyncio
async def test_context_returns_boundary_proposal_from_the_closed_block(
    client, db, test_user: AppUser
):
    """Главный тест сквозного разрыва: карточка итогов (block_boundary)
    материализуется service.refresh_proposals ПО БЛОКУ, КОТОРЫЙ ТОЛЬКО ЧТО
    ЗАКРЫЛ автопереход, — по построению она никогда не лежит на блоке,
    который в момент запроса активен. Старый фильтр `block_id == block.id`
    (по активному блоку) эту карточку не найдёт никогда, хотя
    refresh_proposals её честно создал.

    Блок с реальной завершённой тренировкой внутри доходит до конца, запрос
    /periodization/context сам вызывает safe_refresh_proposals, автопереход
    закрывает block1 и открывает block2, пересчёт создаёт карточку итогов
    по block1. Проверяем, что она есть в ответе и что её block_id указывает
    на ЗАКРЫТЫЙ блок, а не на активный.
    """
    marker = uuid.uuid4().hex[:8]
    exercise = Exercise(
        name=f"Тестовое упражнение итогов блока {marker}",
        category="base",
        main_muscle_group="chest",
        difficulty="beginner",
        equipment_needed=[],
        source="custom",
        app_user_id=test_user.id,
    )
    db.add(exercise)
    await db.flush()

    start = date.today() - timedelta(days=20)
    block1 = await _seed(db, test_user.id, start)

    workout = WorkoutSession(
        app_user_id=test_user.id,
        source="free",
        status="finished",
        finished_at=datetime.now(timezone.utc),
        training_block_id=block1.id,
    )
    db.add(workout)
    await db.flush()

    se = WorkoutSessionExercise(
        workout_session_id=workout.id, exercise_id=exercise.id, order_index=0
    )
    db.add(se)
    await db.flush()

    for set_number in range(1, 4):
        db.add(
            WorkoutSessionSet(
                workout_session_exercise_id=se.id,
                set_number=set_number,
                set_type="normal",
                weight=60.0,
                reps=8,
                effort_level="medium",
                is_completed=True,
            )
        )
    await db.commit()

    try:
        response = await client.get("/periodization/context")

        assert response.status_code == 200
        body = response.json()
        active_block_id = body["block"]["block_id"]
        assert active_block_id != block1.id, (
            "предпосылка теста: автопереход обязан был закрыть block1 и "
            "открыть новый активный блок"
        )

        boundary = [p for p in body["proposals"] if p["kind"] == "block_boundary"]
        assert len(boundary) == 1, (
            "карточка итогов закрытого блока обязана дойти до клиента через "
            "/periodization/context — раньше фильтр по активному блоку её "
            "отсекал"
        )
        assert boundary[0]["block_id"] == block1.id, (
            "карточка итогов обязана нести id ЗАКРЫТОГО блока, а не "
            "активного — иначе клиент откроет разбор не того блока"
        )
    finally:
        # Порядок важен (тот же, что в conftest.test_user и в
        # test_periodization_repository.test_state_snapshot_captures_progression):
        # подходы -> упражнения сессии -> сессия -> упражнение.
        await db.execute(
            delete(WorkoutSessionSet).where(
                WorkoutSessionSet.workout_session_exercise_id == se.id
            )
        )
        await db.execute(
            delete(WorkoutSessionExercise).where(WorkoutSessionExercise.id == se.id)
        )
        await db.execute(delete(WorkoutSession).where(WorkoutSession.id == workout.id))
        await db.execute(delete(Exercise).where(Exercise.id == exercise.id))
        await db.commit()


@pytest.mark.asyncio
async def test_context_proposal_carries_its_block_id(client, db, test_user: AppUser):
    """У каждого предложения в ответе /periodization/context обязан быть
    block_id — иначе клиент не знает, к какому блоку вести пользователя, и
    вынужден подставлять id активного блока (что для карточки итогов
    закрытого блока — уже НЕ ТОТ блок)."""
    block = await _seed(db, test_user.id, date.today())
    proposal = PeriodizationProposal(
        app_user_id=test_user.id, block_id=block.id,
        kind=params.KIND_EARLY_DELOAD, reason_code=params.REASON_FATIGUE_HIGH,
        payload={"after_phase_number": 1}, status=params.STATUS_PENDING,
    )
    db.add(proposal)
    await db.commit()

    response = await client.get("/periodization/context")

    assert response.status_code == 200
    body = response.json()
    assert len(body["proposals"]) == 1
    assert body["proposals"][0]["block_id"] == block.id
