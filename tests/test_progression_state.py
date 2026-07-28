"""Восстановление состояния прогрессии из истории (спека P0-06 §8.3)."""

from datetime import datetime, timedelta

import pytest

from api.services.progression import params
from api.services.progression.metrics import e1rm, weight_for_e1rm
from api.services.progression.state import rebuild_state
from api.services.progression.types import (
    ExerciseHistory,
    Prescription,
    SessionFact,
    SetFact,
    SetPrescription,
)

STEP = 2.5
BASE = datetime(2026, 1, 1)


def presc(weight, rep_min=8, rep_max=12) -> Prescription:
    return Prescription(
        scheme="double",
        sets=(SetPrescription(1, weight, rep_min, rep_max, 2, "normal"),),
        reason_code="progressed",
        reason_text="x",
    )


def session(idx, weight, reps, *, is_deload=False, prescribed=None) -> SessionFact:
    return SessionFact(
        session_id=idx,
        finished_at=BASE + timedelta(days=idx),
        prescription=presc(prescribed if prescribed is not None else weight),
        sets=(SetFact(1, weight, reps, 2),),
        is_deload=is_deload,
    )


def history(*sessions) -> ExerciseHistory:
    # Движок принимает историю от новой к старой.
    return ExerciseHistory(exercise_id=1, sessions=tuple(reversed(sessions)))


def test_empty_history_gives_empty_state():
    st = rebuild_state(ExerciseHistory(exercise_id=1), STEP)
    assert st.working_e1rm is None
    assert st.completed_sessions == 0
    assert st.stalled is False


def test_working_e1rm_comes_from_latest_session():
    st = rebuild_state(history(session(1, 40.0, 10), session(2, 45.0, 8)), STEP)
    assert st.working_e1rm == pytest.approx(45.0 * (1 + 10 / 30))


def test_best_e1rm_ever_is_max_over_history():
    st = rebuild_state(history(session(1, 50.0, 10), session(2, 40.0, 8)), STEP)
    assert st.best_e1rm_ever == pytest.approx(50.0 * (1 + 12 / 30))


def test_training_max_is_ninety_percent_of_working_e1rm():
    st = rebuild_state(history(session(1, 100.0, 1)), STEP)
    assert st.training_max == pytest.approx(st.working_e1rm * params.TRAINING_MAX_RATIO)


def test_last_top_weight_is_from_latest_session_prescription():
    st = rebuild_state(history(session(1, 40.0, 10), session(2, 42.5, 9)), STEP)
    assert st.last_top_weight == pytest.approx(42.5)


def test_last_top_weight_updates_when_latest_prescription_is_zero():
    # Регрессия: раньше `last_top = prescription.top_weight or last_top`
    # считал 0.0 ложным значением и не обновлял last_top, оставляя вес
    # от более старой сессии. У самой свежей сессии предписан явный 0.0 —
    # last_top_weight обязан стать 0.0, а не унаследовать 45.0.
    st = rebuild_state(
        history(
            session(1, 40.0, 10, prescribed=45.0),
            session(2, 42.0, 8, prescribed=0.0),
        ),
        STEP,
    )
    assert st.last_top_weight == 0.0


def test_consecutive_misses_counts_trailing_misses():
    st = rebuild_state(
        history(
            session(1, 40.0, 10),
            session(2, 40.0, 5),  # ниже rep_min=8
            session(3, 40.0, 6),
        ),
        STEP,
    )
    assert st.consecutive_misses == 2


def test_a_hit_resets_the_miss_streak():
    st = rebuild_state(
        history(session(1, 40.0, 5), session(2, 40.0, 6), session(3, 40.0, 10)),
        STEP,
    )
    assert st.consecutive_misses == 0


def test_plateau_needs_minimum_history():
    # Пять сессий без прироста — данных мало, плато не объявляем.
    sessions = [session(i, 40.0, 8) for i in range(1, 6)]
    st = rebuild_state(history(*sessions), STEP)
    assert st.completed_sessions == 5
    assert st.stalled is False


def test_plateau_fires_after_enough_stagnant_sessions():
    sessions = [session(i, 40.0, 8) for i in range(1, 8)]
    st = rebuild_state(history(*sessions), STEP)
    assert st.sessions_since_gain >= params.PLATEAU_STALL_SESSIONS
    assert st.stalled is True


def test_growth_resets_the_stall_counter():
    sessions = [session(i, 40.0, 8) for i in range(1, 7)]
    sessions.append(session(7, 50.0, 10))
    st = rebuild_state(history(*sessions), STEP)
    assert st.sessions_since_gain == 0
    assert st.stalled is False


def test_gain_within_tolerance_counts_as_stall():
    # Прирост e1RM есть, но он строго меньше допуска на шум округления
    # (PLATEAU_GAIN_TOLERANCE) — это ровно тот случай, ради которого допуск
    # существует: шаг оборудования не должен выдаваться за реальный прогресс.
    # Границу считаем через саму константу, а не через захардкоженное число,
    # чтобы тест следовал за params.py.
    base_weight, base_reps, base_rir = 100.0, 10, 2
    base_e1rm = e1rm(base_weight, base_reps, base_rir)
    boundary_e1rm = base_e1rm * params.PLATEAU_GAIN_TOLERANCE
    within_tolerance_e1rm = (base_e1rm + boundary_e1rm) / 2
    assert base_e1rm < within_tolerance_e1rm < boundary_e1rm
    second_weight = weight_for_e1rm(within_tolerance_e1rm, base_reps, base_rir)

    st = rebuild_state(
        history(
            session(1, base_weight, base_reps),
            session(2, second_weight, base_reps),
        ),
        STEP,
    )
    # Прирост в пределах допуска в счёт не идёт — счётчик застоя растёт.
    assert st.sessions_since_gain == 1


def test_gain_above_tolerance_resets_stall_counter():
    # Тот же сценарий, но e1RM второй сессии выходит за границу допуска —
    # это уже настоящий прогресс, и счётчик застоя должен обнулиться.
    base_weight, base_reps, base_rir = 100.0, 10, 2
    base_e1rm = e1rm(base_weight, base_reps, base_rir)
    boundary_e1rm = base_e1rm * params.PLATEAU_GAIN_TOLERANCE
    above_tolerance_e1rm = boundary_e1rm * 1.001  # чуть выше границы допуска
    second_weight = weight_for_e1rm(above_tolerance_e1rm, base_reps, base_rir)

    st = rebuild_state(
        history(
            session(1, base_weight, base_reps),
            session(2, second_weight, base_reps),
        ),
        STEP,
    )
    assert st.sessions_since_gain == 0


def test_deload_sessions_do_not_count_toward_stall():
    sessions = [session(i, 40.0, 8) for i in range(1, 7)]
    sessions.append(session(7, 25.0, 8, is_deload=True))
    st = rebuild_state(history(*sessions), STEP)
    # Сессия разгрузки не увеличивает счётчик застоя: было 5 (первая задаёт базу).
    assert st.sessions_since_gain == 5


def test_skipped_sessions_do_not_count():
    empty = SessionFact(
        session_id=99, finished_at=BASE, prescription=presc(40.0), sets=()
    )
    sessions = [session(i, 40.0, 8) for i in range(1, 4)]
    st_without = rebuild_state(history(*sessions), STEP)
    st_with = rebuild_state(history(*sessions, empty), STEP)
    assert st_with.sessions_since_gain == st_without.sessions_since_gain
    assert st_with.completed_sessions == st_without.completed_sessions


def test_last_scheme_is_taken_from_latest_prescription():
    st = rebuild_state(history(session(1, 40.0, 10)), STEP)
    assert st.last_scheme == "double"
