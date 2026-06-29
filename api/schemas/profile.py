from pydantic import BaseModel, Field


class ProfileResponse(BaseModel):
    id: int
    email: str
    display_name: str | None = None
    email_verified: bool
    is_active: bool


class UpdateProfileRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=120)