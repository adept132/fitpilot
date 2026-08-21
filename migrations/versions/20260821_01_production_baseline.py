"""Production schema baseline.

Revision ID: 20260821_01
Revises:
Create Date: 2026-08-21

The existing production schema was created and reconciled by init_db before
Alembic was introduced. This empty revision records that known-good state.
All schema changes after this revision must have an explicit migration.
"""

from typing import Sequence, Union


revision: str = "20260821_01"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
