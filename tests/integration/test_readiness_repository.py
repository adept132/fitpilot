"""Журнал наблюдений и жизненный цикл боли (спека P0-07 §6.3)."""

from datetime import datetime, timedelta, timezone

import pytest

from api.services.readiness import params, repository
from api.services.readiness.types import CheckinSignals


class _FakeExercise:
    def __init__(self, main, secondary, action):
        self.id = 7
        self.main_muscle_group = main
        self.secondary_muscle_groups = secondary
        self.action = action


def test_build_target_normalizes_russian_muscle_names():
    ex = _FakeExercise("Квадрицепсы", ["Ягодичные"], "squat")
    target = repository.build_exercise_target(ex)
    assert target.main_muscle == "quads"
    assert target.secondary_muscles == ("glutes",)
    assert target.action == "squat"


def test_build_target_unwraps_enum_action():
    from api.services.exercise_pattern_tags import ExerciseAction

    ex = _FakeExercise("Квадрицепсы", [], ExerciseAction.squat)
    assert repository.build_exercise_target(ex).action == "squat"


def test_build_target_survives_unknown_muscle():
    ex = _FakeExercise("Хвост", ["Крыло"], "unknown")
    target = repository.build_exercise_target(ex)
    assert target.main_muscle is None
    assert target.secondary_muscles == ()


def test_build_target_defaults_action_to_unknown():
    ex = _FakeExercise("Грудь", [], None)
    assert repository.build_exercise_target(ex).action == "unknown"


async def test_save_signals_writes_one_row_per_answer(db_session, app_user):
    signals = CheckinSignals(
        sleep=2, stress=4, soreness={"quads": 3}, pain={"knee": 2}
    )
    rows = await repository.save_signals(
        db_session, app_user.id, signals, params.SOURCE_CHECKIN, "uuid-1"
    )
    await db_session.flush()

    kinds = sorted(r.kind for r in rows)
    assert kinds == ["pain", "sleep", "soreness", "stress"]
    assert all(r.client_uuid == "uuid-1" for r in rows)
    assert all(r.source == params.SOURCE_CHECKIN for r in rows)
    by_kind = {r.kind: r for r in rows}
    assert by_kind["soreness"].subject == "quads"
    assert by_kind["pain"].subject == "knee"
    assert by_kind["sleep"].subject is None


async def test_save_signals_is_idempotent_by_client_uuid(db_session, app_user):
    signals = CheckinSignals(sleep=3, stress=3)
    await repository.save_signals(
        db_session, app_user.id, signals, params.SOURCE_CHECKIN, "uuid-dup"
    )
    await db_session.flush()
    second = await repository.save_signals(
        db_session, app_user.id, signals, params.SOURCE_CHECKIN, "uuid-dup"
    )
    await db_session.flush()

    assert second == []
    loaded = await repository.load_signals(db_session, app_user.id, "uuid-dup")
    assert loaded.sleep == 3


async def test_load_signals_round_trips(db_session, app_user):
    original = CheckinSignals(
        sleep=1, stress=5, soreness={"quads": 3, "chest": 2}, pain={"knee": 1}
    )
    await repository.save_signals(
        db_session, app_user.id, original, params.SOURCE_CHECKIN, "uuid-rt"
    )
    await db_session.flush()

    loaded = await repository.load_signals(db_session, app_user.id, "uuid-rt")
    assert loaded.sleep == 1
    assert loaded.stress == 5
    assert loaded.soreness == {"quads": 3, "chest": 2}
    assert loaded.pain == {"knee": 1}


async def test_load_signals_for_unknown_uuid_is_empty(db_session, app_user):
    loaded = await repository.load_signals(db_session, app_user.id, "nope")
    assert loaded.sleep is None
    assert loaded.soreness == {}


async def test_active_pain_returns_recent_non_zero(db_session, app_user):
    await repository.save_signals(
        db_session, app_user.id,
        CheckinSignals(pain={"knee": 2, "elbow": 0}),
        params.SOURCE_POST_SESSION, "uuid-pain",
    )
    await db_session.flush()

    active = await repository.active_pain(db_session, app_user.id)
    assert active == {"knee": 2}


async def test_clearing_pain_writes_a_new_row_not_a_delete(db_session, app_user):
    # Журнал append-only: "прошло" — это новая запись value=0.
    await repository.save_signals(
        db_session, app_user.id, CheckinSignals(pain={"knee": 3}),
        params.SOURCE_POST_SESSION, "uuid-a",
    )
    await db_session.flush()
    await repository.save_signals(
        db_session, app_user.id, CheckinSignals(pain={"knee": 0}),
        params.SOURCE_CHECKIN, "uuid-b",
    )
    await db_session.flush()

    assert await repository.active_pain(db_session, app_user.id) == {}

    from sqlalchemy import select
    from api.services.models import UserObservation

    rows = (
        await db_session.execute(
            select(UserObservation).where(
                UserObservation.app_user_id == app_user.id,
                UserObservation.kind == params.KIND_PAIN,
            )
        )
    ).scalars().all()
    assert len(rows) == 2  # история травмы сохранена


async def test_stale_pain_falls_out_of_the_window(db_session, app_user):
    old = datetime.now(timezone.utc) - timedelta(
        days=params.PAIN_ACTIVE_DAYS + 1
    )
    await repository.save_signals(
        db_session, app_user.id,
        CheckinSignals(pain={"knee": 3}, observed_at=old),
        params.SOURCE_POST_SESSION, "uuid-old",
    )
    await db_session.flush()

    assert await repository.active_pain(db_session, app_user.id) == {}


async def test_latest_observation_per_subject_wins(db_session, app_user):
    earlier = datetime.now(timezone.utc) - timedelta(days=2)
    await repository.save_signals(
        db_session, app_user.id,
        CheckinSignals(pain={"knee": 1}, observed_at=earlier),
        params.SOURCE_POST_SESSION, "uuid-1",
    )
    await db_session.flush()
    await repository.save_signals(
        db_session, app_user.id, CheckinSignals(pain={"knee": 3}),
        params.SOURCE_POST_SESSION, "uuid-2",
    )
    await db_session.flush()

    assert await repository.active_pain(db_session, app_user.id) == {"knee": 3}


async def test_same_timestamp_tiebreak_by_id(db_session, app_user):
    # Страхует таймбрейк по id при одинаковом observed_at.
    # Если кто-то удалит order_by(...id.asc()) из active_pain(), это ловит.
    same_time = datetime.now(timezone.utc)
    await repository.save_signals(
        db_session, app_user.id,
        CheckinSignals(pain={"knee": 2}, observed_at=same_time),
        params.SOURCE_POST_SESSION, "uuid-1st",
    )
    await db_session.flush()
    await repository.save_signals(
        db_session, app_user.id,
        CheckinSignals(pain={"knee": 0}, observed_at=same_time),
        params.SOURCE_POST_SESSION, "uuid-2nd",
    )
    await db_session.flush()

    # Вторая вставка с тем же observed_at и выше id должна победить,
    # обнулив боль. Это зависит от order_by(...id.asc()).
    assert await repository.active_pain(db_session, app_user.id) == {}


async def test_exact_window_boundary_is_inclusive(db_session, app_user):
    # Страхует границу окна PAIN_ACTIVE_DAYS: >= (не >).
    # Наблюдение ровно PAIN_ACTIVE_DAYS дней назад должно быть АКТИВНО.
    # Если поменять на >, это поймёт.
    now = datetime.now(timezone.utc)
    boundary = now - timedelta(days=params.PAIN_ACTIVE_DAYS)
    await repository.save_signals(
        db_session, app_user.id,
        CheckinSignals(pain={"knee": 2}, observed_at=boundary),
        params.SOURCE_POST_SESSION, "uuid-boundary",
    )
    await db_session.flush()

    # Передаём тот же `now`, чтобы граница вычислялась точно относительно
    # момента сохранения, без промежутка времени, который мог бы сделать
    # наблюдение истёкшим.
    active = await repository.active_pain(db_session, app_user.id, now=now)
    assert active == {"knee": 2}, (
        f"Боль ровно на границе {params.PAIN_ACTIVE_DAYS} дней должна быть активна"
    )


async def test_save_signals_idempotency_scoped_per_user(db_session, app_user):
    # Страхует scope идемпотентности по app_user_id.
    # Если app_user_id выпадет из WHERE в save_signals, разные пользователи
    # начнут подавлять друг друга при совпадении client_uuid.
    from api.services.models import AppUser
    import uuid as uuid_module

    marker = uuid_module.uuid4().hex[:12]
    second_user = AppUser(
        firebase_uid=f"test-second-{marker}",
        email=f"test-second-{marker}@example.com",
        display_name="Second Test User",
    )
    db_session.add(second_user)
    await db_session.flush()

    # Оба пользователя сохраняют сигналы под одним и тем же client_uuid,
    # но разными значениями. Результат ДОЛЖЕН быть независимым.
    shared_uuid = "uuid-shared-across-users"
    first_signals = CheckinSignals(sleep=4, stress=2)
    second_signals = CheckinSignals(sleep=1, stress=5)

    await repository.save_signals(
        db_session, app_user.id, first_signals,
        params.SOURCE_CHECKIN, shared_uuid,
    )
    await db_session.flush()
    await repository.save_signals(
        db_session, second_user.id, second_signals,
        params.SOURCE_CHECKIN, shared_uuid,
    )
    await db_session.flush()

    # Повторная попытка от первого пользователя должна вернуть пусто
    # (уже записано), а не перезаписаться.
    retry_first = await repository.save_signals(
        db_session, app_user.id, first_signals,
        params.SOURCE_CHECKIN, shared_uuid,
    )
    await db_session.flush()
    assert retry_first == [], "Повторная идемпотентная попытка должна вернуть []"

    # Каждый пользователь видит только свои сигналы.
    first_loaded = await repository.load_signals(
        db_session, app_user.id, shared_uuid
    )
    second_loaded = await repository.load_signals(
        db_session, second_user.id, shared_uuid
    )

    assert first_loaded.sleep == 4
    assert first_loaded.stress == 2
    assert second_loaded.sleep == 1
    assert second_loaded.stress == 5
