"""Add optional English exercise catalog fields.

Revision ID: 20260830_02
Revises: 20260830_01
Create Date: 2026-08-30
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260830_02"
down_revision: Union[str, Sequence[str], None] = "20260830_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "exercises", sa.Column("name_en", sa.String(length=200), nullable=True)
    )
    op.add_column(
        "exercises", sa.Column("description_en", sa.Text(), nullable=True)
    )
    op.create_index(
        "ix_exercises_name_en", "exercises", ["name_en"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_exercises_name_en", table_name="exercises")
    op.drop_column("exercises", "description_en")
    op.drop_column("exercises", "name_en")
