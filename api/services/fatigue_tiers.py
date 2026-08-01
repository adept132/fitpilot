# api/services/fatigue_scorer.py
from typing import List

from api.services import equipment as equip

# Кластеризация мышц (строгий маппинг кириллицы)
LARGE_MUSCLES = {'грудь', 'широчайшие', 'квадрицепсы', 'ягодицы'}
MEDIUM_MUSCLES = {'средняя часть спины', 'пресс', 'бицепсы ног', 'передняя дельта', 'средняя дельта', 'задняя дельта'}
SMALL_MUSCLES = {'бицепс', 'трицепс', 'трапеция', 'предплечья', 'абдукторы', 'аддукторы', 'икры'}

# Оценка оборудования (ключи — канонические EN-значения)
EQUIPMENT_SCORES = {
    # Свободные веса (Высокий стресс ЦНС, максимальная стабилизация)
    equip.BARBELL: 3,

    # Независимые свободные веса (Высокий стресс, но вес обычно меньше штанги)
    equip.DUMBBELL: 2,
    equip.KETTLEBELL: 2,

    # Работа со своим весом / Частичная опора (Средний стресс)
    equip.BODYWEIGHT: 1,
    equip.PULLUP_BAR: 1,
    equip.DIP_BARS: 1,
    equip.BENCH: 1,

    # Изолированные, блочные и стабилизированные (Низкий системный стресс)
    equip.BLOCK_MACHINE: 0,
    equip.FREE_MACHINE: 0,
    equip.SMITH: 0,
    equip.BAND: 0,
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
        canon = equip.normalize_equipment(eq)
        # Если оборудование известно - берем его балл, иначе 1 (собственный вес)
        scores.append(EQUIPMENT_SCORES.get(canon, 1))

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
    # 1. Коэффициент категории (Базовое = 4, Изолирующее = 1)
    # 4 (не 5): база на крупную мышцу набирает 4+3=7 ещё до оборудования, поэтому
    # свободные веса/своё тело остаются tier-1 (score 10+), а тренажёрные/блочные
    # компаунды (equip=0) опускаются в tier-2 (score 7-8.5), расширяя средний тир.
    cat_norm = category.strip().lower() if category else ""
    score_category = 4 if cat_norm == 'базовое' else 1

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