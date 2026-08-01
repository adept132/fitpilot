from pydantic import BaseModel, Field
from typing import List, Optional
from api.schemas.plan import GeneratedDayOut


class CommandOut(BaseModel):
    type: str
    params: dict = {}
    seed: int = 0
    confidence: float = 1.0
    summary: str = ""


class ClarifyOut(BaseModel):
    question: str
    options: List[str] = []


class ApplyCommandsRequest(BaseModel):
    base_draft: GeneratedDayOut
    command_log: List[CommandOut] = Field(default_factory=list)
    new_comment: Optional[str] = None
    context: dict = Field(default_factory=dict)


class ApplyCommandsResponse(BaseModel):
    final_draft: GeneratedDayOut
    parsed_commands: List[CommandOut]
    reply: str
    clarify: Optional[ClarifyOut] = None
