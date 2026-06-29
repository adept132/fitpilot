from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_firebase_claims, get_db
from api.schemas.app_user import AppUserResponse
from api.services.app_user_service import get_or_create_app_user

router = APIRouter(prefix="/auth", tags=["auth"])

@router.get("/me", response_model=AppUserResponse)
async def get_me(
    firebase_claims: dict = Depends(get_current_firebase_claims),
    db: AsyncSession = Depends(get_db),
):
    app_user = await get_or_create_app_user(db=db, firebase_claims=firebase_claims)
    return app_user