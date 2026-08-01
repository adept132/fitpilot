"""Схемы композиции тела."""

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class BodyEntryRequest(BaseModel):
    weight: Optional[float] = Field(default=None, gt=0, le=500)
    height: Optional[float] = Field(default=None, gt=0, le=300)
    body_fat: Optional[float] = Field(default=None, ge=1, le=70)
    # Обхваты (см) по ключам: waist/chest/hips/arm/forearm/thigh/calf/neck/shoulders.
    measurements: Optional[Dict[str, float]] = None
    # Идемпотентный ключ offline-записи (дедуп повторной отправки).
    client_uuid: Optional[str] = None


class BodyMetricPoint(BaseModel):
    date: Optional[str] = None
    value: float


class BodyOverviewResponse(BaseModel):
    latest_weight: Optional[float] = None
    latest_height: Optional[float] = None
    latest_body_fat: Optional[float] = None
    bmi: Optional[float] = None  # информативно, не как цель
    measurements: Dict[str, float] = {}
    history: Dict[str, List[BodyMetricPoint]] = {}


class BodyEntry(BaseModel):
    date: str
    weight: Optional[float] = None
    body_fat: Optional[float] = None
    measurements: Dict[str, float] = {}
