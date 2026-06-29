from datetime import date, timedelta
from typing import List, Optional
import uuid

from api.services.scheduling import SchedulingMode, WorkoutStatus, MesocyclePhase


class MesocycleEngine:
    @staticmethod
    def generate_interstellar_queue(
            mesocycle_id: uuid.UUID,
            blueprint_day_ids: List[uuid.UUID],  # Упорядоченный массив ID дней из сплита
            start_date: date,
            mode: SchedulingMode,
            allowed_weekdays: Optional[List[int]] = None,
            rolling_pattern: Optional[List[int]] = None,
            blackout_weekdays: Optional[List[int]] = None,
            mesocycle_length_days: int = 28
    ) -> List[dict]:
        """
        Разворачивает абстрактную очередь тренировок на реальную сетку календаря.
        Возвращает список словарей для bulk_insert в таблицу scheduled_workouts.
        """
        if not blueprint_day_ids:
            raise ValueError("Сплит-шаблон не содержит дней для планирования")

        scheduled_workouts = []
        current_date = start_date
        end_date = start_date + timedelta(days=mesocycle_length_days)

        workout_index = 0  # Указатель на текущую тренировку в очереди сплита
        workout_order = 1  # Сквозной порядковый номер для реальности

        # Переменные для отслеживания плавающего ритма (Rolling Pattern)
        active_rhythm_days = 0
        pattern_work_limit = rolling_pattern[0] if rolling_pattern else 1
        pattern_total_cycle = sum(rolling_pattern) if rolling_pattern else 1

        blackout_set = set(blackout_weekdays) if blackout_weekdays else set()
        allowed_set = set(allowed_weekdays) if allowed_weekdays else set()

        while current_date < end_date:
            weekday = current_date.weekday()  # 0 = ПН, 6 = ВС

            # Правило 1: Черные дни (Blackout) имеют абсолютный приоритет заморозки
            if weekday in blackout_set:
                current_date += timedelta(days=1)
                continue

            if mode == SchedulingMode.FIXED_WEEKDAYS:
                # Жесткий режим: проверяем, подходит ли день недели
                if weekday in allowed_set:
                    day_id = blueprint_day_ids[workout_index % len(blueprint_day_ids)]
                    scheduled_workouts.append({
                        "id": uuid.uuid4(),
                        "mesocycle_id": mesocycle_id,
                        "blueprint_day_id": day_id,
                        "scheduled_date": current_date,
                        "status": WorkoutStatus.pending,
                        "mesocycle_phase": MesocyclePhase.accumulation,
                        "workout_order": workout_order
                    })
                    workout_index += 1
                    workout_order += 1

            elif mode == SchedulingMode.ROLLING_PATTERN:
                # Плавающий ритм: вычисляем, где мы внутри цикла (например, внутри 3 дней ритма 2/1)
                step_in_cycle = active_rhythm_days % pattern_total_cycle

                if step_in_cycle < pattern_work_limit:
                    # Это тренировочный день по ритму
                    day_id = blueprint_day_ids[workout_index % len(blueprint_day_ids)]
                    scheduled_workouts.append({
                        "id": uuid.uuid4(),
                        "mesocycle_id": mesocycle_id,
                        "blueprint_day_id": day_id,
                        "scheduled_date": current_date,
                        "status": WorkoutStatus.pending,
                        "mesocycle_phase": MesocyclePhase.accumulation,
                        "workout_order": workout_order
                    })
                    workout_index += 1
                    workout_order += 1
                else:
                    # Это день отдыха по ритму — в базу запись не идет, просто пропускаем дату
                    pass

                # Инкрементируем прожитые дни ритма (blackout дни сюда не попадают)
                active_rhythm_days += 1

            current_date += timedelta(days=1)

        return scheduled_workouts