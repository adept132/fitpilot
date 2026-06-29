from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from api.services.models import AppUserProfile, SplitBlueprint, UserSplit, SplitDaySlot, DayBlueprint

class VolumeService:
    @staticmethod
    async def calculate_session_targets(
        session: AsyncSession,
        app_user_id: int,
        day_tag: str
    ) -> dict:
        # 1. Достаем профиль
        profile_stmt = select(AppUserProfile).where(AppUserProfile.app_user_id == app_user_id)
        profile_res = await session.execute(profile_stmt)
        profile = profile_res.scalar_one_or_none()

        if not profile or not profile.volume_budget:
            return {} # Если бюджета нет, отдаем пустой словарь

        volume_budget = profile.volume_budget
        constraints = volume_budget.get("constraints", {})
        weekly_targets = volume_budget.get("weekly_targets", {})
        max_session_cap = constraints.get("max_sets_per_session_per_muscle", 10)

        # 2. Ищем активный сплит
        split_stmt = (
            select(SplitBlueprint)
            .join(UserSplit, UserSplit.blueprint_id == SplitBlueprint.id)
            .where(UserSplit.app_user_id == app_user_id, UserSplit.is_active == True)
            .options(
                selectinload(SplitBlueprint.slots)
                .selectinload(SplitDaySlot.day)
                .selectinload(DayBlueprint.muscle_targets)
            )
        )
        split_res = await session.execute(split_stmt)
        blueprint = split_res.scalar_one_or_none()

        if not blueprint:
            return {}

        # 3. Считаем частоту и ищем целевой день
        muscle_frequencies = {}
        muscles_in_day = set()

        for slot in blueprint.slots:
            day = slot.day
            template_val = day.template_type.value if hasattr(day.template_type, 'value') else str(day.template_type)
            is_target_day = (day.name.lower() == day_tag.lower() or template_val.lower() == day_tag.lower())

            for target in day.muscle_targets:
                muscle = target.muscle_group_id.lower()
                muscle_frequencies[muscle] = muscle_frequencies.get(muscle, 0) + 1
                if is_target_day:
                    muscles_in_day.add(muscle)

        if not muscles_in_day:
            return {}

        targets_response = {}

        # 4. Считаем сами таргеты
        for muscle in muscles_in_day:
            muscle_data = weekly_targets.get(muscle)
            if not muscle_data:
                continue

            target_weekly_sets = muscle_data.get("target_sets", 0)
            min_floor = muscle_data.get("min_floor", 2)
            frequency = muscle_frequencies.get(muscle, 1)

            if target_weekly_sets == 0 or frequency == 0:
                continue

            raw_session_target = (target_weekly_sets * (blueprint.length_days / 7.0)) / frequency
            rounded_target = round(raw_session_target)
            final_target = max(min_floor, min(max_session_cap, rounded_target))

            targets_response[muscle] = {
                "target_sets": final_target,
                "max_session_cap": max_session_cap
            }

        return targets_response