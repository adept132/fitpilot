import enum
from typing import Tuple, Dict


class DayTacticalType(str, enum.Enum):
    easy = "easy"
    medium = "medium"
    hard = "hard"
    rest = "rest"


class StrategicEffortTier(str, enum.Enum):
    deload = "deload"
    easy = "easy"
    medium = "medium"
    prefailure = "prefailure"
    failure = "failure"


# Матрица DUP для диапазонов повторений [min, max]
REP_MATRIX: Dict[int, Dict[DayTacticalType, Tuple[int, int]]] = {
    1: {  # Tier 1: Тяжелая база (Присед, Тяга, Жим)
        DayTacticalType.easy: (8, 10),
        DayTacticalType.medium: (6, 8),
        DayTacticalType.hard: (4, 6),
    },
    2: {  # Tier 2: Тренажеры и гантели
        DayTacticalType.easy: (12, 15),
        DayTacticalType.medium: (8, 10),
        DayTacticalType.hard: (6, 8),
    },
    3: {  # Tier 3: Изоляция
        DayTacticalType.easy: (15, 20),
        DayTacticalType.medium: (12, 15),
        DayTacticalType.hard: (8, 10),
    }
}

# Базовые значения RIR для стратегических уровней усилия
BASE_RIR_MAPPING = {
    StrategicEffortTier.deload: 4,  # Формальное значение для расчетов
    StrategicEffortTier.easy: 3,
    StrategicEffortTier.medium: 2,
    StrategicEffortTier.prefailure: 1,
    StrategicEffortTier.failure: 0,
}

# Модификаторы RIR в зависимости от Fatigue Tier упражнения
TIER_RIR_MODIFIERS = {
    1: 1,  # Tier 1 (База): +1 к RIR (более безопасно)
    2: 0,  # Tier 2 (Тренажеры): 0 (без изменений)
    3: -1,  # Tier 3 (Изоляция): -1 к RIR (ближе к отказу)
}


def resolve_rep_range(fatigue_tier: int, day_type: DayTacticalType) -> Tuple[int, int]:
    """Возвращает оптимальный диапазон повторений на основе DUP-матрицы."""
    if day_type == DayTacticalType.rest:
        return (0, 0)

    tier_matrix = REP_MATRIX.get(fatigue_tier, REP_MATRIX[2])  # Дефолт на Tier 2
    return tier_matrix.get(day_type, (8, 12))


def resolve_rir(fatigue_tier: int, effort_tier: StrategicEffortTier) -> int:
    """Вычисляет динамический RIR с жестким ограничением от 0 до 3 (Защита от Junk Volume)."""
    base_rir = BASE_RIR_MAPPING.get(effort_tier, 2)
    modifier = TIER_RIR_MODIFIERS.get(fatigue_tier, 0)

    calculated_rir = base_rir + modifier

    # Функция Clamp: жестко удерживаем RIR в рамках [0, 3]
    return max(0, min(3, calculated_rir))