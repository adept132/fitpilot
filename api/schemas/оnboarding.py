from pydantic import BaseModel, Field, root_validator
from typing import Any, List, Literal, Dict, Optional


class OnboardingWidgetRequest(BaseModel):
    gender: Optional[str] = None
    experience_level: Optional[str] = None
    training_frequency: Optional[int] = None
    microcycle_length: Optional[int] = None
    focus_muscles: Optional[list[str]] = None

    @root_validator(pre=False, skip_on_failure=True)
    def check_frequency_bounds(cls, values):
        freq = values.get('training_frequency')
        cycle = values.get('microcycle_length')

        # Если переданы оба параметра, проверяем их логическое соотношение
        if freq is not None and cycle is not None:
            if freq > cycle:
                raise ValueError("Частота тренировок не может превышать длину микроцикла")
            if cycle < 2 or cycle > 14:
                raise ValueError("Длина микроцикла должна быть от 2 до 14 дней")
        return values


# --- Вспомогательные схемы для структуры JSONB (volume_budget) ---

class MuscleTarget(BaseModel):
    target_sets: int
    min_floor: int
    is_focus: bool


class BudgetConstraints(BaseModel):
    systemic_cap_per_week: int
    max_sets_per_session_per_muscle: int


class BudgetMeta(BaseModel):
    focus_muscles: List[str]
    distribution_type: str
    total_weekly_sets: int


class VolumeBudget(BaseModel):
    version: str = "1.0"
    meta: BudgetMeta
    constraints: BudgetConstraints
    weekly_targets: Dict[str, MuscleTarget]

class UpdateSettingsRequest(BaseModel):
    locations: Optional[List[str]] = None
    prehab_flags: Optional[List[str]] = None
    effort_display_mode: Optional[Literal["hidden", "text", "rir"]] = None
    progression_factor: Optional[float] = Field(default=None, gt=0)
    weight_unit: Optional[Literal["kg", "lbs"]] = None
    weight_steps: Optional[Dict[str, float]] = None
    # Конфиг калькулятора блинов (отдельно на единицу): {barWeights, selectedBar, plates}
    plate_config_kg: Optional[Dict[str, Any]] = None
    plate_config_lbs: Optional[Dict[str, Any]] = None