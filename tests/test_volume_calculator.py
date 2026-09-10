"""Характеризационные тесты бюджета объёма (api/services/volume_calculator.py).

P0-09 Task 3: EXPERIENCE_CONSTRAINTS удалён — потолки теперь приходят из
api.services.volume.landmarks (SYSTEMIC_CAP_EFF/SESSION_MAX), а
per-muscle цели клампятся в диапазон MEV/MRV вместо base*0.5..base*1.4.
Числа ниже пересчитаны под новый контракт; там, где это меняет саму суть
теста (не только число), это отмечено отдельным комментарием.
"""

import pytest

from api.services.volume.landmarks import SYSTEMIC_CAP_EFF
from api.services.volume_calculator import calculate_volume_budget


@pytest.mark.parametrize("level", ["beginner", "intermediate", "advanced"])
def test_budget_is_produced_for_every_level(level):
    budget = calculate_volume_budget(level, [])
    assert budget.weekly_targets
    assert budget.meta.total_weekly_sets > 0


@pytest.mark.parametrize("level", ["beginner", "intermediate", "advanced"])
def test_total_respects_systemic_cap(level):
    # ВНИМАНИЕ: при пустом фокусе балансные суммы (60/91/118 для
    # beginner/intermediate/advanced) и так лежат под эффективным кэпом
    # landmarks.SYSTEMIC_CAP_EFF (80/110/140), поэтому ветка обрезки здесь
    # НЕ исполняется — тест фиксирует лишь этот факт конфигурации, а не
    # работу обрезки. Саму обрезку пинит отдельный тест ниже.
    budget = calculate_volume_budget(level, [])
    cap = SYSTEMIC_CAP_EFF[level]
    assert budget.meta.total_weekly_sets <= cap


def test_total_is_trimmed_to_cap_when_focus_pushes_over():
    # Дискриминирующий тест на сам механизм обрезки по systemic_cap.
    # P0-09: подобранный набор фокусов заменён — прежний (chest, biceps,
    # triceps, side_delts, quads) при фокусе целится в MAV, а не в
    # base*1.4, и с этими пятью мышцами сумма (135) уже не переваливает
    # через новый эффективный кэп (140), так что обрезка не срабатывала бы.
    # side_delts/rear_delts/lats/mid_back/calves доводят до-обрезки сумму
    # до 141 — на 1 выше кэпа, и не-фокусные мышцы (в частности chest и
    # quads, у которых raw target = base*0.85 всё ещё выше их MEV) дают
    # ровно 1 подход запаса, так что обрезка снимает сумму ровно до кэпа.
    cap = SYSTEMIC_CAP_EFF["advanced"]
    budget = calculate_volume_budget(
        "advanced", ["side_delts", "rear_delts", "lats", "mid_back", "calves"]
    )
    assert budget.meta.total_weekly_sets == cap
    assert budget.meta.total_weekly_sets > calculate_volume_budget(
        "advanced", []
    ).meta.total_weekly_sets


def test_no_focus_means_balanced_distribution():
    budget = calculate_volume_budget("intermediate", [])
    assert budget.meta.distribution_type == "balanced"


def test_focus_muscles_switch_to_specialization():
    budget = calculate_volume_budget("intermediate", ["chest"])
    assert budget.meta.distribution_type == "specialization"


def test_focus_muscle_gets_more_than_without_focus():
    balanced = calculate_volume_budget("intermediate", [])
    focused = calculate_volume_budget("intermediate", ["chest"])
    assert focused.weekly_targets["chest"].target_sets > balanced.weekly_targets["chest"].target_sets


def test_focus_muscles_accept_comma_separated_string():
    # Историческая вольность вызывающей стороны — строка вместо списка.
    # distribution_type здесь ни о чём не говорит: он выставляется по truthy
    # исходной строки ДО split(','), поэтому проверяем реальный эффект разбиения —
    # что обе перечисленные мышцы (chest И biceps) реально попали в фокус и получили
    # больше сетов, чем в balanced-распределении. Если бы запятая не разбиралась и
    # строка "chest,biceps" осталась одним нераспознанным токеном, ни одна мышца не
    # совпала бы с system_muscle_key и обе остались бы на уровне balanced (или ниже).
    balanced = calculate_volume_budget("intermediate", [])
    budget = calculate_volume_budget("intermediate", "chest,biceps")
    assert budget.meta.distribution_type == "specialization"
    assert budget.weekly_targets["chest"].target_sets > balanced.weekly_targets["chest"].target_sets
    assert budget.weekly_targets["biceps"].target_sets > balanced.weekly_targets["biceps"].target_sets


def test_focus_muscle_names_are_not_translated_from_russian():
    # Характеризует РЕАЛЬНУЮ зону ответственности функции: calculate_volume_budget
    # НЕ переводит пользовательские focus_muscles из русского в системные ключи.
    # MUSCLE_TRANSLATION_MAP применяется только к ключам base_volume_dict, а
    # focus_muscles сравнивается с ними напрямую (safe_focus_muscles без обратного
    # перевода). Поэтому focus=["грудь"] не совпадает ни с одной мышцей — грудь не
    # попадает в фокус, в отличие от английского ключа "chest". Перевод русского
    # имени — ответственность вызывающего слоя (muscle_keys.to_system_key), который
    # profile.py перед вызовом НЕ применяет. См. находку в task-6-report.md.
    # Тест дискриминирующий: если функция начнёт переводить focus сама, русский и
    # английский вариант сравняются и ассерция упадёт — это будет осознанной сменой
    # поведения, требующей пересмотра, а не молчаливой регрессией.
    balanced = calculate_volume_budget("intermediate", [])
    ru_focus = calculate_volume_budget("intermediate", ["грудь"])
    en_focus = calculate_volume_budget("intermediate", ["chest"])
    # Английский ключ реально фокусирует грудь — выше balanced.
    assert (
        en_focus.weekly_targets["chest"].target_sets
        > balanced.weekly_targets["chest"].target_sets
    )
    # Русское имя не распознано — грудь не в фокусе, таргет ниже английского.
    assert (
        ru_focus.weekly_targets["chest"].target_sets
        < en_focus.weekly_targets["chest"].target_sets
    )


def test_shorter_microcycle_reduces_total():
    week = calculate_volume_budget("intermediate", [], microcycle_length=7)
    short = calculate_volume_budget("intermediate", [], microcycle_length=5)
    assert short.meta.total_weekly_sets < week.meta.total_weekly_sets


def test_unknown_level_falls_back_to_beginner_caps():
    budget = calculate_volume_budget("alien", [])
    assert budget.meta.total_weekly_sets <= SYSTEMIC_CAP_EFF["beginner"]
