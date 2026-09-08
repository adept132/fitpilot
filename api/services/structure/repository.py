"""Загрузка входных данных автоподбора сплита из БД.

Единственное место, где системный каталог сплитов и профиль пользователя
переводятся в чистые типы `suggest.py`. Раньше эта трансляция жила прямо в
`GET /splits/suggest`, и когда автоподбор понадобился второму вызывающему
(bootstrap структуры), копировать её было нельзя: в ней сидит перевод
русских подписей мышц в системные ключи, где уже один раз ошиблись —
`key_for_muscle` не знает «Трапеции», «Бицепс бедра» и «Ягодичные», и
покрытие по этим мышцам выходило нулевым.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.services.models import (
    AppUserProfile,
    DayBlueprint,
    SplitBlueprint,
    SplitDaySlot,
)
from api.services.muscle_keys import to_system_key
from api.services.structure.suggest import (
    DayView,
    SplitCandidate,
    SplitView,
    suggest_splits,
)

# Частота не задана — тройка, самая безопасная для неизвестного уровня (§7).
DEFAULT_FREQUENCY = 3
# Каталог намеренно не содержит сплита на семь тренировок без единого дня
# отдыха (§5.1). Спека §7 требует показать шестидневных кандидатов, а не
# пустой список, поэтому частота зажимается, а не отсекается.
MIN_FREQUENCY = 2
MAX_FREQUENCY = 6


async def load_split_views(session: AsyncSession) -> list[SplitView]:
    """Системный каталог сплитов в виде чистых типов автоподбора."""
    blueprints = (await session.execute(
        select(SplitBlueprint)
        .where(SplitBlueprint.is_system.is_(True))
        .options(
            selectinload(SplitBlueprint.slots)
            .selectinload(SplitDaySlot.day)
            .selectinload(DayBlueprint.muscle_targets)
        )
    )).scalars().unique().all()

    return [
        SplitView(
            id=str(bp.id),
            name=bp.name,
            length_days=bp.length_days,
            days=tuple(
                DayView(
                    template_type=slot.day.template_type.value,
                    muscles=frozenset(
                        key for key in (
                            to_system_key(target.muscle_group_id)
                            for target in slot.day.muscle_targets
                        ) if key
                    ),
                )
                for slot in sorted(bp.slots, key=lambda s: s.day_order)
            ),
        )
        for bp in blueprints
    ]


async def rank_splits(
    session: AsyncSession,
    app_user_id: int,
    *,
    training_frequency: Optional[int] = None,
    requirement: Optional[dict] = None,
    limit: int = 3,
) -> list[SplitCandidate]:
    """Кандидаты сплита под профиль пользователя, лучший первым.

    `training_frequency` перекрывает значение из профиля — так эндпоинт
    отдаёт предпросмотр под частоту, которую человек ещё не сохранил.
    Меньше трёх кандидатов (и ноль) — нормальный ответ, см. §5.2.
    """
    profile = (await session.execute(
        select(AppUserProfile).where(AppUserProfile.app_user_id == app_user_id)
    )).scalars().first()

    frequency = (
        training_frequency
        or getattr(profile, "training_frequency", None)
        or DEFAULT_FREQUENCY
    )
    frequency = max(MIN_FREQUENCY, min(int(frequency), MAX_FREQUENCY))

    focus_muscles: list[str] = []
    if profile is not None and profile.volume_budget:
        focus_muscles = list(
            (profile.volume_budget.get("meta") or {}).get("focus_muscles") or []
        )

    return suggest_splits(
        await load_split_views(session),
        training_frequency=frequency,
        focus_muscles=focus_muscles,
        requirement=requirement,
        limit=limit,
    )
