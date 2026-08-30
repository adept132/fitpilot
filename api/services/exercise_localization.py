"""Exercise presentation helpers.

Russian ``name``/``description`` remain the durable catalog text.  English is
an optional presentation layer for system exercises only; user-entered text is
never translated or replaced here.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


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


def sort_exercises(exercises: Iterable[object], language: str) -> list[object]:
    """Sort presentation rows by locale, with the canonical ID as tie-breaker."""
    return sorted(
        exercises,
        key=lambda exercise: (
            display_name(exercise, language).casefold(),
            int(_value(exercise, "id") or 0),
        ),
    )
