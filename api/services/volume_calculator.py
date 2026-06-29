import math
import logging

from api.schemas.оnboarding import VolumeBudget, MuscleTarget, BudgetMeta, BudgetConstraints
from api.services.volume_tables import TrainingVolumeTables

# --- НАСТРОЙКА ЛОГГЕРА ---
logger = logging.getLogger("volume_calculator")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter('%(levelname)s | %(message)s'))
    logger.addHandler(ch)
# -------------------------

EXPERIENCE_CONSTRAINTS = {
    "beginner": {"systemic_cap": 70, "session_max": 6},
    "intermediate": {"systemic_cap": 95, "session_max": 8},
    "advanced": {"systemic_cap": 120, "session_max": 10}
}

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
    caps = EXPERIENCE_CONSTRAINTS.get(experience_level, EXPERIENCE_CONSTRAINTS["beginner"])

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

        if base_sets == 0:
            weekly_targets[system_muscle_key] = MuscleTarget(target_sets=0, min_floor=0, is_focus=is_focus)
            continue

        if distribution_type == "balanced":
            target = base_sets
            mod_type = "base"
        else:
            if is_focus:
                target = base_sets * 1.4  # +40%
                mod_type = "+40%"
            else:
                target = base_sets * 0.85  # -15%
                mod_type = "-15%"

        scaled_target = math.ceil(target * cycle_multiplier)
        min_floor = math.floor((base_sets * 0.5) * cycle_multiplier)
        scaled_target = max(scaled_target, min_floor)

        # Выводим подробный лог по каждой мышце
        logger.debug(
            f"Muscle: {system_muscle_key: <12} | Base: {base_sets: <2} | is_focus: {str(is_focus): <5} | Mod: {mod_type: <5} | Raw Target: {target:.2f} -> Ceil: {scaled_target}")

        # Сохраняем в JSON под английским ключом
        weekly_targets[system_muscle_key] = MuscleTarget(
            target_sets=scaled_target,
            min_floor=min_floor,
            is_focus=is_focus
        )
        total_sets += scaled_target

    systemic_cap = math.ceil(caps["systemic_cap"] * cycle_multiplier)
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
            max_sets_per_session_per_muscle=caps["session_max"]
        ),
        weekly_targets=weekly_targets
    )