"""Backfill domain profiles for authenticated users.

Older authentication flows created ``app_users`` lazily but did not create the
required one-to-one ``app_user_profiles`` row. Such users were authenticated
successfully and then received ``404 Профиль не найден`` from most of the app.

The insert is idempotent and conflict-safe so it can coexist with requests
handled by the new runtime profile guard during a rolling deployment.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import insert as pg_insert

from api.services.models import AppUser, AppUserProfile

revision: str = "20260908_01"
down_revision: Union[str, Sequence[str], None] = "20260822_02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def backfill_missing_profiles(bind) -> None:
    users = AppUser.__table__
    profiles = AppUserProfile.__table__
    missing_user_ids = sa.select(users.c.id).where(
        ~sa.exists(
            sa.select(1).where(profiles.c.app_user_id == users.c.id)
        )
    )
    statement = (
        pg_insert(profiles)
        # Do not expand ORM-side Python defaults into the SELECT. In Alembic's
        # offline SQL mode callable defaults render as NULL; inserting only the
        # foreign key lets PostgreSQL apply the column server defaults instead.
        .from_select(
            [profiles.c.app_user_id],
            missing_user_ids,
            include_defaults=False,
        )
        .on_conflict_do_nothing(index_elements=[profiles.c.app_user_id])
    )
    bind.execute(statement)


def upgrade() -> None:
    backfill_missing_profiles(op.get_bind())


def downgrade() -> None:
    # This is a data repair, not a schema change. Deleting repaired profiles on
    # rollback could destroy settings entered after deployment.
    pass
