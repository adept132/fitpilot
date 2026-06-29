# scripts/backfill_fatigue_tiers.py
import asyncio
from sqlalchemy import select
from api.deps import get_db  # Твой инжектор сессии БД
from api.services.models import Exercise
from api.services.fatigue_tiers import calculate_fatigue_tier


async def run_backfill():
    async for db in get_db():
        # Получаем все упражнения
        result = await db.execute(select(Exercise))
        exercises = result.scalars().all()

        updated_count = 0
        for ex in exercises:
            new_tier = calculate_fatigue_tier(
                category=ex.category,
                main_muscle=ex.main_muscle_group,
                secondary_muscles=ex.secondary_muscle_groups or [],
                equipment=ex.equipment_needed or []
            )

            if ex.fatigue_tier != new_tier:
                ex.fatigue_tier = new_tier
                updated_count += 1

        await db.commit()
        print(f"Успешно обновлено {updated_count} упражнений.")
        break  # Выходим после первой сессии


if __name__ == "__main__":
    asyncio.run(run_backfill())