from typing import Optional as _Optional
from dataclasses import replace
import copy
from datetime import date as _date
from fastapi import APIRouter, Depends, status, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, selectinload
from sqlalchemy import select
from api.deps import get_db
from api.errors import LocalizedHTTPException
from api.i18n import tr
from api.schemas.plan import WorkoutPlanCreate, PlanApplyRequest
from api.services.app_user_service import get_current_app_user
from api.services.models import Mesocycle, MesocyclePhase, WorkoutPlan, AppUserProfile, WorkoutPlanExercise, \
    WorkoutSession, WorkoutSessionExercise, WorkoutSessionSet
from api.services.validator import AntiSuicideValidator, PlanExerciseInput
from api.services.scheduling_engine import SchedulingEngine
from api.services.models import UserSplit, SplitBlueprint, SplitDaySlot, DayBlueprint, Exercise, UserCalendarDay, UserExercisePreference, AppUserMicrocycle, AdvancedGeneratorPreset
from api.services.volume_service import VolumeService
from api.services.plan_generator_service import build_day
from api.services.plan_generation_insights import (
    compare_generated_day, explain_day, missing_requested_day_issue,
    missing_volume_targets_issue,
)
from api.services.muscle_keys import key_for_muscle
from api.services.exercise_selection_engine import SelectionConfig, SelectionPolicy, SelectedExercise
from api.services.exercise_localization import localized_descriptions, localized_names
from api.services.plan_duration import DurationConfig, estimate_duration_seconds, fit_to_duration
from api.services.progression import repository as progression_repo
from api.services.progression.engine import plan_exercise
from api.services.progression.resolve import override_for
from api.schemas.plan import (
    GeneratePlanRequest, GeneratePlanResponse, GeneratedDayOut, GeneratedExerciseOut,
    ConfirmPlanRequest, ConfirmPlanResponse, GenerationInputSummary,
    GenerationComparison, GeneratePlanPreviewRequest, GeneratePlanPreviewResponse,
    GeneratorPresetCreate, GeneratorPresetUpdate, GeneratorPresetOut,
)
from api.schemas.commands import (ApplyCommandsRequest, ApplyCommandsResponse, CommandOut, ClarifyOut,
                                  GeneratorRuleCreate, GeneratorRuleOut, GeneratorRuleUpdate)
import uuid
from api.services.commands.schema import Command, CommandType
from api.services.commands.parser import parse as parse_comment
from api.services.commands.executor import apply as apply_commands_exec

router = APIRouter(prefix="/plans", tags=["Plans"])

LOCATION_EQUIPMENT = {
    "gym": None,
    "free_weights": {
        "barbell", "dumbbell", "kettlebell", "bench", "pullup_bar", "dip_bars",
        "bodyweight", "plate", "box", "exercise_ball", "ab_roller",
    },
    "home": {
        "dumbbell", "kettlebell", "band", "bodyweight", "pullup_bar",
        "ab_roller", "exercise_ball",
    },
}


def _bind_generated_plans_to_split(user_split: UserSplit, plans: list[WorkoutPlan]) -> int:
    """Keep the legacy Workout context in sync with calendar plan binding.

    Workout Center still resolves its selected plan through
    ``UserSplit.selected_plans`` while the schedule resolves it through
    ``UserCalendarDay.plan_id``. Confirmation must update both projections or
    Workout shows "no plan" immediately after a successful generation.
    """
    if not user_split.blueprint:
        return 0

    selected = dict(user_split.selected_plans or {})
    updated = 0
    for slot in user_split.blueprint.slots:
        plan_id = SchedulingEngine._score_and_find_best_plan(
            plans=plans,
            target_day_name=slot.day.name,
            meso_tag="adaptive",
            micro_tag="adaptive",
        )
        key = str(slot.day_order)
        if plan_id is not None and selected.get(key) != plan_id:
            selected[key] = plan_id
            updated += 1
        # Частичная генерация не имеет права стирать планы остальных дней.

    if updated:
        user_split.selected_plans = selected
    return updated


def _allowed_equipment(locations) -> _Optional[set]:
    locs = locations or ["gym"]
    if any(l == "gym" for l in locs):
        return None
    allowed: set = set()
    for l in locs:
        s = LOCATION_EQUIPMENT.get(l)
        if s is None:
            return None
        allowed |= s
    return allowed or None


async def _day_effort_by_tag(db: AsyncSession, app_user_id: int) -> dict[str, str]:
    """Resolve the active microcycle's tactical effort for generated day tags."""
    result = await db.execute(select(AppUserMicrocycle).where(
        AppUserMicrocycle.app_user_id == app_user_id,
        AppUserMicrocycle.is_active.is_(True),
    ))
    microcycle = result.scalars().first()
    efforts: dict[str, str] = {}
    if not microcycle:
        return efforts
    effort_rank = {"deload": 0, "easy": 1, "medium": 2, "hard": 3, "prefailure": 4, "failure": 5}
    for value in (microcycle.days_mapping or {}).values():
        if not isinstance(value, dict):
            continue
        tag = str(value.get("tag") or "").strip().lower()
        effort = str(value.get("type") or "medium").strip().lower()
        if tag and effort != "rest":
            efforts[tag] = effort
            # Repeated-day labels such as "Upper #2" share the generated plan.
            base_tag = tag.split(" #", 1)[0]
            current = efforts.get(base_tag)
            if current is None or effort_rank.get(effort, 2) > effort_rank.get(current, 2):
                # A shared plan must fit its most demanding occurrence. Separate
                # repeated-day plans will use their exact tags once enabled.
                efforts[base_tag] = effort
    return efforts


def _base_day_tag(tag: str) -> str:
    value = str(tag or "").strip()
    head, marker, tail = value.rpartition(" #")
    return head if marker and tail.isdigit() else value


async def _day_occurrences(db: AsyncSession, app_user_id: int) -> dict[str, list[tuple[str, str]]]:
    """Return the actual repeated day labels and effort from the active microcycle."""
    result = await db.execute(select(AppUserMicrocycle).where(
        AppUserMicrocycle.app_user_id == app_user_id,
        AppUserMicrocycle.is_active.is_(True),
    ))
    microcycle = result.scalars().first()
    occurrences: dict[str, list[tuple[str, str]]] = {}
    if not microcycle:
        return occurrences
    for _, value in sorted(
        (microcycle.days_mapping or {}).items(),
        key=lambda item: int(item[0]) if str(item[0]).isdigit() else 10_000,
    ):
        if not isinstance(value, dict):
            continue
        tag = str(value.get("tag") or "").strip()
        effort = str(value.get("type") or "medium").strip().lower()
        if not tag or effort == "rest":
            continue
        occurrences.setdefault(_base_day_tag(tag).lower(), []).append((tag, effort))
    return occurrences


async def _load_generation_context(db, current_user, blueprint_id):
    prof_res = await db.execute(select(AppUserProfile).where(
        AppUserProfile.app_user_id == current_user.id))
    profile = prof_res.scalar_one_or_none()
    if not profile or not profile.volume_budget:
        raise LocalizedHTTPException(400, "plan.onboarding_volume_budget_missing")

    if blueprint_id is None:
        us_res = await db.execute(select(UserSplit).where(
            UserSplit.app_user_id == current_user.id, UserSplit.is_active == True))  # noqa: E712
        active = us_res.scalar_one_or_none()
        if not active:
            raise LocalizedHTTPException(400, "plan.active_split_or_blueprint_required")
        blueprint_id = active.blueprint_id

    bp_res = await db.execute(
        select(SplitBlueprint).where(
            SplitBlueprint.id == blueprint_id,
            (SplitBlueprint.is_system == True)  # noqa: E712
            | (SplitBlueprint.author_id == current_user.id),
        ).options(
            selectinload(SplitBlueprint.slots).selectinload(SplitDaySlot.day)
            .selectinload(DayBlueprint.muscle_targets)))
    blueprint = bp_res.scalar_one_or_none()
    if not blueprint or not blueprint.slots:
        raise LocalizedHTTPException(404, "split.empty_or_not_found")

    pool_res = await db.execute(select(Exercise).where(
        (Exercise.source == "default") | (Exercise.app_user_id == current_user.id)))
    pool = list(pool_res.scalars().all())
    return profile, blueprint, pool


def _generation_input_summary(profile, blueprint, request) -> GenerationInputSummary:
    budget = profile.volume_budget or {}
    weekly = budget.get("weekly_targets") or {}
    weekly_targets = {
        key: int(value.get("target_sets", 0) if isinstance(value, dict) else value)
        for key, value in weekly.items()
    }
    settings = profile.settings or {}
    locations = list(settings.get("locations") or ["gym"])
    allowed = _allowed_equipment(locations)
    config = request.config
    resolved_accents = list(dict.fromkeys(
        config.accent_muscles
        or ([config.accent_muscle] if config.accent_muscle else [])
        or list((budget.get("meta") or {}).get("focus_muscles") or [])
    ))[:2]
    return GenerationInputSummary(
        blueprint_id=blueprint.id,
        split_name=blueprint.name,
        mode="single_day" if request.day_name else "full",
        requested_day_name=request.day_name,
        experience_level=profile.experience_level,
        training_frequency=getattr(profile, "training_frequency", 3),
        microcycle_length=getattr(profile, "microcycle_length", blueprint.length_days),
        weekly_targets=weekly_targets,
        focus_muscles=list((budget.get("meta") or {}).get("focus_muscles") or []),
        volume_distribution=(budget.get("meta") or {}).get("distribution_type"),
        equipment_locations=locations,
        equipment_unrestricted=allowed is None,
        allowed_equipment=sorted(allowed or []),
        prehab_flags=list(settings.get("prehab_flags") or []),
        duration_minutes=config.duration_minutes,
        accent_muscle=config.accent_muscle,
        accent_muscles=resolved_accents,
        repeated_days_mode=config.repeated_days_mode,
        use_supersets=config.use_supersets,
        max_superset_size=config.max_superset_size,
        timer_mode=config.timer_mode,
        fixed_rest_seconds=config.fixed_rest_seconds,
        day_effort=config.day_effort,
    )


async def _generation_comparison(db, current_user, blueprint, days, target_date, single_day=False):
    today = _date.today()
    applied_from = max(target_date or today, today)
    generated_tags = {
        tag.lower()
        for day in days
        for tag in (day.schedule_tags or [day.day_tag])
    }

    split_result = await db.execute(
        select(UserSplit).where(
            UserSplit.app_user_id == current_user.id,
            UserSplit.is_active == True,  # noqa: E712
            UserSplit.blueprint_id == blueprint.id,
        )
    )
    user_split = split_result.scalar_one_or_none()
    split_plan_ids: set[int] = set()
    if user_split:
        slot_by_tag = {
            slot.day.name.lower(): str(slot.day_order)
            for slot in blueprint.slots
        }
        selected = user_split.selected_plans or {}
        split_plan_ids = {
            int(selected[key])
            for tag, key in slot_by_tag.items()
            if tag in generated_tags and selected.get(key) is not None
        }

    calendar_days = list((await db.execute(
        select(UserCalendarDay).where(
            UserCalendarDay.app_user_id == current_user.id,
            UserCalendarDay.target_date >= applied_from,
            UserCalendarDay.is_rest_day == False,  # noqa: E712
            UserCalendarDay.status == "planned",
            UserCalendarDay.actual_workout_session_id.is_(None),
            UserCalendarDay.day_tag.is_not(None),
        )
    )).scalars().all())
    calendar_days.sort(key=lambda day: day.target_date)
    calendar_plan_ids = {
        int(day.plan_id) for day in calendar_days
        if (day.day_tag or "").lower() in generated_tags and day.plan_id is not None
    }
    plan_ids = split_plan_ids | calendar_plan_ids
    plans = []
    if plan_ids:
        plans = list((await db.execute(
            select(WorkoutPlan)
            .where(
                WorkoutPlan.app_user_id == current_user.id,
                WorkoutPlan.id.in_(plan_ids),
            )
            .options(
                selectinload(WorkoutPlan.exercises)
                .selectinload(WorkoutPlanExercise.exercise)
            )
        )).scalars().all())
    missing_plan_ids = plan_ids - {plan.id for plan in plans}
    if missing_plan_ids:
        split_plan_ids -= missing_plan_ids
        calendar_plan_ids -= missing_plan_ids

    plans_by_tag: dict[str, list[WorkoutPlan]] = {}
    for plan in plans:
        plans_by_tag.setdefault(plan.day_tag.lower(), []).append(plan)
    preferred_plan_by_tag = {}
    for calendar_day in calendar_days:
        tag = (calendar_day.day_tag or "").lower()
        if tag in generated_tags and calendar_day.plan_id is not None:
            preferred_plan_by_tag.setdefault(tag, int(calendar_day.plan_id))
    for tag, grouped in plans_by_tag.items():
        preferred_id = preferred_plan_by_tag.get(tag)
        grouped.sort(key=lambda plan: (plan.id != preferred_id, plan.id))
    sources = {}
    for plan_id in plan_ids:
        in_calendar = plan_id in calendar_plan_ids
        in_split = plan_id in split_plan_ids
        sources[plan_id] = (
            "calendar_and_split" if in_calendar and in_split
            else "calendar" if in_calendar else "split"
        )
    dates_by_tag: dict[str, list[_date]] = {}
    for calendar_day in calendar_days:
        tag = (calendar_day.day_tag or "").lower()
        if tag in generated_tags:
            dates_by_tag.setdefault(tag, []).append(calendar_day.target_date)

    comparisons = []
    for day in days:
        tags = [tag.lower() for tag in (day.schedule_tags or [day.day_tag])]
        previous_plans = []
        affected_dates = []
        for tag in tags:
            previous_plans.extend(plans_by_tag.get(tag, []))
            affected_dates.extend(dates_by_tag.get(tag, []))
        unique_plans = list({plan.id: plan for plan in previous_plans}.values())
        comparisons.append(compare_generated_day(
            day, unique_plans, sources, sorted(set(affected_dates)),
            language=getattr(current_user, "_request_language", "en"),
        ))
    all_blueprint_tags = list(dict.fromkeys(
        slot.day.name for slot in sorted(blueprint.slots, key=lambda slot: slot.day_order)
        if slot.day.name.lower() not in generated_tags
        and (slot.day.template_type.value if hasattr(slot.day.template_type, "value") else str(slot.day.template_type))
        not in ("active_rest", "rest")
    ))
    return GenerationComparison(
        applied_from=applied_from,
        mode="single_day" if single_day else "full",
        days=comparisons,
        untouched_day_tags=all_blueprint_tags,
    )


@router.post("/generate", response_model=GeneratePlanResponse)
async def generate_plan(request: GeneratePlanRequest,
                        db: AsyncSession = Depends(get_db),
                        current_user=Depends(get_current_app_user)):
    profile, blueprint, pool = await _load_generation_context(
        db, current_user, request.blueprint_id)

    allowed = _allowed_equipment((profile.settings or {}).get("locations"))
    prehab = (profile.settings or {}).get("prehab_flags", [])
    # Endpoint tests use a minimal sentinel instead of an AsyncSession; production
    # always has execute(). Keeping the preference layer optional also makes the
    # pure generator usable by scripts that do not have a user context.
    preference_rows = []
    if callable(getattr(db, "execute", None)):
        preference_rows = (await db.execute(select(
            UserExercisePreference.exercise_id, UserExercisePreference.preference
        ).where(
            UserExercisePreference.app_user_id == current_user.id,
            UserExercisePreference.exercise_id.is_not(None),
        ))).all()
    favorite_ids = {exercise_id for exercise_id, value in preference_rows if value == "favorite"}
    disliked_ids = {exercise_id for exercise_id, value in preference_rows if value == "disliked"}
    resolved_accents = list(dict.fromkeys(
        request.config.accent_muscles
        or ([request.config.accent_muscle] if request.config.accent_muscle else [])
        or list(((profile.volume_budget or {}).get("meta") or {}).get("focus_muscles") or [])
    ))[:2]
    cfg = SelectionConfig(use_supersets=request.config.use_supersets,
                          max_superset_size=request.config.max_superset_size,
                          accent_muscle=request.config.accent_muscle,
                          accent_muscles=tuple(resolved_accents),
                          duration_minutes=request.config.duration_minutes,
                          seed=request.config.seed,
                          favorite_exercise_ids=favorite_ids,
                          disliked_exercise_ids=disliked_ids)
    pool_by_id = {exercise.id: exercise for exercise in pool}
    effort_by_tag = (
        await _day_effort_by_tag(db, current_user.id)
        if callable(getattr(db, "execute", None)) else {}
    )
    occurrences_by_tag = (
        await _day_occurrences(db, current_user.id)
        if callable(getattr(db, "execute", None)) else {}
    )

    seen: set = set()
    days_out: list[GeneratedDayOut] = []
    original_targets_by_tag: dict[str, dict[str, int]] = {}
    generation_issues = []
    requested_day_exists = False
    for slot in sorted(blueprint.slots, key=lambda s: s.day_order):
        day_bp = slot.day
        # Single-day mode: skip every day except the requested one.
        if request.day_name and day_bp.name.lower() != _base_day_tag(request.day_name).lower():
            continue
        if request.day_name:
            requested_day_exists = True
        tmpl = day_bp.template_type.value if hasattr(day_bp.template_type, "value") else str(day_bp.template_type)
        is_rest = tmpl in ("active_rest", "rest") or not day_bp.muscle_targets
        # Dedup and tag by the day's NAME, not the template value: the scheduler
        # (SchedulingEngine._score_and_find_best_plan) matches plan.day_tag against
        # the calendar day's name (day_bp.name). Multi-word names like
        # "Arms & Shoulders" would never match the template value "arms_shoulders".
        if is_rest or day_bp.name in seen:
            continue
        seen.add(day_bp.name)

        targets_raw = await VolumeService.calculate_session_targets(db, current_user.id, day_bp.name)
        targets = {k: v["target_sets"] for k, v in targets_raw.items()}
        if not targets:
            generation_issues.append(missing_volume_targets_issue(
                day_bp.name, getattr(current_user, "_request_language", "ru")
            ))
            continue
        occurrences = occurrences_by_tag.get(day_bp.name.lower()) or [
            (day_bp.name, effort_by_tag.get(day_bp.name.lower(), request.config.day_effort))
        ]
        if request.day_name:
            requested_occurrence = next(
                (item for item in occurrences if item[0].lower() == request.day_name.lower()),
                (request.day_name, effort_by_tag.get(request.day_name.lower(), request.config.day_effort)),
            )
            variants = [requested_occurrence]
        else:
            variants = occurrences if request.config.repeated_days_mode == "separate" else [
                (day_bp.name, effort_by_tag.get(day_bp.name.lower(), request.config.day_effort))
            ]
        for variant_index, (variant_tag, effort) in enumerate(variants):
            variant_cfg = replace(
                cfg,
                seed=(cfg.seed + variant_index if cfg.seed is not None else None),
            )
            gen = build_day(variant_tag, variant_tag, targets, pool, allowed, prehab,
                            profile.experience_level, variant_cfg, SelectionPolicy(),
                            timer_mode=request.config.timer_mode,
                            fixed_rest_seconds=request.config.fixed_rest_seconds,
                            day_effort=effort)
            schedule_tags = [variant_tag] if request.day_name else (
                [tag for tag, _ in occurrences]
                if request.config.repeated_days_mode == "shared"
                else [variant_tag]
            )
            original_targets_by_tag[variant_tag.lower()] = targets
            generated_day = GeneratedDayOut(
                day_tag=gen.day_tag, day_name=gen.day_name,
                schedule_tags=schedule_tags,
                coverage=gen.coverage, warnings=gen.warnings,
                estimated_duration_seconds=gen.estimated_duration_seconds,
                duration_limit_met=gen.duration_limit_met,
                exercises=[GeneratedExerciseOut(
                    exercise_id=e.exercise_id,
                    name=pool_by_id[e.exercise_id].name,
                    localized_names=localized_names(pool_by_id[e.exercise_id]),
                    localized_descriptions=localized_descriptions(
                        pool_by_id[e.exercise_id]
                    ),
                    target_sets=e.sets,
                    order_index=e.order_index, superset_group_id=e.superset_group_id,
                    fatigue_tier=e.fatigue_tier, primary_muscle=e.primary_muscle,
                    secondary_muscle=e.secondary_muscle,
                    override_reps=None, override_rir=None,
                    preference="favorite" if e.exercise_id in favorite_ids else None) for e in gen.exercises])
            days_out.append(_apply_saved_generator_rules(
                generated_day, profile, blueprint.id, pool, allowed, prehab,
                request.config, resolved_accents, favorite_ids, effort,
            ))
    inputs = _generation_input_summary(profile, blueprint, request)
    comparison = await _generation_comparison(
        db, current_user, blueprint, days_out, request.target_date,
        single_day=request.day_name is not None,
    )
    issues = list(generation_issues)
    if request.day_name and not requested_day_exists:
        issues.append(missing_requested_day_issue(
            request.day_name, getattr(current_user, "_request_language", "ru")
        ))
    for day in days_out:
        issues.extend(explain_day(
            day,
            original_targets_by_tag.get(day.day_tag.lower(), {}),
            pool,
            allowed,
            prehab,
            request.config.duration_minutes,
            resolved_accents,
            disliked_ids,
            getattr(current_user, "_request_language", "ru"),
        ))
    return GeneratePlanResponse(
        days=days_out,
        inputs=inputs,
        comparison=comparison,
        issues=issues,
    )


@router.post("/generate/preview", response_model=GeneratePlanPreviewResponse)
async def preview_generated_plan(
    request: GeneratePlanPreviewRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_app_user),
):
    """Refresh explanations after chat/manual draft edits without saving."""
    profile, blueprint, pool = await _load_generation_context(
        db, current_user, request.blueprint_id,
    )
    pool_by_id = {exercise.id: exercise for exercise in pool}
    canonical_days = []
    for day in request.days:
        if any(exercise.exercise_id not in pool_by_id for exercise in day.exercises):
            raise LocalizedHTTPException(400, "plan.exercise_unavailable")
        canonical_days.append(day.model_copy(update={
            "exercises": [exercise.model_copy(update={
                "name": pool_by_id[exercise.exercise_id].name,
                "localized_names": localized_names(pool_by_id[exercise.exercise_id]),
                "localized_descriptions": localized_descriptions(
                    pool_by_id[exercise.exercise_id]
                ),
            }) for exercise in day.exercises],
        }))
    allowed = _allowed_equipment((profile.settings or {}).get("locations"))
    prehab = (profile.settings or {}).get("prehab_flags", [])
    inputs = _generation_input_summary(profile, blueprint, request)
    comparison = await _generation_comparison(
        db, current_user, blueprint, canonical_days, request.target_date,
        single_day=request.day_name is not None,
    )
    issues = []
    day_blueprints = {slot.day.name.lower(): slot.day for slot in blueprint.slots}
    refreshed_days = []
    effort_by_tag = await _day_effort_by_tag(db, current_user.id)
    for day in canonical_days:
        selected = [SelectedExercise(
            exercise_id=exercise.exercise_id,
            name=pool_by_id[exercise.exercise_id].name,
            sets=exercise.target_sets,
            order_index=exercise.order_index,
            superset_group_id=exercise.superset_group_id,
            fatigue_tier=exercise.fatigue_tier,
            primary_muscle=exercise.primary_muscle,
            secondary_muscle=exercise.secondary_muscle,
        ) for exercise in day.exercises]
        group_sizes: dict[str, int] = {}
        for exercise in day.exercises:
            if exercise.superset_group_id:
                key = str(exercise.superset_group_id)
                group_sizes[key] = group_sizes.get(key, 0) + 1
        if any(size > request.config.max_superset_size for size in group_sizes.values()):
            raise LocalizedHTTPException(
                400,
                "plan.superset_size_exceeded",
                {"max_size": request.config.max_superset_size},
            )
        AntiSuicideValidator.validate_workout_plan(
            profile.experience_level if profile else "beginner",
            [PlanExerciseInput(
                exercise_id=exercise.exercise_id,
                fatigue_tier=exercise.fatigue_tier,
                primary_muscle=exercise.primary_muscle,
                secondary_muscle=exercise.secondary_muscle,
                target_sets=exercise.target_sets,
                superset_group_id=exercise.superset_group_id,
            ) for exercise in day.exercises],
        )
        effort = effort_by_tag.get(_base_day_tag(day.day_tag).lower(), request.config.day_effort)
        estimated_seconds = estimate_duration_seconds(
            selected,
            pool_by_id,
            DurationConfig(
                timer_mode=request.config.timer_mode,
                fixed_rest_seconds=request.config.fixed_rest_seconds,
                day_effort=effort,
            ),
        )
        refreshed_exercises = [exercise.model_copy(update={
            "name": pool_by_id[exercise.exercise_id].name,
            "localized_names": localized_names(pool_by_id[exercise.exercise_id]),
            "localized_descriptions": localized_descriptions(
                pool_by_id[exercise.exercise_id]
            ),
        }) for exercise in day.exercises]
        refreshed_day = day.model_copy(update={
            "exercises": refreshed_exercises,
            "estimated_duration_seconds": estimated_seconds,
            "duration_limit_met": request.config.duration_minutes is None
                or estimated_seconds <= request.config.duration_minutes * 60,
        })
        refreshed_days.append(refreshed_day)
        day_bp = day_blueprints.get(_base_day_tag(day.day_tag).lower())
        targets = {}
        if day_bp:
            raw = await VolumeService.calculate_session_targets(
                db, current_user.id, day_bp.name,
            )
            targets = {key: value["target_sets"] for key, value in raw.items()}
        issues.extend(explain_day(
            refreshed_day, targets, pool, allowed, prehab,
            request.config.duration_minutes,
            request.config.accent_muscles or (
                [request.config.accent_muscle] if request.config.accent_muscle else []
            ),
            language=getattr(current_user, "_request_language", "ru"),
        ))
    return GeneratePlanPreviewResponse(
        days=refreshed_days,
        inputs=inputs,
        comparison=comparison,
        issues=issues,
    )


@router.get("/generate/presets", response_model=list[GeneratorPresetOut])
async def list_generator_presets(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_app_user),
):
    result = await db.execute(select(AdvancedGeneratorPreset).where(
        AdvancedGeneratorPreset.app_user_id == current_user.id,
    ).order_by(AdvancedGeneratorPreset.is_default.desc(), AdvancedGeneratorPreset.created_at.desc()))
    return list(result.scalars().all())


@router.post("/generate/presets", response_model=GeneratorPresetOut, status_code=status.HTTP_201_CREATED)
async def create_generator_preset(
    payload: GeneratorPresetCreate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_app_user),
):
    name = payload.name.strip()
    existing = (await db.execute(select(AdvancedGeneratorPreset).where(
        AdvancedGeneratorPreset.app_user_id == current_user.id,
        AdvancedGeneratorPreset.name == name,
    ))).scalar_one_or_none()
    if existing:
        raise LocalizedHTTPException(409, "plan.generator_preset_name_conflict")
    if payload.is_default:
        for preset in (await db.execute(select(AdvancedGeneratorPreset).where(
            AdvancedGeneratorPreset.app_user_id == current_user.id,
            AdvancedGeneratorPreset.is_default.is_(True),
        ))).scalars().all():
            preset.is_default = False
    preset = AdvancedGeneratorPreset(
        app_user_id=current_user.id, name=name,
        settings=payload.settings, is_default=payload.is_default,
    )
    db.add(preset)
    await db.commit()
    await db.refresh(preset)
    return preset


@router.patch("/generate/presets/{preset_id}", response_model=GeneratorPresetOut)
async def update_generator_preset(
    preset_id: int,
    payload: GeneratorPresetUpdate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_app_user),
):
    preset = (await db.execute(select(AdvancedGeneratorPreset).where(
        AdvancedGeneratorPreset.id == preset_id,
        AdvancedGeneratorPreset.app_user_id == current_user.id,
    ))).scalar_one_or_none()
    if not preset:
        raise LocalizedHTTPException(404, "plan.generator_preset_not_found")
    if payload.name is not None:
        name = payload.name.strip()
        duplicate = (await db.execute(select(AdvancedGeneratorPreset).where(
            AdvancedGeneratorPreset.app_user_id == current_user.id,
            AdvancedGeneratorPreset.name == name,
            AdvancedGeneratorPreset.id != preset_id,
        ))).scalar_one_or_none()
        if duplicate:
            raise LocalizedHTTPException(409, "plan.generator_preset_name_conflict")
        preset.name = name
    if payload.settings is not None:
        preset.settings = payload.settings
    if payload.is_default is not None:
        if payload.is_default:
            for other in (await db.execute(select(AdvancedGeneratorPreset).where(
                AdvancedGeneratorPreset.app_user_id == current_user.id,
                AdvancedGeneratorPreset.is_default.is_(True),
                AdvancedGeneratorPreset.id != preset_id,
            ))).scalars().all():
                other.is_default = False
        preset.is_default = payload.is_default
    await db.commit()
    await db.refresh(preset)
    return preset


@router.delete("/generate/presets/{preset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_generator_preset(
    preset_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_app_user),
):
    preset = (await db.execute(select(AdvancedGeneratorPreset).where(
        AdvancedGeneratorPreset.id == preset_id,
        AdvancedGeneratorPreset.app_user_id == current_user.id,
    ))).scalar_one_or_none()
    if not preset:
        raise LocalizedHTTPException(404, "plan.generator_preset_not_found")
    await db.delete(preset)
    await db.commit()

@router.get("/")
def get_plans(db: Session = Depends(get_db), current_user=Depends(get_current_app_user)):
    """Получить список всех сохраненных планов пользователя."""
    return db.query(WorkoutPlan).filter(WorkoutPlan.app_user_id == current_user.id).all()


@router.get("/{plan_id}")
async def get_plan(plan_id: int, db: AsyncSession = Depends(get_db), current_user=Depends(get_current_app_user)):
    """Получить полную структуру плана вместе с упражнениями и их названиями."""
    stmt = (
        select(WorkoutPlan)
        .where(
            WorkoutPlan.id == plan_id,
            WorkoutPlan.app_user_id == current_user.id
        )
        .options(
            # Магия: загружаем не только связь с таблицей workout_plan_exercises,
            # но и проваливаемся глубже — в саму таблицу exercises
            selectinload(WorkoutPlan.exercises).selectinload(WorkoutPlanExercise.exercise)
        )
    )
    result = await db.execute(stmt)
    plan = result.scalar_one_or_none()

    if not plan:
        raise LocalizedHTTPException(404, "plan.not_found")

    return plan

@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_workout_plan(  # <--- СДЕЛАЛИ ASYNC
        plan_data: WorkoutPlanCreate,
        db: AsyncSession = Depends(get_db),  # <--- ИЗМЕНИЛИ ТИП НА ASYNCSESSION
        current_user=Depends(get_current_app_user)
):
    """Полное сохранение плана с валидацией."""

    # 1. Новый асинхронный синтаксис вместо db.query()
    stmt = select(AppUserProfile).where(AppUserProfile.app_user_id == current_user.id)
    result = await db.execute(stmt)
    profile = result.scalar_one_or_none()

    experience_level = profile.experience_level if profile else "beginner"

    exercises_input = [
        PlanExerciseInput(
            exercise_id=ex.exercise_id, fatigue_tier=ex.fatigue_tier,
            primary_muscle=ex.primary_muscle, secondary_muscle=ex.secondary_muscle,
            target_sets=ex.target_sets, superset_group_id=ex.superset_group_id
        ) for ex in plan_data.exercises
    ]

    AntiSuicideValidator.validate_workout_plan(experience_level, exercises_input)

    new_plan = WorkoutPlan(
        app_user_id=current_user.id,
        name=plan_data.name,
        day_tag=plan_data.day_tag.lower(),  # Возвращаем как было, но оставляем .lower() для надежности
        micro_tag=plan_data.micro_tag.lower(),
        meso_tag=plan_data.meso_tag.lower()
    )
    db.add(new_plan)

    await db.flush()

    for ex in plan_data.exercises:
        new_ex = WorkoutPlanExercise(
            plan_id=new_plan.id, exercise_id=ex.exercise_id,
            order_index=ex.order_index, superset_group_id=ex.superset_group_id,
            target_sets=ex.target_sets, override_reps=ex.override_reps,
            override_rir=ex.override_rir,
        )
        db.add(new_ex)

    await db.commit()
    return {"status": "success", "plan_id": new_plan.id}


@router.put("/{plan_id}")
def update_workout_plan(plan_id: int, plan_data: WorkoutPlanCreate, db: Session = Depends(get_db),
                        current_user=Depends(get_current_app_user)):
    """Редактирование плана: проверяем валидатором, сносим старые упражнения, пишем новые."""
    plan = db.query(WorkoutPlan).filter(WorkoutPlan.id == plan_id, WorkoutPlan.app_user_id == current_user.id).first()
    if not plan:
        raise LocalizedHTTPException(404, "plan.not_found")

    # Валидация
    profile = db.query(AppUserProfile).filter(AppUserProfile.app_user_id == current_user.id).first()
    experience_level = profile.experience_level if profile else "beginner"

    exercises_input = [
        PlanExerciseInput(
            exercise_id=ex.exercise_id, fatigue_tier=ex.fatigue_tier,
            primary_muscle=ex.primary_muscle, secondary_muscle=ex.secondary_muscle,
            target_sets=ex.target_sets, superset_group_id=ex.superset_group_id
        ) for ex in plan_data.exercises
    ]
    AntiSuicideValidator.validate_workout_plan(experience_level, exercises_input)

    # Обновляем метаданные
    plan.name = plan_data.name
    plan.day_tag = plan_data.day_tag
    plan.micro_tag = plan_data.micro_tag
    plan.meso_tag = plan_data.meso_tag

    # Очищаем старые упражнения
    db.query(WorkoutPlanExercise).filter(WorkoutPlanExercise.plan_id == plan.id).delete()

    # Пишем новые
    for ex in plan_data.exercises:
        new_ex = WorkoutPlanExercise(
            plan_id=plan.id, exercise_id=ex.exercise_id,
            order_index=ex.order_index, superset_group_id=ex.superset_group_id,
            target_sets=ex.target_sets, override_reps=ex.override_reps,
            override_rir=ex.override_rir,
        )
        db.add(new_ex)

    db.commit()
    return {"status": "success", "message": tr(getattr(current_user, "_request_language", "en"), "plan.updated")}


@router.delete("/{plan_id}")
def delete_plan(plan_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_app_user)):
    """Удалить план."""
    plan = db.query(WorkoutPlan).filter(WorkoutPlan.id == plan_id, WorkoutPlan.app_user_id == current_user.id).first()
    if not plan:
        raise LocalizedHTTPException(404, "plan.not_found")

    db.delete(plan)
    db.commit()
    return {"status": "success", "message": tr(getattr(current_user, "_request_language", "en"), "plan.deleted")}


async def _load_commands_context(db, current_user, blueprint_id):
    prof_res = await db.execute(select(AppUserProfile).where(AppUserProfile.app_user_id == current_user.id))
    profile = prof_res.scalar_one_or_none()
    pool_res = await db.execute(select(Exercise).where(
        (Exercise.source == "default") | (Exercise.app_user_id == current_user.id)))
    return profile, list(pool_res.scalars().all())


def _profile_generator_rules(profile) -> list[dict]:
    return copy.deepcopy((((profile.settings or {}) if profile else {}).get("generator_rules") or []))


def _rule_matches(rule: dict, blueprint_id, day_tag: str) -> bool:
    if not rule.get("enabled", True):
        return False
    scope = rule.get("scope")
    if scope == "all":
        return True
    if scope == "split":
        return str(rule.get("blueprint_id") or "") == str(blueprint_id or "")
    if scope == "day":
        return (
            str(rule.get("blueprint_id") or "") == str(blueprint_id or "")
            and _base_day_tag(rule.get("day_tag") or "").lower() == _base_day_tag(day_tag).lower()
        )
    return False


def _coverage_after_commands(base_coverage: dict, exercises: list[dict]) -> dict:
    filled_by_key: dict[str, int] = {}
    for exercise in exercises:
        key = key_for_muscle(exercise.get("primary_muscle"))
        if key:
            filled_by_key[key] = filled_by_key.get(key, 0) + int(exercise.get("target_sets") or 0)
    coverage = {
        key: {"target": value.get("target", 0), "filled": filled_by_key.get(key, 0)}
        for key, value in (base_coverage or {}).items()
    }
    for key, filled in filled_by_key.items():
        coverage.setdefault(key, {"target": 0, "filled": filled})
    return coverage


def _canonicalize_command_exercises(exercises: list[dict], pool) -> list[dict]:
    """Replace executor presentation fields from the authorized exercise pool."""
    pool_by_id = {exercise.id: exercise for exercise in pool}
    if any(exercise.get("exercise_id") not in pool_by_id for exercise in exercises):
        raise LocalizedHTTPException(400, "plan.exercise_unavailable")
    return [
        {
            **exercise,
            "name": pool_by_id[exercise["exercise_id"]].name,
            "localized_names": localized_names(
                pool_by_id[exercise["exercise_id"]]
            ),
            "localized_descriptions": localized_descriptions(
                pool_by_id[exercise["exercise_id"]]
            ),
        }
        for exercise in exercises
    ]


def _apply_saved_generator_rules(
    day: GeneratedDayOut, profile, blueprint_id, pool, allowed, prehab,
    generation_config=None, accent_muscles=(), favorite_ids=None, day_effort=None,
):
    matching = [
        rule for rule in _profile_generator_rules(profile)
        if _rule_matches(rule, blueprint_id, day.day_tag)
    ]
    if not matching:
        return day
    commands = [Command.from_dict(rule["command"]) for rule in matching]
    exercises, summaries, warnings = apply_commands_exec(
        [exercise.model_dump() for exercise in day.exercises], commands, pool,
        {"allowed_equipment": allowed, "prehab_flags": prehab},
        profile.experience_level if profile else "beginner",
    )
    estimated_seconds = day.estimated_duration_seconds
    duration_limit_met = day.duration_limit_met
    pool_by_id = {exercise.id: exercise for exercise in pool}
    exercises = _canonicalize_command_exercises(exercises, pool)
    if generation_config is not None:
        selected = [SelectedExercise(
            exercise_id=exercise["exercise_id"], name=exercise["name"],
            sets=exercise["target_sets"], order_index=exercise["order_index"],
            superset_group_id=exercise.get("superset_group_id"),
            fatigue_tier=exercise["fatigue_tier"], primary_muscle=exercise["primary_muscle"],
            secondary_muscle=exercise.get("secondary_muscle"),
        ) for exercise in exercises]
        if all(exercise.exercise_id in pool_by_id for exercise in selected):
            duration_result = fit_to_duration(
                selected, pool_by_id, generation_config.duration_minutes,
                DurationConfig(
                    timer_mode=generation_config.timer_mode,
                    fixed_rest_seconds=generation_config.fixed_rest_seconds,
                    day_effort=day_effort or generation_config.day_effort,
                ),
                accent_muscles=accent_muscles,
                favorite_ids=favorite_ids or set(),
            )
            exercise_by_id = {exercise["exercise_id"]: exercise for exercise in exercises}
            exercises = [{
                **exercise_by_id[row.exercise_id],
                "target_sets": row.sets,
                "order_index": row.order_index,
                "superset_group_id": row.superset_group_id,
            } for row in duration_result.exercises]
            estimated_seconds = duration_result.estimated_seconds
            duration_limit_met = duration_result.limit_met
    applied = [f"Постоянное правило: {summary}" for summary in summaries if summary]
    return GeneratedDayOut(
        day_tag=day.day_tag,
        day_name=day.day_name,
        schedule_tags=day.schedule_tags,
        exercises=exercises,
        coverage=_coverage_after_commands(day.coverage, exercises),
        warnings=[*day.warnings, *applied, *warnings],
        estimated_duration_seconds=estimated_seconds,
        duration_limit_met=duration_limit_met,
    )


@router.get("/commands/rules", response_model=list[GeneratorRuleOut])
async def list_generator_rules(db: AsyncSession = Depends(get_db), current_user=Depends(get_current_app_user)):
    profile = (await db.execute(select(AppUserProfile).where(
        AppUserProfile.app_user_id == current_user.id,
    ))).scalar_one_or_none()
    return _profile_generator_rules(profile)


@router.post("/commands/rules", response_model=GeneratorRuleOut, status_code=status.HTTP_201_CREATED)
async def create_generator_rule(payload: GeneratorRuleCreate, db: AsyncSession = Depends(get_db), current_user=Depends(get_current_app_user)):
    profile = (await db.execute(select(AppUserProfile).where(
        AppUserProfile.app_user_id == current_user.id,
    ))).scalar_one_or_none()
    if not profile:
        raise LocalizedHTTPException(404, "profile.not_found")
    if payload.scope in ("day", "split") and not payload.blueprint_id:
        raise LocalizedHTTPException(400, "plan.rule_split_required")
    if payload.scope == "day" and not payload.day_tag:
        raise LocalizedHTTPException(400, "plan.rule_day_type_required")
    rule = {
        "id": str(uuid.uuid4()), "command": payload.command.model_dump(),
        "scope": payload.scope, "blueprint_id": payload.blueprint_id,
        "day_tag": payload.day_tag, "enabled": True,
    }
    settings = dict(profile.settings or {})
    settings["generator_rules"] = [*_profile_generator_rules(profile), rule]
    profile.settings = settings
    await db.commit()
    return rule


@router.patch("/commands/rules/{rule_id}", response_model=GeneratorRuleOut)
async def update_generator_rule(rule_id: str, payload: GeneratorRuleUpdate, db: AsyncSession = Depends(get_db), current_user=Depends(get_current_app_user)):
    profile = (await db.execute(select(AppUserProfile).where(AppUserProfile.app_user_id == current_user.id))).scalar_one_or_none()
    rules = _profile_generator_rules(profile)
    rule = next((item for item in rules if item.get("id") == rule_id), None)
    if not rule:
        raise LocalizedHTTPException(404, "plan.rule_not_found")
    rule["enabled"] = payload.enabled
    settings = dict(profile.settings or {}); settings["generator_rules"] = rules; profile.settings = settings
    await db.commit()
    return rule


@router.delete("/commands/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_generator_rule(rule_id: str, db: AsyncSession = Depends(get_db), current_user=Depends(get_current_app_user)):
    profile = (await db.execute(select(AppUserProfile).where(AppUserProfile.app_user_id == current_user.id))).scalar_one_or_none()
    rules = _profile_generator_rules(profile)
    filtered = [item for item in rules if item.get("id") != rule_id]
    if len(filtered) == len(rules):
        raise LocalizedHTTPException(404, "plan.rule_not_found")
    settings = dict(profile.settings or {}); settings["generator_rules"] = filtered; profile.settings = settings
    await db.commit()


@router.post("/commands/apply", response_model=ApplyCommandsResponse)
async def apply_commands(request: ApplyCommandsRequest, db: AsyncSession = Depends(get_db),
                         current_user=Depends(get_current_app_user)):
    profile, pool = await _load_commands_context(db, current_user, request.context.get("blueprint_id"))
    exp = profile.experience_level if profile else "beginner"

    log = [Command.from_dict(c.model_dump()) for c in request.command_log]
    draft_ex = [e.model_dump() for e in request.base_draft.exercises]

    clarify = None
    parsed = []
    if request.new_comment:
        new_cmds = parse_comment(request.new_comment, draft_ex, rng_seed=len(log))
        clar = next((c for c in new_cmds if c.type == CommandType.CLARIFY), None)
        if clar:
            clarify = ClarifyOut(**clar.params)
        else:
            log = log + new_cmds
            parsed = new_cmds

    settings = (profile.settings if profile else {}) or {}
    allowed = _allowed_equipment(settings.get("locations")) if profile else None
    ctx = {"allowed_equipment": allowed, "prehab_flags": settings.get("prehab_flags", [])}
    final_ex, _summaries, warns = apply_commands_exec(draft_ex, log, pool, ctx, exp)
    final_ex = _canonicalize_command_exercises(final_ex, pool)

    # Fix 3: recompute coverage["filled"] from the post-command exercise list
    # instead of echoing request.base_draft.coverage unchanged — otherwise the
    # mobile "цели X/Y" goal indicator goes stale as soon as commands add/remove
    # exercises. Semantics mirror plan_generator_service.build_day: filled =
    # sum of target_sets for exercises whose primary_muscle maps (via
    # key_for_muscle) to that EN system key. Each base-coverage key keeps its
    # original (fixed) target; keys that only appear post-edit (e.g. via
    # ADD_MUSCLE) are added with target=0 so newly-covered muscles show up too.
    coverage = _coverage_after_commands(request.base_draft.coverage, final_ex)

    # NOTE: pydantic v2's `model_copy(update=...)` does NOT validate/coerce the
    # updated fields, and BaseModel construction skips re-validating a field
    # whose value is *already* an instance of the target model class (default
    # `revalidate_instances='never'`). Both combined mean a plain
    # `request.base_draft.model_copy(update={"exercises": final_ex, ...})`
    # followed by `ApplyCommandsResponse(final_draft=final_draft, ...)` would
    # silently leave `final_draft.exercises` as raw dicts (verified: attribute
    # access like `e.exercise_id` then raises AttributeError). Rebuilding via
    # the normal GeneratedDayOut(...) constructor forces full validation, so
    # `final_ex` (plain dicts from the executor) is coerced into
    # GeneratedExerciseOut instances.
    final_draft = GeneratedDayOut(
        day_tag=request.base_draft.day_tag, day_name=request.base_draft.day_name,
        coverage=coverage, exercises=final_ex, warnings=warns)
    reply = clarify.question if clarify else ("Готово: " + "; ".join(c.summary for c in parsed) if parsed else "Ок.")
    return ApplyCommandsResponse(
        final_draft=final_draft,
        parsed_commands=[CommandOut(**c.to_dict()) for c in (log if not clarify else [])],
        reply=reply, clarify=clarify)


@router.post("/{plan_id}/apply")
async def apply_plan_to_calendar(
        plan_id: int,
        payload: PlanApplyRequest,
        db: AsyncSession = Depends(get_db),
        current_user=Depends(get_current_app_user)
):
    """Применить план к календарю (создать тренировочную сессию)."""

    # 1. Достаем план вместе с его упражнениями
    stmt = (
        select(WorkoutPlan)
        .where(
            WorkoutPlan.id == plan_id,
            WorkoutPlan.app_user_id == current_user.id
        )
        .options(selectinload(WorkoutPlan.exercises))
    )
    result = await db.execute(stmt)
    plan = result.scalar_one_or_none()

    if not plan:
        raise LocalizedHTTPException(404, "plan.not_found")

    # 2. Создаем новую тренировочную сессию
    new_session = WorkoutSession(
        app_user_id=current_user.id,
        source="plan",
        status="active",
        plan_id=plan.id,
        # notes=f"Применено из плана: {plan.name} (Режим: {payload.apply_mode})"
    )
    db.add(new_session)
    await db.flush()  # Получаем ID сессии

    # P0-06 C1: этот эндпоинт — один из путей создания WorkoutSessionExercise
    # без вызова движка прогрессии (см. также api/routers/workout_center.py
    # start_workout). Без явного расчёта и сохранения предписания здесь
    # тренировка, применённая через /plans/{id}/apply, тоже никогда не
    # получила бы prescription в истории.
    profile_result = await db.execute(
        select(AppUserProfile).where(AppUserProfile.app_user_id == current_user.id)
    )
    profile = profile_result.scalars().first()
    experience_level = profile.experience_level if profile else None
    settings = profile.settings if profile else None

    # У новой сессии (см. WorkoutSession выше) нет ни app_user_mesocycle_id,
    # ни mesocycle_phase — этот эндпоинт не привязывает сессию к мезоциклу.
    # resolve_phase_effort_tier с (None, None) синхронно вернёт дефолт
    # "medium" без запроса — тот же результат, что и раньше неявно.
    phase_effort_tier = await progression_repo.resolve_phase_effort_tier(
        db, new_session.app_user_mesocycle_id, new_session.mesocycle_phase
    )

    exercise_ids = [plan_ex.exercise_id for plan_ex in plan.exercises]
    exercises_by_id: dict[int, Exercise] = {}
    if exercise_ids:
        ex_rows = await db.execute(select(Exercise).where(Exercise.id.in_(exercise_ids)))
        exercises_by_id = {e.id: e for e in ex_rows.scalars().all()}

    # 3. Переносим упражнения из плана в сессию
    for plan_ex in plan.exercises:
        session_ex = WorkoutSessionExercise(
            workout_session_id=new_session.id,
            exercise_id=plan_ex.exercise_id,
            order_index=plan_ex.order_index,
            # Преобразуем UUID в строку, если он есть
            superset_group=str(plan_ex.superset_group_id) if plan_ex.superset_group_id else None,
            # Блокер 2 (финальное ревью P0-06): раньше target_sets плана не
            # попадал в строку сессии — build_context брал дефолт
            # (session_exercise.target_sets or 3), и предписание считалось
            # на 3 подхода, даже если план говорил, скажем, 5. Строки
            # WorkoutSessionSet ниже создаются по plan_ex.target_sets
            # правильно, разъезжались только они и предписание.
            target_sets=plan_ex.target_sets,
        )
        db.add(session_ex)
        await db.flush()  # Получаем ID упражнения в сессии

        # 4. Создаем пустые подходы в соответствии с target_sets
        for set_num in range(1, plan_ex.target_sets + 1):
            new_set = WorkoutSessionSet(
                workout_session_exercise_id=session_ex.id,
                set_number=set_num,
                set_type="normal",
                is_completed=False  # Подходы изначально не выполнены
            )
            db.add(new_set)

        # session_ex.recommended_rep_min/max тут по-прежнему не заполняются
        # (пред-существующий разрыв этого эндпоинта, вне периметра C1/C2/
        # блокера 2 финального ревью) — build_context сам откатится на
        # TIER_REP_FALLBACK по fatigue_tier упражнения. target_sets теперь
        # заполняется явно выше.
        session_ex.exercise = exercises_by_id.get(plan_ex.exercise_id)
        ctx = await progression_repo.build_context(
            db,
            session_ex,
            current_user.id,
            experience_level,
            settings,
            phase_effort_tier=phase_effort_tier,
        )
        prescription = plan_exercise(
            ctx, override=override_for(settings, session_ex.exercise_id)
        )
        progression_repo.persist_prescription(session_ex, prescription)

    await db.commit()

    return {
        "status": "success",
        "message": tr(getattr(current_user, "_request_language", "en"), "plan.applied"),
        "session_id": new_session.id
    }


@router.post("/generate/confirm", response_model=ConfirmPlanResponse)
async def confirm_generated_plan(request: ConfirmPlanRequest,
                                 db: AsyncSession = Depends(get_db),
                                 current_user=Depends(get_current_app_user)):
    prof_res = await db.execute(select(AppUserProfile).where(
        AppUserProfile.app_user_id == current_user.id))
    profile = prof_res.scalar_one_or_none()
    experience = profile.experience_level if profile else "beginner"

    if not request.days:
        raise LocalizedHTTPException(400, "plan.no_training_days")
    if request.mode == "single_day" and len(request.days) != 1:
        raise LocalizedHTTPException(400, "plan.single_day_requires_one_day")

    today = _date.today()
    # Генератор меняет только текущие/будущие назначения. Переданная из
    # старой ссылки дата не должна переписывать историю пользователя.
    applied_from = max(request.target_date or today, today)
    created: list[int] = []
    created_plans: list[WorkoutPlan] = []
    for day in request.days:
        AntiSuicideValidator.validate_workout_plan(
            experience,
            [PlanExerciseInput(exercise_id=e.exercise_id, fatigue_tier=e.fatigue_tier,
                               primary_muscle=e.primary_muscle, secondary_muscle=e.secondary_muscle,
                               target_sets=e.target_sets,
                               superset_group_id=e.superset_group_id) for e in day.exercises])
        plan = WorkoutPlan(app_user_id=current_user.id,
                           name=f"{day.day_name} (сгенерировано)",
                           day_tag=day.day_tag.lower(), micro_tag="adaptive", meso_tag="adaptive")
        db.add(plan)
        await db.flush()
        created_plans.append(plan)
        for e in day.exercises:
            db.add(WorkoutPlanExercise(
                plan_id=plan.id, exercise_id=e.exercise_id, order_index=e.order_index,
                superset_group_id=e.superset_group_id, target_sets=e.target_sets,
                override_reps=e.override_reps, override_rir=e.override_rir))
        created.append(plan.id)
    us_res = await db.execute(
        select(UserSplit)
        .where(
            UserSplit.app_user_id == current_user.id,
            UserSplit.is_active == True,  # noqa: E712
        )
        .options(
            selectinload(UserSplit.blueprint)
            .selectinload(SplitBlueprint.slots)
            .selectinload(SplitDaySlot.day)
        )
    )
    user_split = us_res.scalars().first()
    if user_split:
        # selected_plans — недатированный legacy-контекст. Будущий план не
        # должен становиться «текущим» раньше выбранной даты; календарь ниже
        # уже хранит точное назначение на каждый будущий день.
        if applied_from == today:
            _bind_generated_plans_to_split(user_split, created_plans)
        # Назначаем только созданные типы дней и только начиная с выбранной
        # даты. Остальные планы и прошлые дни остаются нетронутыми.
        by_tag = {
            tag.lower(): plan.id
            for day, plan in zip(request.days, created_plans)
            for tag in (day.schedule_tags or [day.day_tag])
        }
        calendar_days = list((await db.execute(
            select(UserCalendarDay).where(
                UserCalendarDay.app_user_id == current_user.id,
                UserCalendarDay.target_date >= applied_from,
                UserCalendarDay.is_rest_day == False,  # noqa: E712
                UserCalendarDay.status == "planned",
                UserCalendarDay.actual_workout_session_id.is_(None),
            )
        )).scalars().all())
        for calendar_day in calendar_days:
            plan_id = by_tag.get((calendar_day.day_tag or "").lower())
            if plan_id is not None:
                calendar_day.plan_id = plan_id
        await db.commit()
    else:
        await db.commit()

    return ConfirmPlanResponse(
        status="success",
        created_plan_ids=created,
        applied_from=applied_from,
        updated_day_tags=list(dict.fromkeys(
            tag for day in request.days for tag in (day.schedule_tags or [day.day_tag])
        )),
    )
