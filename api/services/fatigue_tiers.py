# api/services/fatigue_scorer.py
from typing import List

# Кластеризация мышц (строгий маппинг кириллицы)
LARGE_MUSCLES = {'грудь', 'широчайшие', 'квадрицепсы', 'ягодицы'}
MEDIUM_MUSCLES = {'средняя часть спины', 'пресс', 'бицепсы ног', 'передняя дельта', 'средняя дельта', 'задняя дельта'}
SMALL_MUSCLES = {'бицепс', 'трицепс', 'трапеция', 'предплечья', 'абдукторы', 'аддукторы', 'икры'}

# Оценка оборудования
EQUIPMENT_SCORES = {
    # Свободные веса (Высокий стресс ЦНС, максимальная стабилизация)
    'штанга': 3,

    # Независимые свободные веса (Высокий стресс, но вес обычно меньше штанги)
    'гантели': 2,
    'гиря': 2,

    # Работа со своим весом / Частичная опора (Средний стресс)
    'свой вес': 1,
    'турник': 1,
    'перекладина': 1,  # Синоним для турника
    'брусья': 1,
    'скамья': 1,

    # Изолированные, блочные и стабилизированные (Низкий системный стресс)
    'тренажер': 0,
    'кроссовер': 0,
    'смит': 0,
    'машина смита': 0,  # Синоним для Смита
    'фитнес-резинка': 0
}


def get_muscle_score(muscle: str, is_primary: bool) -> float:
    """Возвращает балл для мышцы с защитой от пробелов и регистра."""
    if not muscle:
        return 0.0

    m = muscle.strip().lower()

    if m in LARGE_MUSCLES:
        return 3.0 if is_primary else 1.5
    if m in MEDIUM_MUSCLES:
        return 2.0 if is_primary else 1.0
    if m in SMALL_MUSCLES:
        return 1.0 if is_primary else 0.5

    return 0.0  # Fallback, если строка битая


def get_equipment_score(equipment_list: List[str]) -> int:
    """Возвращает максимальный балл оборудования. Неизвестное/пустое = 1."""
    if not equipment_list:
        return 1

    scores = []
    for eq in equipment_list:
        eq_norm = eq.strip().lower()
        # Если оборудование есть в словаре - берем его балл, иначе 1 (собственный вес)
        scores.append(EQUIPMENT_SCORES.get(eq_norm, 1))

    return max(scores)


def calculate_fatigue_tier(
        category: str,
        main_muscle: str,
        secondary_muscles: List[str],
        equipment: List[str]
) -> int:
    """
    Детерминированная модель Fatigue Score.
    """
    # 1. Коэффициент категории (Базовое = 5, Изолирующее = 1)
    cat_norm = category.strip().lower() if category else ""
    score_category = 5 if cat_norm == 'базовое' else 1

    # 2. Коэффициент мышечной массы
    score_primary_muscle = get_muscle_score(main_muscle, is_primary=True)
    score_secondary_muscles = sum(get_muscle_score(m, is_primary=False) for m in secondary_muscles)
    score_muscles = score_primary_muscle + score_secondary_muscles

    # 3. Базовый коэффициент оборудования
    raw_equip_score = get_equipment_score(equipment)

    # 4. Патч-ограничитель абсолютной нагрузки (кап для малых мышц)
    if score_primary_muscle == 1.0:
        score_equipment_adjusted = min(raw_equip_score, 1)
    else:
        score_equipment_adjusted = raw_equip_score

    # Итоговый расчет
    fatigue_score = score_category + score_muscles + score_equipment_adjusted

    # Классификация Tiers
    if fatigue_score >= 9.0:
        return 1
    elif fatigue_score >= 5.0:
        return 2
    else:
        return 3