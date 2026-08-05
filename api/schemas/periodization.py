"""Схемы HTTP-контракта периодизации (P0-08, Задача 11)."""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

from pydantic import BaseModel


class BlockCoordinateRead(BaseModel):
    block_id: int
    block_index: int
    phase_number: int
    phase_name: str
    effort_tier: str
    phase_ordinal: int
    phases_total: int
    day_in_block: int
    days_to_deload: Optional[int]
    workouts_to_deload: Optional[int]
    is_complete: bool
    start_date: date
    planned_end_date: date


class ProposalRead(BaseModel):
    id: int
    block_id: int
    kind: str
    reason_code: str
    reason_text: str
    payload: dict[str, Any]
    status: str


class PeriodizationContextRead(BaseModel):
    block: Optional[BlockCoordinateRead]
    proposals: list[ProposalRead]


class DecisionRequest(BaseModel):
    action: str
    client_uuid: Optional[str] = None
    options: dict[str, Any] = {}


class BlockExerciseSummary(BaseModel):
    exercise_id: int
    name: Optional[str] = None
    entry_e1rm: Optional[float] = None
    exit_e1rm: Optional[float] = None
    delta_pct: Optional[float] = None
    stalled: bool = False


class BlockSummaryRead(BaseModel):
    block_id: int
    block_index: int
    start_date: date
    planned_end_date: date
    actual_end_date: Optional[date]
    status: str
    close_reason: Optional[str]
    had_early_deload: bool
    exercises: list[BlockExerciseSummary]
