"""Тесты правил аномального ввода (api/services/anomaly_guard.py)."""

import pytest

from api.services.anomaly_guard import (
    AnomalyVerdict,
    ExerciseStats,
    check_set,
    resolve_is_anomalous,
)

NO_HISTORY = None
RICH_HISTORY = ExerciseStats(sessions=10, best_e1rm=140.0, median_weight_kg=100.0)
THIN_HISTORY = ExerciseStats(sessions=2, best_e1rm=140.0, median_weight_kg=100.0)


def test_normal_set_is_ok():
    assert check_set(100.0, 10, "normal", RICH_HISTORY).level == "ok"


def test_empty_set_is_ok():
    # Пустая строка — это ещё не факт, проверять нечего.
    assert check_set(None, None, "normal", RICH_HISTORY).level == "ok"


def test_bodyweight_set_is_not_anomalous():
    # Нулевой вес при ненулевых повторах — легитимный вариант с собственным весом.
    assert check_set(0.0, 12, "normal", RICH_HISTORY).level == "ok"


@pytest.mark.parametrize("weight_kg", [600.1, 1000.0, 5000.0])
def test_absurd_weight(weight_kg):
    verdict = check_set(weight_kg, 5, "normal", NO_HISTORY)
    assert verdict.level == "absurd"
    assert verdict.reason


@pytest.mark.parametrize("reps", [301, 1000])
def test_absurd_reps(reps):
    assert check_set(50.0, reps, "normal", NO_HISTORY).level == "absurd"


def test_absolute_rules_work_without_history():
    # Абсолютные правила не требуют истории вообще.
    assert check_set(700.0, 5, "normal", NO_HISTORY).level == "absurd"


def test_e1rm_jump_is_suspicious():
    # e1RM = 250 * (1 + 12/30) = 350 > 1.5 * 140 = 210.
    assert check_set(250.0, 10, "normal", RICH_HISTORY).level == "suspicious"


def test_weight_far_above_median_is_suspicious():
    # 350 > 3 * 100, при этом e1RM-правило тоже сработает — важно, что не "ok".
    assert check_set(350.0, 1, "normal", RICH_HISTORY).level == "suspicious"


def test_thin_history_disables_relative_rules():
    # Меньше трёх сессий — относительные правила молчат, остаются только абсолютные.
    assert check_set(250.0, 10, "normal", THIN_HISTORY).level == "ok"


def test_warmup_set_skips_relative_rules():
    # Разминка не участвует в аналитике, придираться к ней незачем.
    assert check_set(250.0, 10, "warmup", RICH_HISTORY).level == "ok"


def test_warmup_set_still_checked_for_absurd():
    assert check_set(700.0, 10, "warmup", RICH_HISTORY).level == "absurd"


def test_resolve_marks_suspicious_by_default():
    assert resolve_is_anomalous(AnomalyVerdict("suspicious", "x"), confirmed=False) is True


def test_confirmation_clears_suspicious():
    # Пользователь явно подтвердил — уважаем и не выкидываем подход из аналитики.
    assert resolve_is_anomalous(AnomalyVerdict("suspicious", "x"), confirmed=True) is False


def test_confirmation_does_not_clear_absurd():
    # Физически невозможное остаётся аномалией, что бы ни прислал клиент.
    assert resolve_is_anomalous(AnomalyVerdict("absurd", "x"), confirmed=True) is True


def test_ok_is_never_anomalous():
    assert resolve_is_anomalous(AnomalyVerdict("ok", None), confirmed=False) is False
