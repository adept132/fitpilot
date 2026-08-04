"""Пороги и тексты периодизации.

Магических чисел в логике быть не должно: всё, что можно захотеть подкрутить
по накопленным данным, живёт здесь. Значения — первое приближение из спеки §5.3.
"""

from __future__ import annotations

# --- Виды предложений ---
KIND_EARLY_DELOAD = "early_deload"
KIND_POSTPONE_DELOAD = "postpone_deload"
KIND_BLOCK_BOUNDARY = "block_boundary"
KIND_STRUCTURAL = "structural"

# --- Варианты структурной правки ---
# Порядок значим: первый вариант подсвечен в интерфейсе по умолчанию.
# Сдвиг диапазона обратим и сохраняет историю упражнения, замена уводит
# упражнение в бутстрап (история читается по exercise_id), поэтому по
# умолчанию предлагается менее разрушительное.
OPTION_SHIFT_REPS = "shift_reps"
OPTION_REPLACE = "replace"
OPTION_KEEP = "keep"
STRUCTURAL_OPTIONS = [OPTION_SHIFT_REPS, OPTION_REPLACE, OPTION_KEEP]
DEFAULT_STRUCTURAL_OPTION = OPTION_SHIFT_REPS

# --- Статусы ---
STATUS_PENDING = "pending"
STATUS_ACCEPTED = "accepted"
STATUS_DECLINED = "declined"
STATUS_EXPIRED = "expired"

# --- Причины закрытия блока ---
CLOSE_COMPLETED = "completed"
CLOSE_EARLY_DELOAD = "early_deload"
CLOSE_PLATEAU = "plateau"
CLOSE_MANUAL = "manual"
CLOSE_SPLIT_CHANGED = "split_changed"
CLOSE_LAYOFF = "layoff"

DELOAD_TIER = "deload"

# --- Триггер по усталости ---
# Сколько дней подряд systemic держится в полосе fatigued, чтобы это перестало
# быть колебанием и стало поводом разгрузиться.
FATIGUED_DAYS_FOR_DELOAD = 5

# --- Триггер по плато ---
# Доля вставших среди упражнений с достаточной историей.
PLATEAU_STALLED_RATIO = 0.5
# Ниже трёх упражнений доля шумная: одно вставшее из двух даёт 50 %.
PLATEAU_MIN_EXERCISES = 3

# --- Триггер по готовности ---
READINESS_LIMIT_COUNT = 3
READINESS_LIMIT_WINDOW = 5

# --- Перенос плановой разгрузки ---
# Хроническая нагрузка упала до этой доли от уровня на старте блока —
# разгружаться не от чего.
POSTPONE_CHRONIC_RATIO = 0.6

# --- Предохранители ---
# Плановая разгрузка уже на носу — предлагать досрочную абсурдно.
PLANNED_DELOAD_NEAR_WORKOUTS = 3
# В первой фазе блока усталость по определению низкая, а плато не успевает
# набрать статистику.
MIN_PHASE_ORDINAL_FOR_TRIGGERS = 2

# --- Долгий перерыв ---
# Плановый конец блока прошёл, сессий за это время нет — блок пора закрывать.
LAYOFF_DAYS_AFTER_BLOCK_END = 14

# --- Причины ---
REASON_FATIGUE_HIGH = "fatigue_high"
REASON_LOAD_SPIKE = "load_spike"
REASON_BLOCK_PLATEAU = "block_plateau"
REASON_READINESS_LIMITED = "readiness_limited"
REASON_LOAD_DROPPED = "load_dropped"
REASON_BLOCK_COMPLETED = "block_completed"
REASON_STALLED_AFTER_DELOAD = "stalled_after_deload"
REASON_LAYOFF = "layoff"

REASON_TEXTS: dict[str, str] = {
    REASON_FATIGUE_HIGH: "Усталость держится высокой вторую неделю — пора разгрузиться.",
    REASON_LOAD_SPIKE: "Нагрузка выросла слишком резко — пора разгрузиться.",
    REASON_BLOCK_PLATEAU: "Половина упражнений перестала расти — пора разгрузиться.",
    REASON_READINESS_LIMITED: "Вы несколько раз подряд заходили не в форме — пора разгрузиться.",
    REASON_LOAD_DROPPED: "Нагрузки было мало — разгрузку можно отложить.",
    REASON_BLOCK_COMPLETED: "Блок пройден до конца — пора подвести итоги.",
    REASON_STALLED_AFTER_DELOAD: "Упражнение не сдвинулось даже после разгрузки.",
    REASON_LAYOFF: "Блок закончился давно, а тренировок не было — начнём новый с сегодня.",
}
