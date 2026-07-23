"""Характеризационные тесты бюджета объёма (api/services/volume_calculator.py)."""

import pytest

from api.services.volume_calculator import (
    EXPERIENCE_CONSTRAINTS,
    calculate_volume_budget,
)


@pytest.mark.parametrize("level", ["beginner", "intermediate", "advanced"])
def test_budget_is_produced_for_every_level(level):
    budget = calculate_volume_budget(level, [])
    assert budget.weekly_targets
    assert budget.meta.total_weekly_sets > 0


@pytest.mark.parametrize("level", ["beginner", "intermediate", "advanced"])
def test_total_respects_systemic_cap(level):
    # ВНИМАНИЕ: при пустом фокусе базовые таблицы всех уровней и так лежат под
    # кэпом (59/84/111 против 70/95/120), поэтому ветка обрезки здесь НЕ
    # исполняется — тест фиксирует лишь этот факт конфигурации, а не работу
    # обрезки. Саму обрезку пинит отдельный тест ниже.
    budget = calculate_volume_budget(level, [])
    cap = EXPERIENCE_CONSTRAINTS[level]["systemic_cap"]
    assert budget.meta.total_weekly_sets <= cap


def test_total_is_trimmed_to_cap_when_focus_pushes_over():
    # Дискриминирующий тест на сам механизм обрезки по systemic_cap. Пять
    # фокусных мышц на advanced поднимают сумму выше кэпа (120), и функция
    # обязана урезать её ровно до кэпа. Balanced-сумма advanced = 111, так что
    # результат 120 достигается именно прибавкой фокусов с последующей обрезкой,
    # а не совпадением. Если удалить блок «if total_sets > systemic_cap», сумма
    # осталась бы выше 120 и обе ассерции упали бы.
    cap = EXPERIENCE_CONSTRAINTS["advanced"]["systemic_cap"]
    budget = calculate_volume_budget(
        "advanced", ["chest", "biceps", "triceps", "side_delts", "quads"]
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
    assert budget.meta.total_weekly_sets <= EXPERIENCE_CONSTRAINTS["beginner"]["systemic_cap"]
