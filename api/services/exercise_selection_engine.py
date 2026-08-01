"""Deterministic, rule-based exercise selection.

Given per-session muscle set targets (from VolumeService) and constraints, choose
concrete exercises. This module does NOT compute volume and does NOT assign reps
(reps resolve at runtime from fatigue_tier via api.services.resolvers).
"""
from typing import Optional, Iterable

from api.services.exercise_pattern_tags import ExerciseAction
from api.services import equipment as equip

# prehab flag -> actions that are outright forbidden (never relaxed).
INJURY_FORBID: dict[str, set[ExerciseAction]] = {
    "lower_back": {ExerciseAction.hinge},
    "knees": set(),
    "shoulders": set(),
    "elbows": set(),
}
# prehab flag -> actions kept but treated as "limited" (deprioritized in ordering).
INJURY_LIMIT: dict[str, set[ExerciseAction]] = {
    "lower_back": {ExerciseAction.squat},
    "knees": {ExerciseAction.squat},
    "shoulders": {ExerciseAction.push},
    "elbows": {ExerciseAction.flexion, ExerciseAction.extension},
}


def is_axial(ex) -> bool:
    """Heavy squat/hinge loads the spine axially."""
    return ex.action in (ExerciseAction.squat, ExerciseAction.hinge) and ex.fatigue_tier == 1


def superset_eligible(ex) -> bool:
    """Only light isolation, never axial, may enter a superset."""
    return ex.fatigue_tier == 3 and ex.action not in (ExerciseAction.squat, ExerciseAction.hinge)


def _forbidden_actions(prehab_flags: Iterable[str]) -> set[ExerciseAction]:
    forbidden: set[ExerciseAction] = set()
    for flag in prehab_flags or []:
        forbidden |= INJURY_FORBID.get(flag, set())
    return forbidden


def limited_actions(prehab_flags: Iterable[str]) -> set[ExerciseAction]:
    limited: set[ExerciseAction] = set()
    for flag in prehab_flags or []:
        limited |= INJURY_LIMIT.get(flag, set())
    return limited


def _exercise_equipment_keys(ex) -> set[str]:
    keys = set(equip.normalize_equipment_list(ex.equipment_needed or []))
    if not keys:
        keys = {equip.BODYWEIGHT}  # no equipment == bodyweight
    return keys


def filter_pool(pool, allowed_equipment_keys: Optional[set[str]], prehab_flags) -> list:
    """Drop injury-forbidden exercises and (optionally) gate by equipment."""
    forbidden = _forbidden_actions(prehab_flags)
    out = []
    for ex in pool:
        if ex.action in forbidden:
            continue
        if allowed_equipment_keys is not None:
            if not (_exercise_equipment_keys(ex) & allowed_equipment_keys):
                continue
        out.append(ex)
    return out


from api.services.fatigue_tiers import get_muscle_score


def order_by_systemic_cost(selected: list[dict]) -> list[dict]:
    """Rule #2: heaviest CNS cost first. Ascending fatigue_tier (1 first),
    tie-break by descending primary-muscle mass score. Deterministic."""
    return sorted(
        selected,
        key=lambda x: (x["fatigue_tier"], -get_muscle_score(x["primary_muscle"], True)),
    )


import random
from dataclasses import dataclass
from api.services.muscle_keys import key_for_muscle


@dataclass
class SelectionPolicy:
    axial_cap: int = 2
    fractional_coeff: float = 0.5


@dataclass
class SelectionConfig:
    use_supersets: bool = False
    accent_muscle: Optional[str] = None  # EN system key
    seed: Optional[int] = None


@dataclass
class SelectedExercise:
    exercise_id: int
    name: str
    sets: int
    order_index: int
    superset_group_id: Optional[str]
    fatigue_tier: int
    primary_muscle: str            # Russian
    secondary_muscle: Optional[str]  # Russian, first secondary


MIN_SETS = 2
MAX_SETS = 4


def is_compound(ex) -> bool:
    """Compound ("Базовое") vs isolation, from the exercise category."""
    return (getattr(ex, "category", "") or "").strip().lower() == "базовое"


def _split_sets(total: int) -> list[int]:
    """Split `total` sets into the fewest exercises with each in [MIN_SETS, MAX_SETS].
    Even distribution; never yields a 1-set chunk. A tiny positive target (1) is
    closed by a single MIN_SETS exercise (min-2 rule wins over exact budget)."""
    if total <= 0:
        return []
    if total < MIN_SETS:
        return [MIN_SETS]
    n = -(-total // MAX_SETS)  # ceil(total / MAX_SETS): fewest exercises
    base, rem = divmod(total, n)
    sizes = [base + 1] * rem + [base] * (n - rem)
    return [max(MIN_SETS, min(MAX_SETS, s)) for s in sizes]


def _allocate(target: int, compound_fraction: float = 2 / 3) -> tuple[list[int], list[int]]:
    """Return (compound_chunks, isolation_chunks) of per-exercise set counts.
    - target <= MAX_SETS: a single compound-preferred exercise (one 4-set beats 2+2).
    - target  > MAX_SETS: keep compound:isolation ~= 2:1 by sets. Each chunk 2..4."""
    if target <= 0:
        return [], []
    if target <= MAX_SETS:
        return _split_sets(target), []
    compound_sets = round(target * compound_fraction)
    iso_sets = target - compound_sets
    if iso_sets == 1:  # never leave a lone set; fold it into the compound side
        compound_sets += 1
        iso_sets = 0
    return _split_sets(compound_sets), _split_sets(iso_sets)


def select_exercises(session_targets, pool, allowed_equipment_keys, prehab_flags,
                     config: SelectionConfig,
                     policy: SelectionPolicy = SelectionPolicy()) -> list:
    rng = random.Random(config.seed)
    targets: dict[str, int] = {k: int(round(v)) for k, v in session_targets.items() if v and v > 0}

    # Split the filtered pool into compound / isolation candidate lists per muscle.
    filtered = filter_pool(pool, allowed_equipment_keys, prehab_flags)
    comp_by_key: dict[str, list] = {}
    iso_by_key: dict[str, list] = {}
    for ex in filtered:
        k = key_for_muscle(ex.main_muscle_group)
        if k is None:
            continue
        (comp_by_key if is_compound(ex) else iso_by_key).setdefault(k, []).append(ex)

    # Deterministic candidate order within each pool: shuffle, then stable tier asc.
    for d in (comp_by_key, iso_by_key):
        for lst in d.values():
            rng.shuffle(lst)
            lst.sort(key=lambda e: e.fatigue_tier)

    axial_count = [0]
    # Global count of chosen exercises per fatigue_tier. Balance rule: no tier may
    # exceed the combined count of all other tiers (i.e. no tier is a majority).
    tier_counts: dict[int, int] = {}

    def pick_best(lst: list, used_actions: set, used_vectors: set):
        """Choose the best candidate for a slot and remove+return it. Priority:
        1) tier balance — prefer the least-represented fatigue_tier so far;
        2) action/vector diversity — prefer a movement whose action/vector are
           not yet used for this muscle. Respects the axial cap."""
        best_i, best_key = None, None
        for i, ex in enumerate(lst):
            if is_axial(ex) and axial_count[0] >= policy.axial_cap:
                continue
            a = getattr(ex, "action", None)
            v = getattr(ex, "vector", None)
            diversity = (2 if a not in used_actions else 0) + (1 if v not in used_vectors else 0)
            balance = -tier_counts.get(ex.fatigue_tier, 0)
            key = (balance, diversity, -i)  # -i: deterministic, keeps earlier/lower-tier on ties
            if best_key is None or key > best_key:
                best_key, best_i = key, i
        if best_i is None:
            return None
        ex = lst.pop(best_i)
        if is_axial(ex):
            axial_count[0] += 1
        return ex

    chosen: list[dict] = []
    # Largest target first, key name as deterministic tiebreak.
    for muscle_key in sorted(targets, key=lambda k: (-targets[k], k)):
        comps = comp_by_key.get(muscle_key, [])
        isos = iso_by_key.get(muscle_key, [])
        if not comps and not isos:
            continue

        compound_chunks, iso_chunks = _allocate(targets[muscle_key])
        # (chunk_sets, prefer_compound) work items.
        slots = [(s, True) for s in compound_chunks] + [(s, False) for s in iso_chunks]
        used_actions: set = set()  # action/vector diversity is tracked per muscle
        used_vectors: set = set()
        for sets, prefer_compound in slots:
            primary, fallback = (comps, isos) if prefer_compound else (isos, comps)
            ex = (pick_best(primary, used_actions, used_vectors)
                  or pick_best(fallback, used_actions, used_vectors))
            if ex is None:
                break  # no exercises left for this muscle -> partial coverage
            tier_counts[ex.fatigue_tier] = tier_counts.get(ex.fatigue_tier, 0) + 1
            used_actions.add(getattr(ex, "action", None))
            used_vectors.add(getattr(ex, "vector", None))
            secs = ex.secondary_muscle_groups or []
            chosen.append({
                "exercise_id": ex.id,
                "name": ex.name,
                "sets": sets,
                "fatigue_tier": ex.fatigue_tier,
                "primary_muscle": ex.main_muscle_group,
                "secondary_muscle": secs[0] if secs else None,
            })

    # Balance rule (hard, primary over base:isolation): no fatigue_tier may exceed
    # the combined count of the others. Swap an exercise of the dominant tier for a
    # same-muscle candidate of a different tier — but ONLY when the swap strictly
    # reduces the peak tier count (so we don't wreck base:iso for an unreachable
    # balance when the pool lacks the tier variety).
    def _tier_counts() -> dict[int, int]:
        tc: dict[int, int] = {}
        for c in chosen:
            tc[c["fatigue_tier"]] = tc.get(c["fatigue_tier"], 0) + 1
        return tc

    while chosen:
        counts = _tier_counts()
        n = len(chosen)
        if all(v <= n - v for v in counts.values()):
            break
        dominant = max(counts, key=lambda k: (counts[k], -k))
        peak = counts[dominant]
        swapped = False
        for ci, c in enumerate(chosen):
            if c["fatigue_tier"] != dominant:
                continue
            mk = key_for_muscle(c["primary_muscle"])
            leftover = comp_by_key.get(mk, []) + iso_by_key.get(mk, [])
            cands = [e for e in leftover
                     if e.fatigue_tier != dominant
                     and not (is_axial(e) and axial_count[0] >= policy.axial_cap)]
            cands.sort(key=lambda e: (counts.get(e.fatigue_tier, 0), e.id))
            for repl in cands:
                new_counts = dict(counts)
                new_counts[dominant] -= 1
                new_counts[repl.fatigue_tier] = new_counts.get(repl.fatigue_tier, 0) + 1
                if max(new_counts.values()) < peak:
                    (comp_by_key.get(mk, []) if is_compound(repl)
                     else iso_by_key.get(mk, [])).remove(repl)
                    if is_axial(repl):
                        axial_count[0] += 1
                    secs = repl.secondary_muscle_groups or []
                    chosen[ci] = {
                        "exercise_id": repl.id, "name": repl.name, "sets": c["sets"],
                        "fatigue_tier": repl.fatigue_tier,
                        "primary_muscle": repl.main_muscle_group,
                        "secondary_muscle": secs[0] if secs else None,
                    }
                    swapped = True
                    break
            if swapped:
                break
        if not swapped:
            break

    # Strict ascending fatigue_tier for the whole session (tie-break: bigger muscle first).
    ordered = order_by_systemic_cost(chosen)
    result = [
        SelectedExercise(
            exercise_id=c["exercise_id"], name=c["name"], sets=c["sets"],
            order_index=i, superset_group_id=None,
            fatigue_tier=c["fatigue_tier"], primary_muscle=c["primary_muscle"],
            secondary_muscle=c["secondary_muscle"],
        )
        for i, c in enumerate(ordered)
    ]
    if config.use_supersets:
        result = group_supersets(result, rng)
    return result


import uuid


def _muscles_of(sel: "SelectedExercise") -> set:
    m = {sel.primary_muscle}
    if sel.secondary_muscle:
        m.add(sel.secondary_muscle)
    return m


def group_supersets(selected: list, rng: random.Random) -> list:
    """Pair non-heavy exercises into supersets and reorder so partners are adjacent.

    Eligible = every exercise that is NOT a heavy compound (fatigue_tier != 1; this
    also excludes axial squats/hinges, which are tier 1). Pairs are formed greedily
    between exercises whose muscles don't overlap (the AntiSuicideValidator forbids
    same-muscle and tier1+tier1 supersets). Partners are then placed next to each
    other and the list is re-indexed, because the plan editor groups a superset only
    from CONSECUTIVE items sharing a superset_group_id."""
    eligible = [i for i, s in enumerate(selected) if s.fatigue_tier != 1]
    used: set[int] = set()
    for a in range(len(eligible)):
        i = eligible[a]
        if i in used:
            continue
        for b in range(a + 1, len(eligible)):
            j = eligible[b]
            if j in used:
                continue
            if _muscles_of(selected[i]) & _muscles_of(selected[j]):
                continue
            gid = str(uuid.UUID(int=rng.getrandbits(128)))
            selected[i].superset_group_id = gid
            selected[j].superset_group_id = gid
            used.update({i, j})
            break

    # Reorder so each superset's members sit together (keep the first member's slot).
    ordered: list = []
    emitted: set[int] = set()
    for idx, s in enumerate(selected):
        if idx in emitted:
            continue
        emitted.add(idx)
        ordered.append(s)
        if s.superset_group_id:
            for jdx in range(idx + 1, len(selected)):
                if jdx not in emitted and selected[jdx].superset_group_id == s.superset_group_id:
                    emitted.add(jdx)
                    ordered.append(selected[jdx])
    for k, s in enumerate(ordered):
        s.order_index = k
    return ordered
