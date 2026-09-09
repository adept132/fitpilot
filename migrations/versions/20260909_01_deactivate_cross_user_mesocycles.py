"""Deactivate legacy cross-user mesocycle selections.

Relations to system-owned mesocycles (``author_id IS NULL``) and to a user's
own mesocycles remain valid.  The migration deliberately deactivates rather
than deletes illegal historical rows so workout history that refers to the
relation is preserved.

Revision ID: 20260909_01
Revises: 20260908_01
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op

from api.services.models import AppUserMesocycle, Mesocycle


revision: str = "20260909_01"
down_revision: str | None = "20260908_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def deactivate_cross_user_relations(bind) -> None:
    relations = AppUserMesocycle.__table__
    mesocycles = Mesocycle.__table__
    is_cross_user = sa.exists(
        sa.select(1).where(
            mesocycles.c.id == relations.c.mesocycle_id,
            mesocycles.c.author_id.is_not(None),
            mesocycles.c.author_id != relations.c.app_user_id,
        )
    )
    bind.execute(
        sa.update(relations)
        .where(relations.c.is_active.is_(True), is_cross_user)
        .values(is_active=False)
    )


def upgrade() -> None:
    deactivate_cross_user_relations(op.get_bind())


def downgrade() -> None:
    # Ownership-invalid selections must not be reactivated on rollback.
    pass
