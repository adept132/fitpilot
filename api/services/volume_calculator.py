import math
import logging

from api.schemas.оnboarding import VolumeBudget, MuscleTarget, BudgetMeta, BudgetConstraints
from api.services.volume_tables import TrainingVolumeTables
from api.services.volume.landmarks import (
    SESSION_MAX,
    SYSTEMIC_CAP_EFF,
    Landmarks,
    landmarks_for,
)

# --- НАСТРОЙКА ЛОГГЕРА ---
logger = logging.getLogger("volume_calculator")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter('%(levelname)s | %(message)s'))
    logger.addHandler(ch)
# -------------------------

# EXPERIENCE_CONSTRAINTS удалён: systemic_cap считал ПРЯМЫЕ подходы, а
# бюджет с P0-09 живёт в эффективных (measure.INDIRECT_WEIGHT). Оба
# потолка теперь приходят из landmarks — там же, где лежат границы, под
# которые они калиброваны.


def clamp_target(raw_target: float, lm: Landmarks, cycle_multiplier: float) -> int:
    """Отмасштабировать сырую цель на длину микроцикла и загнать в диапазон.

    Цель и границы масштабируются ОДНИМ множителем: длинное окно поднимает и
    законный объём, и потолок, под который он должен помещаться. Раньше
    множитель применялся только к границам, из-за чего цель, уже лежащая
    внутри диапазона, не менялась вовсе при смене длины микроцикла.

    Округление ВНИЗ на всех трёх величинах. Для пола это делает требование
    мягче, для потолка — раньше включает предупреждение; обе стороны
    консервативны, как и вся таблица landmarks.
    """
    scaled = raw_target * cycle_multiplier
    low = math.floor(lm.mev * cycle_multiplier)
    high = math.floor(lm.mrv * cycle_multiplier)
    return int(max(low, min(high, math.floor(scaled))))


BASE_VOLUME = {
    "beginner": 10,
    "intermediate": 14,
    "advanced": 18
}

ALL_MUSCLES = [
    "chest", "lats", "mid_back", "quads", "hamstrings", "glutes",
    "side_delts", "front_delts", "rear_delts", "biceps", "triceps", "calves", "abs"
]
SMALL_MUSCLES = ["side_delts", "front_delts", "rear_delts", "biceps", "triceps", "calves", "abs"]

MUSCLE_TRANSLATION_MAP = {
    "грудь": "chest",
    "широчайшие": "lats",
    "средняя часть спины": "mid_back", # Или "mid_back", если хочешь разделить
    "трапеция": "traps",
    "передняя дельта": "front_delts",
    "средняя дельта": "side_delts",
    "задняя дельта": "rear_delts",
    "бицепс": "biceps",
    "трицепс": "triceps",
    "квадрицепсы": "quads",
    "бицепсы ног": "hamstrings",
    "ягодицы": "glutes",
    "аддукторы": "adductors",
    "абдукторы": "abductors",
    "икры": "calves",
    "пресс": "abs"
}

def calculate_volume_budget(
        experience_level: str,
        focus_muscles: list[str],
        microcycle_length: int = 7
) -> VolumeBudget:
    logger.info("=== START CALCULATE BUDGET ===")
    logger.info(f"INPUT: level='{experience_level}', focus_muscles={focus_muscles}, raw_type={type(focus_muscles)}")

    cycle_multiplier = microcycle_length / 7.0

    base_volume_dict = TrainingVolumeTables.get_default_weekly_volume(experience_level)
    session_max = SESSION_MAX.get(experience_level, SESSION_MAX["beginner"])

    distribution_type = "specialization" if focus_muscles else "balanced"

    if isinstance(focus_muscles, str):
        focus_muscles = focus_muscles.split(',')

    safe_focus_muscles = [str(m).split('.')[-1].lower().strip() for m in focus_muscles]
    logger.debug(f"NORMALIZED focus_muscles: {safe_focus_muscles}")

    weekly_targets = {}
    total_sets = 0

    logger.debug("--- MUSCLE CALCULATION LOOP ---")
    for muscle_enum, base_sets in base_volume_dict.items():
        if hasattr(muscle_enum, 'name'):
            raw_muscle = muscle_enum.name
        elif hasattr(muscle_enum, 'value') and isinstance(muscle_enum.value, str):
            raw_muscle = muscle_enum.value
        else:
            raw_muscle = str(muscle_enum)

        safe_muscle_str = raw_muscle.split('.')[-1].lower().strip()

        # ИСПРАВЛЕНИЕ: Переводим на английский системный ключ
        system_muscle_key = MUSCLE_TRANSLATION_MAP.get(safe_muscle_str, safe_muscle_str)

        # Сравниваем английский ключ с английским массивом с фронта
        is_focus = system_muscle_key in safe_focus_muscles

        lm = landmarks_for(system_muscle_key, experience_level)
        if lm is None:
            # Мышца вне таблицы landmarks: границ нет, судить не по чему.
            # Молча пропускаем, а не выдумываем диапазон.
            continue

        if base_sets == 0:
            weekly_targets[system_muscle_key] = MuscleTarget(
                target_sets=0, min_floor=0, is_focus=is_focus
            )
            continue

        if distribution_type == "balanced":
            raw_target = float(base_sets)
        elif is_focus:
            # Фокус-мышца целится в MAV, а не в base * 1.4: прежний
            # множитель был взят ниоткуда и мог увести цель выше любого
            # физиологического потолка незамеченным.
            raw_target = float(lm.mav)
        else:
            raw_target = base_sets * 0.85

        scaled_target = clamp_target(raw_target, lm, cycle_multiplier)
        min_floor = math.floor(lm.mev * cycle_multiplier)

        logger.debug(
            f"Muscle: {system_muscle_key: <12} | Base: {base_sets: <2} | "
            f"focus: {str(is_focus): <5} | raw {raw_target:.2f} -> {scaled_target} "
            f"| range [{lm.mev}, {lm.mrv}]"
        )

        weekly_targets[system_muscle_key] = MuscleTarget(
            target_sets=scaled_target,
            min_floor=min_floor,
            is_focus=is_focus,
        )
        total_sets += scaled_target

    systemic_cap = math.ceil(
        SYSTEMIC_CAP_EFF.get(experience_level, SYSTEMIC_CAP_EFF["beginner"])
        * cycle_multiplier
    )
    logger.info(f"TOTAL SETS BEFORE CAP: {total_sets} | Systemic Cap for '{experience_level}': {systemic_cap}")

    if total_sets > systemic_cap:
        overage = total_sets - systemic_cap
        logger.warning(f"!!! OVERAGE DETECTED !!! Exceeded by {overage} sets. Cutting non-focus muscles...")

        reducible_muscles = [m for m, data in weekly_targets.items() if not data.is_focus]

        while overage > 0 and reducible_muscles:
            for muscle in reducible_muscles:
                if overage == 0:
                    break

                target_data = weekly_targets[muscle]
                if target_data.target_sets > target_data.min_floor:
                    target_data.target_sets -= 1
                    overage -= 1
                    total_sets -= 1

            if all(weekly_targets[m].target_sets <= weekly_targets[m].min_floor for m in reducible_muscles):
                logger.warning("Reached min_floor for ALL reducible muscles. Can't trim anymore!")
                break
    else:
        logger.info("No overage. Budget fits perfectly inside the cap.")

    logger.info(f"FINAL TOTAL SETS: {total_sets}")
    logger.info("=== END CALCULATE BUDGET ===")

    return VolumeBudget(
        version="1.0",
        meta=BudgetMeta(
            focus_muscles=focus_muscles,
            distribution_type=distribution_type,
            total_weekly_sets=total_sets
        ),
        constraints=BudgetConstraints(
            systemic_cap_per_week=systemic_cap,
            max_sets_per_session_per_muscle=session_max
        ),
        weekly_targets=weekly_targets
    )