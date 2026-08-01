"""RU <-> EN system-key muscle translation.

`Exercise.main_muscle_group` is Russian; volume budgets / VolumeService use EN
system keys. This module is the single bridge between the two spaces, built by
inverting the canonical MUSCLE_TRANSLATION_MAP.
"""
from typing import Optional

from api.services.volume_calculator import MUSCLE_TRANSLATION_MAP

# RU (lowercase) -> EN system key
RU_TO_KEY: dict[str, str] = dict(MUSCLE_TRANSLATION_MAP)
# EN system key -> RU (lowercase)
KEY_TO_RU: dict[str, str] = {v: k for k, v in MUSCLE_TRANSLATION_MAP.items()}


def key_for_muscle(main_muscle_group: Optional[str]) -> Optional[str]:
    """Russian muscle name -> EN system key, or None if unknown."""
    if not main_muscle_group:
        return None
    return RU_TO_KEY.get(main_muscle_group.strip().lower())


# The set of canonical EN system keys (values of MUSCLE_TRANSLATION_MAP).
_SYSTEM_KEYS: set[str] = set(RU_TO_KEY.values())

# DayMuscleTarget.muscle_group_id is stored in inconsistent spaces:
#   - system splits (seed_splits.py): Russian display names, some not in the
#     translation map ("Трапеции", "Бицепс бедра", "Ягодичные");
#   - custom splits (mobile day-builder): UPPERCASE codes ("ANTERIOR_DELT" ...).
# This table maps everything the RU_TO_KEY / _SYSTEM_KEYS checks don't already
# cover (lowercased) -> EN system key. Codes that lowercase to an existing
# system key (chest, biceps, quads, traps, ...) need no entry.
_EXTRA_TO_KEY: dict[str, str] = {
    # UPPERCASE codes (lowercased) that differ from the system key
    "anterior_delt": "front_delts",
    "lateral_delt": "side_delts",
    "posterior_delt": "rear_delts",
    "latissimus": "lats",
    "middle_back": "mid_back",
    # Russian seed variants missing from MUSCLE_TRANSLATION_MAP
    "трапеции": "traps",
    "бицепс бедра": "hamstrings",
    "ягодичные": "glutes",
}


def to_system_key(raw: Optional[str]) -> Optional[str]:
    """Normalize any muscle encoding (EN system key, Russian display name, or
    UPPERCASE UI code) to the canonical EN system key, or None if unknown."""
    if not raw:
        return None
    v = str(raw).strip().lower()
    if v in _SYSTEM_KEYS:
        return v
    if v in RU_TO_KEY:
        return RU_TO_KEY[v]
    return _EXTRA_TO_KEY.get(v)
