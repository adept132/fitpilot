import asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.heuristics import HeuristicsEngine
from app.database import engine
from api.services.models import Exercise


async def run_auto_tagger():
    # Открываем асинхронную сессию
    async with AsyncSession(engine) as session:
        # 1. Асинхронно достаем все упражнения
        result = await session.execute(select(Exercise))
        exercises = result.scalars().all()

        updated_count = 0

        # 2. Прогоняем через эвристику
        for ex in exercises:
            tags = HeuristicsEngine.classify_exercise(ex.name, ex.main_muscle_group)

            ex.action = tags["action"]
            ex.vector = tags["vector"]
            ex.laterality = tags["laterality"]
            updated_count += 1

        # 3. Асинхронно сохраняем в базу
        await session.commit()
        print(f"✅ Успешно автотегировано {updated_count} упражнений.")


if __name__ == "__main__":
    # Запускаем асинхронную функцию через стандартный event loop
    asyncio.run(run_auto_tagger())