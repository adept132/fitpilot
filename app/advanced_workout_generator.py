"""
AdvancedWorkoutGenerator v2
- объем: ТОЛЬКО через TrainingVolumeTables (weekly -> split -> per workout)
- priority: только порядок/урезание, НЕ генерация объема
- акцент: модификация объема
- травмы: как раньше (FORBIDDEN / LIMITED)
"""

import logging
import random
import uuid
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Tuple, Set
from datetime import timedelta, datetime
from enum import Enum, auto
from collections import defaultdict, Counter
from sqlalchemy import update as sqlalchemy_update
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.models import (
    Exercise, UserExercise, UserExercisePreference,
    AdvancedGeneratorPreset, WorkoutLog, User
)

from app.muscle_classification import MuscleGroups
from api.services.volume_tables import TrainingVolumeTables  # новая версия
# если у тебя RepetitionRanges живёт там же:
# from services.volume_tables import RepetitionRanges

logger = logging.getLogger(__name__)

USER_EXERCISE_ID_OFFSET = 100000


# -----------------------------
# Equipment normalization
# -----------------------------
EQUIPMENT_CANONICAL: Dict[str, str] = {
    # canonical -> canonical
    "barbell": "barbell",
    "dumbbell": "dumbbell",
    "cable": "cable",
    "machine": "machine",
    "kettlebell": "kettlebell",
    "bodyweight": "bodyweight",

    # ru/en synonyms -> canonical
    "штанга": "barbell",
    "штанги": "barbell",
    "штангой": "barbell",
    "ez-bar": "barbell",
    "ez bar": "barbell",
    "ez": "barbell",

    "гантели": "dumbbell",
    "гантель": "dumbbell",
    "гантелей": "dumbbell",
    "dumbbells": "dumbbell",

    "блок": "cable",
    "трос": "cable",
    "кроссовер": "cable",

    "тренажер": "machine",
    "тренажёр": "machine",
    "машина": "machine",

    "гиря": "kettlebell",
    "гири": "kettlebell",

    "свой вес": "bodyweight",
    "вес тела": "bodyweight",
}


def _norm(s: Optional[str]) -> str:
    return (s or "").strip().lower()


def canonical_equipment(value: str) -> Optional[str]:
    v = _norm(value)
    if not v:
        return None
    return EQUIPMENT_CANONICAL.get(v, v)  # если неизвестно — оставим как есть


# -----------------------------
# Muscle mapping (Enum -> RU strings used in Exercise.main_muscle_group)
# -----------------------------
MUSCLE_ENUM_TO_RU: Dict[MuscleGroups, str] = {
    MuscleGroups.CHEST: "Грудь",
    MuscleGroups.ANTERIOR_DELT: "Передняя дельта",
    MuscleGroups.LATERAL_DELT: "Средняя дельта",
    MuscleGroups.POSTERIOR_DELT: "Задняя дельта",
    MuscleGroups.LATISSIMUS: "Широчайшие",
    MuscleGroups.MIDDLE_BACK: "Средняя часть спины",
    MuscleGroups.TRAPS: "Трапеция",
    MuscleGroups.BICEPS: "Бицепс",
    MuscleGroups.TRICEPS: "Трицепс",
    MuscleGroups.QUADS: "Квадрицепсы",
    MuscleGroups.HAMSTRINGS: "Бицепсы ног",
    MuscleGroups.GLUTES: "Ягодицы",
    MuscleGroups.ADDUCTORS: "Аддукторы",
    MuscleGroups.ABDUCTORS: "Абдукторы",
    MuscleGroups.CALVES: "Икры",
    MuscleGroups.ABS: "Пресс",
}
RU_TO_MUSCLE_ENUM: Dict[str, MuscleGroups] = {v: k for k, v in MUSCLE_ENUM_TO_RU.items()}


def min_sets_for_muscle_ru(muscle_ru: str) -> int:
    """
    Берём минимумы из философии volume_tables:
    big -> 2, small -> 1
    """
    m = RU_TO_MUSCLE_ENUM.get(muscle_ru)
    if not m:
        return 1
    if m in TrainingVolumeTables.BIG_MUSCLES:
        return TrainingVolumeTables.MIN_SETS_BIG
    return TrainingVolumeTables.MIN_SETS_SMALL


# -----------------------------
# Priority (только порядок/урезание)
# -----------------------------
MUSCLE_PRIORITY: Dict[str, Dict[str, int]] = {
    "pull": {
        "Широчайшие": 3,
        "Средняя часть спины": 3,
        "Бицепс": 2,
        "Задняя дельта": 2,
        "Трапеция": 1,
    },
    "push": {
        "Грудь": 3,
        "Передняя дельта": 3,
        "Средняя дельта": 2,
        "Трицепс": 2,
    },
    "legs": {
        "Квадрицепсы": 3,
        "Бицепсы ног": 3,
        "Ягодицы": 2,
        "Икры": 2,
        "Аддукторы": 1,
        "Абдукторы": 1,
        "Пресс": 1,
    },
    "upper": {
        "Грудь": 3,
        "Широчайшие": 3,
        "Средняя часть спины": 2,
        "Передняя дельта": 2,
        "Средняя дельта": 2,
        "Задняя дельта": 2,
        "Трицепс": 2,
        "Бицепс": 2,
        "Трапеция": 1,
        "Пресс": 1,
    },
    "lower": {
        "Квадрицепсы": 3,
        "Бицепсы ног": 3,
        "Ягодицы": 3,
        "Икры": 2,
        "Аддукторы": 1,
        "Абдукторы": 1,
        "Пресс": 1,
    },
    "fullbody": {
        "Грудь": 2,
        "Широчайшие": 2,
        "Средняя часть спины": 2,
        "Квадрицепсы": 2,
        "Ягодицы": 2,
        "Бицепсы ног": 1,
        "Трицепс": 1,
        "Бицепс": 1,
        "Средняя дельта": 1,
        "Пресс": 1,
        "Икры": 1,
    },
}


# -----------------------------
# Injury enums / restrictions (как у тебя, без изменений)
# -----------------------------
class InjuryZone(Enum):
    SHOULDERS = auto()
    ELBOWS = auto()
    WRISTS = auto()
    SPINE = auto()
    HIPS = auto()
    KNEES = auto()
    ANKLES = auto()


class MovementPattern(Enum):
    SQUAT = auto()
    DEADLIFT = auto()
    HIP_HINGE = auto()
    LUNGE = auto()
    OVERHEAD_PRESS = auto()
    HORIZONTAL_PRESS = auto()
    PULL_UP = auto()
    ROW = auto()
    CURL = auto()
    EXTENSION = auto()
    LATERAL_RAISE = auto()
    CRUNCH = auto()
    PLANK = auto()


class ExercisePermission(Enum):
    ALLOWED = auto()
    LIMITED = auto()
    FORBIDDEN = auto()


@dataclass(frozen=True)
class InjuryRestriction:
    forbidden: List[MovementPattern]
    limited: List[MovementPattern]


INJURY_RESTRICTIONS: Dict[InjuryZone, InjuryRestriction] = {
    InjuryZone.SHOULDERS: InjuryRestriction(
        forbidden=[MovementPattern.OVERHEAD_PRESS, MovementPattern.LATERAL_RAISE, MovementPattern.PULL_UP],
        limited=[MovementPattern.HORIZONTAL_PRESS, MovementPattern.ROW, MovementPattern.PLANK],
    ),
    InjuryZone.ELBOWS: InjuryRestriction(
        forbidden=[MovementPattern.CURL, MovementPattern.EXTENSION],
        limited=[MovementPattern.PULL_UP, MovementPattern.ROW],
    ),
    InjuryZone.WRISTS: InjuryRestriction(
        forbidden=[MovementPattern.PLANK],
        limited=[MovementPattern.HORIZONTAL_PRESS, MovementPattern.CURL, MovementPattern.EXTENSION],
    ),
    InjuryZone.SPINE: InjuryRestriction(
        forbidden=[MovementPattern.SQUAT, MovementPattern.DEADLIFT, MovementPattern.HIP_HINGE],
        limited=[MovementPattern.CRUNCH, MovementPattern.PLANK],
    ),
    InjuryZone.HIPS: InjuryRestriction(
        forbidden=[MovementPattern.SQUAT, MovementPattern.LUNGE],
        limited=[MovementPattern.HIP_HINGE],
    ),
    InjuryZone.KNEES: InjuryRestriction(
        forbidden=[MovementPattern.SQUAT, MovementPattern.LUNGE],
        limited=[],
    ),
    InjuryZone.ANKLES: InjuryRestriction(
        forbidden=[MovementPattern.LUNGE],
        limited=[MovementPattern.SQUAT],
    ),
}

EXERCISE_PATTERNS = {
    "присед": [MovementPattern.SQUAT],
    "гоблет": [MovementPattern.SQUAT],
    "жим ног": [MovementPattern.SQUAT],
    "смит": [MovementPattern.SQUAT],

    "станов": [MovementPattern.DEADLIFT],
    "сумо": [MovementPattern.DEADLIFT],
    "румын": [MovementPattern.HIP_HINGE],

    "жим над": [MovementPattern.OVERHEAD_PRESS],
    "армей": [MovementPattern.OVERHEAD_PRESS],
    "жим лё": [MovementPattern.HORIZONTAL_PRESS],
    "отжим": [MovementPattern.HORIZONTAL_PRESS],

    "подтяг": [MovementPattern.PULL_UP],
    "верхн": [MovementPattern.PULL_UP],
    "тяга": [MovementPattern.ROW],

    "выпад": [MovementPattern.LUNGE],
    "степ": [MovementPattern.LUNGE],

    "бицеп": [MovementPattern.CURL],
    "сгиб": [MovementPattern.CURL],
    "трицеп": [MovementPattern.EXTENSION],
    "разгиб": [MovementPattern.EXTENSION],

    "развед": [MovementPattern.LATERAL_RAISE],
    "махи": [MovementPattern.LATERAL_RAISE],

    "пресс": [MovementPattern.CRUNCH],
    "скручив": [MovementPattern.CRUNCH],
    "планк": [MovementPattern.PLANK],
}


# -----------------------------
# Params (расширяем под новую логику сплитов)
# -----------------------------
@dataclass
class AdvancedWorkoutParams:
    # Раньше было day_tag (push/pull/...); теперь мы храним split_tag + индекс
    # Для обратной совместимости day_tag оставлен: если split_tag не задан, мы его "угадаем".
    day_tag: str

    # Новое:
    split_tag: Optional[str] = None          # fullbody_3 / upper_lower_4 / ppl_6 / upper_lower_rest_repeat
    workout_index: int = 0                   # 0..N-1 внутри паттерна

    accent_muscle: Optional[str] = None      # RU muscle name
    is_short: bool = False
    use_supersets: bool = False
    duration_minutes: Optional[int] = None

    preferred_equipment: List[str] = field(default_factory=list)
    excluded_equipment: List[str] = field(default_factory=list)

    preferred_rep_ranges: List[str] = field(default_factory=list)
    excluded_rep_ranges: List[str] = field(default_factory=list)

    injuries: List[str] = field(default_factory=list)  # InjuryZone values (ints as strings)
    excluded_movement_patterns: List[str] = field(default_factory=list)

    prioritize_favorites: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "day_tag": self.day_tag,
            "split_tag": self.split_tag,
            "workout_index": self.workout_index,
            "accent_muscle": self.accent_muscle,
            "is_short": self.is_short,
            "use_supersets": self.use_supersets,
            "duration_minutes": self.duration_minutes,
            "preferred_equipment": self.preferred_equipment,
            "excluded_equipment": self.excluded_equipment,
            "preferred_rep_ranges": self.preferred_rep_ranges,
            "excluded_rep_ranges": self.excluded_rep_ranges,
            "injuries": self.injuries,
            "excluded_movement_patterns": self.excluded_movement_patterns,
            "prioritize_favorites": self.prioritize_favorites,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AdvancedWorkoutParams":
        return cls(
            day_tag=data.get("day_tag", "fullbody"),
            split_tag=data.get("split_tag"),
            workout_index=int(data.get("workout_index", 0) or 0),
            accent_muscle=data.get("accent_muscle"),
            is_short=bool(data.get("is_short", False)),
            use_supersets=bool(data.get("use_supersets", False)),
            duration_minutes=data.get("duration_minutes"),
            preferred_equipment=data.get("preferred_equipment", []) or [],
            excluded_equipment=data.get("excluded_equipment", []) or [],
            preferred_rep_ranges=data.get("preferred_rep_ranges", []) or [],
            excluded_rep_ranges=data.get("excluded_rep_ranges", []) or [],
            injuries=data.get("injuries", []) or [],
            excluded_movement_patterns=data.get("excluded_movement_patterns", []) or [],
            prioritize_favorites=bool(data.get("prioritize_favorites", True)),
        )


def infer_split_tag_from_day_tag(day_tag: str) -> str:
    """
    Для совместимости со старыми пресетами:
    - fullbody -> fullbody_3
    - upper/lower -> upper_lower_4
    - push/pull/legs -> ppl_6
    """
    dt = _norm(day_tag)
    if dt == "fullbody":
        return "fullbody_3"
    if dt in ("upper", "lower"):
        return "upper_lower_4"
    if dt in ("push", "pull", "legs"):
        return "ppl_6"
    return "upper_lower_4"

REP_RANGE_VALUES = {
    "1-5": (1, 5),
    "6-8": (6, 8),
    "8-12": (8, 12),
    "12-20": (12, 20),
    "20+": (20, 30),
}


class AdvancedWorkoutGenerator:
    """
    v2:
    - target_sets = volume_tables per workout (split_tag + workout_index)
    - priority = порядок/урезание, не источник объёма
    """

    def __init__(self, session: AsyncSession, user_id: int):
        self.session = session
        self.user_id = user_id
        self.params: Optional[AdvancedWorkoutParams] = None
        self.favorite_exercises: Set[int] = set()
        self.disliked_exercises: Set[int] = set()

    async def set_params(self, params: AdvancedWorkoutParams):
        self.params = params
        await self._load_exercise_preferences()

    async def load_preset(self, preset_id: int) -> bool:
        preset = await self.session.get(AdvancedGeneratorPreset, preset_id)
        if not preset or preset.user_id != self.user_id:
            logger.warning(f"Пресет {preset_id} не найден")
            return False

        self.params = AdvancedWorkoutParams.from_dict(preset.settings)
        await self._load_exercise_preferences()
        logger.info(f"Загружен пресет '{preset.name}'")
        return True

    async def load_default_preset(self) -> bool:
        result = await self.session.execute(
            select(AdvancedGeneratorPreset).where(
                AdvancedGeneratorPreset.user_id == self.user_id,
                AdvancedGeneratorPreset.is_default == True
            )
        )
        preset = result.scalar_one_or_none()
        if preset:
            return await self.load_preset(preset.id)
        return False

    # -----------------------------
    # Preferences (починил user_exercises через OFFSET)
    # -----------------------------
    async def _load_exercise_preferences(self):
        # favorites: default Exercise.id
        result = await self.session.execute(
            select(UserExercisePreference.exercise_id).where(
                UserExercisePreference.user_id == self.user_id,
                UserExercisePreference.preference == "favorite",
                UserExercisePreference.exercise_id.isnot(None),
            )
        )
        fav_default = {r[0] for r in result.all() if r and r[0] is not None}

        # favorites: user_exercise_id -> virtual_id
        result = await self.session.execute(
            select(UserExercisePreference.user_exercise_id).where(
                UserExercisePreference.user_id == self.user_id,
                UserExercisePreference.preference == "favorite",
                UserExercisePreference.user_exercise_id.isnot(None),
            )
        )
        fav_user_virtual = {int(r[0]) + USER_EXERCISE_ID_OFFSET for r in result.all() if r and r[0] is not None}

        self.favorite_exercises = fav_default | fav_user_virtual

        # disliked default
        result = await self.session.execute(
            select(UserExercisePreference.exercise_id).where(
                UserExercisePreference.user_id == self.user_id,
                UserExercisePreference.preference == "disliked",
                UserExercisePreference.exercise_id.isnot(None),
            )
        )
        dis_default = {r[0] for r in result.all() if r and r[0] is not None}

        # disliked user -> virtual
        result = await self.session.execute(
            select(UserExercisePreference.user_exercise_id).where(
                UserExercisePreference.user_id == self.user_id,
                UserExercisePreference.preference == "disliked",
                UserExercisePreference.user_exercise_id.isnot(None),
            )
        )
        dis_user_virtual = {int(r[0]) + USER_EXERCISE_ID_OFFSET for r in result.all() if r and r[0] is not None}

        self.disliked_exercises = dis_default | dis_user_virtual

        logger.info(
            f"Предпочтения: {len(self.favorite_exercises)} favorite, {len(self.disliked_exercises)} disliked"
        )

    # -----------------------------
    # Injury permission (как было)
    # -----------------------------
    def _get_exercise_patterns(self, exercise_name: str) -> List[MovementPattern]:
        name_lower = exercise_name.lower()
        for key, patterns in EXERCISE_PATTERNS.items():
            if key in name_lower:
                return patterns
        return []

    def _check_exercise_permission(self, exercise: Exercise) -> ExercisePermission:
        if not self.params or not self.params.injuries:
            return ExercisePermission.ALLOWED

        patterns = self._get_exercise_patterns(exercise.name)
        if not patterns:
            return ExercisePermission.ALLOWED

        forbidden: Set[MovementPattern] = set()
        limited: Set[MovementPattern] = set()

        for injury_str in self.params.injuries:
            try:
                injury = InjuryZone(int(injury_str))
                restriction = INJURY_RESTRICTIONS.get(injury)
                if restriction:
                    forbidden.update(restriction.forbidden)
                    limited.update(restriction.limited)
            except (ValueError, TypeError):
                continue

        for pattern_str in (self.params.excluded_movement_patterns or []):
            try:
                forbidden.add(MovementPattern(int(pattern_str)))
            except ValueError:
                continue

        if any(p in forbidden for p in patterns):
            return ExercisePermission.FORBIDDEN
        if any(p in limited for p in patterns):
            return ExercisePermission.LIMITED

        return ExercisePermission.ALLOWED

    # -----------------------------
    # DB loading exercises (оставил твою модель virtual-id, но теперь prefs совпадают)
    # -----------------------------
    async def _get_exercises_from_db(self) -> List[Exercise]:
        result = await self.session.execute(select(Exercise).where(Exercise.source == "default"))
        preset_exercises = result.scalars().all()

        result = await self.session.execute(select(UserExercise).where(UserExercise.user_id == self.user_id))
        user_exercises = result.scalars().all()

        all_exercises = list(preset_exercises)

        for u_ex in user_exercises:
            ex = Exercise(
                id=u_ex.id + USER_EXERCISE_ID_OFFSET,
                name=u_ex.name,
                category=u_ex.category,
                main_muscle_group=u_ex.main_muscle_group,
                secondary_muscle_groups=u_ex.secondary_muscle_groups,
                equipment_needed=u_ex.equipment_needed,
                difficulty=u_ex.difficulty,
                description=u_ex.description,
                source="user",
            )
            ex._user_exercise_id = u_ex.id
            all_exercises.append(ex)

        return all_exercises

    async def _get_recent_history(self, days: int = 7) -> Dict[str, List[str]]:
        cutoff_date = datetime.utcnow() - timedelta(days=days)

        result = await self.session.execute(
            select(WorkoutLog)
            .where(WorkoutLog.user_id == self.user_id, WorkoutLog.created_at >= cutoff_date)
            .order_by(WorkoutLog.created_at.desc())
        )
        logs = result.scalars().all()

        history_by_muscle: Dict[str, List[str]] = defaultdict(list)

        for log in logs:
            if not log.exercise:
                continue
            ex = log.exercise
            if ex.main_muscle_group:
                history_by_muscle[ex.main_muscle_group].append(ex.name)
            for m in (ex.secondary_muscle_groups or []):
                history_by_muscle[m].append(ex.name)

        return dict(history_by_muscle)

    async def _get_user_level(self) -> str:
        user = await self.session.get(User, self.user_id)
        return user.experience_level if user and user.experience_level else "intermediate"

    # -----------------------------
    # Volume core: volume_tables -> per-workout sets dict (RU keys)
    # -----------------------------
    def _get_day_type_and_target_sets(self, level: str) -> Tuple[str, Dict[str, int]]:
        """
        Возвращает day_type (push/pull/...) и target_sets (RU muscle -> sets)
        """
        assert self.params

        split_tag = self.params.split_tag or infer_split_tag_from_day_tag(self.params.day_tag)
        workout_index = int(self.params.workout_index or 0)

        day_type, sets_by_enum = TrainingVolumeTables.get_sets_per_muscle_per_workout(
            level=level,
            split_tag=split_tag,
            workout_index=workout_index,
            is_short=self.params.is_short,  # short уже можно учесть тут
        )

        target_sets_ru: Dict[str, int] = {
            MUSCLE_ENUM_TO_RU[m]: s
            for m, s in sets_by_enum.items()
            if s and s > 0 and m in MUSCLE_ENUM_TO_RU
        }
        return day_type, target_sets_ru

    # -----------------------------
    # Accent modifies volume (не создаёт объём из priority)
    # -----------------------------
    def _apply_accent(self, target_sets: Dict[str, int], accent_muscle: str) -> Dict[str, int]:
        """
        +50% на акцент, -25% на остальные активные мышцы.
        Минимумы сохраняем по мышцам (big/small).
        """
        accent_muscle = accent_muscle.strip()
        if accent_muscle not in target_sets:
            return target_sets

        new_sets: Dict[str, int] = {}
        for m, cnt in target_sets.items():
            if m == accent_muscle:
                v = int(cnt * 1.5 + 0.5)
            else:
                v = int(cnt * 0.75 + 0.5)

            if v > 0:
                v = max(v, min_sets_for_muscle_ru(m))
            new_sets[m] = v

        return new_sets

    # -----------------------------
    # Duration trimming: priority = кто режется первым
    # -----------------------------
    def _trim_to_duration_budget(
        self,
        target_sets: Dict[str, int],
        day_type: str,
        use_supersets: bool
    ) -> Dict[str, int]:
        """
        Если duration_minutes задан:
        - считаем бюджет сетов (грубая эвристика как у тебя)
        - если target_sets больше бюджета — режем сеты начиная с низкого priority,
          но не опускаемся ниже минимума по мышце
        """
        assert self.params

        if not self.params.duration_minutes:
            return target_sets

        duration = int(self.params.duration_minutes)
        if duration <= 0:
            return target_sets

        max_sets = duration // (2 if use_supersets else 3)
        max_sets = max(1, int(max_sets))

        current = sum(target_sets.values())
        if current <= max_sets:
            return target_sets

        priorities = MUSCLE_PRIORITY.get(day_type, {})
        # если мышцы нет в priorities — считаем её низким приоритетом (0)
        def pr(m: str) -> int:
            return priorities.get(m, 0)

        # копируем
        trimmed = target_sets.copy()

        # нижние границы
        mins = {m: (min_sets_for_muscle_ru(m) if trimmed[m] > 0 else 0) for m in trimmed}

        # режем по 1 сету за шаг, от низкого priority к высокому
        # (чтобы поведение было стабильным и предсказуемым)
        while sum(trimmed.values()) > max_sets:
            # кандидаты, у кого можно отрезать
            candidates = [m for m in trimmed if trimmed[m] > mins[m]]
            if not candidates:
                break
            # выберем “самую неважную” мышцу
            worst_pr = min(pr(m) for m in candidates)
            worst = [m for m in candidates if pr(m) == worst_pr]
            m_cut = random.choice(worst)
            trimmed[m_cut] -= 1

        logger.info(
            f"Duration trimming: duration={duration}m, supersets={use_supersets}, "
            f"budget_sets={max_sets}, before={current}, after={sum(trimmed.values())}"
        )
        return trimmed

    # -----------------------------
    # Equipment filtering
    # -----------------------------
    def _passes_equipment_filter(self, ex: Exercise) -> bool:
        assert self.params
        if not self.params.preferred_equipment:
            return True

        allowed = {canonical_equipment(x) for x in self.params.preferred_equipment}
        allowed.discard(None)

        ex_eq = {canonical_equipment(x) for x in (ex.equipment_needed or [])}
        ex_eq.discard(None)

        # если у упражнения нет оборудования — считаем bodyweight
        if not ex_eq:
            ex_eq = {"bodyweight"}

        return any(e in allowed for e in ex_eq)

    def _select_exercise_with_history_and_favorites(
            self,
            candidates: List[Exercise],
            target_muscle: str,
            history: Dict[str, List[str]],
            used_ids: Set[int],
    ) -> Optional[Exercise]:
        """
        Смысл:
        - НЕ используем secondary как "целевую" (как ты и хочешь)
        - учитываем disliked (если prioritize_favorites)
        - учитываем favorites как бонус, но не ломаем разнообразие
        """
        if not candidates:
            return None

        available = [ex for ex in candidates if ex.id not in used_ids]
        if not available:
            return None

        if self.params and self.params.prioritize_favorites:
            available = [ex for ex in available if ex.id not in self.disliked_exercises]
        if not available:
            return None

        muscle_history = history.get(target_muscle, [])
        counts = Counter(muscle_history)

        favorite_compound = []
        favorite_isolation = []
        never_used_compound = []
        never_used_isolation = []
        used_once_compound = []
        used_once_isolation = []
        used_more = []

        for ex in available:
            c = counts.get(ex.name, 0)
            is_fav = ex.id in self.favorite_exercises
            is_compound = (ex.category == "Базовое")

            if is_fav:
                if c == 0:
                    (favorite_compound if is_compound else favorite_isolation).append(ex)
                elif c == 1:
                    (used_once_compound if is_compound else used_once_isolation).append(ex)
                else:
                    used_more.append(ex)
            else:
                if c == 0:
                    (never_used_compound if is_compound else never_used_isolation).append(ex)
                elif c == 1:
                    (used_once_compound if is_compound else used_once_isolation).append(ex)
                else:
                    used_more.append(ex)

        for bucket in [
            favorite_compound, favorite_isolation,
            never_used_compound, never_used_isolation,
            used_once_compound, used_once_isolation,
            used_more
        ]:
            if bucket:
                return random.choice(bucket)

        return None

    def _calculate_sets_for_exercise(self, remaining: int) -> int:
        """
        2..4, как и было.
        """
        if remaining <= 0:
            return 0
        if remaining == 1:
            return 1  # потом исправим пост-процессом
        if remaining >= 4:
            return 4
        return remaining  # 2 или 3

    def _apply_rep_range(self) -> Tuple[int, int]:
        """
        Берём из preferred_rep_ranges, если задано; иначе 8-12.
        """
        if not self.params or not self.params.preferred_rep_ranges:
            return (8, 12)

        pool = [r for r in self.params.preferred_rep_ranges if r not in (self.params.excluded_rep_ranges or [])]
        if not pool:
            return (8, 12)
        chosen = random.choice(pool)
        return REP_RANGE_VALUES.get(chosen, (8, 12))

    def _reorder_exercises(self, exercises: List[Dict]) -> List[Dict]:
        if len(exercises) <= 1:
            return exercises

        compound = [ex for ex in exercises if ex["type"] == "compound"]
        first = random.choice(compound) if compound else exercises[0]

        result = [first]
        remaining = [ex for ex in exercises if ex is not first]

        last_muscle = first["target_muscle"]
        while remaining:
            candidates = [ex for ex in remaining if ex["target_muscle"] != last_muscle]
            chosen = random.choice(candidates) if candidates else remaining[0]
            result.append(chosen)
            remaining.remove(chosen)
            last_muscle = chosen["target_muscle"]

        return result

    def _group_into_supersets(self, exercises: List[Dict]) -> List[Dict]:
        if len(exercises) <= 1:
            return exercises

        grouped = []
        used = set()

        # первое упражнение одиночное
        grouped.append(exercises[0])
        used.add(0)

        for i in range(1, len(exercises)):
            if i in used:
                continue
            ex_a = exercises[i]
            pair_found = False

            for j in range(i + 1, len(exercises)):
                if j in used:
                    continue
                ex_b = exercises[j]

                main_a = ex_a["target_muscle"]
                main_b = ex_b["target_muscle"]

                sec_a = set(ex_a.get("secondary_muscles", []))
                sec_b = set(ex_b.get("secondary_muscles", []))

                conflict = (
                        main_a == main_b or
                        main_a in sec_b or
                        main_b in sec_a
                )

                if not conflict:
                    sid = str(uuid.uuid4())
                    ex_a["superset_id"] = sid
                    ex_b["superset_id"] = sid
                    grouped.extend([ex_a, ex_b])
                    used.update([i, j])
                    pair_found = True
                    break

            if not pair_found:
                grouped.append(ex_a)
                used.add(i)

        return grouped

    def _fix_one_set_cases(self, selected: List[Dict]) -> List[Dict]:
        """
        Если где-то осталось 1 сет — пытаемся:
        - добавить 1 сет к этому упражнению, забрав у другого упражнения (у кого > min)
        - иначе просто поднять до 2 (да, это +1 сет, но лучше, чем 1 сет)
        """
        if not selected:
            return selected

        # быстрый индекс по мышцам
        def min_for_ex(ex: Dict) -> int:
            return min_sets_for_muscle_ru(ex["target_muscle"])

        for i, ex in enumerate(selected):
            if ex["sets"] >= 2:
                continue

            # попробуем забрать 1 сет у кого-то, у кого > min
            taken = False
            for j in range(len(selected) - 1, -1, -1):
                if j == i:
                    continue
                donor = selected[j]
                donor_min = min_for_ex(donor)
                if donor["sets"] > max(2, donor_min):
                    donor["sets"] -= 1
                    ex["sets"] = 2
                    taken = True
                    break

            if not taken:
                ex["sets"] = 2

        return selected

    async def generate_workout(self) -> Optional[Dict]:
        if not self.params:
            raise ValueError("Параметры не установлены")

        level = await self._get_user_level()
        exercises = await self._get_exercises_from_db()
        history = await self._get_recent_history()

        # 1) Получаем day_type и объемы через volume_tables
        day_type, target_sets = self._get_day_type_and_target_sets(level)

        # 2) Акцент (если задан)
        if self.params.accent_muscle:
            target_sets = self._apply_accent(target_sets, self.params.accent_muscle)

        # 3) Duration trim (priority = кто режется первым)
        target_sets = self._trim_to_duration_budget(target_sets, day_type, self.params.use_supersets)

        # 4) Список целевых мышц дня (по объёму)
        target_muscles = [m for m, s in target_sets.items() if s > 0]

        # 5) Фильтруем упражнения: только те, где main_muscle в target_muscles
        # (как ты и хочешь: "целевая мышца = главная")
        filtered = [
            ex for ex in exercises
            if ex.main_muscle_group in target_muscles
        ]

        # 6) Equipment filter
        if self.params.preferred_equipment:
            filtered = [ex for ex in filtered if self._passes_equipment_filter(ex)]

        if not filtered:
            logger.warning("Нет упражнений после фильтрации")
            return None

        # 7) Подбор упражнений по мышцам: priority задаёт порядок, но не объём
        priorities = MUSCLE_PRIORITY.get(day_type, {})
        muscles_sorted = sorted(
            target_muscles,
            key=lambda m: priorities.get(m, 0),
            reverse=True
        )

        selected: List[Dict] = []
        used_ids: Set[int] = set()
        forbidden_skipped = 0
        limited_included = 0

        for muscle in muscles_sorted:
            remaining = target_sets.get(muscle, 0)
            if remaining <= 0:
                continue

            # кандидаты только по main_muscle
            muscle_candidates = [ex for ex in filtered if ex.main_muscle_group == muscle]

            # если нет кандидатов — просто не можем закрыть объём этой мышцы
            if not muscle_candidates:
                logger.warning(f"Нет упражнений с main_muscle={muscle}")
                continue

            while remaining > 0:
                # выбираем упражнение с учетом history/favorites
                chosen = self._select_exercise_with_history_and_favorites(
                    muscle_candidates, muscle, history, used_ids
                )
                if not chosen:
                    break

                # травмы
                permission = self._check_exercise_permission(chosen)
                if permission == ExercisePermission.FORBIDDEN:
                    forbidden_skipped += 1
                    used_ids.add(chosen.id)
                    continue
                if permission == ExercisePermission.LIMITED:
                    limited_included += 1

                sets = self._calculate_sets_for_exercise(remaining)
                reps_min, reps_max = self._apply_rep_range()

                selected.append({
                    "exercise": chosen,
                    "target_muscle": muscle,
                    "secondary_muscles": chosen.secondary_muscle_groups or [],
                    "sets": sets,
                    "reps_min": reps_min,
                    "reps_max": reps_max,
                    "type": "compound" if chosen.category == "Базовое" else "isolation",
                    "permission": permission,
                })

                remaining -= sets
                used_ids.add(chosen.id)

                # если мы упёрлись в отсутствие упражнений (все использованы)
                if all(ex.id in used_ids for ex in muscle_candidates):
                    break

            # сохраним остаток (не обязательно, но полезно в логах)
            target_sets[muscle] = remaining

        if not selected:
            logger.warning("Не выбрано ни одного упражнения")
            return None

        # 8) Пост-обработка: убрать 1 сетовые хвосты, порядок, суперсеты
        selected = self._fix_one_set_cases(selected)
        selected = self._reorder_exercises(selected)

        if self.params.use_supersets and len(selected) >= 4:
            selected = self._group_into_supersets(selected)

        # 9) Форматирование ответа (как у тебя)
        exercises_data = []
        for i, ex in enumerate(selected, 1):
            notes = f"{ex['target_muscle']} ({ex['type']})"
            if ex["permission"] == ExercisePermission.LIMITED:
                notes = f"⚠️ {notes}"

            exercises_data.append({
                "order": i,
                "exercise_id": ex["exercise"].id,
                "name": ex["exercise"].name,
                "sets": ex["sets"],
                "reps": f"{ex['reps_min']}-{ex['reps_max']}",
                "superset_id": ex.get("superset_id"),
                "notes": notes,
                "permission": ex["permission"].name,
            })

        favorite_count = sum(1 for ex in selected if ex["exercise"].id in self.favorite_exercises)
        equipment_used = list({eq for ex in selected for eq in (ex["exercise"].equipment_needed or [])})
        limited_count = sum(1 for ex in selected if ex["permission"] == ExercisePermission.LIMITED)

        return {
            "exercises": exercises_data,
            "params": self.params.to_dict() | {"resolved_day_type": day_type},
            "stats": {
                "total_exercises": len(exercises_data),
                "favorite_count": favorite_count,
                "equipment_used": equipment_used,
                "total_sets": sum(ex["sets"] for ex in selected),
                "limited_count": limited_count,
                "forbidden_skipped": forbidden_skipped,
            }
        }


class AdvancedGeneratorPresetService:
    """CRUD сервис для пресетов продвинутого генератора"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_preset(
        self,
        user_id: int,
        name: str,
        params: AdvancedWorkoutParams,
        description: Optional[str] = None,
        is_default: bool = False,
    ) -> AdvancedGeneratorPreset:
        # если делаем дефолтным — сбросить предыдущий дефолт
        if is_default:
            await self.session.execute(
                sqlalchemy_update(AdvancedGeneratorPreset)
                .where(
                    AdvancedGeneratorPreset.user_id == user_id,
                    AdvancedGeneratorPreset.is_default == True
                )
                .values(is_default=False)
            )

        preset = AdvancedGeneratorPreset(
            user_id=user_id,
            name=name,
            description=description,
            settings=params.to_dict(),
            is_default=is_default,
        )

        self.session.add(preset)
        await self.session.commit()
        await self.session.refresh(preset)
        return preset

    async def get_user_presets(self, user_id: int) -> List[AdvancedGeneratorPreset]:
        result = await self.session.execute(
            select(AdvancedGeneratorPreset)
            .where(AdvancedGeneratorPreset.user_id == user_id)
            .order_by(
                AdvancedGeneratorPreset.is_default.desc(),
                AdvancedGeneratorPreset.created_at.desc(),
            )
        )
        return result.scalars().all()

    async def get_default_preset(self, user_id: int) -> Optional[AdvancedGeneratorPreset]:
        result = await self.session.execute(
            select(AdvancedGeneratorPreset).where(
                AdvancedGeneratorPreset.user_id == user_id,
                AdvancedGeneratorPreset.is_default == True
            )
        )
        return result.scalar_one_or_none()

    async def set_default(self, preset_id: int, user_id: int) -> bool:
        preset = await self.session.get(AdvancedGeneratorPreset, preset_id)
        if not preset or preset.user_id != user_id:
            return False

        # сброс старого дефолта
        await self.session.execute(
            sqlalchemy_update(AdvancedGeneratorPreset)
            .where(
                AdvancedGeneratorPreset.user_id == user_id,
                AdvancedGeneratorPreset.is_default == True
            )
            .values(is_default=False)
        )

        preset.is_default = True
        await self.session.commit()
        return True

    async def update_preset(
        self,
        preset_id: int,
        user_id: int,
        params: Optional[AdvancedWorkoutParams] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
        is_default: Optional[bool] = None,
    ) -> Optional[AdvancedGeneratorPreset]:
        preset = await self.session.get(AdvancedGeneratorPreset, preset_id)
        if not preset or preset.user_id != user_id:
            return None

        if params is not None:
            preset.settings = params.to_dict()
        if name is not None:
            preset.name = name
        if description is not None:
            preset.description = description
        if is_default is not None:
            if is_default:
                await self.session.execute(
                    sqlalchemy_update(AdvancedGeneratorPreset)
                    .where(
                        AdvancedGeneratorPreset.user_id == user_id,
                        AdvancedGeneratorPreset.is_default == True
                    )
                    .values(is_default=False)
                )
            preset.is_default = is_default

        await self.session.commit()
        await self.session.refresh(preset)
        return preset

    async def delete_preset(self, preset_id: int, user_id: int) -> bool:
        preset = await self.session.get(AdvancedGeneratorPreset, preset_id)
        if not preset or preset.user_id != user_id:
            return False

        await self.session.delete(preset)
        await self.session.commit()
        return True