"""Neutralize legacy cross-user mesocycle selections and block snapshots.

Relations to system-owned mesocycles (``author_id IS NULL``) and to a user's
own mesocycles remain valid.  The migration deliberately preserves both the
relation row and the TrainingBlock row so workout results keep their stable
foreign keys.  Foreign phase labels are irreversible security-sensitive data,
so invalid block snapshots are replaced with effort-tier labels and detached
from their foreign template before the selection is deactivated.

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
    # Do this before deactivating relations.  Existing TrainingBlock snapshots
    # are independent copies and remain readable after an AppUserMesocycle is
    # inactive, so repairing only the relation does not close the leak.
    #
    # Keep the block itself (and therefore WorkoutSession.training_block_id),
    # phase numbers, effort tiers, durations, entry/exit state and all workout
    # results.  Only the foreign human-readable labels and provenance links are
    # neutralized.  Valid system-owned and user-owned blocks do not match the
    # WHERE clause and remain byte-for-byte untouched.
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
        WHERE
            EXISTS (
                SELECT 1
                FROM mesocycles AS direct_meso
                WHERE direct_meso.id = block.mesocycle_id
                  AND direct_meso.author_id IS NOT NULL
                  AND direct_meso.author_id <> block.app_user_id
            )
            OR EXISTS (
                SELECT 1
                FROM app_user_mesocycles AS relation
                JOIN mesocycles AS relation_meso
                  ON relation_meso.id = relation.mesocycle_id
                WHERE relation.id = block.user_mesocycle_id
                  AND (
                      relation.app_user_id <> block.app_user_id
                      OR (
                          relation_meso.author_id IS NOT NULL
                          AND relation_meso.author_id <> block.app_user_id
                      )
                  )
            )
    """))

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
    # Intentionally irreversible: restoring foreign labels or reactivating an
    # ownership-invalid selection would reintroduce the security leak.
    pass
