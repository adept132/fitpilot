"""Каноническое оборудование (EN) + перевод и классификация.

БД хранит equipment_needed каноническими EN-значениями. На фронте показываем
через словарь RU. Здесь — единый источник нормализации (RU-синонимы/старые
значения/EN -> EN), классификации для фильтра поиска и категории шага веса.
"""

from typing import List, Optional

# --- Канонические значения ---
BARBELL = "barbell"
DUMBBELL = "dumbbell"
SMITH = "smith"
FREE_MACHINE = "free_machine"
BLOCK_MACHINE = "block_machine"
BENCH = "bench"
PULLUP_BAR = "pullup_bar"
DIP_BARS = "dip_bars"
BODYWEIGHT = "bodyweight"
KETTLEBELL = "kettlebell"
BAND = "band"

CANONICAL = {
    BARBELL,
    DUMBBELL,
    SMITH,
    FREE_MACHINE,
    BLOCK_MACHINE,
    BENCH,
    PULLUP_BAR,
    DIP_BARS,
    BODYWEIGHT,
    KETTLEBELL,
    BAND,
}

# EN -> RU (для справки/дефолтов; основной перевод — на фронте)
RU_LABELS = {
    BARBELL: "Штанга",
    DUMBBELL: "Гантели",
    SMITH: "Машина Смита",
    FREE_MACHINE: "Тренажёр со свободным весом",
    BLOCK_MACHINE: "Блочный тренажер",
    BENCH: "Скамья",
    PULLUP_BAR: "Перекладина",
    DIP_BARS: "Брусья",
    BODYWEIGHT: "Свой вес",
    KETTLEBELL: "Гиря",
    BAND: "Фитнес-резинка",
}

# Все синонимы (RU/EN, lowercase, ё->е) -> канон.
# ВНИМАНИЕ: "Машина Смита" разбирается раньше generic "машина".
_SYNONYMS = {
    # barbell
    "barbell": BARBELL, "штанга": BARBELL, "штанги": BARBELL, "штангой": BARBELL,
    "ez-bar": BARBELL, "ez bar": BARBELL, "ez": BARBELL,
    # dumbbell
    "dumbbell": DUMBBELL, "dumbbells": DUMBBELL,
    "гантели": DUMBBELL, "гантель": DUMBBELL, "гантелей": DUMBBELL,
    # smith (раньше generic "машина")
    "smith": SMITH, "машина смита": SMITH, "смит": SMITH, "смита": SMITH,
    # free-weight machine
    "free_machine": FREE_MACHINE,
    "тренажер со свободным весом": FREE_MACHINE,
    # block/cable machine (бывш. Тренажер + Кроссовер)
    "block_machine": BLOCK_MACHINE, "блочный тренажер": BLOCK_MACHINE,
    "тренажер": BLOCK_MACHINE, "тренажеры": BLOCK_MACHINE, "машина": BLOCK_MACHINE,
    "machine": BLOCK_MACHINE, "кроссовер": BLOCK_MACHINE, "cable": BLOCK_MACHINE,
    "блок": BLOCK_MACHINE, "трос": BLOCK_MACHINE,
    # bench
    "bench": BENCH, "скамья": BENCH,
    # pull-up bar / turnik
    "pullup_bar": PULLUP_BAR, "перекладина": PULLUP_BAR, "турник": PULLUP_BAR,
    # dip bars
    "dip_bars": DIP_BARS, "брусья": DIP_BARS,
    # bodyweight
    "bodyweight": BODYWEIGHT, "свой вес": BODYWEIGHT, "вес тела": BODYWEIGHT,
    "весом тела": BODYWEIGHT,
    # kettlebell
    "kettlebell": KETTLEBELL, "гиря": KETTLEBELL, "гири": KETTLEBELL,
    # band
    "band": BAND, "resistance_band": BAND, "фитнес-резинка": BAND,
    "резинка": BAND, "эспандер": BAND,
}


def normalize_equipment(value: Optional[str]) -> Optional[str]:
    """RU-синоним/старое значение/EN -> канонический EN (или None)."""
    if not value:
        return None
    v = str(value).strip().lower().replace("ё", "е")
    if not v:
        return None
    if v in _SYNONYMS:
        return _SYNONYMS[v]
    if v in CANONICAL:
        return v
    return None


def normalize_equipment_list(items) -> List[str]:
    if items is None:
        return []
    if isinstance(items, str):
        items = [items]
    result: List[str] = []
    for item in items:
        canon = normalize_equipment(item)
        if canon and canon not in result:
            result.append(canon)
    return result


# --- Классификация для фильтра поиска (machine/free) ---
_MACHINE_BUCKET = {BLOCK_MACHINE, FREE_MACHINE, SMITH}


def classify_bucket(equipment_needed) -> str:
    """"machine" | "free" | "unknown" — для фильтра каталога."""
    normalized = set(normalize_equipment_list(equipment_needed))
    if not normalized:
        return "unknown"
    if normalized & _MACHINE_BUCKET:
        return "machine"
    return "free"


# --- Категория шага веса ---
STEP_PLATE = "plate"        # штанга/смит/free_machine: 1.25/2.5/5 кг | 2.5/5/10 lb
STEP_DUMBBELL = "dumbbell"  # гантели: ±0.5
STEP_BLOCK = "block"        # блочный тренажер: 10/15/20 lb (всегда lb)
STEP_OTHER = "other"        # остальное: дефолтный шаг

_STEP_CATEGORY = {
    BARBELL: STEP_PLATE,
    SMITH: STEP_PLATE,
    FREE_MACHINE: STEP_PLATE,
    DUMBBELL: STEP_DUMBBELL,
    BLOCK_MACHINE: STEP_BLOCK,
}
# Приоритет "весонесущего" оборудования, если у упражнения их несколько.
_STEP_PRIORITY = [BARBELL, DUMBBELL, SMITH, FREE_MACHINE, BLOCK_MACHINE, KETTLEBELL]


def equipment_to_step_category(equipment_needed) -> str:
    normalized = set(normalize_equipment_list(equipment_needed))
    for eq in _STEP_PRIORITY:
        if eq in normalized:
            return _STEP_CATEGORY.get(eq, STEP_OTHER)
    return STEP_OTHER
