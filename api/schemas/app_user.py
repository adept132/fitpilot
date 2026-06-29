from datetime import datetime
from pydantic import BaseModel, ConfigDict


class AppUserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    firebase_uid: str
    email: str
    display_name: str | None
    is_active: bool
    email_verified: bool
    created_at: datetime
    updated_at: datetime
    last_seen_at: datetime | None