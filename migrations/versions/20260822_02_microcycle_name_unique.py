"""Unique (app_user_id, name) on app_user_microcycles (P1-03).

ensure_structure (api/services/structure/bootstrap.py) relies on an
IntegrityError from the unique index to detect the race between two
concurrent POST /profile/structure/bootstrap calls — mirroring how
uq_mesocycle_author_code already protects Mesocycle. Without it, two
parallel calls each insert their own five personal microcycles: ten rows,
possibly two marked active.

env.py is async, but upgrade() runs against a SYNC connection from
op.get_bind(). Existing duplicate (app_user_id, name) rows must be resolved
before the constraint can be created, or the CREATE fails on any database
that already raced. For each duplicate group we keep one row untouched
(prefer is_active, tie-broken by smallest id) and rename the rest with a
" (2)", " (3)", ... suffix, truncating the base name so it still fits
String(120). No rows are deleted.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from api.services.models import AppUserMicrocycle

revision: str = "20260822_02"
down_revision: Union[str, Sequence[str], None] = "20260822_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    micros = AppUserMicrocycle.__table__

    rows = bind.execute(
        sa.select(
            micros.c.id, micros.c.app_user_id, micros.c.name, micros.c.is_active,
        ).order_by(micros.c.app_user_id, micros.c.name, micros.c.id)
    ).fetchall()

    groups: dict[tuple[int, str], list] = {}
    for row in rows:
        groups.setdefault((row.app_user_id, row.name), []).append(row)

    for (app_user_id, name), group in groups.items():
        if len(group) <= 1:
            continue
        # Keep one row untouched: active first, else smallest id.
        group.sort(key=lambda r: (not r.is_active, r.id))
        for idx, row in enumerate(group[1:], start=2):
            suffix = f" ({idx})"
            base = name[: 120 - len(suffix)]
            bind.execute(
                micros.update()
                .where(micros.c.id == row.id)
                .values(name=f"{base}{suffix}")
            )

    op.create_unique_constraint(
        "uq_app_user_microcycle_name",
        "app_user_microcycles",
        ["app_user_id", "name"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_app_user_microcycle_name", "app_user_microcycles", type_="unique",
    )
