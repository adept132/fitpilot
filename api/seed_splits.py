"""Сид системных дней и сплитов (P1-03 часть 1, §5.1).

Идемпотентен: дописывает недостающее по имени и не трогает существующее.
Прежний предохранитель «системные сплиты уже существуют — отмена» снят —
он не давал дописать каталог, а пересоздание уничтожило бы id, на которые
ссылается UserSplit.blueprint_id у живых пользователей.
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.services.models import (
    DayBlueprint, DayMuscleTarget, DayTemplateType, SplitBlueprint, SplitDaySlot,
)
from api.services.structure.split_catalog import SPLITS
from app.database import SessionLocal

DAYS_DATA: dict[str, tuple[DayTemplateType, list[str]]] = {
    "Push": (DayTemplateType.PUSH, [
        "Грудь", "Передняя дельта", "Средняя дельта", "Трицепс",
    ]),
    "Pull": (DayTemplateType.PULL, [
        "Широчайшие", "Средняя часть спины", "Трапеции", "Задняя дельта", "Бицепс",
    ]),
    "Legs": (DayTemplateType.LEGS, [
        "Квадрицепсы", "Бицепс бедра", "Ягодичные", "Икры",
    ]),
    "Upper": (DayTemplateType.UPPER, [
        "Грудь", "Широчайшие", "Средняя часть спины", "Передняя дельта",
        "Средняя дельта", "Задняя дельта", "Бицепс", "Трицепс",
    ]),
    "Lower": (DayTemplateType.LOWER, [
        "Квадрицепсы", "Бицепс бедра", "Ягодичные", "Икры", "Пресс",
    ]),
    "Arms & Shoulders": (DayTemplateType.ARMS_SHOULDERS, [
        "Бицепс", "Трицепс", "Средняя дельта", "Передняя дельта", "Задняя дельта",
    ]),
    "Full Body": (DayTemplateType.FULL_BODY, [
        "Грудь", "Широчайшие", "Квадрицепсы", "Бицепс бедра",
        "Средняя дельта", "Бицепс", "Трицепс",
    ]),
    "Rest": (DayTemplateType.ACTIVE_REST, []),
}


async def ensure_system_days(session: AsyncSession) -> dict[str, DayBlueprint]:
    """Системные дни по именам; недостающие создаются."""
    existing = {
        day.name: day
        for day in (await session.execute(
            select(DayBlueprint)
            .where(DayBlueprint.is_system.is_(True))
            .options(selectinload(DayBlueprint.muscle_targets))
        )).scalars().all()
    }

    for name, (template_type, muscles) in DAYS_DATA.items():
        if name in existing:
            continue
        day = DayBlueprint(name=name, template_type=template_type,
                           is_system=True, author_id=None)
        for muscle in muscles:
            day.muscle_targets.append(DayMuscleTarget(muscle_group_id=muscle))
        session.add(day)
        existing[name] = day

    await session.flush()
    return existing


async def ensure_system_splits(session: AsyncSession) -> int:
    """Системные сплиты по именам; недостающие создаются. Возвращает число созданных."""
    days = await ensure_system_days(session)

    present = {
        name for name in (await session.execute(
            select(SplitBlueprint.name).where(SplitBlueprint.is_system.is_(True))
        )).scalars().all()
    }

    created = 0
    for definition in SPLITS:
        if definition.name in present:
            continue
        split = SplitBlueprint(
            name=definition.name,
            length_days=definition.length_days,
            is_system=True,
            author_id=None,
        )
        for order, day_name in enumerate(definition.schedule, start=1):
            split.slots.append(SplitDaySlot(day_id=days[day_name].id, day_order=order))
        session.add(split)
        created += 1

    await session.flush()
    return created


async def seed_data() -> None:
    async with SessionLocal() as session:
        created = await ensure_system_splits(session)
        await session.commit()
        print(f"Системные сплиты: создано {created}, всего в каталоге {len(SPLITS)}.")


if __name__ == "__main__":
    asyncio.run(seed_data())
