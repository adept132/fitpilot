"""Require positive provenance for persisted TrainingBlock phase labels.

Legacy rows start untrusted. A row is backfilled as trusted only while an
existing source mesocycle positively proves system or same-user ownership.
Every other snapshot is security-cleaned without deleting its block or any
workout result that refers to it.

Revision ID: 20260909_02
Revises: 20260909_01
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260909_02"
down_revision: str | None = "20260909_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def backfill_and_cleanup_block_provenance(bind) -> None:
    """Trust only provable legacy sources and neutralize every remainder."""
    # Legacy NULL/false rows remain untrusted. A true source-linked row is
    # revalidated below. The only true row allowed to survive this reset is a
    # source-less block explicitly created by the new authenticated factory;
    # that marker is the positive, migration-safe origin proof unavailable to
    # historical generic rows (which receive false from the added column).
    bind.execute(sa.text("""
        UPDATE training_blocks
        SET phase_snapshot_trusted = false
        WHERE phase_snapshot_trusted IS DISTINCT FROM true
           OR mesocycle_id IS NOT NULL
           OR user_mesocycle_id IS NOT NULL
    """))

    # A direct source is sufficient when the optional relation is absent. If
    # both references exist, both must describe the same template and the full
    # relation chain must exist and belong to the block's user. A relation-only
    # row is trusted only when its joined mesocycle positively exists and is
    # system/same-user owned. NULL source references never imply trust.
    bind.execute(sa.text("""
        UPDATE training_blocks AS block
        SET phase_snapshot_trusted = true
        WHERE
            (
                block.mesocycle_id IS NOT NULL
                AND EXISTS (
                    SELECT 1
                    FROM mesocycles AS direct_meso
                    WHERE direct_meso.id = block.mesocycle_id
                      AND (
                          direct_meso.author_id IS NULL
                          OR direct_meso.author_id = block.app_user_id
                      )
                )
                AND (
                    block.user_mesocycle_id IS NULL
                    OR EXISTS (
                        SELECT 1
                        FROM app_user_mesocycles AS relation
                        JOIN mesocycles AS relation_meso
                          ON relation_meso.id = relation.mesocycle_id
                        WHERE relation.id = block.user_mesocycle_id
                          AND relation.app_user_id = block.app_user_id
                          AND relation.mesocycle_id = block.mesocycle_id
                          AND (
                              relation_meso.author_id IS NULL
                              OR relation_meso.author_id = block.app_user_id
                          )
                    )
                )
            )
            OR (
                block.mesocycle_id IS NULL
                AND block.user_mesocycle_id IS NOT NULL
                AND EXISTS (
                    SELECT 1
                    FROM app_user_mesocycles AS relation
                    JOIN mesocycles AS relation_meso
                      ON relation_meso.id = relation.mesocycle_id
                    WHERE relation.id = block.user_mesocycle_id
                      AND relation.app_user_id = block.app_user_id
                      AND (
                          relation_meso.author_id IS NULL
                          OR relation_meso.author_id = block.app_user_id
                      )
                )
            )
    """))

    # Security-first cleanup is explicitly authorized even though it may close
    # legitimate legacy generic blocks: after their FKs became NULL they are
    # indistinguishable from cascade-orphaned foreign snapshots. Preserve IDs,
    # workout links, phase numbers/tiers/durations and all result/state data;
    # remove only untrusted labels/provenance and active use.
    bind.execute(sa.text("""
        UPDATE training_blocks AS block
        SET
            phases = COALESCE(
                (
                    SELECT jsonb_agg(
                        CASE
                            WHEN jsonb_typeof(item.value) = 'object' THEN
                                jsonb_set(
                                    item.value,
                                    '{name}',
                                    to_jsonb(
                                        COALESCE(
                                            NULLIF(item.value ->> 'effort_tier', ''),
                                            'phase'
                                        )::text
                                    ),
                                    true
                                )
                            ELSE item.value
                        END
                        ORDER BY item.ordinality
                    )
                    FROM jsonb_array_elements(block.phases)
                        WITH ORDINALITY AS item(value, ordinality)
                ),
                '[]'::jsonb
            ),
            status = CASE
                WHEN block.status = 'active' THEN 'closed'
                ELSE block.status
            END,
            close_reason = CASE
                WHEN block.status = 'active' THEN 'ownership_invalid'
                ELSE block.close_reason
            END,
            actual_end_date = CASE
                WHEN block.status = 'active' THEN COALESCE(
                    block.actual_end_date,
                    LEAST(
                        block.planned_end_date,
                        GREATEST(block.start_date, CURRENT_DATE)
                    )
                )
                ELSE block.actual_end_date
            END,
            user_mesocycle_id = NULL,
            mesocycle_id = NULL
        WHERE block.phase_snapshot_trusted = false
    """))


def upgrade() -> None:
    bind = op.get_bind()
    # init_db may already have synchronized the model column before Alembic
    # runs, so expansion is idempotent. It is nullable only during backfill.
    bind.execute(sa.text("""
        ALTER TABLE training_blocks
        ADD COLUMN IF NOT EXISTS phase_snapshot_trusted boolean
    """))
    backfill_and_cleanup_block_provenance(bind)
    bind.execute(sa.text("""
        ALTER TABLE training_blocks
        ALTER COLUMN phase_snapshot_trusted SET DEFAULT false,
        ALTER COLUMN phase_snapshot_trusted SET NOT NULL
    """))


def downgrade() -> None:
    # Irreversible security cleanup: retaining the discriminator is safer than
    # making sanitized rows implicitly trusted again on rollback.
    pass
