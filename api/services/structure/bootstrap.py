"""Заведение структуры пользователя (P1-03 часть 1, §5.5).

Единственный файл модуля structure/, ходящий в БД: остальные — чистые данные
и алгоритмы.

Пресеты применяются созданием ЛИЧНЫХ копий, а не ссылкой на системную строку
(решение 3): копия правится и удаляется существующими эндпоинтами без единой
правки роутеров и без миграции.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.services.models import (
    AppUserMesocycle,
    AppUserMicrocycle,
    AppUserProfile,
    Mesocycle,
    MesocyclePhase,
    SplitBlueprint,
    SplitDaySlot,
    UserSplit,
)
from api.services.structure import mesocycle_presets as meso_presets
from api.services.structure import microcycle_profiles as micro_profiles


async def _active_split_slots(
    session: AsyncSession, app_user_id: int
) -> list[micro_profiles.SlotView] | None:
    """Слоты активного сплита в порядке day_order, либо None."""
    active = (await session.execute(
        select(UserSplit).where(
            UserSplit.app_user_id == app_user_id, UserSplit.is_active.is_(True),
        )
    )).scalars().first()
    if active is None:
        return None

    blueprint = (await session.execute(
        select(SplitBlueprint)
        .where(SplitBlueprint.id == active.blueprint_id)
        .options(selectinload(SplitBlueprint.slots).selectinload(SplitDaySlot.day))
    )).scalars().first()
    if blueprint is None or not blueprint.slots:
        return None

    return [
        micro_profiles.SlotView(template_type=slot.day.template_type.value)
        for slot in sorted(blueprint.slots, key=lambda s: s.day_order)
    ]


async def _experience_level(session: AsyncSession, app_user_id: int) -> str:
    profile = (await session.execute(
        select(AppUserProfile).where(AppUserProfile.app_user_id == app_user_id)
    )).scalars().first()
    return (getattr(profile, "experience_level", None) or "beginner").strip().lower()


async def ensure_structure(session: AsyncSession, app_user_id: int) -> dict:
    """Создать недостающие копии пресетов и активировать по одной.

    Идемпотентна: повторный вызов ничего не создаёт и не переключает активные.
    Не коммитит — это забота вызывающей стороны, как и в остальном модуле.
    """
    slots = await _active_split_slots(session, app_user_id)
    if slots is None:
        # Длину микроцикла неоткуда взять — заводить структуру нельзя.
        return {"mesocycles_created": 0, "microcycles_created": 0, "activated": False}

    length = len(slots)
    level = await _experience_level(session, app_user_id)
    beginner = level == "beginner"

    # --- Мезоциклы ---
    existing_meso = {
        row.code: row
        for row in (await session.execute(
            select(Mesocycle).where(Mesocycle.author_id == app_user_id)
        )).scalars().all()
    }
    mesocycles_created = 0
    for preset in meso_presets.MESOCYCLE_PRESETS:
        if preset.code in existing_meso:
            continue
        meso = Mesocycle(
            author_id=app_user_id,
            name=preset.name,
            code=preset.code,
            description=preset.description,
            phases_in_cycle=len(preset.tiers),
        )
        session.add(meso)
        await session.flush()
        for number, tier in enumerate(preset.tiers, start=1):
            session.add(MesocyclePhase(
                mesocycle_id=meso.id,
                phase_number=number,
                name=meso_presets.phase_name(tier),
                effort_tier=tier,
            ))
        existing_meso[preset.code] = meso
        mesocycles_created += 1

    # --- Микроциклы ---
    existing_micro = {
        row.name: row
        for row in (await session.execute(
            select(AppUserMicrocycle).where(
                AppUserMicrocycle.app_user_id == app_user_id
            )
        )).scalars().all()
    }
    microcycles_created = 0
    for profile in micro_profiles.MICROCYCLE_PROFILES:
        if profile.name in existing_micro:
            continue
        micro = AppUserMicrocycle(
            app_user_id=app_user_id,
            name=profile.name,
            length_days=length,
            days_mapping=micro_profiles.build_days_mapping(profile.code, slots),
        )
        session.add(micro)
        await session.flush()
        existing_micro[profile.name] = micro
        microcycles_created += 1

    # --- Активация ---
    has_active_meso = (await session.execute(
        select(AppUserMesocycle).where(
            AppUserMesocycle.app_user_id == app_user_id,
            AppUserMesocycle.is_active.is_(True),
        )
    )).scalars().first() is not None

    if not has_active_meso:
        code = (
            meso_presets.DEFAULT_FOR_BEGINNER if beginner
            else meso_presets.DEFAULT_FOR_EXPERIENCED
        )
        session.add(AppUserMesocycle(
            app_user_id=app_user_id,
            mesocycle_id=existing_meso[code].id,
            is_active=True,
            microcycle_length=length,
            # Без номера фазы calculate_exercise_recommendation не найдёт
            # effort_tier и молча откатится на medium.
            current_phase=1,
        ))

    has_active_micro = any(row.is_active for row in existing_micro.values())
    if not has_active_micro:
        code = (
            micro_profiles.DEFAULT_FOR_BEGINNER if beginner
            else micro_profiles.DEFAULT_FOR_EXPERIENCED
        )
        existing_micro[micro_profiles.profile_by_code(code).name].is_active = True

    return {
        "mesocycles_created": mesocycles_created,
        "microcycles_created": microcycles_created,
        "activated": True,
    }
