"""Константы автопилота цели (P0-12).

Все значения — стартовые, под калибровку на реальных данных. Каждая строка
несёт основание: число без основания в этом проекте не живёт.
"""

from __future__ import annotations

# --- Пороги рождения предложения (спека §5.4) ---
# Оба обязаны сработать вместе: 8 % отставания на длинном сроке — это дни,
# а 7 дней на коротком — это десятки процентов. По одному порогу движок
# либо молчал бы там, где надо говорить, либо шумел бы на ровном месте.
MIN_RATE_GAP_RATIO = 0.10          # требуемый темп выше планового на 10 %
MIN_ETA_GAP_DAYS = 7               # и ETA позже дедлайна на неделю

# --- Калибровка (спека §5.3) ---
CALIBRATION_MIN_WINDOWS = 2        # меньше двух закрытых окон — множителя нет
CALIBRATION_MIN_SESSIONS = 4       # меньше четырёх сессий лифта — тоже нет
CALIBRATION_MIN_FACTOR = 0.3       # ниже — прогноз превращается в бесконечность
CALIBRATION_MAX_FACTOR = 1.0       # выше единицы исполнение не бывает

# --- Горизонт симуляции ---
HORIZON_MATERIALIZED = "materialized"  # дни взяты из календаря
HORIZON_PROJECTED = "projected"        # дни достроены по ритму блока
MAX_SIMULATED_SESSIONS = 260           # ~2 года при 2.5 сессиях в неделю: предел,
                                       # за которым обещание всё равно ничего не стоит

# --- Ступени лестницы рычагов (спека §5.4), по возрастанию цены ---
#
# УДАЛЕНЫ (P0-12, задача обрезки лестницы): rep_range и sets. Разбор всех
# четырёх схем прогрессии показал, что ни одна не читает target_sets в
# условии продвижения веса — LEVER_SETS не мог дать НИКАКОГО симулированного
# эффекта никогда (см. tests/test_goal_simulate.py, было
# test_apply_lever_sets_has_no_measurable_effect_on_pace). LEVER_REP_RANGE
# «дожатие до верха диапазона» истинно по конструкции синтетического
# исполнителя (он и так всегда выполняет по верхней границе, §5.3) — эффект
# был бы разовым артефактом первого шага, а не реальным рычагом. Выдумывать
# коэффициент дозы вместо честного нуля спека отвергает (решение 6).
LEVER_ENSURE_PRESENT = "ensure_present"
LEVER_SCHEME = "scheme"
LEVER_LIFT_FREQUENCY = "lift_frequency"
LEVER_STRUCTURAL = "structural"

LADDER = (
    LEVER_ENSURE_PRESENT,
    LEVER_SCHEME,
    LEVER_LIFT_FREQUENCY,
    LEVER_STRUCTURAL,
)

# Ступени, перекраивающие календарь. Ради двух недель этого не делают.
STRUCTURAL_LEVERS = frozenset({LEVER_LIFT_FREQUENCY, LEVER_STRUCTURAL})
MIN_MICROCYCLES_FOR_STRUCTURAL = 2

# --- Коды причин ---
REASON_LIFT_MISSING = "lift_missing"
REASON_PACE_BEHIND = "pace_behind"
REASON_ABOVE_CEILING = "above_ceiling"
REASON_TREND_DOWN = "trend_down"
# Лестница дошла до конца, но найденные рычаги всё равно не закрывают разрыв
# целиком — честно отличаем это от REASON_PACE_BEHIND, где рычаги гап закрывают.
REASON_PARTIAL_CATCHUP = "partial_catchup"
# Ни одна ступень лестницы не применима (схема уже лучшая или не тяжёлый
# компаунд, горизонт слишком короткий для структурных ступеней) — рычагов
# нет вовсе, не только текста.
REASON_NO_LEVER_LEFT = "no_lever_left"

REASON_TEXTS: dict[str, str] = {
    REASON_LIFT_MISSING: "Целевого упражнения нет в ближайших неделях плана.",
    REASON_PACE_BEHIND: "В текущем темпе к сроку не успеваем.",
    REASON_ABOVE_CEILING: "Нужный темп выше того, что даёт тренированность.",
    REASON_TREND_DOWN: "Результат снижается — сейчас вопрос не в скорости.",
    REASON_PARTIAL_CATCHUP: "Даже все правки не закрывают разрыв — стоит пересмотреть срок или целевой вес.",
    REASON_NO_LEVER_LEFT: "Сейчас нет доступного рычага ускорения — стоит пересмотреть срок или целевой вес.",
}
