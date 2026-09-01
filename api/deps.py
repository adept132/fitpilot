from fastapi import Depends, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from api.core.firebase_admin import verify_firebase_token
from api.errors import LocalizedHTTPException

from collections.abc import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import SessionLocal  # поправишь под свой путь

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session

firebase_bearer_scheme = HTTPBearer()

async def get_current_firebase_claims(
    credentials: HTTPAuthorizationCredentials = Depends(firebase_bearer_scheme),
) -> dict:
    token = credentials.credentials

    try:
        decoded_token = verify_firebase_token(token)
        return decoded_token
    except Exception:
        raise LocalizedHTTPException(status.HTTP_401_UNAUTHORIZED, "auth.invalid_token")

