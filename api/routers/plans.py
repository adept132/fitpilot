from typing import Optional as _Optional
from datetime import date as _date
from fastapi import APIRouter, Depends, status, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, selectinload
from sqlalchemy import select
from api.deps import get_db
from api.schemas.plan import WorkoutPlanCreate, PlanApplyRequest
from api.services.app_user_service import get_current_app_user
from api.services.models import Mesocycle, MesocyclePhase, WorkoutPlan, AppUserProfile, WorkoutPlanExercise, \
    WorkoutSession, WorkoutSessionExercise, WorkoutSessionSet
from api.services.validator import AntiSuicideValidator, PlanExerciseInput
from api.services.scheduling_engine import SchedulingEngine
from api.services.models import UserSplit, SplitBlueprint, SplitDaySlot, DayBlueprint, Exercise
from api.services.volume_service import VolumeService
from api.services.plan_generator_service import build_day
from api.services.muscle_keys import key_for_muscle
from api.services.exercise_selection_engine import SelectionConfig, SelectionPolicy
from api.services.progression import repository as progression_repo
from api.services.progression.engine import plan_exercise
from api.services.progression.resolve import override_for
from api.schemas.plan import (
    GeneratePlanRequest, GeneratePlanResponse, GeneratedDayOut, GeneratedExerciseOut,
    ConfirmPlanRequest, ConfirmPlanResponse,
)
from api.schemas.commands import (ApplyCommandsRequest, ApplyCommandsResponse, CommandOut, ClarifyOut)
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


async def _load_generation_context(db, current_user, blueprint_id):
    prof_res = await db.execute(select(AppUserProfile).where(
        AppUserProfile.app_user_id == current_user.id))
    profile = prof_res.scalar_one_or_none()
    if not profile or not profile.volume_budget:
        raise HTTPException(400, "Онбординг не завершён: нет объёмного бюджета")

    if blueprint_id is None:
        us_res = await db.execute(select(UserSplit).where(
            UserSplit.app_user_id == current_user.id, UserSplit.is_active == True))  # noqa: E712
        active = us_res.scalar_one_or_none()
        if not active:
            raise HTTPException(400, "Нет активного сплита и не передан blueprint_id")
        blueprint_id = active.blueprint_id

    bp_res = await db.execute(
        select(SplitBlueprint).where(SplitBlueprint.id == blueprint_id).options(
            selectinload(SplitBlueprint.slots).selectinload(SplitDaySlot.day)
            .selectinload(DayBlueprint.muscle_targets)))
    blueprint = bp_res.scalar_one_or_none()
    if not blueprint or not blueprint.slots:
        raise HTTPException(404, "Сплит пуст или не найден")

    pool_res = await db.execute(select(Exercise).where(
        (Exercise.source == "default") | (Exercise.app_user_id == current_user.id)))
    pool = list(pool_res.scalars().all())
    return profile, blueprint, pool


@router.post("/generate", response_model=GeneratePlanResponse)
async def generate_plan(request: GeneratePlanRequest,
                        db: AsyncSession = Depends(get_db),
                        current_user=Depends(get_current_app_user)):
    profile, blueprint, pool = await _load_generation_context(
        db, current_user, request.blueprint_id)

    allowed = _allowed_equipment((profile.settings or {}).get("locations"))
    prehab = (profile.settings or {}).get("prehab_flags", [])
    cfg = SelectionConfig(use_supersets=request.config.use_supersets,
                          accent_muscle=request.config.accent_muscle,
                          seed=request.config.seed)

    seen: set = set()
    days_out: list[GeneratedDayOut] = []
    for slot in sorted(blueprint.slots, key=lambda s: s.day_order):
        day_bp = slot.day
        # Single-day mode: skip every day except the requested one.
        if request.day_name and day_bp.name != request.day_name:
            continue
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
            continue
        gen = build_day(day_bp.name, day_bp.name, targets, pool, allowed, prehab,
                        profile.experience_level, cfg, SelectionPolicy())
        days_out.append(GeneratedDayOut(
            day_tag=gen.day_tag, day_name=gen.day_name, coverage=gen.coverage,
            warnings=gen.warnings,
            exercises=[GeneratedExerciseOut(
                exercise_id=e.exercise_id, name=e.name, target_sets=e.sets,
                order_index=e.order_index, superset_group_id=e.superset_group_id,
                fatigue_tier=e.fatigue_tier, primary_muscle=e.primary_muscle,
                secondary_muscle=e.secondary_muscle) for e in gen.exercises]))
    return GeneratePlanResponse(days=days_out)

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
        raise HTTPException(status_code=404, detail="План не найден")

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
            target_sets=ex.target_sets
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
        raise HTTPException(status_code=404, detail="План не найден")

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
            target_sets=ex.target_sets
        )
        db.add(new_ex)

    db.commit()
    return {"status": "success", "message": "План обновлен"}


@router.delete("/{plan_id}")
def delete_plan(plan_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_app_user)):
    """Удалить план."""
    plan = db.query(WorkoutPlan).filter(WorkoutPlan.id == plan_id, WorkoutPlan.app_user_id == current_user.id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="План не найден")

    db.delete(plan)
    db.commit()
    return {"status": "success", "message": "План удален"}


async def _load_commands_context(db, current_user, blueprint_id):
    prof_res = await db.execute(select(AppUserProfile).where(AppUserProfile.app_user_id == current_user.id))
    profile = prof_res.scalar_one_or_none()
    pool_res = await db.execute(select(Exercise).where(
        (Exercise.source == "default") | (Exercise.app_user_id == current_user.id)))
    return profile, list(pool_res.scalars().all())


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

    # Fix 3: recompute coverage["filled"] from the post-command exercise list
    # instead of echoing request.base_draft.coverage unchanged — otherwise the
    # mobile "цели X/Y" goal indicator goes stale as soon as commands add/remove
    # exercises. Semantics mirror plan_generator_service.build_day: filled =
    # sum of target_sets for exercises whose primary_muscle maps (via
    # key_for_muscle) to that EN system key. Each base-coverage key keeps its
    # original (fixed) target; keys that only appear post-edit (e.g. via
    # ADD_MUSCLE) are added with target=0 so newly-covered muscles show up too.
    base_coverage = request.base_draft.coverage or {}
    filled_by_key: dict = {}
    for e in final_ex:
        k = key_for_muscle(e.get("primary_muscle"))
        if k:
            filled_by_key[k] = filled_by_key.get(k, 0) + (e.get("target_sets") or 0)
    coverage = {key: {"target": val.get("target", 0), "filled": filled_by_key.get(key, 0)}
                for key, val in base_coverage.items()}
    for key, filled in filled_by_key.items():
        if key not in coverage:
            coverage[key] = {"target": 0, "filled": filled}

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
        raise HTTPException(status_code=404, detail="План не найден")

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
        "message": "План успешно применен",
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

    created: list[int] = []
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
        for e in day.exercises:
            db.add(WorkoutPlanExercise(
                plan_id=plan.id, exercise_id=e.exercise_id, order_index=e.order_index,
                superset_group_id=e.superset_group_id, target_sets=e.target_sets))
        created.append(plan.id)
    await db.commit()

    us_res = await db.execute(select(UserSplit).where(
        UserSplit.app_user_id == current_user.id, UserSplit.is_active == True))  # noqa: E712
    if us_res.scalar_one_or_none():
        await SchedulingEngine.rebind_plans(db, current_user.id, _date.today())

    return ConfirmPlanResponse(status="success", created_plan_ids=created)
