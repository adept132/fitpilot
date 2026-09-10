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
    UserCalendarDay, AppUserMicrocycle,
    UserSplit,
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

            # Equal-score plans are versions of the same day prescription in
            # practice. Prefer the newest one so confirming a regenerated plan
            # immediately rebinds the calendar to the just-created version
            # instead of keeping an older generated draft.
            if score > max_score or (
                score == max_score
                and (best_plan_id is None or plan.id > best_plan_id)
            ):
                max_score = score
                best_plan_id = plan.id

        return best_plan_id

    @staticmethod
    async def rebind_plans(session: AsyncSession, app_user_id: int, from_date: date) -> int:
        """Re-score user plans onto future non-rest calendar days (fills plan_id)."""
        plans_res = await session.execute(
            select(WorkoutPlan).where(WorkoutPlan.app_user_id == app_user_id)
        )
        user_plans = list(plans_res.scalars().all())

        days_res = await session.execute(
            select(UserCalendarDay).where(
                UserCalendarDay.app_user_id == app_user_id,
                UserCalendarDay.target_date >= from_date,
                UserCalendarDay.is_rest_day == False,  # noqa: E712
            )
        )
        days = list(days_res.scalars().all())

        updated = 0
        for day in days:
            new_id = SchedulingEngine._score_and_find_best_plan(
                plans=user_plans, target_day_name=day.day_tag or "",
                meso_tag=day.meso_tag or "medium", micro_tag=day.micro_tag or "adaptive",
            )
            if new_id is not None and new_id != day.plan_id:
                day.plan_id = new_id
                updated += 1
        await session.commit()
        return updated

    @staticmethod
    async def generate_block_days(
            session: AsyncSession,
            app_user_id: int,
            block,
            from_date: date,
            until_date: date,
    ) -> int:
        """Записать дни календаря для блока, беря фазу ИЗ СНИМКА блока.

        Единственный источник meso_tag: раньше их было три (модульная формула
        в генераторе, зажим в превью и ручной current_phase), и они расходились
        начиная с границы первого блока.

        Дни с target_date < from_date не трогаются вовсе — вызывающая сторона
        обязана передавать from_date не раньше «завтра» при перегенерации.
        """
        from api.services.periodization.position import position
        from api.services.periodization.repository import block_state

        state = block_state(block)

        blueprint = None
        slots_queue: List[SplitDaySlot] = []
        active_split = (await session.execute(
            select(UserSplit).where(
                UserSplit.app_user_id == app_user_id,
                UserSplit.is_active == True  # noqa: E712
            )
        )).scalar_one_or_none()

        if active_split:
            blueprint = (await session.execute(
                select(SplitBlueprint)
                .where(SplitBlueprint.id == active_split.blueprint_id)
                .options(
                    selectinload(SplitBlueprint.slots)
                    .selectinload(SplitDaySlot.day)
                    .selectinload(DayBlueprint.muscle_targets)
                )
            )).scalar_one_or_none()
            if blueprint and blueprint.slots:
                slots_queue = sorted(blueprint.slots, key=lambda s: s.day_order)

        if not slots_queue:
            return 0

        blackout_weekdays = []
        if active_split.selected_plans and "blackout_weekdays" in active_split.selected_plans:
            blackout_weekdays = active_split.selected_plans["blackout_weekdays"]

        user_micro = (await session.execute(
            select(AppUserMicrocycle).where(
                AppUserMicrocycle.app_user_id == app_user_id,
                AppUserMicrocycle.is_active == True  # noqa: E712
            )
        )).scalar_one_or_none()
        days_mapping = user_micro.days_mapping if user_micro else {}
        micro_length = user_micro.length_days if user_micro else len(slots_queue)

        plans = list((await session.execute(
            select(WorkoutPlan).where(WorkoutPlan.app_user_id == app_user_id)
        )).scalars().all())

        # P0-09: дни, пережившие выборочную перегенерацию (_wipe_future_calendar
        # оставляет дни с фактом/принятой правкой), уже занимают часть диапазона
        # [from_date, until_date]. Ниже эти даты пропускаются при материализации,
        # НО не выводятся из-под учёта счётчиков (total_workout_days_passed,
        # позиция в сплите) — иначе раскладка сплита разъехалась бы для всех
        # дней ПОСЛЕ пропущенной даты. Опрос сделан один раз до цикла, а не
        # индивидуальным SELECT на каждую дату.
        existing_dates = set((await session.execute(
            select(UserCalendarDay.target_date).where(
                UserCalendarDay.app_user_id == app_user_id,
                UserCalendarDay.target_date >= from_date,
                UserCalendarDay.target_date <= until_date,
            )
        )).scalars().all())

        # Счётчик отработанных дней сплита ведём от НАЧАЛА блока, иначе при
        # перегенерации с середины сплит начнётся заново с первого дня.
        current_date = block.start_date
        total_workout_days_passed = 0
        created = 0

        while current_date <= until_date:
            weekday = current_date.weekday()
            slot = slots_queue[total_workout_days_passed % len(slots_queue)]
            day_bp = slot.day
            micro_day_num = (total_workout_days_passed % micro_length) + 1

            targets = [m.muscle_group_id for m in day_bp.muscle_targets] if day_bp.muscle_targets else []
            is_rest_in_split = day_bp.template_type in ["active_rest", "rest"] or len(targets) == 0
            is_banned = weekday in blackout_weekdays

            pos = position(state, current_date)
            micro_tag_calc = days_mapping.get(str(micro_day_num), {}).get("type", "adaptive")

            if is_banned:
                is_rest_day = True
                if is_rest_in_split:
                    total_workout_days_passed += 1
            else:
                is_rest_day = is_rest_in_split
                # P0-08 ревью Задачи 7, Находка 4 (Minor): счётчик продвигается
                # для ЛЮБОГО не-блэкаутного дня, включая день отдыха внутри
                # сплита — так же, как ниже в launch_and_unroll_plan. Старая
                # карусельная ветка ensure_horizon продвигает счётчик только
                # для НЕ-отдыха; расхождение унаследовано из старого кода (не
                # этой задачей) и сознательно не трогается здесь.
                total_workout_days_passed += 1

            # P0-09: current_date not in existing_dates — единственное
            # дополнительное условие. Оно ТОЛЬКО подавляет вставку строки;
            # ветки выше (weekday/slot/pos/total_workout_days_passed) уже
            # отработали в этой итерации безусловно, так что пропуск даты
            # здесь не сдвигает раскладку сплита на последующих днях.
            if current_date >= from_date and current_date not in existing_dates:
                plan_id_to_save = None
                if not is_rest_day:
                    plan_id_to_save = SchedulingEngine._score_and_find_best_plan(
                        plans=plans, target_day_name=day_bp.name,
                        meso_tag=pos.effort_tier, micro_tag=micro_tag_calc,
                    )
                session.add(UserCalendarDay(
                    app_user_id=app_user_id,
                    target_date=current_date,
                    block_id=block.id,
                    user_mesocycle_id=block.user_mesocycle_id,
                    mesocycle_phase_number=pos.phase_number,
                    user_microcycle_id=block.user_microcycle_id,
                    microcycle_day_number=micro_day_num,
                    day_tag=day_bp.name,
                    micro_tag=micro_tag_calc,
                    meso_tag=pos.effort_tier,
                    plan_id=plan_id_to_save,
                    is_rest_day=is_rest_day,
                    is_blackout=is_banned,
                    status="planned",
                ))
                created += 1

            current_date += timedelta(days=1)

        await session.commit()
        return created

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

        # P0-08 ревью Задачи 7, Находка 2: если периодизация настроена,
        # единственный источник фазы для календаря — снимок активного блока
        # (как и в ensure_horizon/generate_block_days), а не живой шаблон
        # мезоцикла, который читает старый цикл ниже. Без этой ветки
        # POST /splits/.../launch пересобирал бы до 90 дней календаря старым
        # путём и не проставлял бы block_id вовсе — ровно та рассинхронизация
        # между launch и ensure_horizon, ради устранения которой затевалась
        # вся задача. Если блока нет (периодизация не настроена) — ниже
        # работает прежний код без изменений.
        #
        # Порядок вызовов, который сложится в Задаче 14 (закрытие блока при
        # смене сплита ПЕРЕД вызовом этой функции), здесь ничего не меняет:
        # ensure_active_block просто увидит уже актуальный на момент вызова
        # блок — старый или новый, без разницы.
        from api.services.periodization.repository import ensure_active_block

        # P0-08, повторное ревью Задачи 7, Находка 3: "пора ли закрывать
        # текущий блок" обязано решаться по РЕАЛЬНОМУ сегодня, а не по
        # клиентскому start_date запуска сплита. start_date не валидируется
        # и может быть в будущем (пользователь планирует запуск наперёд) —
        # если передать её сюда как today, ещё живой текущий блок закрылся
        # бы досрочно и задним числом: exit_state посчитался бы по неполным
        # данным, а новый блок получил бы start_date раньше настоящего
        # сегодня. Диапазон генерации дней ниже (from_date=start_date,
        # until_date=block.planned_end_date) по-прежнему определяется
        # клиентским start_date — меняется только вход в решение о переходе.
        block = await ensure_active_block(session, app_user_id, date.today())
        if block is not None:
            await SchedulingEngine.generate_block_days(
                session, app_user_id, block,
                from_date=start_date,
                until_date=block.planned_end_date,
            )
            return

        slots_queue = sorted(blueprint.slots, key=lambda s: s.day_order)
        split_length = len(slots_queue)

        # P0-09: та же дыра с дублирующейся строкой, что была в
        # generate_block_days (см. её комментарий выше), открыта и здесь —
        # эта ветка живёт, когда у пользователя не настроена периодизация
        # (ensure_active_block вернула None выше), и splits.py всё равно
        # спускает сюда даты, уже занятые уцелевшими днями (attach_session_to_day
        # пишет status="completed" независимо от периодизации). Опрос сделан
        # один раз до цикла, как и там.
        existing_dates = set((await session.execute(
            select(UserCalendarDay.target_date).where(
                UserCalendarDay.app_user_id == app_user_id,
                UserCalendarDay.target_date >= start_date,
                UserCalendarDay.target_date <= start_date + timedelta(days=preview_length_days - 1),
            )
        )).scalars().all())

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

            # P0-09: current_date not in existing_dates — единственное
            # дополнительное условие, зеркалит generate_block_days. Все
            # вычисления и счётчики выше (weekday/slot/pos/
            # total_workout_days_passed) уже отработали безусловно в этой
            # итерации, так что пропуск вставки здесь не сдвигает раскладку
            # сплита на последующих днях. НЕ continue — иначе current_date
            # не продвинулся бы и цикл завис.
            if current_date not in existing_dates:
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

        # P0-08: если у пользователя есть активный блок, горизонт достраивается
        # ИЗ СНИМКА БЛОКА. Старая карусельная ветка ниже остаётся для тех, у
        # кого периодизация не настроена (блок в этом случае не создаётся).
        from api.services.periodization.repository import ensure_active_block

        block = await ensure_active_block(session, app_user_id, today)
        if block is not None:
            await SchedulingEngine.generate_block_days(
                session, app_user_id, block,
                from_date=max_date + timedelta(days=1),
                until_date=min(block.planned_end_date, today + timedelta(days=horizon_days)),
            )
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
