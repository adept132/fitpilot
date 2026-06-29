from __future__ import annotations

import uuid
from collections import defaultdict
from decimal import Decimal
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.services.models import WorkoutSession, WorkoutSessionExercise, WorkoutSessionSet

def ensure_unique_exercise_ids_for_new_superset(
    session_exercises: list[WorkoutSessionExercise],
) -> None:
    exercise_ids = [item.exercise_id for item in session_exercises]

    if len(exercise_ids) != len(set(exercise_ids)):
        raise HTTPException(
            status_code=400,
            detail="Нельзя создать суперсет с повторяющимися упражнениями.",
        )

class WorkoutSupersetService:
    @staticmethod
    def _set_weight_to_float(value: Decimal | float | int | None) -> float:
        if value is None:
            return 0.0
        return float(value)

    @staticmethod
    def _sets_count(session_exercise: WorkoutSessionExercise) -> int:
        return len(session_exercise.sets or [])

    @staticmethod
    def _volume_total(session_exercise: WorkoutSessionExercise) -> float:
        total = 0.0
        for set_item in session_exercise.sets or []:
            weight = WorkoutSupersetService._set_weight_to_float(set_item.weight)
            reps = set_item.reps or 0
            total += weight * reps
        return total

    @staticmethod
    def _rounds_completed(exercises: list[WorkoutSessionExercise]) -> int:
        if not exercises:
            return 0
        return min(WorkoutSupersetService._sets_count(ex) for ex in exercises)

    @staticmethod
    def _sets_total(exercises: list[WorkoutSessionExercise]) -> int:
        return sum(WorkoutSupersetService._sets_count(ex) for ex in exercises)

    @staticmethod
    def _volume_group_total(exercises: list[WorkoutSessionExercise]) -> float:
        return sum(WorkoutSupersetService._volume_total(ex) for ex in exercises)

    @staticmethod
    def _has_incomplete_round(exercises: list[WorkoutSessionExercise]) -> bool:
        if not exercises:
            return False
        counts = [WorkoutSupersetService._sets_count(ex) for ex in exercises]
        return len(set(counts)) > 1

    @staticmethod
    def _first_incomplete_session_exercise_id(
        exercises: list[WorkoutSessionExercise],
    ) -> int | None:
        if not exercises:
            return None

        rounds_completed = WorkoutSupersetService._rounds_completed(exercises)

        for exercise in sorted(exercises, key=lambda ex: ex.order_index):
            if WorkoutSupersetService._sets_count(exercise) == rounds_completed:
                return exercise.id

        return None

    @staticmethod
    def _superset_label_map(
        session_exercises: list[WorkoutSessionExercise],
    ) -> dict[str, str]:
        ordered_groups: list[str] = []
        seen: set[str] = set()

        for exercise in sorted(session_exercises, key=lambda ex: ex.order_index):
            if not exercise.superset_group:
                continue
            group = str(exercise.superset_group)
            if group in seen:
                continue
            seen.add(group)
            ordered_groups.append(group)

        labels: dict[str, str] = {}
        for idx, group in enumerate(ordered_groups):
            labels[group] = chr(ord("A") + idx)

        return labels

    @staticmethod
    def _serialize_set(set_item: WorkoutSessionSet) -> dict:
        return {
            "id": set_item.id,
            "set_number": set_item.set_number,
            "weight": float(set_item.weight) if set_item.weight is not None else None,
            "reps": set_item.reps,
            "effort_level": set_item.effort_level,
            "notes": set_item.notes,
            "is_completed": set_item.is_completed,
        }

    @staticmethod
    async def _get_last_performance_sets(
        session: AsyncSession,
        app_user_id: int,
        exercise_id: int,
    ) -> list[dict]:
        stmt = (
            select(WorkoutSession)
            .join(
                WorkoutSessionExercise,
                WorkoutSessionExercise.workout_session_id == WorkoutSession.id,
            )
            .where(
                WorkoutSession.app_user_id == app_user_id,
                WorkoutSession.status == "finished",
                WorkoutSessionExercise.exercise_id == exercise_id,
            )
            .options(
                selectinload(WorkoutSession.exercises).selectinload(
                    WorkoutSessionExercise.sets
                ),
                selectinload(WorkoutSession.exercises).selectinload(
                    WorkoutSessionExercise.exercise
                ),
            )
            .order_by(WorkoutSession.finished_at.desc())
        )

        result = await session.execute(stmt)
        workout = result.scalars().first()

        if not workout:
            return []

        session_exercise = next(
            (item for item in workout.exercises if item.exercise_id == exercise_id),
            None,
        )

        if not session_exercise:
            return []

        completed_sets = [s for s in session_exercise.sets if s.is_completed]
        return [WorkoutSupersetService._serialize_set(set_item) for set_item in completed_sets]

    @staticmethod
    async def get_workout_session_exercises(
        session: AsyncSession,
        workout_id: int,
        app_user_id: int,
    ) -> list[WorkoutSessionExercise]:
        stmt = (
            select(WorkoutSession)
            .where(
                WorkoutSession.id == workout_id,
                WorkoutSession.app_user_id == app_user_id,
            )
            .options(
                selectinload(WorkoutSession.exercises).selectinload(
                    WorkoutSessionExercise.exercise
                ),
                selectinload(WorkoutSession.exercises).selectinload(
                    WorkoutSessionExercise.sets
                ),
            )
        )

        result = await session.execute(stmt)
        workout = result.scalar_one_or_none()

        if workout is None:
            raise HTTPException(status_code=404, detail="Workout not found")

        return sorted(workout.exercises, key=lambda ex: ex.order_index)

    @staticmethod
    async def build_workout_structure(
        session: AsyncSession,
        workout_id: int,
        app_user_id: int,
    ) -> list[dict]:
        session_exercises = await WorkoutSupersetService.get_workout_session_exercises(
            session=session,
            workout_id=workout_id,
            app_user_id=app_user_id,
        )

        labels = WorkoutSupersetService._superset_label_map(session_exercises)

        grouped: dict[str, list[WorkoutSessionExercise]] = defaultdict(list)
        for exercise in session_exercises:
            if exercise.superset_group:
                grouped[str(exercise.superset_group)].append(exercise)

        items: list[dict] = []
        emitted_supersets: set[str] = set()

        for exercise in session_exercises:
            if not exercise.superset_group:
                items.append(
                    {
                        "type": "exercise",
                        "session_exercise_id": exercise.id,
                        "order_index": exercise.order_index,
                        "exercise_id": exercise.exercise_id,
                        "exercise_name": exercise.exercise.name if exercise.exercise else "Без названия",
                        "sets_count": WorkoutSupersetService._sets_count(exercise),
                        "volume_total": WorkoutSupersetService._volume_total(exercise),
                        "sets": [
                            WorkoutSupersetService._serialize_set(set_item)
                            for set_item in exercise.sets
                        ],
                    }
                )
                continue

            group = str(exercise.superset_group)
            if group in emitted_supersets:
                continue

            emitted_supersets.add(group)
            group_exercises = sorted(grouped[group], key=lambda ex: ex.order_index)

            items.append(
                {
                    "type": "superset",
                    "superset_group": group,
                    "label": labels.get(group, "A"),
                    "order_index": min(ex.order_index for ex in group_exercises),
                    "rounds_completed": WorkoutSupersetService._rounds_completed(group_exercises),
                    "sets_total": WorkoutSupersetService._sets_total(group_exercises),
                    "volume_total": WorkoutSupersetService._volume_group_total(group_exercises),
                    "has_incomplete_round": WorkoutSupersetService._has_incomplete_round(group_exercises),
                    "exercises": [
                        {
                            "session_exercise_id": ex.id,
                            "order_index": ex.order_index,
                            "exercise_id": ex.exercise_id,
                            "exercise_name": ex.exercise.name if ex.exercise else "Без названия",
                            "sets_count": WorkoutSupersetService._sets_count(ex),
                            "volume_total": WorkoutSupersetService._volume_total(ex),
                        }
                        for ex in group_exercises
                    ],
                }
            )

        return items

    @staticmethod
    async def create_superset(
            session: AsyncSession,
            workout_id: int,
            app_user_id: int,
            source_session_exercise_id: int,
            target_session_exercise_ids: list[int],
    ) -> str:
        if not target_session_exercise_ids:
            raise HTTPException(
                status_code=400,
                detail="At least one target exercise is required",
            )

        all_session_exercise_ids = [
            source_session_exercise_id,
            *target_session_exercise_ids,
        ]

        if len(all_session_exercise_ids) != len(set(all_session_exercise_ids)):
            raise HTTPException(
                status_code=400,
                detail="В суперсете не должно быть повторяющихся упражнений.",
            )

        session_exercises = await WorkoutSupersetService.get_workout_session_exercises(
            session=session,
            workout_id=workout_id,
            app_user_id=app_user_id,
        )

        exercises_by_id = {exercise.id: exercise for exercise in session_exercises}
        source = exercises_by_id.get(source_session_exercise_id)

        if source is None:
            raise HTTPException(status_code=404, detail="Source exercise not found")

        selected: list[WorkoutSessionExercise] = [source]

        for target_id in target_session_exercise_ids:
            target = exercises_by_id.get(target_id)
            if target is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Target exercise {target_id} not found",
                )
            selected.append(target)

        exercise_ids = [exercise.exercise_id for exercise in selected]
        if len(exercise_ids) != len(set(exercise_ids)):
            raise HTTPException(
                status_code=400,
                detail="Нельзя создать суперсет с повторяющимися упражнениями.",
            )

        superset_group = str(uuid4())

        for exercise in selected:
            exercise.superset_group = superset_group

        selected_sorted = sorted(selected, key=lambda ex: ex.order_index)

        start_order = min(ex.order_index for ex in selected_sorted)
        for idx, exercise in enumerate(selected_sorted):
            exercise.order_index = start_order + idx

        await session.commit()
        return superset_group

    @staticmethod
    async def add_existing_exercise_to_superset(
        session: AsyncSession,
        app_user_id: int,
        superset_group: str,
        session_exercise_id: int,
    ) -> None:
        stmt = (
            select(WorkoutSessionExercise)
            .join(
                WorkoutSession,
                WorkoutSession.id == WorkoutSessionExercise.workout_session_id,
            )
            .where(
                WorkoutSession.app_user_id == app_user_id,
            )
            .options(selectinload(WorkoutSessionExercise.exercise))
        )

        result = await session.execute(stmt)
        exercises = result.scalars().all()

        group_exercises = [ex for ex in exercises if str(ex.superset_group or "") == superset_group]
        candidate = next((ex for ex in exercises if ex.id == session_exercise_id), None)

        if not group_exercises:
            raise HTTPException(status_code=404, detail="Superset not found")

        if candidate is None:
            raise HTTPException(status_code=404, detail="Exercise not found")

        if candidate.workout_session_id != group_exercises[0].workout_session_id:
            raise HTTPException(
                status_code=400,
                detail="Exercise belongs to another workout",
            )

        candidate.superset_group = superset_group
        candidate.order_index = max(ex.order_index for ex in group_exercises) + 1

        await session.commit()

    @staticmethod
    async def add_new_exercise_to_superset(
            session: AsyncSession,
            app_user_id: int,
            superset_group: str,
            exercise_id: int,
    ):
        member_result = await session.execute(
            select(WorkoutSessionExercise)
            .join(
                WorkoutSession,
                WorkoutSession.id == WorkoutSessionExercise.workout_session_id,
            )
            .where(
                WorkoutSessionExercise.superset_group == superset_group,
                WorkoutSession.app_user_id == app_user_id,
            )
        )
        members = member_result.scalars().all()

        if not members:
            raise ValueError("Суперсет не найден")

        workout_session_id = members[0].workout_session_id

        max_order_result = await session.execute(
            select(func.max(WorkoutSessionExercise.order_index)).where(
                WorkoutSessionExercise.workout_session_id == workout_session_id
            )
        )
        max_order_index = max_order_result.scalar_one_or_none()
        next_order_index = (max_order_index or 0) + 1

        new_session_exercise = WorkoutSessionExercise(
            workout_session_id=workout_session_id,
            exercise_id=exercise_id,
            order_index=next_order_index,
            superset_group=superset_group,
            notes=None,
        )

        session.add(new_session_exercise)
        await session.commit()
        await session.refresh(new_session_exercise)

        return new_session_exercise

    @staticmethod
    async def remove_exercise_from_superset(
        session: AsyncSession,
        app_user_id: int,
        superset_group: str,
        session_exercise_id: int,
    ) -> None:
        stmt = (
            select(WorkoutSessionExercise)
            .join(
                WorkoutSession,
                WorkoutSession.id == WorkoutSessionExercise.workout_session_id,
            )
            .where(
                WorkoutSession.app_user_id == app_user_id,
            )
            .options(
                selectinload(WorkoutSessionExercise.exercise),
                selectinload(WorkoutSessionExercise.sets),
            )
        )

        result = await session.execute(stmt)
        exercises = result.scalars().all()

        group_exercises = [ex for ex in exercises if str(ex.superset_group or "") == superset_group]
        target = next((ex for ex in group_exercises if ex.id == session_exercise_id), None)

        if target is None:
            raise HTTPException(status_code=404, detail="Exercise not found in superset")

        target.superset_group = None

        remaining = [ex for ex in group_exercises if ex.id != session_exercise_id]
        if len(remaining) == 1:
            remaining[0].superset_group = None

        await session.commit()

    @staticmethod
    async def get_superset_flow(
            session: AsyncSession,
            app_user_id: int,
            superset_group: str,
    ) -> dict:
        stmt = (
            select(WorkoutSessionExercise)
            .join(
                WorkoutSession,
                WorkoutSession.id == WorkoutSessionExercise.workout_session_id,
            )
            .where(
                WorkoutSession.app_user_id == app_user_id,
                WorkoutSessionExercise.superset_group == superset_group,
            )
            .options(
                selectinload(WorkoutSessionExercise.exercise),
                selectinload(WorkoutSessionExercise.sets),
            )
            .order_by(WorkoutSessionExercise.order_index)
        )

        result = await session.execute(stmt)
        exercises = result.scalars().all()

        if not exercises:
            raise HTTPException(status_code=404, detail="Superset not found")

        workout_id = exercises[0].workout_session_id

        labels = WorkoutSupersetService._superset_label_map(exercises)
        label = labels.get(superset_group, "A")

        rounds_completed = WorkoutSupersetService._rounds_completed(exercises)
        current_round_number = rounds_completed + 1
        has_incomplete_round = WorkoutSupersetService._has_incomplete_round(exercises)
        first_incomplete_session_exercise_id = (
            WorkoutSupersetService._first_incomplete_session_exercise_id(exercises)
        )

        items = []
        for exercise in exercises:
            sets_count = WorkoutSupersetService._sets_count(exercise)
            last_performance_sets = await WorkoutSupersetService._get_last_performance_sets(
                session=session,
                app_user_id=app_user_id,
                exercise_id=exercise.exercise_id,
            )

            items.append(
                {
                    "session_exercise_id": exercise.id,
                    "order_index": exercise.order_index,
                    "exercise_id": exercise.exercise_id,
                    "exercise_name": exercise.exercise.name if exercise.exercise else "Без названия",
                    "sets": [
                        WorkoutSupersetService._serialize_set(set_item)
                        for set_item in exercise.sets
                    ],
                    "last_performance_sets": last_performance_sets,
                    "is_current_round_completed": sets_count > rounds_completed,
                    "recommended_rir": exercise.recommended_rir,
                    "recommended_rep_min": exercise.recommended_rep_min,
                    "recommended_rep_max": exercise.recommended_rep_max,
                }
            )

        return {
            "workout_id": workout_id,
            "superset_group": superset_group,
            "label": label,
            "rounds_completed": rounds_completed,
            "current_round_number": current_round_number,
            "has_incomplete_round": has_incomplete_round,
            "first_incomplete_session_exercise_id": first_incomplete_session_exercise_id,
            "exercises": items,
        }

    @staticmethod
    async def cleanup_single_member_supersets(
        session: AsyncSession,
        workout_id: int,
        app_user_id: int,
    ) -> None:
        session_exercises = await WorkoutSupersetService.get_workout_session_exercises(
            session=session,
            workout_id=workout_id,
            app_user_id=app_user_id,
        )

        grouped: dict[str, list[WorkoutSessionExercise]] = defaultdict(list)
        for exercise in session_exercises:
            if exercise.superset_group:
                grouped[str(exercise.superset_group)].append(exercise)

        changed = False
        for _, items in grouped.items():
            if len(items) == 1:
                items[0].superset_group = None
                changed = True

        if changed:
            await session.commit()

    @staticmethod
    async def delete_superset(
        session: AsyncSession,
        app_user_id: int,
        superset_group: str,
    ) -> None:
        stmt = (
            select(WorkoutSessionExercise)
            .join(
                WorkoutSession,
                WorkoutSession.id == WorkoutSessionExercise.workout_session_id,
            )
            .where(
                WorkoutSession.app_user_id == app_user_id,
                WorkoutSessionExercise.superset_group == superset_group,
            )
        )

        result = await session.execute(stmt)
        exercises = result.scalars().all()

        if not exercises:
            raise HTTPException(status_code=404, detail="Superset not found")

        for exercise in exercises:
            exercise.superset_group = None

        await session.commit()

    @staticmethod
    async def get_workout_session_exercises_map(
        session: AsyncSession,
        workout_id: int,
        app_user_id: int,
    ) -> dict[int, WorkoutSessionExercise]:
        session_exercises = await WorkoutSupersetService.get_workout_session_exercises(
            session=session,
            workout_id=workout_id,
            app_user_id=app_user_id,
        )
        return {exercise.id: exercise for exercise in session_exercises}

    @staticmethod
    async def cleanup_superset_groups_after_reorder(
        session: AsyncSession,
        workout_id: int,
        app_user_id: int,
    ) -> None:
        session_exercises = await WorkoutSupersetService.get_workout_session_exercises(
            session=session,
            workout_id=workout_id,
            app_user_id=app_user_id,
        )

        grouped: dict[str, list[WorkoutSessionExercise]] = defaultdict(list)

        for exercise in session_exercises:
            if exercise.superset_group:
                grouped[str(exercise.superset_group)].append(exercise)

        changed = False
        for _, members in grouped.items():
            if len(members) == 1:
                members[0].superset_group = None
                changed = True

        if changed:
            await session.commit()

    @staticmethod
    async def reorder_workout_structure(
        session: AsyncSession,
        workout_id: int,
        app_user_id: int,
        items: list[dict],
    ) -> None:
        exercises_map = await WorkoutSupersetService.get_workout_session_exercises_map(
            session=session,
            workout_id=workout_id,
            app_user_id=app_user_id,
        )

        next_order_index = 0
        touched_ids: set[int] = set()

        for item in items:
            item_type = item.get("type")

            if item_type == "exercise":
                session_exercise_id = item["session_exercise_id"]
                exercise = exercises_map.get(session_exercise_id)

                if exercise is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Exercise {session_exercise_id} not found",
                    )

                exercise.order_index = next_order_index
                exercise.superset_group = None

                touched_ids.add(exercise.id)
                next_order_index += 1
                continue

            if item_type == "superset":
                superset_group = item["superset_group"]
                members = item.get("members", [])

                if not members:
                    continue

                for member in members:
                    session_exercise_id = member["session_exercise_id"]
                    exercise = exercises_map.get(session_exercise_id)

                    if exercise is None:
                        raise HTTPException(
                            status_code=404,
                            detail=f"Exercise {session_exercise_id} not found",
                        )

                    exercise.order_index = next_order_index
                    exercise.superset_group = superset_group

                    touched_ids.add(exercise.id)
                    next_order_index += 1

                continue

            raise HTTPException(
                status_code=400,
                detail=f"Unsupported item type: {item_type}",
            )

        # Все упражнения тренировки должны быть описаны в payload
        untouched = [ex_id for ex_id in exercises_map.keys() if ex_id not in touched_ids]
        if untouched:
            raise HTTPException(
                status_code=400,
                detail=f"Some workout exercises are missing in reorder payload: {untouched}",
            )

        await session.commit()

        await WorkoutSupersetService.cleanup_superset_groups_after_reorder(
            session=session,
            workout_id=workout_id,
            app_user_id=app_user_id,
        )

    @staticmethod
    async def move_exercise_to_superset(
        session: AsyncSession,
        workout_id: int,
        app_user_id: int,
        session_exercise_id: int,
        superset_group: str,
    ) -> None:
        exercises_map = await WorkoutSupersetService.get_workout_session_exercises_map(
            session=session,
            workout_id=workout_id,
            app_user_id=app_user_id,
        )

        exercise = exercises_map.get(session_exercise_id)
        if exercise is None:
            raise HTTPException(status_code=404, detail="Exercise not found")

        exercise.superset_group = superset_group
        await session.commit()

        await WorkoutSupersetService.cleanup_superset_groups_after_reorder(
            session=session,
            workout_id=workout_id,
            app_user_id=app_user_id,
        )

    @staticmethod
    async def move_exercise_out_of_superset(
        session: AsyncSession,
        workout_id: int,
        app_user_id: int,
        session_exercise_id: int,
    ) -> None:
        exercises_map = await WorkoutSupersetService.get_workout_session_exercises_map(
            session=session,
            workout_id=workout_id,
            app_user_id=app_user_id,
        )

        exercise = exercises_map.get(session_exercise_id)
        if exercise is None:
            raise HTTPException(status_code=404, detail="Exercise not found")

        exercise.superset_group = None
        await session.commit()

        await WorkoutSupersetService.cleanup_superset_groups_after_reorder(
            session=session,
            workout_id=workout_id,
            app_user_id=app_user_id,
        )

    async def start_superset(
            db: AsyncSession,
            session_exercise_id: int,
    ) -> WorkoutSessionExercise:
        result = await db.execute(
            select(WorkoutSessionExercise).where(
                WorkoutSessionExercise.id == session_exercise_id
            )
        )
        session_exercise = result.scalar_one_or_none()

        if session_exercise is None:
            raise ValueError("Упражнение тренировки не найдено")

        if session_exercise.superset_group:
            return session_exercise

        session_exercise.superset_group = str(uuid.uuid4())

        await db.commit()
        await db.refresh(session_exercise)

        return session_exercise