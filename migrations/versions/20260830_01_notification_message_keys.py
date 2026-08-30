"""Add semantic message fields to durable notifications.

Revision ID: 20260830_01
Revises: 20260822_02
Create Date: 2026-08-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260830_01"
down_revision: Union[str, Sequence[str], None] = "20260822_02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "app_notifications",
        sa.Column("message_key", sa.String(128), nullable=True),
    )
    op.add_column(
        "app_notifications",
        sa.Column("message_params", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("app_notifications", "message_params")
    op.drop_column("app_notifications", "message_key")
