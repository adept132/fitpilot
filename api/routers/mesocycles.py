from fastapi import APIRouter, Depends, status, HTTPException
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from uuid import UUID
from api.deps import get_db
from api.routers.workout_center import build_context
from api.schemas.mesocycle import MesocycleCreate, UpdateSelectedMesocyclePayload, UpdateMesocyclePhasePayload
from api.services.app_user_service import get_current_app_user
from api.services.models import Mesocycle, MesocyclePhase, AppUser, AppUserMesocycle
from api.services.validator import AntiSuicideValidator

router = APIRouter(prefix="/mesocycles", tags=["Mesocycles"])


@router.get("/")
async def get_mesocycles(db: AsyncSession = Depends(get_db)):
    """Получить список всех доступных шаблонов мезоциклов."""
    result = await db.execute(select(Mesocycle))
    return result.scalars().all()


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_mesocycle(
    meso_data: MesocycleCreate,
    db: AsyncSession = Depends(get_db),
    current_user: AppUser = Depends(get_current_app_user) # <--- Достаем юзера
):
    effort_tiers = [phase.effort_tier for phase in sorted(meso_data.phases, key=lambda p: p.phase_number)]
    AntiSuicideValidator.validate_mesocycle_sequence(effort_tiers)

    new_meso = Mesocycle(
        author_id=current_user.id, # <--- ПРИВЯЗЫВАЕМ К СОЗДАТЕЛЮ
        name=meso_data.name,
        code=meso_data.code,
        description=meso_data.description,
        phases_in_cycle=meso_data.phases_in_cycle
    )
    db.add(new_meso)
    await db.flush()

    for phase in meso_data.phases:
        new_phase = MesocyclePhase(
            mesocycle_id=new_meso.id,
            phase_number=phase.phase_number,
            name=phase.name,
            effort_tier=phase.effort_tier
        )
        db.add(new_phase)

    await db.commit()
    # Pydantic/FastAPI не умеют напрямую сериализовать UUID в JSON, поэтому конвертируем в строку
    return {"status": "success", "mesocycle_id": str(new_meso.id)}


@router.get("/{mesocycle_id}")
async def get_mesocycle(mesocycle_id: UUID, db: AsyncSession = Depends(get_db),
                        current_user=Depends(get_current_app_user)):
    """Получить детальную информацию о конкретном мезоцикле."""
    stmt = (
        select(Mesocycle)
        .where(Mesocycle.id == mesocycle_id, Mesocycle.author_id == current_user.id)
        .options(selectinload(Mesocycle.phases))
    )
    result = await db.execute(stmt)
    meso = result.scalar_one_or_none()

    if not meso:
        raise HTTPException(status_code=404, detail="Стратегия не найдена")
    return meso


@router.delete("/{mesocycle_id}")
async def delete_mesocycle(mesocycle_id: UUID, db: AsyncSession = Depends(get_db),
                           current_user=Depends(get_current_app_user)):
    """Удалить мезоцикл (каскадно удалятся все его фазы)."""
    stmt = select(Mesocycle).where(Mesocycle.id == mesocycle_id, Mesocycle.author_id == current_user.id)
    result = await db.execute(stmt)
    meso = result.scalar_one_or_none()

    if not meso:
        raise HTTPException(status_code=404, detail="Стратегия не найдена")

    await db.delete(meso)
    await db.commit()
    return {"status": "success", "message": "Стратегия удалена"}