"""Сид пакета новых системных упражнений (август 2026) в базу.

Данные лежат в scripts/data/new_exercises_2026_08.json — это уже нормализованный
пакет: канонические коды оборудования, ЯВНЫЕ id и привязка к free-exercise-db.

Почему id явные, а не из sequence: относительный путь к картинке техники
(`exercises/<id>/0.jpg`) зашит и в media/ репозитория, и в колонку image_urls.
Если локальная и задеплоенная база раздадут упражнениям разные id, картинки
разъедутся. Поэтому id — часть данных, одинаковая везде.

Производные атрибуты (fatigue_tier, action/vector/laterality) НЕ хранятся в
JSON, а считаются теми же скриптами, что и для остальной базы:
fatigue_tiers.calculate_fatigue_tier и HeuristicsEngine.classify_exercise.
Так пакет не разъедется с остальной базой, если модель поменяется.

Запуск (из корня репозитория, БД берётся из DATABASE_URL):

    python -m scripts.seed_new_exercises --dry-run   # показать, что будет сделано
    python -m scripts.seed_new_exercises             # применить
    python -m scripts.seed_new_exercises --no-images # без копирования файлов в media/

Идемпотентно: повторный запуск обновляет уже существующие строки на месте.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import List

from sqlalchemy import func, select, text

from app.database import SessionLocal, engine
from api.services.models import Exercise
from api.services.fatigue_tiers import calculate_fatigue_tier
from api.services.heuristics import HeuristicsEngine

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
DATA_FILE = SCRIPTS_DIR / "data" / "new_exercises_2026_08.json"
CACHE_IMAGES = SCRIPTS_DIR / ".cache" / "free-exercise-db" / "exercises"
MEDIA_EXERCISES = REPO_ROOT / "media" / "exercises"


def load_data() -> List[dict]:
    if not DATA_FILE.exists():
        sys.exit(f"[seed] Нет файла данных: {DATA_FILE}")
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))


def copy_images(item: dict) -> List[str]:
    """Копирует картинки техники из кэша в media/exercises/<id>/ и возвращает
    относительные пути для image_urls. Без кэша возвращает пути «как будут»:
    в задеплоенной среде файлы уже лежат в репозитории."""
    rel_urls = [f"exercises/{item['id']}/{i}.jpg"
                for i, _ in enumerate(item["dataset_images"])]

    if not CACHE_IMAGES.exists():
        return rel_urls

    dest_dir = MEDIA_EXERCISES / str(item["id"])
    dest_dir.mkdir(parents=True, exist_ok=True)
    for i, src_rel in enumerate(item["dataset_images"]):
        src = CACHE_IMAGES / src_rel
        if not src.exists():
            print(f"  ! {item['id']}: нет картинки в кэше {src_rel}")
            continue
        shutil.copyfile(src, dest_dir / f"{i}.jpg")
    return rel_urls


async def main(dry_run: bool, with_images: bool) -> None:
    data = load_data()
    print(f"[seed] Записей в пакете: {len(data)} (id {data[0]['id']}..{data[-1]['id']})")

    inserted = updated = 0
    conflicts: List[str] = []

    async with SessionLocal() as session:
        # Имена заняты кем-то другим? name UNIQUE — ловим до вставки.
        names = [x["name"] for x in data]
        ids = [x["id"] for x in data]
        taken = (await session.execute(
            select(Exercise.id, Exercise.name)
            .where(func.lower(Exercise.name).in_([n.lower() for n in names]))
            .where(Exercise.id.notin_(ids))
        )).all()
        for row in taken:
            conflicts.append(f"имя {row.name!r} уже занято строкой #{row.id}")
        if conflicts:
            for c in conflicts:
                print(f"  ! {c}")
            sys.exit("[seed] Прерываю: конфликт имён с существующими строками.")

        for item in data:
            tier = calculate_fatigue_tier(
                item["category"],
                item["main_muscle_group"],
                item["secondary_muscle_groups"],
                item["equipment_needed"],
            )
            tags = HeuristicsEngine.classify_exercise(
                item["name"], item["main_muscle_group"]
            )
            image_urls = copy_images(item) if (with_images and not dry_run) else [
                f"exercises/{item['id']}/{i}.jpg"
                for i, _ in enumerate(item["dataset_images"])
            ]

            ex = await session.get(Exercise, item["id"])
            action = "UPDATE" if ex else "INSERT"
            if ex is None:
                ex = Exercise(id=item["id"])
                session.add(ex)
                inserted += 1
            else:
                updated += 1

            ex.name = item["name"]
            ex.category = item["category"]
            ex.main_muscle_group = item["main_muscle_group"]
            ex.secondary_muscle_groups = item["secondary_muscle_groups"]
            ex.equipment_needed = item["equipment_needed"]
            ex.difficulty = item["difficulty"]
            ex.description = item["description"]
            ex.source = "default"
            ex.app_user_id = None
            ex.fatigue_tier = tier
            ex.action = tags["action"]
            ex.vector = tags["vector"]
            ex.laterality = tags["laterality"]
            ex.image_urls = image_urls
            # Картинка взята прямым сопоставлением с датасетом по английскому
            # названию — техника точная, а не «родственника».
            ex.image_approx = False

            if dry_run:
                print(f"  [{action}] #{item['id']:<4} tier={tier} "
                      f"{tags['action'].value}/{tags['vector'].value}/"
                      f"{tags['laterality'].value}  {item['name']}")

        if dry_run:
            await session.rollback()
            print(f"\n[seed] DRY-RUN: вставили бы {inserted}, обновили бы {updated}.")
            return

        await session.commit()

    # Сид вставляет ЯВНЫЕ id и не двигает счётчик — без этого создание
    # пользовательских упражнений упадёт на duplicate key (см. _ensure_sequences).
    async with engine.begin() as conn:
        seq = (await conn.execute(
            text("SELECT pg_get_serial_sequence('exercises', 'id')")
        )).scalar_one()
        if seq:
            await conn.execute(text(
                f"SELECT setval('{seq}', GREATEST("
                f"  (SELECT COALESCE(MAX(id), 0) FROM exercises),"
                f"  (SELECT last_value FROM {seq})))"
            ))

    print(f"[seed] Готово. Вставлено: {inserted}, обновлено: {updated}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="показать план без записи в БД")
    parser.add_argument("--no-images", action="store_true",
                        help="не копировать файлы картинок в media/")
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run, with_images=not args.no_images))
