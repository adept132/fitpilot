"""Разовая чистка дублей по client_uuid перед созданием уникальных индексов.

Нужен, если init_db напечатал предупреждение «не удалось создать uq_...»: значит,
в таблицах уже есть строки-двойники, оставленные багом read-then-insert в
/sync/workouts (см. P0-03). Скрипт оставляет из каждой группы дублей самую
свежую строку (max(updated_at), при равенстве — max(id)), остальные удаляет.

Запуск:
    python -m scripts.dedupe_sync_uuids            # только показать, что нашлось
    python -m scripts.dedupe_sync_uuids --apply    # удалить дубли
"""
from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import text

from app.database import engine

# (описание, таблица, колонки-скоуп) — тот же скоуп, что у уникальных индексов.
TARGETS = [
    ("тренировки", "workout_sessions", ["app_user_id", "client_uuid"]),
    ("упражнения сессии", "workout_session_exercises", ["workout_session_id", "client_uuid"]),
    ("подходы", "workout_session_sets", ["workout_session_exercise_id", "client_uuid"]),
]


def _dupe_ids_sql(table: str, scope: list[str]) -> str:
    partition = ", ".join(scope)
    return f"""
        SELECT id FROM (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY {partition}
                       ORDER BY updated_at DESC NULLS LAST, id DESC
                   ) AS rn
            FROM {table}
            WHERE client_uuid IS NOT NULL
        ) ranked
        WHERE rn > 1
    """


async def main(apply: bool) -> None:
    # Дети удаляются каскадом вместе с родителем, поэтому идём сверху вниз:
    # почистив тренировки, часть дублей ниже исчезнет сама.
    async with engine.begin() as conn:
        for label, table, scope in TARGETS:
            ids = (await conn.execute(text(_dupe_ids_sql(table, scope)))).scalars().all()
            if not ids:
                print(f"[dedupe] {label}: дублей нет")
                continue

            print(f"[dedupe] {label}: найдено дублей — {len(ids)}")
            if not apply:
                print(f"[dedupe]   (dry-run, id: {ids[:20]}{'...' if len(ids) > 20 else ''})")
                continue

            await conn.execute(
                text(f"DELETE FROM {table} WHERE id = ANY(:ids)"), {"ids": list(ids)}
            )
            print(f"[dedupe]   удалено {len(ids)}")

    if apply:
        print("[dedupe] Готово. Перезапустите бэкенд — init_db создаст уникальные индексы.")
    else:
        print("[dedupe] Это был dry-run. Повторите с --apply, чтобы удалить.")

    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="реально удалить дубли")
    asyncio.run(main(parser.parse_args().apply))
