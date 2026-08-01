import re
from typing import List
from api.services.commands.schema import Command, CommandType
from api.services.commands import slots
from api.services.exercise_matcher import ExerciseMatcher

# Token-level matching + clear-winner margin for the *ambiguity* scan below.
#
# Matching at whole-name granularity (char-overlap ratio of the full fragment
# against the full exercise name) over-triggers CLARIFY on the common path:
# "убери приседания" matches both "Приседания со штангой" AND "Подтягивания"
# at a similar whole-name char-overlap score, even though only one of them is
# a real candidate. Instead, score each candidate by its single BEST-matching
# WORD (still using ExerciseMatcher._calculate_similarity /
# _normalize_string — no new similarity math), then decide hit-vs-ambiguous by
# a floor + margin around the best score:
#   - a candidate must clear _ABS_FLOOR to be considered at all;
#   - among those, only candidates within _MARGIN of the best score are "hits";
#   - exactly one hit -> a single confident match; two or more -> ambiguous.
#
# Measured (via ExerciseMatcher token scores, normalized+lowered):
#   "жим" vs [Жим штанги, Жим гантелей]      -> 1.00, 1.00      (gap 0)    -> CLARIFY
#   "тягу" vs [Становая тяга, Тяга ... блока] -> 0.75, 0.75      (gap 0)    -> CLARIFY
#   "приседания" vs [Приседания..., Подтягивания] -> 1.00, 0.55 (gap ~0.45) -> single hit
#   "жим штанги" vs [Жим штанги, Жим гантелей]    -> 0.75, 0.46 (gap ~0.29) -> single hit
#   "выпады" vs [Выпады с гантелями, Жим в тренажёре] -> 1.00, 0.13 (gap 0.87, and
#   0.13 doesn't even clear _ABS_FLOOR) -> single hit
_ABS_FLOOR = 0.35
_MARGIN = 0.15


def _split_clauses(text: str) -> List[str]:
    parts = re.split(r"\s+и\s+|,|;|\bа также\b", text)
    return [p.strip() for p in parts if p and p.strip()]


def _exercise_matches(fragment: str, draft: list) -> list:
    """All draft exercises whose name fuzzy-matches the fragment (for ambiguity).

    Computes similarity directly (rather than delegating to
    slots.match_exercise, which enforces a stricter 0.5 "single confident
    match" floor) so that multiple plausible candidates can be collected and
    routed to CLARIFY instead of only ever returning at most one hit.

    See the module-level comment above for the token-level + margin design.
    """
    frag_norm = ExerciseMatcher._normalize_string(fragment)
    # Fix A (round-trip closure): a re-sent CLARIFY option is the EXACT full
    # exercise name. For names that share a word (e.g. "Подъём на бицепс" /
    # "Подъём на трицепс") the token-margin scorer below ties both even for the
    # full-name option, dead-ending in another CLARIFY. If the fragment equals
    # exactly one candidate's normalized name, resolve it deterministically.
    # Free-form first-pass input ("убери подъем" -> "подъем") equals NEITHER
    # full name, so it falls through to token-margin scoring unchanged.
    exact = [ex for ex in draft if ExerciseMatcher._normalize_string(ex["name"]) == frag_norm]
    if len(exact) == 1:
        return exact
    scored = []
    for ex in draft:
        name_norm = ExerciseMatcher._normalize_string(ex["name"])
        words = name_norm.split() or [name_norm]
        score = max(ExerciseMatcher._calculate_similarity(frag_norm, w) for w in words)
        scored.append((ex, score))
    pool = [(ex, score) for ex, score in scored if score >= _ABS_FLOOR]
    if not pool:
        return []
    best = max(score for _, score in pool)
    return [ex for ex, score in pool if score >= best - _MARGIN]


def _clarify(q, options=None):
    return Command(type=CommandType.CLARIFY, params={"question": q, "options": options or []},
                   confidence=0.0, summary=q)


def _parse_clause(clause: str, draft: list, seed: int) -> Command:
    t = clause.lower().replace("ё", "е")

    # SET_ALL_SETS ("все по N" / "все по N-M")
    if re.search(r"\bвсе\b.*\bпо\b", t) or re.search(r"\bкажд", t):
        rng = slots.extract_range(t)
        if rng:
            return Command(CommandType.SET_ALL_SETS, {"range": list(rng)}, seed, summary=f"все по {rng[0]}-{rng[1]}")
        n = slots.extract_number(t)
        if n:
            return Command(CommandType.SET_ALL_SETS, {"n": n}, seed, summary=f"все по {n}")

    # SCALE_VOLUME (global session intensity — only when no specific muscle is
    # named; "грудь потяжелее" should become a per-muscle ADJUST_MUSCLE_SETS,
    # not a blanket SCALE_VOLUME, even though "потяжел" matches both cues).
    if not slots.resolve_muscles(t):
        if re.search(r"тяжел|интенсивн|потяжел|усложн", t):
            return Command(CommandType.SCALE_VOLUME, {"factor": 1.2}, seed, summary="тяжелее (×1.2)")
        if re.search(r"легч|полегч|проще|облегч", t):
            return Command(CommandType.SCALE_VOLUME, {"factor": 0.8}, seed, summary="легче (×0.8)")

    # SET_BASE_ISO_RATIO
    if re.search(r"больше баз|больше компаунд", t):
        return Command(CommandType.SET_BASE_ISO_RATIO, {"ratio": 3.0}, seed, summary="больше базы")
    if re.search(r"больше изоляц", t):
        return Command(CommandType.SET_BASE_ISO_RATIO, {"ratio": 1.0}, seed, summary="больше изоляции")

    # EQUIPMENT_CONSTRAINT
    if re.search(r"\bбез\b", t):
        eq = slots.resolve_equipment(t)
        if eq:
            return Command(CommandType.EQUIPMENT_CONSTRAINT, {"mode": "exclude", "equipment": eq}, seed,
                           summary=f"без {', '.join(eq)}")
    if re.search(r"только", t):
        eq = slots.resolve_equipment(t)
        if eq:
            return Command(CommandType.EQUIPMENT_CONSTRAINT, {"mode": "only", "equipment": eq}, seed,
                           summary=f"только {', '.join(eq)}")

    # ADD_INJURY
    if re.search(r"болит|болят|травм", t):
        flag = slots.resolve_injury(t)
        if flag:
            return Command(CommandType.ADD_INJURY, {"flag": flag}, seed, summary=f"травма: {flag}")

    # REPLACE
    # Fix B (round-trip closure): GREEDY first group so a re-sent option whose
    # from-name itself contains " на " ("замени Подъём на бицепс на изоляцию")
    # splits at the LAST " на " -> from="Подъём на бицепс", to="изоляцию",
    # instead of the non-greedy split at the FIRST " на " which truncated the
    # from-name and re-looped to CLARIFY. Single-" на " comments (e.g. "замени
    # жим на изоляцию") are unaffected: greedy == non-greedy there.
    m = re.search(r"замен[иь]?\s+(.+)\s+на\s+(.+)$", t)
    if m:
        from_frag, to_frag = m.group(1), m.group(2)
        hits = _exercise_matches(from_frag, draft)
        if len(hits) == 0:
            return _clarify(f"Не нашёл упражнение «{from_frag}» в плане.")
        if len(hits) > 1:
            # Fix 1: options must be RE-SENDABLE full comments (not bare
            # names) so the mobile client can resend the tapped option
            # verbatim as new_comment and have it resolve unambiguously.
            return _clarify("Какое именно упражнение заменить?",
                            [f"замени {h['name']} на {to_frag}" for h in hits])
        from_ex = hits[0]
        # criteria in `to`
        crit = {}
        if "изоляц" in to_frag:
            crit["category"] = "isolation"
        elif "баз" in to_frag or "компаунд" in to_frag:
            crit["category"] = "compound"
        if re.search(r"жим", to_frag):
            crit["action"] = "push"
        elif re.search(r"тяг", to_frag):
            crit["action"] = "pull"
        eq = slots.resolve_equipment(to_frag)
        if eq:
            crit["equipment"] = eq
        if crit:
            return Command(CommandType.REPLACE_EXERCISE,
                           {"from_exercise_id": from_ex["exercise_id"], "to": {"criteria": crit}}, seed,
                           summary=f"замена «{from_ex['name']}» по критерию")
        return _clarify(f"Уточни, на что заменить «{from_ex['name']}».")

    # EXCLUDE
    if re.search(r"убер|удал|не хочу|ненавиж|исключ", t):
        frag = re.sub(r"^(убер[иь]?|удал[иь]?|не хочу|ненавижу|исключ[иь]?)\s+", "", t).strip()
        hits = _exercise_matches(frag, draft)
        if len(hits) == 1:
            return Command(CommandType.EXCLUDE_EXERCISE, {"exercise_id": hits[0]["exercise_id"]}, seed,
                           summary=f"убрать «{hits[0]['name']}»")
        if len(hits) > 1:
            # Fix 1: same re-sendable-option guarantee for EXCLUDE ambiguity.
            return _clarify("Какое упражнение убрать?", [f"убери {h['name']}" for h in hits])
        return _clarify(f"Не нашёл «{frag}» в плане.")

    # ACCENT
    if re.search(r"акцент|упор|фокус", t):
        ms = slots.resolve_muscles(t)
        if ms:
            return Command(CommandType.ACCENT_MUSCLE, {"muscles": ms}, seed, summary=f"акцент: {', '.join(ms)}")

    # ADD_MUSCLE ("добавь <мышца>")
    if re.search(r"добав", t) and slots.resolve_muscles(t):
        ms = slots.resolve_muscles(t)
        return Command(CommandType.ADD_MUSCLE, {"muscles": ms}, seed, summary=f"добавить: {', '.join(ms)}")

    # ADJUST_MUSCLE_SETS (+N / -N / больше / меньше <мышца>)
    ms = slots.resolve_muscles(t)
    if ms:
        n = slots.extract_number(t)
        if re.search(r"меньше|убав|снизь|сократи|легч|полегч|проще|мягче", t) or re.search(r"-\s*\d", t):
            delta = -(n if n else 2)
            return Command(CommandType.ADJUST_MUSCLE_SETS, {"muscles": ms, "delta": delta}, seed,
                           summary=f"−{abs(delta)} на {', '.join(ms)}")
        if re.search(r"больше|добав|увелич|потяжел", t) or re.search(r"\+\s*\d", t):
            delta = (n if n else 2)
            return Command(CommandType.ADJUST_MUSCLE_SETS, {"muscles": ms, "delta": delta}, seed,
                           summary=f"+{delta} на {', '.join(ms)}")

    return _clarify(f"Не понял: «{clause}». Уточни?")


def parse(comment: str, draft_exercises: list, *, rng_seed: int = 0) -> List[Command]:
    clauses = _split_clauses(comment)
    out = []
    for i, clause in enumerate(clauses):
        out.append(_parse_clause(clause, draft_exercises, rng_seed + i))
    return out or [_clarify("Пустой комментарий.")]
