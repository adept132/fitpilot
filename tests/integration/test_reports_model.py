from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from api.services.models import PeriodReport


@pytest.mark.asyncio
async def test_second_insert_of_same_period_is_ignored(db, test_user):
    """Воркер и клиент могут материализовать один период одновременно —
    уникальный ключ обязан свести это к одной строке без исключения."""
    values = {
        "app_user_id": test_user.id,
        "period_type": "week",
        "period_start": date(2026, 8, 10),
        "period_end": date(2026, 8, 16),
        "payload": {"metrics": {}, "actions": []},
        "rules_version": 1,
        "shape_version": 1,
    }
    statement = insert(PeriodReport).values(**values).on_conflict_do_nothing(
        index_elements=[
            PeriodReport.app_user_id,
            PeriodReport.period_type,
            PeriodReport.period_start,
        ]
    )
    await db.execute(statement)
    await db.execute(statement)
    await db.commit()

    rows = (await db.execute(
        select(PeriodReport).where(PeriodReport.app_user_id == test_user.id)
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].seen_at is None
