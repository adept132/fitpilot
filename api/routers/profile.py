from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from api.schemas.profile import ProfileResponse, UpdateProfileRequest
from api.schemas.оnboarding import UpdateSettingsRequest, VolumeBudget, OnboardingWidgetRequest
from api.services.app_user_service import get_current_app_user
from api.services.models import AppUser, AppUserProfile, UserAnthropometry
from api.services.progression.resolve import KNOWN_SCHEMES
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

    # Обновляем JSONB поле settings (частично — только переданные поля)
    current_settings = dict(profile.settings) if profile.settings else {}
    if payload.locations is not None:
        current_settings["locations"] = payload.locations
    if payload.prehab_flags is not None:
        current_settings["prehab_flags"] = payload.prehab_flags

    # === ДОБАВЛЯЕМ СОХРАНЕНИЕ ===
    if payload.effort_display_mode is not None:
        current_settings["effort_display_mode"] = payload.effort_display_mode

    if payload.progression_factor is not None:
        current_settings["progression_factor"] = payload.progression_factor

    if payload.weight_unit is not None:
        current_settings["weight_unit"] = payload.weight_unit

    if payload.weight_steps is not None:
        merged_steps = dict(current_settings.get("weight_steps") or {})
        merged_steps.update(payload.weight_steps)
        current_settings["weight_steps"] = merged_steps

    if payload.plate_config_kg is not None:
        current_settings["plate_config_kg"] = payload.plate_config_kg
    if payload.plate_config_lbs is not None:
        current_settings["plate_config_lbs"] = payload.plate_config_lbs

    # P0-06: ручной выбор схемы прогрессии по упражнениям
    # (settings["progression"]["overrides"][exercise_id] = scheme).
    # Мусорное имя схемы молча ляжет в settings и будет тихо игнорироваться
    # движком (resolve.override_for сверяется с KNOWN_SCHEMES и на неизвестное
    # имя отдаёт None) — пользователь решит, что настроил, а ничего не
    # изменится. Отказываем сразу и явно, а не тихо игнорируем.
    #
    # null у значения — не мусор, а команда снять override для упражнения.
    # Семантика PATCH здесь — слияние (см. ниже), поэтому единственный способ
    # убрать одну запись, не трогая остальные, это явно передать null для её
    # ключа; отклонять null как «не строку» значило бы не оставить пользователю
    # рабочего способа снять override вообще.
    #
    # Значения, которые не строка и не null (числа, bool, вложенные объекты),
    # синтаксически не являются именем схемы ни при каких обстоятельствах —
    # отдаём 422 сразу, до сверки с KNOWN_SCHEMES, чтобы не упасть на
    # необработанном TypeError при попытке сравнить/объединить их со строками.
    #
    # Ключ — идентификатор упражнения, JSON гарантирует, что он всегда строка;
    # нечисловой ключ (например, "abc") просто никогда не совпадёт с
    # str(exercise_id) в resolve.override_for и будет безопасно проигнорирован
    # движком — отдельно отвергать его нет смысла, он не может ничего сломать.
    #
    # Остаточная находка ревью: payload.progression типизирован как
    # Dict[str, Any] в UpdateSettingsRequest, поэтому FastAPI/pydantic сам
    # отвергнет progression-не-словарь (list/str/int) 422-м до входа сюда.
    # Но overrides — Any внутри этого словаря, никакой схемной проверки не
    # проходит, и .get("overrides") может вернуть что угодно: список, строку,
    # число. Тогда `overrides.items()` падает необработанным AttributeError —
    # клиент получает 500 вместо 422. Проверяем оба уровня явно и здесь, а не
    # полагаемся только на pydantic — типы в UpdateSettingsRequest могут
    # ослабнуть в будущем незаметно для этого обработчика.
    if payload.progression is not None:
        if not isinstance(payload.progression, dict):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Поле progression должно быть объектом (например "
                    '{"overrides": {...}}), получено: '
                    f"{type(payload.progression).__name__}"
                ),
            )

        raw_overrides = payload.progression.get("overrides")
        if raw_overrides is None:
            overrides: dict = {}
        elif not isinstance(raw_overrides, dict):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Поле progression.overrides должно быть объектом "
                    "(ключ — id упражнения строкой, значение — имя схемы "
                    f"или null), получено: {type(raw_overrides).__name__}"
                ),
            )
        else:
            overrides = raw_overrides

        unknown: list[str] = []
        for exercise_key, scheme in overrides.items():
            if scheme is None:
                continue  # снятие override — обрабатывается отдельно ниже
            if not isinstance(scheme, str):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Схема прогрессии для упражнения {exercise_key} должна быть "
                        f"строкой (или null, чтобы снять override), получено: "
                        f"{type(scheme).__name__}"
                    ),
                )
            if scheme not in KNOWN_SCHEMES:
                unknown.append(scheme)
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"Неизвестные схемы прогрессии: {', '.join(sorted(set(unknown)))}",
            )

        # Мёрджим overrides по exercise_id, а не заменяем весь словарь целиком —
        # иначе сохранение схемы для упражнения A стирало бы ранее сохранённую
        # схему для упражнения B (тот же приём уже применён выше для weight_steps).
        # null для ключа удаляет запись из смёрдженного словаря — это и есть
        # способ снять override, не трогая остальные.
        current_progression = dict(current_settings.get("progression") or {})
        merged_overrides = dict(current_progression.get("overrides") or {})
        for exercise_key, scheme in overrides.items():
            if scheme is None:
                merged_overrides.pop(exercise_key, None)
            else:
                merged_overrides[exercise_key] = scheme
        current_progression["overrides"] = merged_overrides
        current_settings["progression"] = current_progression

    # P0-07: тумблер чек-ина. Валидируем явно — settings это свободный
    # JSONB, и мусор оттуда позже вылезет 500-й, а не 422-й.
    if payload.readiness is not None:
        if not isinstance(payload.readiness, dict):
            raise HTTPException(status_code=422, detail="readiness должен быть объектом")
        block = dict(current_settings.get("readiness") or {})
        if "checkin_enabled" in payload.readiness:
            value = payload.readiness["checkin_enabled"]
            if not isinstance(value, bool):
                raise HTTPException(
                    status_code=422, detail="checkin_enabled должен быть булевым"
                )
            block["checkin_enabled"] = value
        current_settings["readiness"] = block

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
