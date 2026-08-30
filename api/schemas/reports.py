from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

PeriodType = Literal["week", "month", "year"]


class ReportActionRead(BaseModel):
    id: str
    title: str
    reason: str
    title_key: str = ""
    body_key: str = ""
    params: dict[str, str | int | float | bool] = Field(default_factory=dict)
    route: str
    muscle: str | None = None


class ReportHeadlineRead(BaseModel):
    label: str
    value: str


class ReportCardRead(BaseModel):
    period_type: PeriodType
    period_start: date
    period_end: date
    generated_at: datetime
    seen: bool
    headline: list[ReportHeadlineRead]


class ReportListRead(BaseModel):
    items: list[ReportCardRead]
    unseen_count: int


class ReportRead(BaseModel):
    shape_version: int
    rules_version: int
    period_type: PeriodType
    period_start: date
    period_end: date
    generated_at: datetime
    seen_at: datetime | None = None
    metrics: dict[str, Any]
    actions: list[ReportActionRead]
