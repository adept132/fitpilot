from fastapi import APIRouter, Depends, status, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, selectinload
from sqlalchemy import select
from api.deps import get_db
from api.schemas.plan import WorkoutPlanCreate, PlanApplyRequest
from api.services.app_user_service import get_current_app_user
from api.services.models import Mesocycle, MesocyclePhase, WorkoutPlan, AppUserProfile, WorkoutPlanExercise, \
    WorkoutSession, WorkoutSessionExercise, WorkoutSessionSet
from api.services.validator import AntiSuicideValidator, PlanExerciseInput

router = APIRouter(prefix="/plans", tags=["Plans"])

@router.get("/")
def get_plans(db: Session = Depends(get_db), current_user=Depends(get_current_app_user)):
    """Получить список всех сохраненных планов пользователя."""
    return db.query(WorkoutPlan).filter(WorkoutPlan.app_user_id == current_user.id).all()


@router.get("/{plan_id}")
async def get_plan(plan_id: int, db: AsyncSession = Depends(get_db), current_user=Depends(get_current_app_user)):
    """Получить полную структуру плана вместе с упражнениями и их названиями."""
    stmt = (
        select(WorkoutPlan)
        .where(
            WorkoutPlan.id == plan_id,
            WorkoutPlan.app_user_id == current_user.id
        )
        .options(
            # Магия: загружаем не только связь с таблицей workout_plan_exercises,
            # но и проваливаемся глубже — в саму таблицу exercises
            selectinload(WorkoutPlan.exercises).selectinload(WorkoutPlanExercise.exercise)
        )
    )
    result = await db.execute(stmt)
    plan = result.scalar_one_or_none()

    if not plan:
        raise HTTPException(status_code=404, detail="План не найден")

    return plan

@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_workout_plan(  # <--- СДЕЛАЛИ ASYNC
        plan_data: WorkoutPlanCreate,
        db: AsyncSession = Depends(get_db),  # <--- ИЗМЕНИЛИ ТИП НА ASYNCSESSION
        current_user=Depends(get_current_app_user)
):
    """Полное сохранение плана с валидацией."""

    # 1. Новый асинхронный синтаксис вместо db.query()
    stmt = select(AppUserProfile).where(AppUserProfile.app_user_id == current_user.id)
    result = await db.execute(stmt)
    profile = result.scalar_one_or_none()

    experience_level = profile.experience_level if profile else "beginner"

    exercises_input = [
        PlanExerciseInput(
            exercise_id=ex.exercise_id, fatigue_tier=ex.fatigue_tier,
            primary_muscle=ex.primary_muscle, secondary_muscle=ex.secondary_muscle,
            target_sets=ex.target_sets, superset_group_id=ex.superset_group_id
        ) for ex in plan_data.exercises
    ]

    AntiSuicideValidator.validate_workout_plan(experience_level, exercises_input)

    new_plan = WorkoutPlan(
        app_user_id=current_user.id,
        name=plan_data.name,
        day_tag=plan_data.day_tag.lower(),  # Возвращаем как было, но оставляем .lower() для надежности
        micro_tag=plan_data.micro_tag.lower(),
        meso_tag=plan_data.meso_tag.lower()
    )
    db.add(new_plan)

    await db.flush()

    for ex in plan_data.exercises:
        new_ex = WorkoutPlanExercise(
            plan_id=new_plan.id, exercise_id=ex.exercise_id,
            order_index=ex.order_index, superset_group_id=ex.superset_group_id,
            target_sets=ex.target_sets
        )
        db.add(new_ex)

    await db.commit()
    return {"status": "success", "plan_id": new_plan.id}


@router.put("/{plan_id}")
def update_workout_plan(plan_id: int, plan_data: WorkoutPlanCreate, db: Session = Depends(get_db),
                        current_user=Depends(get_current_app_user)):
    """Редактирование плана: проверяем валидатором, сносим старые упражнения, пишем новые."""
    plan = db.query(WorkoutPlan).filter(WorkoutPlan.id == plan_id, WorkoutPlan.app_user_id == current_user.id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="План не найден")

    # Валидация
    profile = db.query(AppUserProfile).filter(AppUserProfile.app_user_id == current_user.id).first()
    experience_level = profile.experience_level if profile else "beginner"

    exercises_input = [
        PlanExerciseInput(
            exercise_id=ex.exercise_id, fatigue_tier=ex.fatigue_tier,
            primary_muscle=ex.primary_muscle, secondary_muscle=ex.secondary_muscle,
            target_sets=ex.target_sets, superset_group_id=ex.superset_group_id
        ) for ex in plan_data.exercises
    ]
    AntiSuicideValidator.validate_workout_plan(experience_level, exercises_input)

    # Обновляем метаданные
    plan.name = plan_data.name
    plan.day_tag = plan_data.day_tag
    plan.micro_tag = plan_data.micro_tag
    plan.meso_tag = plan_data.meso_tag

    # Очищаем старые упражнения
    db.query(WorkoutPlanExercise).filter(WorkoutPlanExercise.plan_id == plan.id).delete()

    # Пишем новые
    for ex in plan_data.exercises:
        new_ex = WorkoutPlanExercise(
            plan_id=plan.id, exercise_id=ex.exercise_id,
            order_index=ex.order_index, superset_group_id=ex.superset_group_id,
            target_sets=ex.target_sets
        )
        db.add(new_ex)

    db.commit()
    return {"status": "success", "message": "План обновлен"}


@router.delete("/{plan_id}")
def delete_plan(plan_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_app_user)):
    """Удалить план."""
    plan = db.query(WorkoutPlan).filter(WorkoutPlan.id == plan_id, WorkoutPlan.app_user_id == current_user.id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="План не найден")

    db.delete(plan)
    db.commit()
    return {"status": "success", "message": "План удален"}


@router.post("/{plan_id}/apply")
async def apply_plan_to_calendar(
        plan_id: int,
        payload: PlanApplyRequest,
        db: AsyncSession = Depends(get_db),
        current_user=Depends(get_current_app_user)
):
    """Применить план к календарю (создать тренировочную сессию)."""

    # 1. Достаем план вместе с его упражнениями
    stmt = (
        select(WorkoutPlan)
        .where(
            WorkoutPlan.id == plan_id,
            WorkoutPlan.app_user_id == current_user.id
        )
        .options(selectinload(WorkoutPlan.exercises))
    )
    result = await db.execute(stmt)
    plan = result.scalar_one_or_none()

    if not plan:
        raise HTTPException(status_code=404, detail="План не найден")

    # 2. Создаем новую тренировочную сессию
    new_session = WorkoutSession(
        app_user_id=current_user.id,
        source="plan",
        status="active",
        plan_id=plan.id,
        # notes=f"Применено из плана: {plan.name} (Режим: {payload.apply_mode})"
    )
    db.add(new_session)
    await db.flush()  # Получаем ID сессии

    # 3. Переносим упражнения из плана в сессию
    for plan_ex in plan.exercises:
        session_ex = WorkoutSessionExercise(
            workout_session_id=new_session.id,
            exercise_id=plan_ex.exercise_id,
            order_index=plan_ex.order_index,
            # Преобразуем UUID в строку, если он есть
            superset_group=str(plan_ex.superset_group_id) if plan_ex.superset_group_id else None
        )
        db.add(session_ex)
        await db.flush()  # Получаем ID упражнения в сессии

        # 4. Создаем пустые подходы в соответствии с target_sets
        for set_num in range(1, plan_ex.target_sets + 1):
            new_set = WorkoutSessionSet(
                workout_session_exercise_id=session_ex.id,
                set_number=set_num,
                set_type="normal",
                is_completed=False  # Подходы изначально не выполнены
            )
            db.add(new_set)

    await db.commit()

    return {
        "status": "success",
        "message": "План успешно применен",
        "session_id": new_session.id
    }