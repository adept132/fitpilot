"""Exercise presentation helpers.

Russian ``name``/``description`` remain the durable catalog text.  English is
an optional presentation layer for system exercises only; user-entered text is
never translated or replaced here.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
import unicodedata


PREFERENCE_RANK = {"favorite": 0, None: 1, "disliked": 2}


def _value(exercise: object, field: str) -> Any:
    if isinstance(exercise, Mapping):
        return exercise.get(field)
    return getattr(exercise, field, None)


def is_system_exercise(exercise: object) -> bool:
    return _value(exercise, "source") == "default"


def localized_names(exercise: object) -> dict[str, str]:
    original = _value(exercise, "name")
    names = {"ru": original} if isinstance(original, str) and original else {}
    english = _value(exercise, "name_en")
    if is_system_exercise(exercise) and isinstance(english, str) and english:
        names["en"] = english
    return names


def localized_descriptions(exercise: object) -> dict[str, str]:
    original = _value(exercise, "description")
    descriptions = (
        {"ru": original} if isinstance(original, str) and original else {}
    )
    english = _value(exercise, "description_en")
    if is_system_exercise(exercise) and isinstance(english, str) and english:
        descriptions["en"] = english
    return descriptions


def display_name(exercise: object, language: str) -> str:
    names = localized_names(exercise)
    return names.get("en") if language == "en" and names.get("en") else names.get("ru", "")


def display_description(exercise: object, language: str) -> str | None:
    descriptions = localized_descriptions(exercise)
    return (
        descriptions.get("en")
        if language == "en" and descriptions.get("en")
        else descriptions.get("ru")
    )


def normalized_display_name(exercise: object, language: str) -> str:
    """Return the locale-selected name normalized only for deterministic collation."""
    name = display_name(exercise, language)
    return unicodedata.normalize("NFKC", " ".join(name.split()).casefold())


def sort_exercises(
    exercises: Iterable[object],
    language: str,
    *,
    preferences: Mapping[int, str] | None = None,
    by_similarity: bool = False,
) -> list[object]:
    """Apply the final exercise ordering contract.

    Preference rank is the leading dimension when supplied, followed by search
    relevance when requested, the locale-selected normalized display name, and
    canonical exercise ID.  Every dimension is explicit so database row order
    never affects an API result.
    """
    def order_key(exercise: object) -> tuple[int, float, str, int]:
        exercise_id = int(_value(exercise, "id") or 0)
        preference = preferences.get(exercise_id) if preferences is not None else None
        similarity = float(_value(exercise, "similarity") or 0.0)
        return (
            PREFERENCE_RANK.get(preference, 1) if preferences is not None else 0,
            -similarity if by_similarity else 0.0,
            normalized_display_name(exercise, language),
            exercise_id,
        )

    return sorted(
        exercises,
        key=order_key,
    )
