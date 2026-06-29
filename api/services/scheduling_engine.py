import uuid
from datetime import date, timedelta
from typing import List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.services.models import (
    AppUserMesocycle,
    Mesocycle,
    SplitBlueprint,
    SplitDaySlot,
    DayBlueprint,
    WorkoutPlan,
    UserCalendarDay, AppUserMicrocycle
)


class SchedulingEngine:

    @staticmethod
    def _score_and_find_best_plan(
            plans: List[WorkoutPlan],
            target_day_name: str,
            meso_tag: str,
            micro_tag: str
    ) -> Optional[int]:
        """
        Ищет лучший план. Если нет точного совпадения, ищет по подстроке имени (например, 'push' == 'Push Day').
        """
        safe_target = target_day_name.lower()

        # Фильтруем планы, пытаясь найти совпадение тега плана и имени дня из блюпринта
        valid_plans = [p for p in plans if p.day_tag.lower() in safe_target or safe_target in p.day_tag.lower()]

        if not valid_plans:
            return None

        best_plan_id = None
        max_score = -1

        for plan in valid_plans:
            score = 0

            # 1. Оцениваем фазу мезоцикла
            if plan.meso_tag == meso_tag:
                score += 10
            elif plan.meso_tag == "adaptive":
                score += 5
            elif meso_tag == "deload" and plan.meso_tag != "deload":
                score -= 20

            # 2. Оцениваем микро-тег
            if plan.micro_tag == micro_tag:
                score += 1
            elif plan.micro_tag == "adaptive":
                score += 0.5

            if score > max_score:
                max_score = score
                best_plan_id = plan.id

        return best_plan_id

    @staticmethod
    async def launch_and_unroll_plan(
            session: AsyncSession,
            app_user_id: int,
            split_blueprint_id: uuid.UUID,
            start_date: date,
            blackout_weekdays: List[int],
            user_mesocycle_id: Optional[int] = None,
            preview_length_days: int = 90
    ) -> None:

        # 1. Сплит (Базовая структура)
        split_stmt = (
            select(SplitBlueprint)
            .where(SplitBlueprint.id == split_blueprint_id)
            .options(
                selectinload(SplitBlueprint.slots)
                .selectinload(SplitDaySlot.day)
                .selectinload(DayBlueprint.muscle_targets)
            )
        )
        split_res = await session.execute(split_stmt)
        blueprint = split_res.scalar_one_or_none()
        if not blueprint or not blueprint.slots:
            raise ValueError("Сплит пуст или не найден")

        slots_queue = sorted(blueprint.slots, key=lambda s: s.day_order)
        split_length = len(slots_queue)

        # 2. Микроцикл (Настройки тяжести дней)
        micro_stmt = (
            select(AppUserMicrocycle)
            .where(
                AppUserMicrocycle.app_user_id == app_user_id,
                AppUserMicrocycle.is_active == True
            )
        )
        micro_res = await session.execute(micro_stmt)
        user_micro = micro_res.scalar_one_or_none()

        # Длина микроцикла: если ее нет, она равна длине сплита
        micro_length = user_micro.length_days if user_micro else split_length
        days_mapping = user_micro.days_mapping if user_micro else {}

        # 3. Мезоцикл (Фазы нагрузки)
        phases_list = []
        # Длина фазы по умолчанию СТРОГО равна длине микроцикла!
        days_per_phase = micro_length

        if user_mesocycle_id:
            meso_stmt = select(AppUserMesocycle).where(AppUserMesocycle.id == user_mesocycle_id)
            meso_res = await session.execute(meso_stmt)
            user_meso = meso_res.scalar_one_or_none()
            if user_meso:
                strategy_stmt = select(Mesocycle).where(Mesocycle.id == user_meso.mesocycle_id).options(
                    selectinload(Mesocycle.phases))
                strategy_res = await session.execute(strategy_stmt)
                strategy = strategy_res.scalar_one()
                phases_list = sorted(strategy.phases, key=lambda p: p.phase_number)

        # 4. Загрузка планов
        plans_stmt = select(WorkoutPlan).where(WorkoutPlan.app_user_id == app_user_id)
        plans_res = await session.execute(plans_stmt)
        user_plans = list(plans_res.scalars().all())

        current_date = start_date
        total_workout_days_passed = 0

        # 5. Главный цикл генерации
        for _ in range(preview_length_days):
            weekday = current_date.weekday()

            # --- ВЫЧИСЛЕНИЕ КООРДИНАТ ПО ЕДИНОМУ СЧЕТЧИКУ ---
            # Индекс слота в сплите (0, 1, 2...)
            slot_index = total_workout_days_passed % split_length
            current_slot = slots_queue[slot_index]
            day_bp = current_slot.day

            # День микроцикла (1, 2, 3... 6)
            micro_day_num = (total_workout_days_passed % micro_length) + 1

            target_names = [m.muscle_group_id for m in day_bp.muscle_targets] if day_bp.muscle_targets else []
            is_rest_in_split = day_bp.template_type in ["active_rest", "rest"] or len(target_names) == 0
            is_banned = weekday in blackout_weekdays

            # Фаза мезоцикла
            meso_tag_calc = "medium"
            phase_number = None
            if phases_list:
                current_phase_idx = (total_workout_days_passed // days_per_phase) % len(phases_list)
                current_phase = phases_list[current_phase_idx]
                meso_tag_calc = current_phase.effort_tier
                phase_number = current_phase.phase_number

            # Тег микроцикла (hard, easy, recovery...)
            day_config = days_mapping.get(str(micro_day_num), {})
            micro_tag_calc = day_config.get("type", "adaptive")

            plan_id_to_save = None

            # Логика продвижения календаря
            if is_banned:
                if is_rest_in_split:
                    is_rest_day = True
                    total_workout_days_passed += 1  # День отдыха потрачен с пользой
                else:
                    is_rest_day = True  # Ждем окончания блэкаута, счетчик стоит
            else:
                is_rest_day = is_rest_in_split
                if not is_rest_day:
                    plan_id_to_save = SchedulingEngine._score_and_find_best_plan(
                        plans=user_plans,
                        target_day_name=day_bp.name,
                        meso_tag=meso_tag_calc,
                        micro_tag=micro_tag_calc
                    )

                total_workout_days_passed += 1  # День сплита отработан

            cal_day = UserCalendarDay(
                app_user_id=app_user_id,
                target_date=current_date,
                user_mesocycle_id=user_mesocycle_id if user_mesocycle_id else None,
                mesocycle_phase_number=phase_number,
                user_microcycle_id=user_micro.id if user_micro else None,
                microcycle_day_number=micro_day_num,  # <--- ПИШЕМ РЕАЛЬНЫЙ ДЕНЬ МИКРОЦИКЛА
                day_tag=day_bp.name,
                micro_tag=micro_tag_calc,
                meso_tag=meso_tag_calc,
                plan_id=plan_id_to_save,
                is_rest_day=is_rest_day,
                is_blackout=is_banned,
                status="planned"
            )
            session.add(cal_day)
            current_date += timedelta(days=1)

        await session.commit()

    @staticmethod
    async def ensure_horizon(
            session: AsyncSession,
            app_user_id: int,
            today: date,
            horizon_days: int = 90
    ) -> None:
        """
        Проверяет, достаточно ли дней сгенерировано в календаре.
        Если до конца расписания осталось меньше 30 дней, достраивает его до горизонта.
        """
        from sqlalchemy import func
        from datetime import datetime

        # 1. Узнаем, когда заканчивается текущее расписание в БД
        max_date_stmt = select(func.max(UserCalendarDay.target_date)).where(
            UserCalendarDay.app_user_id == app_user_id
        )
        max_date = (await session.execute(max_date_stmt)).scalar()

        if not max_date:
            return  # Календаря нет, достраивать нечего

        # Если впереди еще есть запас (больше 30 дней), экономим ресурсы и ничего не делаем
        if (max_date - today).days >= 30:
            return

        # 2. Ищем стартовую точку и настройки в активном UserSplit
        from api.services.models import UserSplit
        split_stmt = select(UserSplit).where(
            UserSplit.app_user_id == app_user_id,
            UserSplit.is_active == True
        )
        active_split = (await session.execute(split_stmt)).scalar_one_or_none()
        if not active_split:
            return

        start_date = active_split.start_date.date() if isinstance(active_split.start_date,
                                                                  datetime) else active_split.start_date

        # Достаем забаненные дни из JSONB
        blackout_weekdays = []
        if active_split.selected_plans and "blackout_weekdays" in active_split.selected_plans:
            blackout_weekdays = active_split.selected_plans["blackout_weekdays"]

        # 3. Загружаем Сплит
        blueprint_stmt = (
            select(SplitBlueprint)
            .where(SplitBlueprint.id == active_split.blueprint_id)
            .options(
                selectinload(SplitBlueprint.slots)
                .selectinload(SplitDaySlot.day)
                .selectinload(DayBlueprint.muscle_targets)
            )
        )
        blueprint = (await session.execute(blueprint_stmt)).scalar_one_or_none()
        if not blueprint or not blueprint.slots:
            return

        slots_queue = sorted(blueprint.slots, key=lambda s: s.day_order)
        split_length = len(slots_queue)

        # 4. Загружаем Микроцикл
        micro_stmt = select(AppUserMicrocycle).where(
            AppUserMicrocycle.app_user_id == app_user_id,
            AppUserMicrocycle.is_active == True
        )
        user_micro = (await session.execute(micro_stmt)).scalar_one_or_none()

        micro_length = user_micro.length_days if user_micro else split_length
        days_mapping = user_micro.days_mapping if user_micro else {}

        # 5. Загружаем Мезоцикл (СТРОГО с длиной фазы = длине микроцикла)
        phases_list = []
        days_per_phase = micro_length
        user_mesocycle_id = None

        meso_stmt = select(AppUserMesocycle).where(
            AppUserMesocycle.app_user_id == app_user_id,
            AppUserMesocycle.is_active == True
        )
        user_meso = (await session.execute(meso_stmt)).scalar_one_or_none()

        if user_meso:
            user_mesocycle_id = user_meso.id
            strategy_stmt = select(Mesocycle).where(Mesocycle.id == user_meso.mesocycle_id).options(
                selectinload(Mesocycle.phases))
            strategy = (await session.execute(strategy_stmt)).scalar_one()
            phases_list = sorted(strategy.phases, key=lambda p: p.phase_number)

        # 6. Загружаем планы тренировок
        plans_stmt = select(WorkoutPlan).where(WorkoutPlan.app_user_id == app_user_id)
        plans_res = await session.execute(plans_stmt)
        user_plans = list(plans_res.scalars().all())

        # --- НАСТРОЙКИ ПЕРЕМОТКИ ---
        target_end_date = today + timedelta(days=horizon_days)
        current_date = start_date
        total_workout_days_passed = 0

        # 7. Запускаем Fast-Forward карусель
        while current_date <= target_end_date:
            weekday = current_date.weekday()

            # Вычисляем координаты
            slot_index = total_workout_days_passed % split_length
            current_slot = slots_queue[slot_index]
            day_bp = current_slot.day

            micro_day_num = (total_workout_days_passed % micro_length) + 1

            target_names = [m.muscle_group_id for m in day_bp.muscle_targets] if day_bp.muscle_targets else []
            is_rest_in_split = day_bp.template_type in ["active_rest", "rest"] or len(target_names) == 0
            is_banned = weekday in blackout_weekdays

            # Фаза мезоцикла
            meso_tag_calc = "medium"
            phase_number = None
            if phases_list:
                current_phase_idx = (total_workout_days_passed // days_per_phase) % len(phases_list)
                current_phase = phases_list[current_phase_idx]
                meso_tag_calc = current_phase.effort_tier
                phase_number = current_phase.phase_number

            # Тег микроцикла
            day_config = days_mapping.get(str(micro_day_num), {})
            micro_tag_calc = day_config.get("type", "adaptive")

            # Логика продвижения календаря
            if is_banned:
                if is_rest_in_split:
                    is_rest_day = True
                    total_workout_days_passed += 1
                else:
                    is_rest_day = True
            else:
                is_rest_day = is_rest_in_split
                if not is_rest_day:
                    total_workout_days_passed += 1

            # 8. МАГИЯ ЗДЕСЬ: Скорим планы и пишем в БД ТОЛЬКО если дошли до края сгенерированного горизонта
            if current_date > max_date:
                plan_id_to_save = None

                # Тратим ресурсы на поиск плана только для не-отдыха
                if not is_rest_day:
                    plan_id_to_save = SchedulingEngine._score_and_find_best_plan(
                        plans=user_plans,
                        target_day_name=day_bp.name,
                        meso_tag=meso_tag_calc,
                        micro_tag=micro_tag_calc
                    )

                cal_day = UserCalendarDay(
                    app_user_id=app_user_id,
                    target_date=current_date,
                    user_mesocycle_id=user_mesocycle_id,
                    mesocycle_phase_number=phase_number,
                    user_microcycle_id=user_micro.id if user_micro else None,
                    microcycle_day_number=micro_day_num,
                    day_tag=day_bp.name,
                    micro_tag=micro_tag_calc,
                    meso_tag=meso_tag_calc,
                    plan_id=plan_id_to_save,
                    is_rest_day=is_rest_day,
                    is_blackout=is_banned,
                    status="planned"
                )
                session.add(cal_day)

            current_date += timedelta(days=1)

        await session.commit()