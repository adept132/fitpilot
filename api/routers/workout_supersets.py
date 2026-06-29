from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from api.schemas.supersets import (
    AddExerciseToSupersetRequest,
    AddNewExerciseToSupersetRequest,
    RemoveExerciseFromSupersetRequest,
    SupersetFlowResponse, StartSupersetResponse,
)
from api.services.app_user_service import get_current_app_user
from api.services.workout_superset_service import WorkoutSupersetService
from api.services.models import AppUser, WorkoutSessionExercise, WorkoutSession

router = APIRouter(prefix="/workout-supersets", tags=["workout-supersets"])


@router.get("/{superset_group}", response_model=SupersetFlowResponse)
async def get_superset_flow(
    superset_group: str,
    session: AsyncSession = Depends(get_db),
    app_user: AppUser = Depends(get_current_app_user),
):
    data = await WorkoutSupersetService.get_superset_flow(
        session=session,
        app_user_id=app_user.id,
        superset_group=superset_group,
    )
    return SupersetFlowResponse(**data)


@router.post("/{superset_group}/members")
async def add_existing_exercise_to_superset(
    superset_group: str,
    payload: AddExerciseToSupersetRequest,
    session: AsyncSession = Depends(get_db),
    app_user: AppUser = Depends(get_current_app_user),
):
    stmt = (
        select(WorkoutSessionExercise)
        .join(
            WorkoutSession,
            WorkoutSession.id == WorkoutSessionExercise.workout_session_id,
        )
        .where(
            WorkoutSessionExercise.id == payload.session_exercise_id,
            WorkoutSession.app_user_id == app_user.id,
        )
    )

    result = await session.execute(stmt)
    target_exercise = result.scalar_one_or_none()

    if target_exercise is None:
        raise HTTPException(status_code=404, detail="Упражнение не найдено")

    await ensure_exercise_not_duplicated_in_superset(
        session=session,
        superset_group=superset_group,
        exercise_id=target_exercise.exercise_id,
        exclude_session_exercise_id=target_exercise.id,
    )

    await WorkoutSupersetService.add_existing_exercise_to_superset(
        session=session,
        app_user_id=app_user.id,
        superset_group=superset_group,
        session_exercise_id=payload.session_exercise_id,
    )
    return {"ok": True}


@router.post("/{superset_group}/add-exercise")
async def add_new_exercise_to_superset(
    superset_group: str,
    payload: AddNewExerciseToSupersetRequest,
    session: AsyncSession = Depends(get_db),
    app_user: AppUser = Depends(get_current_app_user),
):
    await ensure_exercise_not_duplicated_in_superset(
        session=session,
        superset_group=superset_group,
        exercise_id=payload.exercise_id,
    )

    item = await WorkoutSupersetService.add_new_exercise_to_superset(
        session=session,
        app_user_id=app_user.id,
        superset_group=superset_group,
        exercise_id=payload.exercise_id,
    )
    return {"session_exercise_id": item.id}


@router.post("/{superset_group}/remove-member")
async def remove_exercise_from_superset(
    superset_group: str,
    payload: RemoveExerciseFromSupersetRequest,
    session: AsyncSession = Depends(get_db),
    app_user: AppUser = Depends(get_current_app_user),
):
    await WorkoutSupersetService.remove_exercise_from_superset(
        session=session,
        app_user_id=app_user.id,
        superset_group=superset_group,
        session_exercise_id=payload.session_exercise_id,
    )
    return {"ok": True}

@router.delete("/{superset_group}")
async def delete_superset(
    superset_group: str,
    session: AsyncSession = Depends(get_db),
    app_user: AppUser = Depends(get_current_app_user),
):
    await WorkoutSupersetService.delete_superset(
        session=session,
        app_user_id=app_user.id,
        superset_group=superset_group,
    )
    return {"ok": True}

@router.post(
    "/session-exercises/{session_exercise_id}/start",
    response_model=StartSupersetResponse,
)
async def start_superset_endpoint(
    session_exercise_id: int,
    db: AsyncSession = Depends(get_db),
):
    try:
        session_exercise = await WorkoutSupersetService.start_superset(db, session_exercise_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    return StartSupersetResponse(
        superset_group=session_exercise.superset_group,
        session_exercise_id=session_exercise.id,
    )

async def ensure_exercise_not_duplicated_in_superset(
    session: AsyncSession,
    superset_group: str,
    exercise_id: int,
    exclude_session_exercise_id: int | None = None,
) -> None:
    stmt = select(WorkoutSessionExercise).where(
        WorkoutSessionExercise.superset_group == superset_group,
        WorkoutSessionExercise.exercise_id == exercise_id,
    )

    result = await session.execute(stmt)
    existing_items = result.scalars().all()

    if exclude_session_exercise_id is not None:
        existing_items = [
            item for item in existing_items
            if item.id != exclude_session_exercise_id
        ]

    if existing_items:
        raise HTTPException(
            status_code=400,
            detail="Это упражнение уже добавлено в данный суперсет.",
        )