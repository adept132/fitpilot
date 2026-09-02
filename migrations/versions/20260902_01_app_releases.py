"""Add the additive release lane and release registry schema.

Revision ID: 20260902_01
Revises: 20260830_02
Create Date: 2026-09-02
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260902_01"
down_revision: Union[str, Sequence[str], None] = "20260830_02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "app_release_lanes",
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("expected_source_commit", sa.CHAR(length=40), nullable=False),
        sa.Column("expected_ci_run_id", sa.String(length=128), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("platform", "channel"),
    )
    op.create_table(
        "app_releases",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("delivery_method", sa.String(length=24), nullable=False),
        sa.Column("version_code", sa.Integer(), nullable=False),
        sa.Column("version_name", sa.String(length=64), nullable=False),
        sa.Column("runtime_version", sa.String(length=255), nullable=True),
        sa.Column("fingerprint", sa.String(length=128), nullable=True),
        sa.Column("release_notes", postgresql.JSONB(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'published'"),
            nullable=False,
        ),
        sa.Column(
            "is_mandatory",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("min_supported_version_code", sa.Integer(), nullable=True),
        sa.Column("artifact_storage_key", sa.String(length=255), nullable=True),
        sa.Column("artifact_sha256", sa.CHAR(length=64), nullable=True),
        sa.Column("artifact_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("source_commit", sa.CHAR(length=40), nullable=False),
        sa.Column("ci_run_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("eas_build_id", sa.String(length=128), nullable=True),
        sa.Column("eas_update_group_id", sa.String(length=128), nullable=True),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawal_reason", sa.String(length=500), nullable=True),
        sa.Column("mandatory_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("artifact_deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("platform = 'android'", name="ck_app_releases_platform"),
        sa.CheckConstraint(
            "delivery_method IN ('direct_apk', 'eas_update', 'google_play')",
            name="ck_app_releases_delivery_method",
        ),
        sa.CheckConstraint("version_code > 0", name="ck_app_releases_version_code_positive"),
        sa.CheckConstraint(
            "min_supported_version_code IS NULL OR min_supported_version_code <= version_code",
            name="ck_app_releases_min_supported_version",
        ),
        sa.CheckConstraint(
            "(delivery_method = 'direct_apk' AND artifact_storage_key IS NOT NULL "
            "AND artifact_sha256 IS NOT NULL AND artifact_size_bytes > 0) OR "
            "(delivery_method = 'eas_update' AND eas_update_group_id IS NOT NULL "
            "AND runtime_version IS NOT NULL AND artifact_storage_key IS NULL) OR "
            "(delivery_method = 'google_play' AND artifact_storage_key IS NULL)",
            name="ck_app_releases_delivery_payload",
        ),
        sa.CheckConstraint(
            "delivery_method = 'direct_apk' OR "
            "(artifact_sha256 IS NULL AND artifact_size_bytes IS NULL)",
            name="ck_app_releases_non_apk_artifact_fields",
        ),
        sa.CheckConstraint(
            "status IN ('published', 'withdrawn')",
            name="ck_app_releases_status",
        ),
        sa.CheckConstraint(
            "(status = 'published' AND withdrawn_at IS NULL AND withdrawal_reason IS NULL) OR "
            "(status = 'withdrawn' AND withdrawn_at IS NOT NULL AND withdrawal_reason IS NOT NULL)",
            name="ck_app_releases_withdrawal_state",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_app_releases_idempotency_key"),
        sa.UniqueConstraint(
            "eas_update_group_id", name="uq_app_releases_eas_update_group_id"
        ),
    )
    op.create_index(
        "uq_app_releases_direct_version",
        "app_releases",
        ["platform", "channel", "version_code"],
        unique=True,
        postgresql_where=sa.text("delivery_method = 'direct_apk'"),
    )
    op.create_index(
        "ix_app_releases_latest_published",
        "app_releases",
        ["platform", "channel", sa.text("version_code DESC"), sa.text("published_at DESC")],
        postgresql_where=sa.text("status = 'published'"),
    )


def downgrade() -> None:
    op.drop_index("ix_app_releases_latest_published", table_name="app_releases")
    op.drop_index("uq_app_releases_direct_version", table_name="app_releases")
    op.drop_table("app_releases")
    op.drop_table("app_release_lanes")
