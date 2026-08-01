"""Физическое удаление аккаунтов, у которых истёк grace period.

То же самое делает старт приложения (lifespan), но продакшен может не
перезапускаться неделями — а обещание «удалим через 30 дней» надо выполнять.
Ставится в cron/Task Scheduler раз в сутки.

Запуск:
    python -m scripts.purge_deleted_accounts            # показать, кого удалит
    python -m scripts.purge_deleted_accounts --apply    # удалить
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

from sqlalchemy import select

from api.services.account_service import (
    DELETION_GRACE_PERIOD,
    delete_firebase_user,
    purge_expired,
    purge_at,
)
from api.services.models import AppUser
from app.database import SessionLocal, engine


async def main(apply: bool) -> None:
    now = datetime.now(timezone.utc)
    cutoff = now - DELETION_GRACE_PERIOD

    async with SessionLocal() as db:
        pending = (
            await db.execute(
                select(AppUser.id, AppUser.email, AppUser.deletion_requested_at)
                .where(AppUser.deletion_requested_at.is_not(None))
                .order_by(AppUser.deletion_requested_at)
            )
        ).all()

        due = [row for row in pending if row[2] <= cutoff]
        waiting = [row for row in pending if row[2] > cutoff]

        for user_id, email, requested in waiting:
            print(f"[wait]  id={user_id} {email} — удаление {purge_at(requested):%Y-%m-%d}")
        for user_id, email, requested in due:
            print(f"[due]   id={user_id} {email} — срок истёк {purge_at(requested):%Y-%m-%d}")

        if not due:
            print("[purge] удалять нечего")
        elif not apply:
            print(f"[purge] dry-run: к удалению {len(due)}. Повторите с --apply.")
        else:
            purged = await purge_expired(db, now=now)
            print(f"[purge] удалено аккаунтов: {len(purged)} {purged}")

    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="реально удалить")
    asyncio.run(main(parser.parse_args().apply))
