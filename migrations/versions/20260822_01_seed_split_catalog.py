"""Seed the 16-split system catalog (P1-03 part 1).

Schema unchanged: this revision only inserts rows. Idempotent by name, so it
is safe on a database that already carries the four legacy system splits.

env.py is async, but upgrade() runs against a SYNC connection from
op.get_bind() — hence core inserts over the same catalog data rather than the
async ensure_system_splits().
"""

import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from api.seed_splits import DAYS_DATA
from api.services.models import (
    DayBlueprint, DayMuscleTarget, SplitBlueprint, SplitDaySlot,
)
from api.services.structure.split_catalog import SPLITS

revision: str = "20260822_01"
down_revision: Union[str, Sequence[str], None] = "20260821_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    days = DayBlueprint.__table__
    targets = DayMuscleTarget.__table__
    splits = SplitBlueprint.__table__
    slots = SplitDaySlot.__table__

    day_ids = {
        row.name: row.id
        for row in bind.execute(
            sa.select(days.c.name, days.c.id).where(days.c.is_system.is_(True))
        )
    }

    for name, (template_type, muscles) in DAYS_DATA.items():
        if name in day_ids:
            continue
        day_id = uuid.uuid4()
        bind.execute(days.insert().values(
            id=day_id, name=name, template_type=template_type,
            is_system=True, author_id=None,
        ))
        if muscles:
            bind.execute(targets.insert(), [
                {"id": uuid.uuid4(), "day_id": day_id, "muscle_group_id": muscle}
                for muscle in muscles
            ])
        day_ids[name] = day_id

    present = {
        row.name
        for row in bind.execute(
            sa.select(splits.c.name).where(splits.c.is_system.is_(True))
        )
    }

    for definition in SPLITS:
        if definition.name in present:
            continue
        split_id = uuid.uuid4()
        bind.execute(splits.insert().values(
            id=split_id, name=definition.name,
            length_days=definition.length_days, is_system=True, author_id=None,
        ))
        bind.execute(slots.insert(), [
            {
                "id": uuid.uuid4(),
                "blueprint_id": split_id,
                "day_id": day_ids[day_name],
                "day_order": order,
            }
            for order, day_name in enumerate(definition.schedule, start=1)
        ])


def downgrade() -> None:
    # Каталог — данные, а не схема. Откат удалял бы сплиты, на которые уже
    # могли сослаться UserSplit живых пользователей, поэтому пусто намеренно.
    pass
