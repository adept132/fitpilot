import asyncio
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

# ВАЖНО: Проверь пути импортов под структуру твоего проекта

from api.services.models import (
    DayBlueprint, SplitBlueprint, SplitDaySlot,
    DayMuscleTarget, DayTemplateType
)
from app.database import SessionLocal


async def seed_data():
    async with SessionLocal() as session:
        # Проверяем, есть ли уже системные сплиты, чтобы не дублировать
        result = await session.execute(select(SplitBlueprint).where(SplitBlueprint.is_system == True))
        if result.scalars().first():
            print("Системные сплиты уже существуют. Отмена.")
            return

        print("Создаем системные кубики дней (Day Blueprints)...")

        # 1. Создаем дни и их целевые мышцы
        days_data = {
            "Push": (DayTemplateType.PUSH, [
                "Грудь", "Передняя дельта", "Средняя дельта", "Трицепс"
            ]),
            "Pull": (DayTemplateType.PULL, [
                "Широчайшие", "Средняя часть спины", "Трапеции", "Задняя дельта", "Бицепс"
            ]),
            "Legs": (DayTemplateType.LEGS, [
                "Квадрицепсы", "Бицепс бедра", "Ягодичные", "Икры"
            ]),
            "Upper": (DayTemplateType.UPPER, [
                "Грудь", "Широчайшие", "Средняя часть спины", "Передняя дельта",
                "Средняя дельта", "Задняя дельта", "Бицепс", "Трицепс"
            ]),
            "Lower": (DayTemplateType.LOWER, [
                "Квадрицепсы", "Бицепс бедра", "Ягодичные", "Икры", "Пресс"
            ]),
            "Arms & Shoulders": (DayTemplateType.ARMS_SHOULDERS, [
                "Бицепс", "Трицепс", "Средняя дельта", "Передняя дельта", "Задняя дельта"
            ]),
            "Full Body": (DayTemplateType.FULL_BODY, [
                "Грудь", "Широчайшие", "Квадрицепсы", "Бицепс бедра",
                "Средняя дельта", "Бицепс", "Трицепс"
            ]),
            "Rest": (DayTemplateType.ACTIVE_REST, [])
        }

        created_days = {}
        for name, (template_type, muscles) in days_data.items():
            day = DayBlueprint(
                name=name,
                template_type=template_type,
                is_system=True,
                author_id=None
            )
            for muscle in muscles:
                day.muscle_targets.append(DayMuscleTarget(muscle_group_id=muscle))

            session.add(day)
            created_days[name] = day

        # Сохраняем дни, чтобы получить их ID для связки со сплитами
        await session.flush()

        print("Создаем системные сплиты (Split Blueprints)...")

        # 2. Создаем сплиты и расставляем кубики по слотам
        # 2. Создаем сплиты и расставляем кубики по слотам
        splits_data = [
            {
                "name": "Full Body (3 Дня)",
                "length_days": 7,
                "schedule": ["Full Body", "Rest", "Full Body", "Rest", "Full Body", "Rest", "Rest"]
            },
            {
                "name": "Upper / Lower (4 Дня)",
                "length_days": 7,
                "schedule": ["Upper", "Lower", "Rest", "Upper", "Lower", "Rest", "Rest"]
            },
            {
                "name": "Гибрид PHAT-style (5 Дней)",
                "length_days": 7,
                "schedule": ["Upper", "Lower", "Rest", "Push", "Pull", "Legs", "Rest"]
            },
            {
                "name": "PPL x2 (6 Дней)",
                "length_days": 7,
                "schedule": ["Push", "Pull", "Legs", "Push", "Pull", "Legs", "Rest"]
            }
        ]

        for split_info in splits_data:
            split = SplitBlueprint(
                name=split_info["name"],
                length_days=split_info["length_days"],
                is_system=True,
                author_id=None
            )

            for order, day_name in enumerate(split_info["schedule"], start=1):
                slot = SplitDaySlot(
                    day_id=created_days[day_name].id,
                    day_order=order
                )
                split.slots.append(slot)

            session.add(split)

        # Коммитим всё в базу
        await session.commit()
        print("База успешно заполнена системными сплитами и тренировочными днями!")


if __name__ == "__main__":
    # Запускаем асинхронный скрипт
    asyncio.run(seed_data())