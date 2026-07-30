"""Схемы чек-ина готовности (P0-07)."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator

from api.services.readiness import params
from api.services.volume_calculator import MUSCLE_TRANSLATION_MAP

# Мышечные system keys — единственный допустимый subject крепатуры.
MUSCLE_KEYS = frozenset(MUSCLE_TRANSLATION_MAP.values())
# Боль живёт в двух пространствах: мышцы и суставы (спека §6.4).
PAIN_SUBJECTS = MUSCLE_KEYS | params.JOINT_KEYS


class CheckinRequest(BaseModel):
    """Ответы чек-ина. Любое поле может отсутствовать — это не ошибка,
    а честно пропущенный вопрос."""

    client_uuid: str = Field(min_length=1, max_length=64)
    sleep: Optional[int] = Field(default=None, ge=params.SLEEP_MIN, le=params.SLEEP_MAX)
    stress: Optional[int] = Field(default=None, ge=params.STRESS_MIN, le=params.STRESS_MAX)
    soreness: dict[str, int] = Field(default_factory=dict)
    pain: dict[str, int] = Field(default_factory=dict)
    source: str = params.SOURCE_CHECKIN

    @field_validator("soreness")
    @classmethod
    def _check_soreness(cls, value: dict[str, int]) -> dict[str, int]:
        for muscle, level in value.items():
            if muscle not in MUSCLE_KEYS:
                raise ValueError(f"неизвестная мышца: {muscle}")
            if not params.SORENESS_MIN <= level <= params.SORENESS_MAX:
                raise ValueError(f"крепатура вне шкалы: {level}")
        return value

    @field_validator("pain")
    @classmethod
    def _check_pain(cls, value: dict[str, int]) -> dict[str, int]:
        for place, level in value.items():
            if place not in PAIN_SUBJECTS:
                raise ValueError(f"неизвестное место боли: {place}")
            if not params.PAIN_MIN <= level <= params.PAIN_MAX:
                raise ValueError(f"боль вне шкалы: {level}")
        return value

    @field_validator("source")
    @classmethod
    def _check_source(cls, value: str) -> str:
        if value not in (params.SOURCE_CHECKIN, params.SOURCE_POST_SESSION):
            raise ValueError(f"неизвестный источник: {value}")
        return value


class MuscleFlagOut(BaseModel):
    muscle: str
    level: str
    reason_code: str


class VerdictOut(BaseModel):
    level: str
    reason_code: str
    reason_text: str
    muscle_flags: list[MuscleFlagOut] = []
    pain_places: list[str] = []
    completeness: str


class CheckinResponse(BaseModel):
    """verdict=None означает "ответов не было" — движок работает как P0-06."""

    verdict: Optional[VerdictOut] = None


class CheckinContextResponse(BaseModel):
    """Всё, что нужно нарисовать экран чек-ина."""

    checkin_enabled: bool
    # Мышцы дня: до CHECKIN_MAX_MUSCLE_CHIPS system keys.
    muscles: list[str] = []
    # Место -> сила. Показывается предвыбранным с вопросом "всё ещё болит?".
    active_pain: dict[str, int] = {}
    # Предзаполнение при двух тренировках в день (CHECKIN_REASK_HOURS).
    recent_sleep: Optional[int] = None
    recent_stress: Optional[int] = None
