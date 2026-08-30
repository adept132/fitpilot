from datetime import date

from pydantic import BaseModel, Field
from typing import List, Dict, Optional, Literal
from uuid import UUID


# Описание упражнения внутри плана
class PlanExerciseCreate(BaseModel):
    exercise_id: int = Field(..., description="ID упражнения из глобальной базы")
    order_index: int = Field(..., description="Порядковый номер в тренировке")
    target_sets: int = Field(default=1, ge=1, description="Количество подходов")

    # Если заполнено одним и тем же UUID для нескольких упражнений - они объединятся в суперсет
    superset_group_id: Optional[UUID] = None

    # --- ДАННЫЕ ДЛЯ ВАЛИДАТОРА ---
    # Важный нюанс: в реальном production-коде бэкенд не должен доверять фронтенду эти поля.
    # Бэкенд должен сам сходить в БД (в таблицу exercises), взять exercise_id и достать оттуда
    # fatigue_tier и primary_muscle, чтобы пользователь не смог "хакнуть" валидатор, подменив данные.
    # Но для удобства архитектуры (или если фронтенд собирает объект целиком) мы кладем их сюда:
    fatigue_tier: int = Field(..., ge=1, le=3, description="1 - База, 2 - Тренажеры, 3 - Изоляция")
    primary_muscle: str = Field(..., example="chest")
    secondary_muscle: Optional[str] = Field(None, example="triceps")
    override_reps: Optional[str] = Field(default=None, pattern=r"^\d{1,2}(?:-\d{1,2})?$")
    override_rir: Optional[int] = Field(default=None, ge=0, le=10)


# Главная схема создания плана
class WorkoutPlanCreate(BaseModel):
    name: str = Field(..., max_length=255, example="Push - Adaptive - Hard")

    # Теги для каскадной фильтрации
    day_tag: str = Field(..., example="push")
    micro_tag: str = Field(..., description="'easy', 'medium', 'hard', 'adaptive'")
    meso_tag: str = Field(..., description="'deload', 'easy', 'medium', 'prefailure', 'failure', 'adaptive'")

    # Массив упражнений, который валидатор будет проверять на перекрытие мышц в суперсетах и хард-капы
    exercises: List[PlanExerciseCreate]

class MuscleTarget(BaseModel):
    target_sets: int
    max_session_cap: int

class VolumeTargetsResponse(BaseModel):
    day_tag: str
    split_duration: int
    targets: Dict[str, MuscleTarget]
    experience_level: Literal["beginner", "intermediate", "advanced"] = "beginner"

class PlanApplyRequest(BaseModel):
    apply_mode: str
    target_date: date
    day_tag: str
    micro_tag: str

class UpdatePlanContextPayload(BaseModel):
    plan_id: Optional[int] = None

class WorkoutCenterPlanRead(BaseModel):
    id: int
    name: str


class GenerateConfig(BaseModel):
    use_supersets: bool = False
    max_superset_size: Literal[2, 3] = 2
    duration_minutes: Optional[int] = Field(default=None, ge=30, le=120)
    accent_muscle: Optional[str] = None
    accent_muscles: List[str] = Field(default_factory=list, max_length=2)
    repeated_days_mode: Literal["shared", "separate"] = "shared"
    seed: Optional[int] = None
    timer_mode: Literal["smart", "fixed"] = "smart"
    fixed_rest_seconds: int = Field(default=120, ge=30, le=600)
    day_effort: Literal["deload", "easy", "medium", "hard", "prefailure", "failure"] = "medium"


class GeneratePlanRequest(BaseModel):
    blueprint_id: Optional[UUID] = None
    # When set, generate only the split day whose DayBlueprint.name matches
    # (single-day generation for the "choose plan" flow). None = whole week.
    day_name: Optional[str] = None
    target_date: Optional[date] = None
    config: GenerateConfig = Field(default_factory=GenerateConfig)


class GeneratedExerciseOut(BaseModel):
    exercise_id: int
    name: str
    localized_names: Dict[str, str] = Field(default_factory=dict)
    target_sets: int
    order_index: int
    superset_group_id: Optional[str] = None
    fatigue_tier: int
    primary_muscle: str
    secondary_muscle: Optional[str] = None
    override_reps: Optional[str] = None
    override_rir: Optional[int] = Field(default=None, ge=0, le=10)
    preference: Optional[Literal["favorite", "disliked"]] = None


class GeneratedDayOut(BaseModel):
    day_tag: str
    day_name: str
    schedule_tags: List[str] = Field(default_factory=list)
    exercises: List[GeneratedExerciseOut]
    coverage: Dict[str, Dict[str, int]]
    warnings: List[str] = Field(default_factory=list)
    estimated_duration_seconds: int = 0
    duration_limit_met: bool = True


class GenerationInputSummary(BaseModel):
    blueprint_id: UUID
    split_name: str
    mode: Literal["full", "single_day"]
    requested_day_name: Optional[str] = None
    experience_level: str
    training_frequency: int
    microcycle_length: int
    weekly_targets: Dict[str, int] = Field(default_factory=dict)
    focus_muscles: List[str] = Field(default_factory=list)
    goal_source: Literal["volume_budget"] = "volume_budget"
    volume_distribution: Optional[str] = None
    equipment_locations: List[str] = Field(default_factory=list)
    equipment_unrestricted: bool = False
    allowed_equipment: List[str] = Field(default_factory=list)
    prehab_flags: List[str] = Field(default_factory=list)
    duration_minutes: Optional[int] = None
    accent_muscle: Optional[str] = None
    accent_muscles: List[str] = Field(default_factory=list)
    repeated_days_mode: Literal["shared", "separate"] = "shared"
    use_supersets: bool = False
    max_superset_size: Literal[2, 3] = 2
    timer_mode: Literal["smart", "fixed"] = "smart"
    fixed_rest_seconds: int = 120
    day_effort: str = "medium"


class GenerationIssueAction(BaseModel):
    type: Literal[
        "edit_day", "change_duration", "change_equipment", "change_accent",
        "edit_volume", "edit_split", "review_limitations", "review_preferences",
    ]
    label: str
    label_key: str = ""
    params: Dict[str, str | int | bool] = Field(default_factory=dict)


class GenerationIssue(BaseModel):
    code: str
    severity: Literal["info", "warning", "blocking"]
    day_tag: Optional[str] = None
    muscle: Optional[str] = None
    title: str
    reason: str
    title_key: str = ""
    body_key: str = ""
    params: Dict[str, str | int | bool] = Field(default_factory=dict)
    action: GenerationIssueAction


class ComparedExercise(BaseModel):
    exercise_id: int
    name: str


class PreviousPlanSummary(BaseModel):
    plan_id: int
    name: str
    source: Literal["calendar", "split", "calendar_and_split"]


class GeneratedDayComparison(BaseModel):
    day_tag: str
    day_name: str
    status: Literal["new", "unchanged", "changed"]
    previous_plans: List[PreviousPlanSummary] = Field(default_factory=list)
    affected_calendar_days: int = 0
    affected_dates: List[date] = Field(default_factory=list)
    previous_exercise_count: int = 0
    generated_exercise_count: int = 0
    previous_sets: int = 0
    generated_sets: int = 0
    added_exercises: List[ComparedExercise] = Field(default_factory=list)
    removed_exercises: List[ComparedExercise] = Field(default_factory=list)
    modified_exercises: List[ComparedExercise] = Field(default_factory=list)


class GenerationComparison(BaseModel):
    applied_from: date
    mode: Literal["full", "single_day"]
    days: List[GeneratedDayComparison] = Field(default_factory=list)
    untouched_day_tags: List[str] = Field(default_factory=list)


class GeneratePlanResponse(BaseModel):
    days: List[GeneratedDayOut]
    inputs: GenerationInputSummary
    comparison: GenerationComparison
    issues: List[GenerationIssue] = Field(default_factory=list)


class GeneratePlanPreviewRequest(BaseModel):
    days: List[GeneratedDayOut]
    blueprint_id: Optional[UUID] = None
    day_name: Optional[str] = None
    target_date: Optional[date] = None
    config: GenerateConfig = Field(default_factory=GenerateConfig)


class GeneratePlanPreviewResponse(BaseModel):
    days: List[GeneratedDayOut] = Field(default_factory=list)
    inputs: GenerationInputSummary
    comparison: GenerationComparison
    issues: List[GenerationIssue] = Field(default_factory=list)


class ConfirmPlanRequest(BaseModel):
    days: List[GeneratedDayOut]
    mode: Literal["full", "single_day"] = "full"
    target_date: Optional[date] = None


class ConfirmPlanResponse(BaseModel):
    status: str
    created_plan_ids: List[int]
    applied_from: date
    updated_day_tags: List[str] = Field(default_factory=list)


class GeneratorPresetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    settings: Dict[str, object] = Field(default_factory=dict)
    is_default: bool = False


class GeneratorPresetUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    settings: Optional[Dict[str, object]] = None
    is_default: Optional[bool] = None


class GeneratorPresetOut(BaseModel):
    id: int
    name: str
    settings: Dict[str, object]
    is_default: bool

    class Config:
        from_attributes = True
