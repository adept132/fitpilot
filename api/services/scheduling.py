import enum


class SchedulingMode(str, enum.Enum):
    FIXED_WEEKDAYS = "FIXED_WEEKDAYS"  # Жесткие дни недели
    ROLLING_PATTERN = "ROLLING_PATTERN"  # Плавающий ритм (например, 2/1)

class WorkoutStatus(str, enum.Enum):
    pending = "pending"
    completed = "completed"
    skipped = "skipped"

class MesocyclePhase(str, enum.Enum):
    accumulation = "accumulation"  # Накопление
    overreaching = "overreaching"  # Выход на пик
    deload = "deload"              # Разгрузка