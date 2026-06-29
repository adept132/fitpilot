# handlers/volume_tables.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Any, Tuple

from app.muscle_classification import MuscleGroups


@dataclass(frozen=True)
class SplitDefinition:
    """
    split_tag — как в БД (fullbody_3 / upper_lower_4 / ppl_6 / upper_lower_rest_repeat)
    pattern — последовательность "типов тренировки" внутри недели/цикла.
    sessions_per_week — сколько тренировок в неделю (как в БД).
    """
    split_tag: str
    sessions_per_week: int
    pattern: List[str]


class TrainingVolumeTables:
    """
    Распределитель тренировочного бюджета (volume_budget из JSONB)
    по реальным дням сплита с сохранением эталонных анатомических баз.
    """

    # =========================
    # ЭТАЛОННЫЕ БАЗЫ (SEED DATA)
    # =========================

    WEEKLY_VOLUME_BEGINNER: Dict[MuscleGroups, int] = {
        MuscleGroups.CHEST: 8,
        MuscleGroups.ANTERIOR_DELT: 0,
        MuscleGroups.LATERAL_DELT: 4,
        MuscleGroups.POSTERIOR_DELT: 2,
        MuscleGroups.LATISSIMUS: 5,
        MuscleGroups.MIDDLE_BACK: 5,
        MuscleGroups.TRAPS: 0,
        MuscleGroups.BICEPS: 4,
        MuscleGroups.TRICEPS: 4,
        MuscleGroups.QUADS: 8,
        MuscleGroups.HAMSTRINGS: 6,
        MuscleGroups.GLUTES: 5,
        MuscleGroups.ADDUCTORS: 0,
        MuscleGroups.ABDUCTORS: 0,
        MuscleGroups.CALVES: 4,
        MuscleGroups.ABS: 4,
    }

    WEEKLY_VOLUME_INTERMEDIATE: Dict[MuscleGroups, int] = {
        MuscleGroups.CHEST: 10,
        MuscleGroups.ANTERIOR_DELT: 2,
        MuscleGroups.LATERAL_DELT: 6,
        MuscleGroups.POSTERIOR_DELT: 3,
        MuscleGroups.LATISSIMUS: 6,
        MuscleGroups.MIDDLE_BACK: 6,
        MuscleGroups.TRAPS: 2,
        MuscleGroups.BICEPS: 5,
        MuscleGroups.TRICEPS: 5,
        MuscleGroups.QUADS: 10,
        MuscleGroups.HAMSTRINGS: 7,
        MuscleGroups.GLUTES: 8,
        MuscleGroups.ADDUCTORS: 2,
        MuscleGroups.ABDUCTORS: 2,
        MuscleGroups.CALVES: 5,
        MuscleGroups.ABS: 5,
    }

    WEEKLY_VOLUME_ADVANCED: Dict[MuscleGroups, int] = {
        MuscleGroups.CHEST: 12,
        MuscleGroups.ANTERIOR_DELT: 3,
        MuscleGroups.LATERAL_DELT: 9,
        MuscleGroups.POSTERIOR_DELT: 4,
        MuscleGroups.LATISSIMUS: 8,
        MuscleGroups.MIDDLE_BACK: 8,
        MuscleGroups.TRAPS: 3,
        MuscleGroups.BICEPS: 7,
        MuscleGroups.TRICEPS: 7,
        MuscleGroups.QUADS: 12,
        MuscleGroups.HAMSTRINGS: 9,
        MuscleGroups.GLUTES: 10,
        MuscleGroups.ADDUCTORS: 3,
        MuscleGroups.ABDUCTORS: 3,
        MuscleGroups.CALVES: 7,
        MuscleGroups.ABS: 6,
    }

    # =========================
    # SPLITS (как в БД)
    # =========================

    SPLITS: Dict[str, SplitDefinition] = {
        "fullbody_3": SplitDefinition(
            split_tag="fullbody_3",
            sessions_per_week=3,
            pattern=["fullbody", "fullbody", "fullbody"],
        ),
        "upper_lower_4": SplitDefinition(
            split_tag="upper_lower_4",
            sessions_per_week=4,
            pattern=["upper", "lower", "upper", "lower"],
        ),
        "ppl_6": SplitDefinition(
            split_tag="ppl_6",
            sessions_per_week=6,
            pattern=["push", "pull", "legs", "push", "pull", "legs"],
        ),
        "upper_lower_rest_repeat": SplitDefinition(
            split_tag="upper_lower_rest_repeat",
            sessions_per_week=4,
            pattern=["upper", "lower", "upper", "lower"],
        ),
    }

    # =========================
    # DAY TYPES -> ACTIVE MUSCLES
    # =========================

    DAY_ACTIVE_MUSCLES: Dict[str, List[MuscleGroups]] = {
        "fullbody": [
            MuscleGroups.CHEST, MuscleGroups.LATISSIMUS, MuscleGroups.MIDDLE_BACK,
            MuscleGroups.QUADS, MuscleGroups.HAMSTRINGS, MuscleGroups.GLUTES,
            MuscleGroups.LATERAL_DELT, MuscleGroups.BICEPS, MuscleGroups.TRICEPS,
            MuscleGroups.ABS, MuscleGroups.CALVES,
        ],
        "upper": [
            MuscleGroups.CHEST, MuscleGroups.ANTERIOR_DELT, MuscleGroups.LATERAL_DELT,
            MuscleGroups.POSTERIOR_DELT, MuscleGroups.LATISSIMUS, MuscleGroups.MIDDLE_BACK,
            MuscleGroups.TRAPS, MuscleGroups.BICEPS, MuscleGroups.TRICEPS, MuscleGroups.ABS,
        ],
        "lower": [
            MuscleGroups.QUADS, MuscleGroups.HAMSTRINGS, MuscleGroups.GLUTES,
            MuscleGroups.ADDUCTORS, MuscleGroups.ABDUCTORS, MuscleGroups.CALVES, MuscleGroups.ABS,
        ],
        "push": [
            MuscleGroups.CHEST, MuscleGroups.ANTERIOR_DELT, MuscleGroups.LATERAL_DELT, MuscleGroups.TRICEPS,
        ],
        "pull": [
            MuscleGroups.LATISSIMUS, MuscleGroups.MIDDLE_BACK, MuscleGroups.TRAPS,
            MuscleGroups.POSTERIOR_DELT, MuscleGroups.BICEPS,
        ],
        "legs": [
            MuscleGroups.QUADS, MuscleGroups.HAMSTRINGS, MuscleGroups.GLUTES,
            MuscleGroups.ADDUCTORS, MuscleGroups.ABDUCTORS, MuscleGroups.CALVES, MuscleGroups.ABS,
        ],
        "rest": [],
    }

    # =========================
    # MIN SETS POLICY (Soft floors for a session)
    # =========================

    BIG_MUSCLES = {
        MuscleGroups.CHEST, MuscleGroups.LATISSIMUS, MuscleGroups.MIDDLE_BACK,
        MuscleGroups.QUADS, MuscleGroups.HAMSTRINGS, MuscleGroups.GLUTES,
    }

    SMALL_MUSCLES = {
        MuscleGroups.ANTERIOR_DELT, MuscleGroups.LATERAL_DELT, MuscleGroups.POSTERIOR_DELT,
        MuscleGroups.TRAPS, MuscleGroups.BICEPS, MuscleGroups.TRICEPS,
        MuscleGroups.ADDUCTORS, MuscleGroups.ABDUCTORS, MuscleGroups.CALVES, MuscleGroups.ABS,
    }

    MIN_SETS_BIG = 2
    MIN_SETS_SMALL = 1

    # =========================
    # PUBLIC API
    # =========================

    @classmethod
    def get_default_weekly_volume(cls, level: str) -> Dict[MuscleGroups, int]:
        """Отдает эталонный базовый объем для генерации JSONB-бюджета"""
        volumes = {
            "beginner": cls.WEEKLY_VOLUME_BEGINNER,
            "intermediate": cls.WEEKLY_VOLUME_INTERMEDIATE,
            "advanced": cls.WEEKLY_VOLUME_ADVANCED,
        }
        return volumes.get(level, cls.WEEKLY_VOLUME_BEGINNER)

    @classmethod
    def get_split_definition(cls, split_tag: str) -> SplitDefinition:
        return cls.SPLITS.get(split_tag, cls.SPLITS["fullbody_3"])

    @classmethod
    def get_pattern_for_week(cls, split_tag: str) -> List[str]:
        return cls.get_split_definition(split_tag).pattern[:]

    @classmethod
    def compute_weekly_frequency(cls, split_tag: str) -> Dict[MuscleGroups, int]:
        pattern = cls.get_pattern_for_week(split_tag)
        all_muscles = set(m for muscles in cls.DAY_ACTIVE_MUSCLES.values() for m in muscles)
        freq: Dict[MuscleGroups, int] = {m: 0 for m in all_muscles}

        for day_type in pattern:
            active = cls.DAY_ACTIVE_MUSCLES.get(day_type, [])
            for m in active:
                freq[m] += 1

        return freq

    @classmethod
    def get_sets_per_muscle_per_workout(
            cls,
            volume_budget: Dict[str, Any],  # Теперь принимает распарсенный JSONB из профиля
            split_tag: str,
            workout_index: int,
            is_short: bool = False,
    ) -> Tuple[str, Dict[MuscleGroups, int]]:
        """
        Берет индивидуальный бюджет пользователя и распределяет его на конкретный день сплита.
        """
        split_def = cls.get_split_definition(split_tag)
        pattern = split_def.pattern if split_def.pattern else ["fullbody", "fullbody", "fullbody"]

        workout_index = max(0, min(workout_index, len(pattern) - 1))
        day_type = pattern[workout_index]
        active_today = set(cls.DAY_ACTIVE_MUSCLES.get(day_type, []))

        weekly_freq = cls.compute_weekly_frequency(split_tag)
        occurrence_index_today = cls._compute_occurrence_index_for_day(pattern, workout_index)

        # Извлекаем данные из индивидуального бюджета
        weekly_targets = volume_budget.get("weekly_targets", {})
        constraints = volume_budget.get("constraints", {})
        session_max_sets = constraints.get("max_sets_per_session_per_muscle", 8)

        sets_today: Dict[MuscleGroups, int] = {}

        for muscle_str, target_data in weekly_targets.items():
            muscle_enum = cls._parse_muscle(muscle_str)
            if not muscle_enum:
                continue

            if muscle_enum not in active_today:
                sets_today[muscle_enum] = 0
                continue

            weekly_vol = target_data.get("target_sets", 0)
            freq = weekly_freq.get(muscle_enum, 0)

            if freq <= 0:
                sets_today[muscle_enum] = 0
                continue

            occ = occurrence_index_today.get(muscle_enum, 0)
            sets = cls._distribute_weekly_volume_to_session(weekly_vol, freq, occ)

            # 1. Мягкие минимумы сессии
            sets = cls._apply_min_sets(muscle_enum, sets)

            # 2. Жесткие лимиты сессии (защита от перетрена в один день)
            if sets > session_max_sets:
                sets = session_max_sets

            # 3. Режим короткой тренировки
            if is_short and sets > 0:
                sets = max(cls._min_sets_for_muscle(muscle_enum), int(sets * 0.75 + 0.5))

            sets_today[muscle_enum] = sets

        return day_type, sets_today

    # =========================
    # INTERNAL HELPERS
    # =========================

    @classmethod
    def _parse_muscle(cls, muscle_str: str) -> MuscleGroups | None:
        try:
            return MuscleGroups(muscle_str)
        except ValueError:
            return None

    @classmethod
    def _min_sets_for_muscle(cls, muscle: MuscleGroups) -> int:
        if muscle in cls.BIG_MUSCLES:
            return cls.MIN_SETS_BIG
        return cls.MIN_SETS_SMALL

    @classmethod
    def _apply_min_sets(cls, muscle: MuscleGroups, sets: int) -> int:
        if sets <= 0:
            return 0
        return max(sets, cls._min_sets_for_muscle(muscle))

    @staticmethod
    def _distribute_weekly_volume_to_session(weekly_vol: int, freq: int, occurrence_index: int) -> int:
        base = weekly_vol // freq
        remainder = weekly_vol % freq
        return base + (1 if occurrence_index < remainder else 0)

    @classmethod
    def _compute_occurrence_index_for_day(
            cls,
            pattern: List[str],
            workout_index: int
    ) -> Dict[MuscleGroups, int]:
        seen: Dict[MuscleGroups, int] = {}
        for i in range(0, workout_index + 1):
            day_type = pattern[i]
            active = cls.DAY_ACTIVE_MUSCLES.get(day_type, [])
            for m in active:
                if m not in seen:
                    seen[m] = 0
                if i != workout_index:
                    seen[m] += 1
        return seen


class RepetitionRanges:
    """Диапазоны повторений с пропорциональным распределением."""

    STANDARD_RANGES = {
        "low": {"min": 4, "max": 6},
        "medium": {"min": 6, "max": 8},
        "high": {"min": 8, "max": 10},
        "very_high": {"min": 10, "max": 12},
        "extreme": {"min": 12, "max": 15},
    }

    LEVEL_DISTRIBUTION = {
        "beginner": {"low": 0, "medium": 0.1, "high": 0.3, "very_high": 0.4, "extreme": 0.2},
        "intermediate": {"low": 0.15, "medium": 0.15, "high": 0.3, "very_high": 0.25, "extreme": 0.15},
        "advanced": {"low": 0.25, "medium": 0.25, "high": 0.25, "very_high": 0.15, "extreme": 0.1},
    }

    @classmethod
    def get_ranges(cls, level: str) -> Dict[str, Dict[str, int]]:
        return cls.STANDARD_RANGES

    @classmethod
    def get_distribution(cls, level: str) -> Dict[str, float]:
        return cls.LEVEL_DISTRIBUTION.get(level, cls.LEVEL_DISTRIBUTION["beginner"])

    @classmethod
    def get_proportional_pool(cls, level: str, total_exercises: int, is_short: bool = False) -> list:
        base_dist = cls.LEVEL_DISTRIBUTION.get(level, cls.LEVEL_DISTRIBUTION["beginner"]).copy()

        if is_short:
            base_dist["low"] += 0.2
            base_dist["medium"] += 0.2
            current_sum = sum(base_dist.values())
            for key in base_dist:
                base_dist[key] = base_dist[key] / current_sum

        pool = []
        counts = {}
        for r_type, percentage in base_dist.items():
            counts[r_type] = int(percentage * total_exercises + 0.5)

        actual_total = sum(counts.values())
        diff = total_exercises - actual_total
        if diff != 0:
            target_key = max(base_dist, key=base_dist.get)
            counts[target_key] += diff

        for r_type, count in counts.items():
            range_data = cls.STANDARD_RANGES[r_type]
            for _ in range(count):
                pool.append(range_data)

        return pool