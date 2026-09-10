"""Store the exact EAS update ID used by release automation.

Revision ID: 20260906_01
Revises: 20260902_01
"""

from alembic import op
import sqlalchemy as sa


revision = "20260906_01"
down_revision = "20260902_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_releases",
        sa.Column("eas_update_id", sa.String(length=128), nullable=True),
    )
    op.create_unique_constraint(
        "uq_app_releases_eas_update_id", "app_releases", ["eas_update_id"]
    )
    op.create_check_constraint(
        "ck_app_releases_eas_update_id_delivery",
        "app_releases",
        "delivery_method = 'eas_update' OR eas_update_id IS NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_app_releases_eas_update_id_delivery",
        "app_releases",
        type_="check",
    )
    op.drop_constraint(
        "uq_app_releases_eas_update_id", "app_releases", type_="unique"
    )
    op.drop_column("app_releases", "eas_update_id")
