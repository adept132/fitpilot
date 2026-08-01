"""Словарь англоязычных названий Strong -> наши русские названия.

Зачем: Strong экспортирует английские имена ('Bench Press (Barbell)'), а наша
база — русская ('Жим лежа на прямой скамье'). Fuzzy-матчинг через языки не
работает, поэтому топ Strong-упражнений закрываем явным словарём, а хвост
уходит на экран ручного сопоставления.

Формат имени в Strong: 'Название (Оборудование)'. Оборудование из скобок
разбираем отдельно и приводим к нашим каноническим EN-кодам через
equipment.normalize_equipment().
"""

import re
from typing import List, Optional, Tuple

from api.services.equipment import (
    BARBELL,
    BLOCK_MACHINE,
    BODYWEIGHT,
    DUMBBELL,
    FREE_MACHINE,
    KETTLEBELL,
    SMITH,
)

# Оборудование в скобках у Strong -> наш канон.
_EQUIPMENT_IN_PARENS = {
    "barbell": [BARBELL],
    "dumbbell": [DUMBBELL],
    "machine": [FREE_MACHINE],
    "cable": [BLOCK_MACHINE],
    "smith machine": [SMITH],
    "bodyweight": [BODYWEIGHT],
    "weighted": [BODYWEIGHT],
    "assisted": [FREE_MACHINE],
    "kettlebell": [KETTLEBELL],
    "band": [],
    "plate loaded": [FREE_MACHINE],
}

_PARENS_RE = re.compile(r"\s*\(([^)]*)\)\s*$")


def split_equipment(name: str) -> Tuple[str, List[str]]:
    """'Bench Press (Barbell)' -> ('Bench Press', ['barbell']).

    Возвращает имя без скобок и список наших канонических кодов оборудования.
    """
    raw = (name or "").strip()
    match = _PARENS_RE.search(raw)
    if not match:
        return raw, []

    base = _PARENS_RE.sub("", raw).strip()
    inside = match.group(1).strip().lower()
    return base, list(_EQUIPMENT_IN_PARENS.get(inside, []))


def _key(name: str) -> str:
    """Ключ словаря: нижний регистр, схлопнутые пробелы, без скобок."""
    base, _ = split_equipment(name)
    return re.sub(r"\s+", " ", base.strip().lower())


def _build(pairs: dict) -> dict:
    return {_key(en): ru for en, ru in pairs.items()}


# Ключ — имя Strong БЕЗ скобок с оборудованием; значение — точное имя в нашей
# базе. Варианты с разным оборудованием ('Bench Press (Barbell)' и
# '(Dumbbell)') различаются, поэтому они разведены в _BY_EQUIPMENT ниже.
_GENERIC = _build({
    # Спина
    "bent over row": "Тяга штанги в наклоне",
    "t bar row": "Тяга Т-штанги с упором в наклоне",
    "lat pulldown": "Тяга верхнего блока широким хватом",
    "lat pulldown wide grip": "Тяга верхнего блока широким хватом",
    "lat pulldown close grip": "Тяга верхнего блока узким хватом",
    "behind the neck pulldown": "Тяга верхнего блока за голову",
    "seated row": "Тяга нижнего блока",
    "seated cable row": "Тяга нижнего блока",
    "pull up": "Подтягивания",
    "pullup": "Подтягивания",
    "chin up": "Подтягивания обратным хватом",
    "chinup": "Подтягивания обратным хватом",
    "straight arm pulldown": "Пуловер в кроссовере",

    # Грудь
    "decline bench press": "Жим штанги лежа на скамье с отрицательным наклоном",
    "chest press": "Жим в тренажере",
    "incline chest press": "Наклонный жим в тренажере",
    "pec deck": "Сведение рук в тренажере (бабочка)",
    "butterfly": "Сведение рук в тренажере (бабочка)",
    "cable fly": "Сведение рук в кроссовере",
    "cable crossover": "Сведение рук в кроссовере",
    "push up": "Отжимания",
    "pushup": "Отжимания",

    # Ноги
    "leg press": "Жим ногами",
    "leg extension": "Выпрямление ног в тренажере",
    "lying leg curl": "Сгибание ног в тренажере лежа",
    "seated leg curl": "Сгибание ног в тренажере сидя",
    "leg curl": "Сгибание ног в тренажере лежа",
    "hack squat": "Приседания в гакке",
    "hip abduction": "Разведение ног в тренажере",
    "hip adduction": "Сведение ног в тренажере",
    "standing calf raise": "Подъем на носки в тренажере стоя",
    "seated calf raise": "Подъем на носки в тренажере сидя",
    "calf press": "Подъем на носки в тренажере для жима ногами",

    # Плечи
    "arnold press": "Жим гантелей сидя",
    "reverse fly": "Обратная бабочка",
    "reverse pec deck": "Обратная бабочка",
    "lateral raise": "Разведение рук в стороны с гантелями стоя",

    # Руки
    "preacher curl": "Сгибание рук на бицепс на скамье Скотта",
    "concentration curl": "Концентрированные сгибания на бицепс сидя",
    "zottman curl": "Сгибания Зоттмана",
    "triceps pushdown": "Разгибания на трицепс вниз в кроссовере двумя руками",
    "triceps rope pushdown": "Разгибания на трицепс вниз в кроссовере двумя руками",
    "skullcrusher": "Французский жим со штангой лежа",
    "lying triceps extension": "Французский жим со штангой лежа",
    "overhead triceps extension": "Разгибания гантели двумя руками из-за головы стоя",
    "dip": "Отжимания на брусьях",
    "triceps dip": "Отжимания на брусьях",

    # Пресс
    "hanging leg raise": "Подъем ног в висе",
    "lying leg raise": "Подъем ног в положении лежа",
    "cable crunch": "Скручивания в блоке",
    "machine crunch": "Скручивания в тренажере",
    "oblique crunch": "Косые скручивания",
    "sit up": "Подъем туловища из положения лежа",
    "situp": "Подъем туловища из положения лежа",
})

# Имена, значение которых зависит от оборудования в скобках.
# Ключ: (имя без скобок, канон оборудования).
_BY_EQUIPMENT = {
    ("bench press", BARBELL): "Жим лежа на прямой скамье",
    ("bench press", DUMBBELL): "Жим гантелей лежа на горизонтальной скамье",
    ("bench press", SMITH): "Жим лежа на горизонтальной скамье в машине Смита",
    ("incline bench press", BARBELL): "Жим штанги лежа средним хватом на скамье с положительным наклоном",
    ("incline bench press", DUMBBELL): "Жим гантелей лежа на скамье с положительным наклоном",
    ("incline bench press", SMITH): "Жим лежа на наклонной скамье в машине Смита",

    ("squat", BARBELL): "Приседания со штангой",
    ("squat", SMITH): "Приседания в машине Смита",

    ("deadlift", BARBELL): "Становая тяга",
    ("deadlift", DUMBBELL): "Становая тяга с гантелями",
    ("romanian deadlift", BARBELL): "Румынская становая тяга",
    ("sumo deadlift", BARBELL): "Становая тяга сумо",

    ("overhead press", BARBELL): "Армейский жим стоя",
    ("seated overhead press", BARBELL): "Армейский жим сидя",
    ("shoulder press", DUMBBELL): "Жим гантелей сидя",
    ("shoulder press", FREE_MACHINE): "Жим сидя в тренажере",
    ("overhead press", DUMBBELL): "Жим гантелей сидя",

    ("upright row", BARBELL): "Вертикальная тяга штанги к груди стоя",
    ("upright row", DUMBBELL): "Вертикальная тяга гантелей к подбородку",

    ("bent over row", DUMBBELL): "Тяга гантелей в наклоне",
    ("bent over one arm row", DUMBBELL): "Тяга гантели в наклоне",

    ("bicep curl", BARBELL): "Подъем штанги на бицепс",
    ("bicep curl", DUMBBELL): "Подъем гантелей на бицепс стоя",
    ("bicep curl", BLOCK_MACHINE): "Сгибание рук на бицепс в кроссовере",
    ("seated bicep curl", DUMBBELL): "Подъем гантелей на бицепс сидя",
    ("incline bicep curl", DUMBBELL): "Подъем гантелей на бицепс на наклонной скамье",
    ("hammer curl", DUMBBELL): 'Подъем гантелей на бицепс хватом "молоток"',

    ("lateral raise", DUMBBELL): "Разведение рук в стороны с гантелями стоя",
    ("lateral raise", BLOCK_MACHINE): "Отведение руки в сторону в кроссовере",
    ("lateral raise", FREE_MACHINE): "Разведение рук в тренажере",
    ("reverse fly", DUMBBELL): "Разведение гантелей сидя в наклоне",

    ("chest fly", DUMBBELL): "Сведение гантелей лежа на горизонтальной скамье",
    ("incline chest fly", DUMBBELL): "Сведение гантелей лежа на скамье с положительным наклоном",
    ("chest fly", FREE_MACHINE): "Сведение рук в тренажере (бабочка)",

    ("pullover", DUMBBELL): "Пуловер с гантелей лежа на скамье",
    ("pullover", BLOCK_MACHINE): "Пуловер в кроссовере",

    ("shrug", BARBELL): "Шраги со штангой",
    ("shrug", DUMBBELL): "Шраги с гантелями",
    ("shrug", BLOCK_MACHINE): "Шраги в кроссовере",

    ("hip thrust", BARBELL): "Подъем ягодиц со штангой",
    ("hip thrust", SMITH): "Подъем ягодиц в машине Смита",
    ("hip thrust", FREE_MACHINE): "Ягодичный мост в тренажере",

    ("lunge", DUMBBELL): "Выпады с гантелями",
    ("lunge", SMITH): "Выпады в машине Смита",

    ("wrist curl", BARBELL): "Сгибание запястий на скамье со штангой ладонями вверх",
    ("wrist curl", DUMBBELL): "Сгибание запястий на скамье с гантелями ладонями вверх",
    ("reverse wrist curl", BARBELL): "Сгибание запястий на скамье со штангой ладонями вниз",
    ("reverse wrist curl", DUMBBELL): "Сгибание запястий на скамье с гантелями ладонями вниз",
}

_BY_EQUIPMENT_NORMALIZED = {(_key(n), eq): ru for (n, eq), ru in _BY_EQUIPMENT.items()}


def lookup_ru_name(strong_name: str) -> Optional[str]:
    """Имя Strong -> точное имя в нашей базе, либо None.

    Сначала пробуем пару (имя, оборудование) — она точнее, потом общий словарь.
    """
    base, equipment = split_equipment(strong_name)
    key = _key(base)

    for eq in equipment:
        hit = _BY_EQUIPMENT_NORMALIZED.get((key, eq))
        if hit:
            return hit

    # Имя без оборудования в скобках, но само по себе однозначное.
    if not equipment:
        for (dict_key, _), ru in _BY_EQUIPMENT_NORMALIZED.items():
            if dict_key == key:
                return ru

    return _GENERIC.get(key)
