from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from api.schemas.microcycle import MicrocycleCreate
from api.services.app_user_service import get_current_app_user
from api.services.models import AppUserMicrocycle

# Убедись, что схема MicrocycleCreate импортируется корректно
# router = APIRouter(prefix="/microcycles", tags=["Microcycles"])

router = APIRouter(prefix="/microcycles", tags=["Microcycles"])


@router.get("/")
async def get_microcycles(db: AsyncSession = Depends(get_db), current_user=Depends(get_current_app_user)):
    """Получить все микроциклы (сплиты) пользователя."""
    stmt = select(AppUserMicrocycle).where(AppUserMicrocycle.app_user_id == current_user.id)
    result = await db.execute(stmt)
    return result.scalars().all()


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_microcycle(micro_data: MicrocycleCreate, db: AsyncSession = Depends(get_db),
                            current_user=Depends(get_current_app_user)):
    """Создать новый микроцикл (сплит)."""
    new_micro = AppUserMicrocycle(
        app_user_id=current_user.id,
        name=micro_data.name,
        length_days=micro_data.length_days,
        days_mapping=jsonable_encoder(micro_data.days_mapping)
    )
    db.add(new_micro)
    await db.commit()
    await db.refresh(new_micro)
    return new_micro


@router.get("/{micro_id}")
async def get_microcycle(micro_id: int, db: AsyncSession = Depends(get_db), current_user=Depends(get_current_app_user)):
    """Получить детали конкретного микроцикла."""
    stmt = select(AppUserMicrocycle).where(
        AppUserMicrocycle.id == micro_id,
        AppUserMicrocycle.app_user_id == current_user.id
    )
    result = await db.execute(stmt)
    micro = result.scalar_one_or_none()

    if not micro:
        raise HTTPException(status_code=404, detail="Микроцикл не найден")
    return micro


@router.put("/{micro_id}")
async def update_microcycle(micro_id: int, micro_data: MicrocycleCreate, db: AsyncSession = Depends(get_db),
                            current_user=Depends(get_current_app_user)):
    """Редактировать микроцикл. Запрещено, если он сейчас активен."""
    stmt = select(AppUserMicrocycle).where(
        AppUserMicrocycle.id == micro_id,
        AppUserMicrocycle.app_user_id == current_user.id
    )
    result = await db.execute(stmt)
    micro = result.scalar_one_or_none()

    if not micro:
        raise HTTPException(status_code=404, detail="Микроцикл не найден")

    if micro.is_active:
        raise HTTPException(
            status_code=400,
            detail="Нельзя редактировать активный микроцикл. Сначала деактивируйте его или создайте новый."
        )

    micro.name = micro_data.name
    micro.length_days = micro_data.length_days
    micro.days_mapping = jsonable_encoder(micro_data.days_mapping)

    await db.commit()
    await db.refresh(micro)
    return micro


@router.delete("/{micro_id}")
async def delete_microcycle(micro_id: int, db: AsyncSession = Depends(get_db),
                            current_user=Depends(get_current_app_user)):
    """Удалить микроцикл."""
    stmt = select(AppUserMicrocycle).where(
        AppUserMicrocycle.id == micro_id,
        AppUserMicrocycle.app_user_id == current_user.id
    )
    result = await db.execute(stmt)
    micro = result.scalar_one_or_none()

    if not micro:
        raise HTTPException(status_code=404, detail="Микроцикл не найден")

    if micro.is_active:
        raise HTTPException(
            status_code=400,
            detail="Нельзя удалить активный микроцикл."
        )

    await db.delete(micro)
    await db.commit()
    return {"status": "success", "message": "Микроцикл удален"}