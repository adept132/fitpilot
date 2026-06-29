from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from api.schemas.profile import ProfileResponse, UpdateProfileRequest
from api.schemas.оnboarding import UpdateSettingsRequest, VolumeBudget, OnboardingWidgetRequest
from api.services.app_user_service import get_current_app_user
from api.services.models import AppUser, AppUserProfile, UserAnthropometry
from api.services.volume_calculator import calculate_volume_budget

router = APIRouter(tags=["profile"])


@router.get("/users/me/profile", response_model=ProfileResponse)
async def get_profile_me(
    app_user: AppUser = Depends(get_current_app_user),
):
    return ProfileResponse(
        id=app_user.id,
        email=app_user.email,
        display_name=app_user.display_name,
        email_verified=app_user.email_verified,
        is_active=app_user.is_active,
    )


@router.patch("/profile/me", response_model=ProfileResponse)
async def update_profile_me(
    payload: UpdateProfileRequest,
    session: AsyncSession = Depends(get_db),
    app_user: AppUser = Depends(get_current_app_user),
):
    app_user.display_name = payload.display_name.strip() if payload.display_name else None

    await session.commit()
    await session.refresh(app_user)

    return ProfileResponse(
        id=app_user.id,
        email=app_user.email,
        display_name=app_user.display_name,
        email_verified=app_user.email_verified,
        is_active=app_user.is_active,
    )


@router.get("/profile")
async def get_my_profile(
        current_user: AppUser = Depends(get_current_app_user),
        db: AsyncSession = Depends(get_db)
):
    # 1. Достаем профиль пользователя
    profile_result = await db.execute(
        select(AppUserProfile).where(AppUserProfile.app_user_id == current_user.id)
    )
    profile = profile_result.scalars().first()

    if not profile:
        raise HTTPException(status_code=404, detail="Профиль не найден")

    # 2. Достаем самую свежую запись антропометрии (вес/рост)
    anthro_result = await db.execute(
        select(UserAnthropometry)
        .where(UserAnthropometry.app_user_id == current_user.id)
        .order_by(desc(UserAnthropometry.recorded_at))
        .limit(1)
    )
    latest_anthro = anthro_result.scalars().first()

    # 3. Формируем ответ, который идеально совпадает с типом UserProfile на фронтенде
    return {
        "username": profile.username or current_user.display_name,
        "experience_level": profile.experience_level,
        "training_frequency": profile.training_frequency,
        # Если добавлял microcycle_length в БД, раскомментируй строку ниже:
        # "microcycle_length": profile.microcycle_length,
        "gender": profile.gender,
        "current_streak": profile.current_streak,
        "total_workouts": profile.total_workouts,
        "pro_mode_enabled": profile.pro_mode_enabled,
        "volume_budget": profile.volume_budget,
        "settings": profile.settings,
        "latest_anthropometry": {
            "weight": latest_anthro.weight,
            "height": latest_anthro.height
        } if latest_anthro else None
    }


@router.patch("/profile/settings")
async def update_profile_settings(
        payload: UpdateSettingsRequest,
        current_user: AppUser = Depends(get_current_app_user),
        db: AsyncSession = Depends(get_db)
):
    profile_result = await db.execute(
        select(AppUserProfile).where(AppUserProfile.app_user_id == current_user.id)
    )
    profile = profile_result.scalars().first()

    if not profile:
        raise HTTPException(status_code=404, detail="Профиль не найден")

    # Обновляем JSONB поле settings
    current_settings = dict(profile.settings) if profile.settings else {}
    current_settings["locations"] = payload.locations
    current_settings["prehab_flags"] = payload.prehab_flags

    # === ДОБАВЛЯЕМ СОХРАНЕНИЕ ===
    if payload.effort_display_mode is not None:
        current_settings["effort_display_mode"] = payload.effort_display_mode

    profile.settings = current_settings
    await db.commit()

    return {"status": "ok", "settings": profile.settings}


@router.patch("/profile/onboarding")
async def update_profile_onboarding(
        payload: OnboardingWidgetRequest,
        current_user: AppUser = Depends(get_current_app_user),
        db: AsyncSession = Depends(get_db)
):
    # 1. Достаем профиль (с ленивым созданием)
    profile_result = await db.execute(
        select(AppUserProfile).where(AppUserProfile.app_user_id == current_user.id)
    )
    profile = profile_result.scalars().first()

    if not profile:
        profile = AppUserProfile(app_user_id=current_user.id)
        db.add(profile)

    # 2. Обновляем биометрию (Виджет 1), если поля пришли в запросе
    if payload.gender is not None:
        profile.gender = payload.gender
    if payload.experience_level is not None:
        profile.experience_level = payload.experience_level
    if payload.training_frequency is not None:
        profile.training_frequency = payload.training_frequency

    # Если ты добавил microcycle_length в БД, раскомментируй:
    # if hasattr(payload, 'microcycle_length') and payload.microcycle_length:
    #     profile.microcycle_length = payload.microcycle_length

    # Активируем PRO Mode для продвинутых
    if profile.experience_level == "advanced":
        profile.pro_mode_enabled = True

    # 3. Достаем текущие фокусные мышцы (чтобы не затереть их при сохранении Виджета 1)
    current_focus = []
    if profile.volume_budget and "meta" in profile.volume_budget:
        current_focus = profile.volume_budget["meta"].get("focus_muscles", [])

    # Если фронт прислал новые фокусы (Виджет 2) - берем их, иначе старые
    focus_muscles = payload.focus_muscles if payload.focus_muscles is not None else current_focus

    # 4. Прогоняем данные через Калькулятор
    budget_obj = calculate_volume_budget(
        experience_level=profile.experience_level or "beginner",
        focus_muscles=focus_muscles,
        # microcycle_length=getattr(profile, 'microcycle_length', 7)
    )

    # Сохраняем свежую матрицу в JSONB
    profile.volume_budget = budget_obj.model_dump()

    await db.commit()

    # Возвращаем рассчитанный бюджет
    return budget_obj


@router.put("/profile/budget")
async def update_custom_budget(
        payload: VolumeBudget,
        current_user: AppUser = Depends(get_current_app_user),
        db: AsyncSession = Depends(get_db)
):
    """Эндпоинт для сохранения ручных настроек ползунков из Виджета 2 (Advanced)"""
    profile_result = await db.execute(
        select(AppUserProfile).where(AppUserProfile.app_user_id == current_user.id)
    )
    profile = profile_result.scalars().first()

    if not profile:
        raise HTTPException(status_code=404, detail="Профиль не найден")

    # Просто перезаписываем JSONB тем, что накрутил пользователь
    profile.volume_budget = payload.model_dump()
    await db.commit()

    return profile.volume_budget