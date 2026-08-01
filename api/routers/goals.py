"""CRUD целей пользователя + вычисление статуса (прогресс/ETA/реалистичность)."""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.deps import get_db
from api.schemas.goals import GoalCreate, GoalResponse, GoalStatus, GoalUpdate
from api.services.app_user_service import get_current_app_user
from api.services.goal_service import (
    GOAL_MEASUREMENT,
    GOAL_STRENGTH,
    compute_goal_status,
)
from api.services.models import AppUser, AppUserProfile, Exercise, UserGoal

router = APIRouter(prefix="/goals", tags=["goals"])


async def _profile(db: AsyncSession, app_user_id: int) -> Optional[AppUserProfile]:
    return (await db.execute(
        select(AppUserProfile).where(AppUserProfile.app_user_id == app_user_id)
    )).scalar_one_or_none()


async def _status_for(db: AsyncSession, goal: UserGoal, profile) -> GoalStatus:
    data = await compute_goal_status(
        db, goal,
        profile.experience_level if profile else None,
        profile.settings if profile else None,
    )
    return GoalStatus(**data)


def _to_response(goal: UserGoal, exercise_name: Optional[str], status_obj: GoalStatus) -> GoalResponse:
    return GoalResponse(
        id=goal.id,
        goal_type=goal.goal_type,
        target_value=float(goal.target_value),
        unit=goal.unit,
        exercise_id=goal.exercise_id,
        exercise_name=exercise_name,
        target_reps=goal.target_reps,
        metric_key=goal.metric_key,
        deadline=goal.deadline.isoformat() if goal.deadline else None,
        is_completed=goal.is_completed,
        status=status_obj,
    )


@router.post("", response_model=GoalResponse, status_code=status.HTTP_201_CREATED)
async def create_goal(
    payload: GoalCreate,
    db: AsyncSession = Depends(get_db),
    current_user: AppUser = Depends(get_current_app_user),
):
    if payload.goal_type == GOAL_STRENGTH and not payload.exercise_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Силовая цель требует exercise_id")
    if payload.goal_type == GOAL_MEASUREMENT and not payload.metric_key:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Цель-замер требует metric_key")

    # Идемпотентность offline-повтора: цель с этим client_uuid уже создана.
    if payload.client_uuid:
        existing = (await db.execute(
            select(UserGoal).where(
                UserGoal.app_user_id == current_user.id,
                UserGoal.client_uuid == payload.client_uuid,
            )
        )).scalar_one_or_none()
        if existing:
            return existing

    goal = UserGoal(
        app_user_id=current_user.id,
        client_uuid=payload.client_uuid,
        goal_type=payload.goal_type,
        target_value=payload.target_value,
        unit=payload.unit,
        exercise_id=payload.exercise_id,
        target_reps=payload.target_reps,
        metric_key=payload.metric_key,
        deadline=payload.deadline,
    )
    db.add(goal)
    await db.commit()
    await db.refresh(goal)

    profile = await _profile(db, current_user.id)
    exercise_name = await _exercise_name(db, goal.exercise_id)
    return _to_response(goal, exercise_name, await _status_for(db, goal, profile))


@router.get("", response_model=List[GoalResponse])
async def list_goals(
    include_completed: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: AppUser = Depends(get_current_app_user),
):
    stmt = (
        select(UserGoal)
        .options(selectinload(UserGoal.exercise))
        .where(UserGoal.app_user_id == current_user.id)
        .order_by(UserGoal.created_at.desc())
    )
    if not include_completed:
        stmt = stmt.where(UserGoal.is_completed == False)  # noqa: E712

    goals = list((await db.execute(stmt)).scalars().all())
    profile = await _profile(db, current_user.id)

    out: List[GoalResponse] = []
    for goal in goals:
        name = goal.exercise.name if goal.exercise else None
        out.append(_to_response(goal, name, await _status_for(db, goal, profile)))
    return out


@router.patch("/{goal_id}", response_model=GoalResponse)
async def update_goal(
    goal_id: int,
    payload: GoalUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: AppUser = Depends(get_current_app_user),
):
    goal = await _owned_goal(db, goal_id, current_user.id)

    if payload.target_value is not None:
        goal.target_value = payload.target_value
    if payload.target_reps is not None:
        goal.target_reps = payload.target_reps
    if payload.deadline is not None:
        goal.deadline = payload.deadline
    if payload.is_completed is not None:
        goal.is_completed = payload.is_completed

    await db.commit()
    await db.refresh(goal)

    profile = await _profile(db, current_user.id)
    name = await _exercise_name(db, goal.exercise_id)
    return _to_response(goal, name, await _status_for(db, goal, profile))


@router.delete("/{goal_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_goal(
    goal_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: AppUser = Depends(get_current_app_user),
):
    goal = await _owned_goal(db, goal_id, current_user.id)
    await db.delete(goal)
    await db.commit()


async def _owned_goal(db: AsyncSession, goal_id: int, app_user_id: int) -> UserGoal:
    goal = (await db.execute(
        select(UserGoal).where(
            UserGoal.id == goal_id, UserGoal.app_user_id == app_user_id
        )
    )).scalar_one_or_none()
    if goal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Цель не найдена")
    return goal


async def _exercise_name(db: AsyncSession, exercise_id: Optional[int]) -> Optional[str]:
    if exercise_id is None:
        return None
    return (await db.execute(
        select(Exercise.name).where(Exercise.id == exercise_id)
    )).scalar_one_or_none()
