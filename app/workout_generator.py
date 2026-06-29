# services/workout_generator.py
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from random import choice
from typing import Dict, List, Optional, Set, Tuple
from collections import defaultdict, Counter

from sqlalchemy import select

from api.services.models import Exercise, User, WorkoutLog
from app.muscle_classification import MuscleGroups
from api.services.volume_tables import TrainingVolumeTables, RepetitionRanges

logger = logging.getLogger(__name__)


# -----------------------------
# Muscle name mapping (Enum <-> RU strings from DB)
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


def _safe_lower(s: Optional[str]) -> str:
    return (s or "").strip().lower()


class WorkoutGenerator:
    """
    Стандартный генератор (без логики травм), но:
      - объем берется из TrainingVolumeTables (недельные таблицы -> per-workout по сплиту)
      - частота считается по всему сплиту (split_tag + workout_index)
      - работает с RU-строками мышц в Exercise.main_muscle_group/secondary_muscle_groups
    """

    def __init__(self, session):
        self.session = session
        self.muscle_priorities = self._build_muscle_priorities()

    def _build_muscle_priorities(self) -> Dict[str, Dict[str, int]]:
        """
        Приоритеты для порядка набора упражнений (не для расчета объема!).
        Ключи — day_type из volume_tables: fullbody/upper/lower/push/pull/legs.
        Значения — RU-названия мышц, как в базе.
        """
        return {
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
                # "Предплечья": 1,  # если реально есть как muscle group в БД — верни, иначе лучше убрать
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
    # Public API
    # -----------------------------
    async def generate_workout_for_day(
        self,
        user_id: int,
        split_tag: str,
        workout_index: int,
        accent_muscle: Optional[str] = None,  # RU name, e.g. "Грудь"
        is_short: bool = False,
        use_supersets: bool = False,
    ) -> Tuple[Dict, List[Dict]]:
        """
        split_tag: fullbody_3 / upper_lower_4 / ppl_6 / upper_lower_rest_repeat (как в БД)
        workout_index: 0..(sessions_per_week-1) индекс тренировки в паттерне сплита
        accent_muscle: RU название мышцы (как в БД), опционально
        """

        user = await self.session.get(User, user_id)
        if not user:
            raise ValueError("Пользователь не найден")

        level = user.experience_level or "beginner"

        # 1) Объем на тренировку берём из volume_tables
        day_type, sets_by_enum = TrainingVolumeTables.get_sets_per_muscle_per_workout(
            level=level,
            split_tag=split_tag,
            workout_index=workout_index,
            is_short=is_short,
        )

        # 2) Конвертация в RU-ключи (как в Exercise.main_muscle_group)
        sets_per_muscle: Dict[str, int] = {
            MUSCLE_ENUM_TO_RU[m]: s
            for m, s in sets_by_enum.items()
            if s and s > 0 and m in MUSCLE_ENUM_TO_RU
        }

        # 3) Акцент: +50% на одну мышцу, -25% на остальные активные
        # (оставляем как у тебя; если захочешь — можно сделать "мягкий акцент" без штрафа остальным)
        if accent_muscle:
            accent_muscle = accent_muscle.strip()
            if accent_muscle in sets_per_muscle:
                sets_per_muscle = self._apply_accent_logic(sets_per_muscle, accent_muscle)

        # 4) Приоритеты для порядка набора упражнений
        priorities = self.muscle_priorities.get(day_type, {}).copy()
        # если в приоритетах есть мышцы, которых нет в объемах этого дня — убираем
        priorities = {m: p for m, p in priorities.items() if sets_per_muscle.get(m, 0) > 0}

        # fallback: если почему-то priorities пустые — просто берём мышцы из sets_per_muscle
        if not priorities:
            priorities = {m: 1 for m in sets_per_muscle.keys()}

        # 5) Данные из БД + история
        available_exercises = await self._get_exercises_from_db()
        recent_by_muscle = await self._get_recent_exercises_history(user_id, days=7)

        # фильтрация по мышцам дня (и main, и secondary)
        target_muscles = list(sets_per_muscle.keys())
        filtered_exercises = self._filter_exercises_by_muscles(available_exercises, target_muscles)

        # 6) Выбор упражнений под нужные сеты
        selected = self._select_exercises_smart(
            exercises=filtered_exercises,
            priorities=priorities,
            sets_per_muscle=sets_per_muscle,
            level=level,
            recent_exercises=recent_by_muscle,
        )

        # 7) Суперсеты
        if use_supersets and len(selected) > 1:
            selected = self._group_into_supersets(selected)

        # 8) План + форматирование упражнений (повторы)
        workout_plan = self._create_plan_structure(
            user_id=user_id,
            split_tag=split_tag,
            workout_index=workout_index,
            day_type=day_type,
            level=level,
            accent_muscle=accent_muscle,
            is_short=is_short,
            use_supersets=use_supersets,
        )

        exercises_data = self._format_exercises(selected, level, is_short=is_short)
        return workout_plan, exercises_data

    # -----------------------------
    # Accent logic
    # -----------------------------
    def _apply_accent_logic(self, sets_dict: Dict[str, int], accent_muscle: str) -> Dict[str, int]:
        """
        Акцент: sets * 1.5
        Остальные активные: sets * 0.75
        Округление: int(x + 0.5)

        Примечание:
        Минимумы по мышцам лучше держать в volume_tables (там big/small).
        Тут мы НЕ форсим min=2, чтобы не раздувать малые группы.
        """
        new_sets: Dict[str, int] = {}
        for muscle, count in sets_dict.items():
            if muscle == accent_muscle:
                new_sets[muscle] = max(1, int(count * 1.5 + 0.5))
            else:
                new_sets[muscle] = max(1, int(count * 0.75 + 0.5))
        return new_sets

    # -----------------------------
    # DB + history
    # -----------------------------
    async def _get_exercises_from_db(self) -> List[Exercise]:
        stmt = select(Exercise)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def _get_recent_exercises_history(self, user_id: int, days: int = 7) -> Dict[str, List[str]]:
        cutoff_date = datetime.utcnow() - timedelta(days=days)
        stmt = (
            select(WorkoutLog)
            .where(WorkoutLog.user_id == user_id, WorkoutLog.created_at >= cutoff_date)
            .order_by(WorkoutLog.created_at.desc())
        )
        result = await self.session.execute(stmt)
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

    # -----------------------------
    # Filtering & selection
    # -----------------------------
    def _filter_exercises_by_muscles(self, exercises: List[Exercise], target_muscles: List[str]) -> List[Exercise]:
        target = set(target_muscles)
        filtered: List[Exercise] = []

        for ex in exercises:
            if ex.main_muscle_group in target:
                filtered.append(ex)
                continue
            secondary = ex.secondary_muscle_groups or []
            if any(m in target for m in secondary):
                filtered.append(ex)

        logger.info(f"Фильтрация упражнений: {len(exercises)} → {len(filtered)}")
        return filtered

    def _select_exercises_smart(
        self,
        exercises: List[Exercise],
        priorities: Dict[str, int],
        sets_per_muscle: Dict[str, int],
        level: str,
        recent_exercises: Dict[str, List[str]],
    ) -> List[Dict]:
        """
        Распределяет подходы по мышцам, выбирая 1+ упражнений на мышцу (2-4 сета за упражнение).
        Важно: учитывает упражнения, где мышца вторична (fallback).
        """

        selected: List[Dict] = []
        remaining_sets = sets_per_muscle.copy()
        used_exercise_ids: Set[int] = set()

        # Идём по мышцам по приоритету (3 -> 2 -> 1)
        muscles_sorted = sorted(priorities.items(), key=lambda x: x[1], reverse=True)

        for muscle, _prio in muscles_sorted:
            need = remaining_sets.get(muscle, 0)
            if need <= 0:
                continue

            while remaining_sets.get(muscle, 0) > 0:
                candidates = self._find_exercises_for_muscle_all(exercises, muscle, used_exercise_ids)
                if not candidates:
                    logger.warning(f"Нет доступных упражнений для {muscle}")
                    break

                chosen = self._select_exercise_with_history(
                    candidates=candidates,
                    target_muscle=muscle,
                    recent_exercises=recent_exercises,
                    used_exercises=used_exercise_ids,
                )
                if not chosen:
                    break

                exercise_sets = self._calculate_exercise_sets(remaining_sets[muscle], level)
                ex_type = "compound" if chosen.category == "Базовое" else "isolation"

                selected.append(
                    {
                        "exercise": chosen,
                        "target_muscle": muscle,
                        "exercise_type": ex_type,
                        "sets": exercise_sets,
                    }
                )
                remaining_sets[muscle] -= exercise_sets
                used_exercise_ids.add(chosen.id)

        # Финальный проход: если где-то остались подходы (часто из-за нехватки упражнений main),
        # пытаемся добить любыми упражнениями, где мышца вторична.
        for muscle, rem in list(remaining_sets.items()):
            if rem <= 0:
                continue

            while remaining_sets.get(muscle, 0) > 0:
                candidates = self._find_exercises_for_muscle_secondary_only(exercises, muscle, used_exercise_ids)
                if not candidates:
                    break

                chosen = self._select_exercise_with_history(
                    candidates=candidates,
                    target_muscle=muscle,
                    recent_exercises=recent_exercises,
                    used_exercises=used_exercise_ids,
                )
                if not chosen:
                    break

                exercise_sets = self._calculate_exercise_sets(remaining_sets[muscle], level)
                ex_type = "compound" if chosen.category == "Базовое" else "isolation"

                selected.append(
                    {
                        "exercise": chosen,
                        "target_muscle": muscle,
                        "exercise_type": ex_type,
                        "sets": exercise_sets,
                    }
                )
                remaining_sets[muscle] -= exercise_sets
                used_exercise_ids.add(chosen.id)

        selected = self._fix_sets_and_reorder(selected)
        logger.info(f"Выбрано упражнений: {len(selected)}")
        return selected

    def _find_exercises_for_muscle_all(
        self,
        exercises: List[Exercise],
        target_muscle: str,
        used_ids: Set[int],
    ) -> List[Exercise]:
        """
        Возвращает кандидаты для мышцы:
          1) упражнения, где target_muscle — main
          2) затем упражнения, где target_muscle — secondary
        """
        main_hits: List[Exercise] = []
        secondary_hits: List[Exercise] = []

        for ex in exercises:
            if ex.id in used_ids:
                continue
            if ex.main_muscle_group == target_muscle:
                main_hits.append(ex)
            elif target_muscle in (ex.secondary_muscle_groups or []):
                secondary_hits.append(ex)

        # Сначала main, затем secondary (fallback)
        return main_hits + secondary_hits

    def _find_exercises_for_muscle_secondary_only(
        self,
        exercises: List[Exercise],
        target_muscle: str,
        used_ids: Set[int],
    ) -> List[Exercise]:
        return [
            ex
            for ex in exercises
            if ex.id not in used_ids and target_muscle in (ex.secondary_muscle_groups or [])
        ]

    def _select_exercise_with_history(
        self,
        candidates: List[Exercise],
        target_muscle: str,
        recent_exercises: Dict[str, List[str]],
        used_exercises: Set[int],
    ) -> Optional[Exercise]:
        """
        Предпочтение:
          - сначала базовые, потом изоляция
          - сначала неиспользованные за последние дни, потом использованные 1 раз, потом 2+ раз
        """
        if not candidates:
            return None

        history = recent_exercises.get(target_muscle, [])
        counts = Counter(history)

        never_used_compound: List[Exercise] = []
        never_used_isolation: List[Exercise] = []
        used_once_compound: List[Exercise] = []
        used_once_isolation: List[Exercise] = []
        used_more: List[Exercise] = []

        for ex in candidates:
            if ex.id in used_exercises:
                continue

            c = counts.get(ex.name, 0)
            is_compound = (ex.category == "Базовое")

            if c == 0:
                (never_used_compound if is_compound else never_used_isolation).append(ex)
            elif c == 1:
                (used_once_compound if is_compound else used_once_isolation).append(ex)
            else:
                used_more.append(ex)

        for bucket in [never_used_compound, used_once_compound, never_used_isolation, used_once_isolation, used_more]:
            if bucket:
                chosen = choice(bucket)
                return chosen

        return None

    def _calculate_exercise_sets(self, remaining_sets: int, level: str) -> int:
        """
        2-4 сетов за упражнение.
        Если осталось меньше 2 — берём остаток (потом _fix_sets_and_reorder попробует исправить).
        """
        min_sets, max_sets = 2, 4
        if remaining_sets < min_sets:
            return remaining_sets
        if remaining_sets > max_sets:
            return max_sets
        return remaining_sets

    # -----------------------------
    # Post-processing (sets + order)
    # -----------------------------
    def _fix_sets_and_reorder(self, selected_exercises: List[Dict]) -> List[Dict]:
        """
        - Дотягиваем упражнения до 2 сетов, если получилось 1 (пытаемся забрать у предыдущих).
        - Переставляем, чтобы не шли подряд упражнения на одну мышцу.
        """
        fixed: List[Dict] = []
        for i, ex in enumerate(selected_exercises):
            if ex["sets"] >= 2:
                fixed.append(ex)
                continue

            # sets == 1 (или 0) — пытаемся забрать у предыдущего упражнения на ДРУГУЮ мышцу
            taken = False
            for j in range(len(fixed) - 1, -1, -1):
                if fixed[j]["sets"] > 2 and fixed[j]["target_muscle"] != ex["target_muscle"]:
                    fixed[j]["sets"] -= 1
                    ex["sets"] = 2
                    taken = True
                    break

            # если не получилось — ставим 2 (да, это создаёт +1 сет; лучше, чем 1 сет в плане)
            if not taken:
                ex["sets"] = 2

            fixed.append(ex)

        return self._reorder_exercises(fixed)

    def _reorder_exercises(self, exercises: List[Dict]) -> List[Dict]:
        if len(exercises) <= 1:
            return exercises

        compound = [ex for ex in exercises if ex["exercise_type"] == "compound"]
        isolation = [ex for ex in exercises if ex["exercise_type"] == "isolation"]

        first = choice(compound) if compound else exercises[0]
        remaining = [ex for ex in exercises if ex is not first]

        result = [first]
        last_muscle = first["target_muscle"]

        # простая "антисоседняя" перестановка
        while remaining:
            candidates = [ex for ex in remaining if ex["target_muscle"] != last_muscle]
            chosen = choice(candidates) if candidates else remaining[0]
            result.append(chosen)
            remaining.remove(chosen)
            last_muscle = chosen["target_muscle"]

        return result

    # -----------------------------
    # Output formatting
    # -----------------------------
    def _format_exercises(self, selected_exercises: List[Dict], level: str, is_short: bool = False) -> List[Dict]:
        exercises_data: List[Dict] = []
        total_ex = len(selected_exercises)
        if total_ex == 0:
            return []

        rep_pool = RepetitionRanges.get_proportional_pool(level, total_ex, is_short=is_short)
        rep_pool.sort(key=lambda x: x["min"])  # тяжелые первыми

        indexed = [{"data": ex, "original_order": i} for i, ex in enumerate(selected_exercises)]
        indexed.sort(key=lambda x: 0 if x["data"]["exercise_type"] == "compound" else 1)

        for i, item in enumerate(indexed):
            item["assigned_range"] = rep_pool[i]

        indexed.sort(key=lambda x: x["original_order"])

        for order, item in enumerate(indexed, 1):
            ex_selection = item["data"]
            exercise = ex_selection["exercise"]
            rr = item["assigned_range"]

            type_display = "Базовое" if ex_selection["exercise_type"] == "compound" else "Изоляция"
            exercises_data.append(
                {
                    "order": order,
                    "exercise_id": exercise.id,
                    "name": exercise.name,
                    "sets": ex_selection["sets"],
                    "reps": f"{rr['min']}-{rr['max']}",
                    "weight": None,
                    "superset_id": ex_selection.get("superset_id"),
                    "notes": f"{ex_selection['target_muscle']} ({type_display})",
                }
            )

        return exercises_data

    def _create_plan_structure(
        self,
        user_id: int,
        split_tag: str,
        workout_index: int,
        day_type: str,
        level: str,
        accent_muscle: Optional[str],
        is_short: bool,
        use_supersets: bool,
    ) -> Dict:
        meta = []
        meta.append(f"Сплит: {split_tag}")
        meta.append(f"День: {day_type} ({workout_index + 1})")
        if accent_muscle:
            meta.append(f"Акцент: {accent_muscle}")
        if is_short:
            meta.append("Режим: Express")
        if use_supersets:
            meta.append("Тип: Суперсеты")

        return {
            "name": f"Генератор тренировки ({day_type}) - {datetime.now().strftime('%d.%m.%Y')}",
            "description": f"Умная тренировка для {level} уровня | " + " | ".join(meta),
            "tags": {
                "day_tag": day_type,          # важно: day_type, не split_tag
                "split_tag": split_tag,       # добавляем отдельно
                "workout_index": workout_index,
                "difficulty": level,
                "source": "smart_generated",
                "auto_generated": True,
            },
            "exercises": [],
            "user_id": user_id,
            "is_generated": True,
            "is_public": False,
        }

    # -----------------------------
    # Supersets (оставил твою исправленную логику)
    # -----------------------------
    def _group_into_supersets(self, exercises: List[Dict]) -> List[Dict]:
        import uuid

        grouped_list: List[Dict] = []
        used_indices: Set[int] = set()

        if exercises:
            grouped_list.append(exercises[0])
            used_indices.add(0)

        for i in range(1, len(exercises)):
            if i in used_indices:
                continue

            ex_a = exercises[i]
            pair_found = False

            for j in range(i + 1, len(exercises)):
                if j in used_indices:
                    continue

                ex_b = exercises[j]

                main_a = ex_a["exercise"].main_muscle_group
                main_b = ex_b["exercise"].main_muscle_group

                sec_a = set(ex_a["exercise"].secondary_muscle_groups or [])
                sec_b = set(ex_b["exercise"].secondary_muscle_groups or [])

                conflict = (
                    main_a == main_b
                    or main_a in sec_b
                    or main_b in sec_a
                )

                if not conflict:
                    sid = str(uuid.uuid4())
                    ex_a["superset_id"] = sid
                    ex_b["superset_id"] = sid

                    grouped_list.append(ex_a)
                    grouped_list.append(ex_b)
                    used_indices.add(i)
                    used_indices.add(j)
                    pair_found = True
                    break

            if not pair_found:
                grouped_list.append(ex_a)
                used_indices.add(i)

        return grouped_list