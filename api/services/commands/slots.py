import re
from typing import Optional
from api.services.muscle_keys import RU_TO_KEY
from api.services import equipment as equip
from api.services.exercise_matcher import ExerciseMatcher

# Muscle synonyms (lowercased, common colloquial + case-ish forms) -> EN system key.
_MUSCLE_WORDS = {
    "грудь": "chest", "груди": "chest", "грудную": "chest",
    "широчайшие": "lats",
    "квадрицепс": "quads", "квадрицепсы": "quads", "квадры": "quads",
    "бицепс бедра": "hamstrings", "бицепсы ног": "hamstrings",
    "ягодицы": "glutes", "ягодичные": "glutes", "ягодиц": "glutes",
    "икры": "calves", "икр": "calves",
    "пресс": "abs", "трапеция": "traps", "трапеции": "traps",
    "бицепс": "biceps", "бицепсы": "biceps", "бицуху": "biceps",
    "трицепс": "triceps", "трицепсы": "triceps",
    "передняя дельта": "front_delts", "средняя дельта": "side_delts", "задняя дельта": "rear_delts",
}
_MUSCLE_GROUPS = {
    "ноги": ["quads", "hamstrings", "glutes", "calves"],
    "ног": ["quads", "hamstrings", "glutes", "calves"],
    "плечи": ["front_delts", "side_delts", "rear_delts"],
    "плеч": ["front_delts", "side_delts", "rear_delts"],
    "дельты": ["front_delts", "side_delts", "rear_delts"],
    "спина": ["lats", "mid_back"], "спины": ["lats", "mid_back"],
    "руки": ["biceps", "triceps"], "рук": ["biceps", "triceps"],
}
_INJURY = {"колен": "knees", "поясниц": "lower_back", "спин": "lower_back",
           "плеч": "shoulders", "локт": "elbows"}


def resolve_muscles(text: str) -> list:
    t = text.lower().replace("ё", "е")
    found: list = []
    for phrase, keys in _MUSCLE_GROUPS.items():
        if phrase in t:
            for k in keys:
                if k not in found:
                    found.append(k)
    for word, key in _MUSCLE_WORDS.items():
        if key and word in t and key not in found:
            found.append(key)
    if found:
        return found
    # also try the canonical RU->key map (covers exact muscle names) as a fallback
    # only when nothing else matched (keeps the group-test exactness above).
    for ru, key in RU_TO_KEY.items():
        if ru in t and key not in found:
            found.append(key)
    return found


def resolve_equipment(text: str) -> list:
    t = text.lower().replace("ё", "е")
    out = []
    # Longest-phrase-first substring matching over the canonical synonym map so
    # multi-word synonyms (e.g. "машина смита", "тренажер со свободным весом")
    # match before shorter overlapping ones (e.g. generic "машина"/"тренажер").
    working = t
    consumed_spans = []
    for phrase in sorted(equip._SYNONYMS.keys(), key=len, reverse=True):
        idx = working.find(phrase)
        if idx == -1:
            continue
        canon = equip._SYNONYMS[phrase]
        if canon not in out:
            out.append(canon)
        consumed_spans.append((idx, idx + len(phrase)))
        # blank out the matched span so shorter overlapping synonyms don't also fire
        working = working[:idx] + (" " * len(phrase)) + working[idx + len(phrase):]
    for phrase in ("свободн", "свободный вес", "свободные веса"):
        if phrase in working:
            for c in (equip.BARBELL, equip.DUMBBELL):
                if c not in out:
                    out.append(c)
    return out


def resolve_injury(text: str) -> Optional[str]:
    t = text.lower()
    for stem, flag in _INJURY.items():
        if stem in t:
            return flag
    return None


def extract_range(text: str):
    m = re.search(r"(\d+)\s*[-–]\s*(\d+)", text)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return (min(a, b), max(a, b))
    return None


def extract_number(text: str):
    if extract_range(text):
        return None
    m = re.search(r"(\d+)", text)
    return int(m.group(1)) if m else None


def match_exercise(fragment: str, candidates: list):
    best, best_score = None, 0.0
    for c in candidates:
        s = ExerciseMatcher._calculate_similarity(
            ExerciseMatcher._normalize_string(fragment),
            ExerciseMatcher._normalize_string(c["name"]),
        )
        if s > best_score:
            best, best_score = c, s
    return best if best_score >= 0.5 else None
