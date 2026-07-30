"""Шкалы, пороги и справочник мест боли.

Магических чисел в логике быть не должно: всё, что можно захотеть
подкрутить по накопленным данным, живёт здесь.
"""

from __future__ import annotations

# --- Уровни ---
LEVEL_OK = "ok"
LEVEL_CAUTION = "caution"
LEVEL_LIMIT = "limit"

LEVEL_ORDER: dict[str, int] = {LEVEL_OK: 0, LEVEL_CAUTION: 1, LEVEL_LIMIT: 2}
_LEVEL_BY_ORDER: dict[int, str] = {v: k for k, v in LEVEL_ORDER.items()}


def downgrade(level: str) -> str:
    """На ступень вниз. Вспомогательная мышца страдает меньше целевой:
    боль в плече обязана придержать жим лёжа, но не так же жёстко, как
    жим стоя, где плечо целевое (спека §6.5)."""
    return _LEVEL_BY_ORDER[max(0, LEVEL_ORDER[level] - 1)]


def higher(a: str, b: str) -> str:
    return a if LEVEL_ORDER[a] >= LEVEL_ORDER[b] else b


# --- Источник уровня ---
SOURCE_PAIN = "pain"
SOURCE_SORENESS = "soreness"
SOURCE_GLOBAL = "global"

# --- Полнота ответа ---
COMPLETENESS_FULL = "full"
COMPLETENESS_PARTIAL = "partial"

# --- Виды наблюдений (UserObservation.kind) ---
KIND_SLEEP = "sleep"
KIND_STRESS = "stress"
KIND_SORENESS = "soreness"
KIND_PAIN = "pain"

# --- Источник наблюдения (UserObservation.source) ---
SOURCE_CHECKIN = "checkin"
SOURCE_POST_SESSION = "post_session"

# --- Шкалы ---
SLEEP_MIN, SLEEP_MAX = 1, 5        # выше = лучше
STRESS_MIN, STRESS_MAX = 1, 5      # выше = ХУЖЕ
SORENESS_MIN, SORENESS_MAX = 0, 3
PAIN_MIN, PAIN_MAX = 0, 3

# Направления шкал намеренно разные ("стресс 5 = отлично" нечитаемо),
# поэтому пороги названы явно. Никаких "> 3" в логике.
SLEEP_BAD_AT_OR_BELOW = 2
STRESS_BAD_AT_OR_ABOVE = 4
SORENESS_CAUTION = 2
SORENESS_LIMIT = 3

# --- Жизненный цикл боли ---
# Журнал append-only: "прошло" пишет новую запись value=0, а не удаляет
# старую. История травмы сохраняется — ровно то, ради чего заводилась
# таблица UserObservation.
PAIN_ACTIVE_DAYS = 14

# --- Экран чек-ина ---
# Пятый вопрос превращает чек-ин в медосмотр, и заполняемость падает до нуля.
CHECKIN_MAX_MUSCLE_CHIPS = 4
# Две тренировки в день: сон и стресс предзаполняются прошлыми ответами.
CHECKIN_REASK_HOURS = 8
# Свободная тренировка: мышц дня ещё нет, берём нагруженные недавно.
CHECKIN_RECENT_MUSCLE_HOURS = 72

# --- Справочник мест боли ---
JOINT_KEYS = frozenset(
    {"shoulder", "elbow", "wrist", "knee", "hip", "lower_back", "ankle", "neck"}
)

# Сустав -> (затронутые мышцы, затронутые паттерны ExerciseAction).
#
# Сустав задевает упражнение, ТОЛЬКО если совпало и то, и другое.
# Конъюнкция здесь несущая: на дизъюнкции боль в локте выключила бы всю
# верхнюю тренировку. Если action == "unknown", требование по паттерну
# снимается и решаем по мышце — ошибаемся в сторону осторожности.
#
# Мышцы — только реальные system keys (их 16). Ключей lower_back,
# forearms и neck среди мышц нет, они существуют лишь как суставы.
JOINT_IMPACT: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "shoulder": (
        frozenset({"chest", "front_delts", "side_delts", "rear_delts",
                   "lats", "mid_back", "traps", "triceps"}),
        frozenset({"push", "pull", "abduction", "elevation",
                   "shoulder_extension"}),
    ),
    # Только те, что реально пересекают локоть как основные движители.
    # Добавить сюда lats означало бы дать подтягиваниям limit вместо
    # caution — а через синергиста это должно быть мягче (спека §6.4).
    "elbow": (
        frozenset({"biceps", "triceps"}),
        frozenset({"push", "pull", "flexion", "extension"}),
    ),
    "wrist": (
        frozenset({"chest", "biceps", "triceps"}),
        frozenset({"push", "pull", "carry"}),
    ),
    "knee": (
        frozenset({"quads", "hamstrings", "glutes", "calves"}),
        frozenset({"squat", "flexion", "extension", "plantarflexion"}),
    ),
    "hip": (
        frozenset({"glutes", "hamstrings", "quads", "adductors", "abductors"}),
        frozenset({"squat", "hinge", "abduction", "adduction"}),
    ),
    "lower_back": (
        frozenset({"glutes", "hamstrings", "quads", "lats", "mid_back",
                   "traps", "abs"}),
        frozenset({"squat", "hinge", "carry", "core", "rotation",
                   "lateral_flexion"}),
    ),
    "ankle": (
        frozenset({"calves", "quads", "glutes"}),
        frozenset({"squat", "plantarflexion"}),
    ),
    "neck": (
        frozenset({"traps"}),
        frozenset({"elevation", "extension"}),
    ),
}

# --- Тексты вердикта (глобальный уровень) ---
VERDICT_REASON_TEXTS: dict[str, str] = {
    "readiness_ok": "Состояние в норме.",
    "readiness_caution": "Восстановление неполное — сегодня поаккуратнее.",
    "readiness_limit": "Плохой сон и высокий стресс — работаем в щадящем режиме.",
}
