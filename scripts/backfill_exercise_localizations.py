"""Check or apply the reviewed English system-exercise catalog."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import select

from api.services.models import Exercise
from app.database import SessionLocal


DEFAULT_TRANSLATIONS = (
    REPO_ROOT
    / "api"
    / "data"
    / "exercise_localizations_en.json"
)


@dataclass(frozen=True)
class Translation:
    name: str
    description: str


@dataclass(frozen=True)
class BackfillUpdate:
    exercise_id: int
    name_en: str
    description_en: str


@dataclass(frozen=True)
class BackfillPlan:
    updates: tuple[BackfillUpdate, ...]
    missing_translation_ids: tuple[int, ...]
    custom_ids_skipped: tuple[int, ...]


def load_translations(path: Path = DEFAULT_TRANSLATIONS) -> dict[int, Translation]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("translation catalog must be a JSON object")

    translations: dict[int, Translation] = {}
    for raw_id, value in raw.items():
        if not isinstance(value, dict):
            raise ValueError(f"translation {raw_id} must be an object")
        try:
            exercise_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid exercise id: {raw_id!r}") from exc
        name = value.get("name")
        description = value.get("description")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"translation {exercise_id} has no English name")
        if "_" in name:
            raise ValueError(
                f"translation {exercise_id} exposes an unreviewed dataset identifier"
            )
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f"translation {exercise_id} has no English description")
        translations[exercise_id] = Translation(name.strip(), description.strip())
    return translations


def plan_backfill(
    exercises: Iterable[Exercise], translations: Mapping[int, Translation]
) -> BackfillPlan:
    updates: list[BackfillUpdate] = []
    missing: list[int] = []
    custom: list[int] = []
    for exercise in sorted(exercises, key=lambda row: row.id):
        if exercise.source != "default":
            custom.append(exercise.id)
            continue
        translation = translations.get(exercise.id)
        if translation is None:
            missing.append(exercise.id)
            continue
        if (
            exercise.name_en != translation.name
            or exercise.description_en != translation.description
        ):
            updates.append(
                BackfillUpdate(
                    exercise_id=exercise.id,
                    name_en=translation.name,
                    description_en=translation.description,
                )
            )
    return BackfillPlan(tuple(updates), tuple(missing), tuple(custom))


async def _run(apply: bool, translations_path: Path) -> int:
    translations = load_translations(translations_path)
    async with SessionLocal() as session:
        exercises = list(
            (
                await session.execute(select(Exercise).order_by(Exercise.id))
            ).scalars().all()
        )
        plan = plan_backfill(exercises, translations)
        if apply:
            by_id = {exercise.id: exercise for exercise in exercises}
            for update in plan.updates:
                exercise = by_id[update.exercise_id]
                # plan_backfill admitted system rows only.
                exercise.name_en = update.name_en
                exercise.description_en = update.description_en
            await session.commit()

        missing_names = sorted(
            exercise.id
            for exercise in exercises
            if exercise.source == "default" and not exercise.name_en
        )

    print(
        f"catalog={len(translations)} system={sum(e.source == 'default' for e in exercises)} "
        f"updates={len(plan.updates) if apply else 0} custom_skipped={len(plan.custom_ids_skipped)}"
    )
    if plan.missing_translation_ids:
        print(
            "missing catalog translations: "
            + ", ".join(map(str, plan.missing_translation_ids))
        )
    if missing_names:
        print("system exercises missing name_en: " + ", ".join(map(str, missing_names)))
    unapplied_drift = bool(plan.updates) and not apply
    if unapplied_drift:
        print(
            "system exercises differ from reviewed catalog: "
            + ", ".join(str(item.exercise_id) for item in plan.updates)
        )
    return 1 if plan.missing_translation_ids or missing_names or unapplied_drift else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--check", action="store_true")
    parser.add_argument("--translations", type=Path, default=DEFAULT_TRANSLATIONS)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args.apply, args.translations)))


if __name__ == "__main__":
    main()
