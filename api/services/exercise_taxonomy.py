"""Единый источник соответствий словаря упражнений для авто-заполнения.

Метки free-exercise-db (английские) приводим к нашему словарю (русские мышцы +
канонические коды оборудования из api.services.equipment). Модуль общий: им
пользуется офлайн-построитель индекса (scripts/build_exercise_index.py) и рантайм
классификатор (api.services.exercise_classifier), чтобы метки корпуса и выдача
классификатора жили в одном словаре.
"""

from typing import List, Optional

from api.services import equipment as equip

# --- Мышцы: primaryMuscles/secondaryMuscles датасета (EN) -> наши RU-названия ---
# Значения ДОЛЖНЫ совпадать с MUSCLE_OPTIONS на экране создания (create.tsx), иначе
# предзаполнение не подсветит нужный чип. neck в нашем словаре нет — пропускаем.
MUSCLE_EN_TO_RU = {
    "chest": "Грудь",
    "lats": "Широчайшие",
    "middle back": "Средняя часть спины",
    "lower back": "Поясница",
    "traps": "Трапеция",
    "shoulders": "Средняя дельта",  # датасет не различает дельты; уточняет наш каталог
    "biceps": "Бицепс",
    "triceps": "Трицепс",
    "forearms": "Предплечья",
    "quadriceps": "Квадрицепсы",
    "hamstrings": "Бицепсы ног",
    "glutes": "Ягодицы",
    "calves": "Икры",
    "abdominals": "Пресс",
    "adductors": "Аддукторы",
    "abductors": "Абдукторы",
}

# Полный список наших мышц (для валидации/фолбэка) — как в create.tsx MUSCLE_OPTIONS.
RU_MUSCLES = [
    "Передняя дельта", "Средняя дельта", "Задняя дельта",
    "Широчайшие", "Средняя часть спины", "Трапеция", "Поясница",
    "Грудь", "Бицепс", "Трицепс", "Предплечья",
    "Квадрицепсы", "Бицепсы ног", "Ягодицы", "Аддукторы", "Абдукторы", "Икры",
    "Пресс",
]

# --- Оборудование: значение датасета (EN, одиночная строка) -> наш канон ---
DATASET_EQUIP_TO_CANON = {
    "barbell": equip.BARBELL,
    "dumbbell": equip.DUMBBELL,
    "cable": equip.BLOCK_MACHINE,
    "machine": equip.FREE_MACHINE,
    "body only": equip.BODYWEIGHT,
    "kettlebells": equip.KETTLEBELL,
    "bands": equip.BAND,
    "e-z curl bar": equip.BARBELL,
    # medicine ball / exercise ball / foam roll / other / none -> нет соответствия
}


def muscle_en_to_ru(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    return MUSCLE_EN_TO_RU.get(name.strip().lower())


def dataset_equipment_to_canon(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return DATASET_EQUIP_TO_CANON.get(value.strip().lower())


def dataset_muscles_to_ru(names) -> List[str]:
    """Список EN-мышц датасета -> список наших RU (без дублей, порядок сохраняем)."""
    out: List[str] = []
    for n in names or []:
        ru = muscle_en_to_ru(n)
        if ru and ru not in out:
            out.append(ru)
    return out
