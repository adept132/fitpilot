import enum

class MesocyclePhaseEnum(enum.Enum):
    deload = "deload"           # Разгрузка (например, RIR 4 или снижение весов)
    easy = "easy"               # Легкая (RIR 3)
    medium = "medium"           # Средняя (RIR 2)
    prefailure = "prefailure"   # Тяжелая / Пред-отказная (RIR 1)
    failure = "failure"         # Отказная (RIR 0)