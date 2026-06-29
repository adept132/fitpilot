# handlers/muscle_classification.py
from typing import List


class MuscleGroups:
    """Классификация мышечных групп"""

    # Дельты
    ANTERIOR_DELT = "Передняя дельта"
    LATERAL_DELT = "Средняя дельта"
    POSTERIOR_DELT = "Задняя дельта"

    # Спина
    LATISSIMUS = "Широчайшие"
    MIDDLE_BACK = "Средняя часть спины"
    TRAPS = "Трапеция"
    LOWER_BACK = "Поясница"

    # Грудь
    CHEST = "Грудь"

    # Руки
    BICEPS = "Бицепс"
    TRICEPS = "Трицепс"
    FOREARMS = "Предплечья"

    # Ноги
    QUADS = "Квадрицепсы"
    HAMSTRINGS = "Бицепсы ног"
    GLUTES = "Ягодицы"
    ADDUCTORS = "Аддукторы"
    ABDUCTORS = "Абдукторы"
    CALVES = "Икры"

    # Пресс
    ABS = "Пресс"

    # Функциональные группы
    PUSH_MUSCLES = [CHEST, ANTERIOR_DELT, LATERAL_DELT, TRICEPS]
    PULL_MUSCLES = [LATISSIMUS, MIDDLE_BACK, POSTERIOR_DELT, BICEPS, TRAPS]
    LEGS_MUSCLES = [QUADS, HAMSTRINGS, GLUTES, ADDUCTORS, ABDUCTORS, CALVES]
    UPPER_BODY = PUSH_MUSCLES + PULL_MUSCLES
    LOWER_BODY = LEGS_MUSCLES + [ABS]


class ExerciseClassification:
    """Классификация упражнений"""

    # По типу нагрузки
    COMPOUND = "Базовое"
    ISOLATION = "Изолирующее"

    # По направлению движения
    PUSH = "Толчковое"
    PULL = "Тяговое"
    SQUAT = "Приседательное"
    HINGE = "Мост/Румынская тяга"
    CORE = "Пресс/Стабилизация"

    # По оборудованию
    BARBELL = "Штанга"
    DUMBBELL = "Гантели"
    MACHINE = "Тренажер"
    CABLE = "Блок"
    BODYWEIGHT = "Свой вес"
    KETTLEBELL = "Гиря"
    MEDICINE_BALL = "Медбол"

    @classmethod
    def get_muscle_targeting(cls, exercise_type: str) -> List[str]:
        """Возвращает типичные целевые мышцы для типа упражнения"""
        targeting = {
            cls.PUSH: MuscleGroups.PUSH_MUSCLES,
            cls.PULL: MuscleGroups.PULL_MUSCLES,
            cls.SQUAT: MuscleGroups.LEGS_MUSCLES + [MuscleGroups.GLUTES],
            cls.HINGE: MuscleGroups.HAMSTRINGS + MuscleGroups.GLUTES + MuscleGroups.LOWER_BACK,
            cls.CORE: [MuscleGroups.ABS, MuscleGroups.TRAPS]
        }
        return targeting.get(exercise_type, [])