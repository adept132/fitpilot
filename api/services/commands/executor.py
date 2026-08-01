from api.services.commands.schema import Command, CommandType
from api.services.muscle_keys import key_for_muscle
from api.services.exercise_selection_engine import MIN_SETS, MAX_SETS


def _reindex(draft):
    for i, e in enumerate(draft):
        e["order_index"] = i
    return draft


def apply_inplace(draft: list, command: Command):
    p = command.params
    t = command.type
    warn = ""

    if t == CommandType.EXCLUDE_EXERCISE:
        draft = [e for e in draft if e["exercise_id"] != p["exercise_id"]]
        return _reindex(draft), warn

    if t == CommandType.SET_ALL_SETS:
        if "n" in p:
            for e in draft:
                e["target_sets"] = max(MIN_SETS, int(p["n"]))
        else:
            lo, hi = p["range"]
            for e in draft:
                e["target_sets"] = max(lo, min(hi, e["target_sets"]))
        return draft, warn

    if t == CommandType.SCALE_VOLUME:
        f = float(p["factor"])
        for e in draft:
            e["target_sets"] = max(MIN_SETS, min(MAX_SETS, round(e["target_sets"] * f)))
        return draft, warn

    if t == CommandType.ADJUST_MUSCLE_SETS and int(p.get("delta", 0)) < 0:
        keys = set(p["muscles"])
        remaining = -int(p["delta"])
        # remove sets from that muscle's exercises down to MIN_SETS, then drop whole exercises
        targets = [e for e in draft if key_for_muscle(e["primary_muscle"]) in keys]
        for e in sorted(targets, key=lambda x: -x["target_sets"]):
            while remaining > 0 and e["target_sets"] > MIN_SETS:
                e["target_sets"] -= 1
                remaining -= 1
        # if still need to remove, drop smallest exercises
        while remaining > 0:
            drop = min((e for e in draft if key_for_muscle(e["primary_muscle"]) in keys),
                       key=lambda x: x["target_sets"], default=None)
            if drop is None:
                break
            draft.remove(drop)
            remaining -= drop["target_sets"]
        return _reindex(draft), warn

    if t == CommandType.REPLACE_EXERCISE and "exercise_id" in p.get("to", {}):
        to = p["to"]
        for e in draft:
            if e["exercise_id"] == p["from_exercise_id"]:
                e.update({"exercise_id": to["exercise_id"], "name": to["name"],
                          "fatigue_tier": to.get("fatigue_tier", e["fatigue_tier"]),
                          "primary_muscle": to.get("primary_muscle", e["primary_muscle"]),
                          "secondary_muscle": to.get("secondary_muscle"), "superset_group_id": None})
                break
        return draft, warn

    raise ValueError(f"apply_inplace does not handle {t}")


# ===========================================================================
# Task 6: re-select branches + orchestration/replay + validator clamp
# ===========================================================================
import copy
import random

from api.services.exercise_selection_engine import (
    filter_pool, _allocate, is_compound,
)
from api.services import equipment as equip

# Mirrors AntiSuicideValidator.CAP_MAPPING in api/services/validator.py (which is
# method-local and not cleanly importable). Verified to match exactly; the
# validator RAISES on over-cap, so we trim locally down to these caps instead.
_CAPS = {"beginner": 6, "intermediate": 8, "advanced": 10}


def _to_draft_ex(ex, sets, order):
    secs = ex.secondary_muscle_groups or []
    return {"exercise_id": ex.id, "name": ex.name, "target_sets": sets, "order_index": order,
            "superset_group_id": None, "fatigue_tier": ex.fatigue_tier,
            "primary_muscle": ex.main_muscle_group, "secondary_muscle": secs[0] if secs else None}


def reselect_for_muscle(muscle_key, target_sets, pool, used_ids, constraints, seed,
                        compound_fraction=None):
    """Pick NEW exercises for one muscle at `target_sets`, honoring
    equipment/injury/disliked/used. Returns list of (Exercise, sets).

    `compound_fraction` defaults to None (uses `_allocate(target)`, engine default);
    when provided, uses `_allocate(target, compound_fraction)` for the base:iso split.
    """
    rng = random.Random(seed)
    allowed = constraints.get("allowed_equipment")
    prehab = constraints.get("prehab_flags", [])
    disliked = constraints.get("disliked_ids", set())
    cands = [e for e in filter_pool(pool, allowed, prehab)
             if key_for_muscle(e.main_muscle_group) == muscle_key
             and e.id not in used_ids and e.id not in disliked]
    if not cands:
        return []
    rng.shuffle(cands)
    cands.sort(key=lambda e: e.fatigue_tier)
    if compound_fraction is None:
        comp_chunks, iso_chunks = _allocate(int(target_sets))
    else:
        comp_chunks, iso_chunks = _allocate(int(target_sets), compound_fraction)
    out = []
    comps = [e for e in cands if is_compound(e)]
    isos = [e for e in cands if not is_compound(e)]
    for sets in comp_chunks:
        src = comps or isos
        if src:
            out.append((src.pop(0), sets))
    for sets in iso_chunks:
        src = isos or comps
        if src:
            out.append((src.pop(0), sets))
    return out  # list of (Exercise, sets)


def _reselect_violators(draft, pool, constraints, seed):
    """Remove draft exercises no longer passing filter_pool (equipment/injury) and
    re-select a same-muscle replacement at the same set count for each. Exercises not
    present in `pool` at all cannot be verified and are left in place."""
    allowed = constraints.get("allowed_equipment")
    prehab = constraints.get("prehab_flags", [])
    allowed_ids = {e.id for e in filter_pool(pool, allowed, prehab)}
    pool_ids = {e.id for e in pool}
    used = {e["exercise_id"] for e in draft}
    kept, added = [], []
    for e in draft:
        eid = e["exercise_id"]
        if eid in pool_ids and eid not in allowed_ids:
            mk = key_for_muscle(e["primary_muscle"])
            used.discard(eid)
            for rex, sets in reselect_for_muscle(mk, e["target_sets"], pool, used,
                                                 constraints, seed):
                used.add(rex.id)
                added.append(_to_draft_ex(rex, sets, len(kept) + len(added)))
        else:
            kept.append(e)
    return kept + added


def _apply_reselect(draft, command, pool, constraints, order_base):
    t, p = command.type, command.params

    if t == CommandType.ADD_MUSCLE:
        used = {e["exercise_id"] for e in draft}
        added = []
        for mk in p["muscles"]:
            target = p.get("target_override") or 4
            for ex, sets in reselect_for_muscle(mk, target, pool, used, constraints, command.seed):
                used.add(ex.id)
                added.append(_to_draft_ex(ex, sets, len(draft) + len(added)))
        return draft + added

    if t == CommandType.REPLACE_EXERCISE and "criteria" in p.get("to", {}):
        crit = p["to"]["criteria"]
        from_ex = next((e for e in draft if e["exercise_id"] == p["from_exercise_id"]), None)
        if not from_ex:
            return draft
        mk = key_for_muscle(from_ex["primary_muscle"])
        used = {e["exercise_id"] for e in draft}
        allowed = constraints.get("allowed_equipment")
        prehab = constraints.get("prehab_flags", [])
        cands = [e for e in filter_pool(pool, allowed, prehab)
                 if key_for_muscle(e.main_muscle_group) == mk and e.id not in used]
        if crit.get("category") == "isolation":
            cands = [e for e in cands if not is_compound(e)]
        elif crit.get("category") == "compound":
            cands = [e for e in cands if is_compound(e)]
        if crit.get("action"):
            cands = [e for e in cands if str(getattr(e, "action", "")).split(".")[-1] == crit["action"]]
        rng = random.Random(command.seed)
        rng.shuffle(cands)
        if cands:
            repl = cands[0]
            idx = draft.index(from_ex)
            draft[idx] = _to_draft_ex(repl, from_ex["target_sets"], from_ex["order_index"])
        return draft

    if t == CommandType.ACCENT_MUSCLE:
        # Additive accent: bump each muscle's total by ~1.5x, covering the delta
        # with NEW exercises. Existing exercises' sets are untouched; the final
        # per-muscle hard cap in _validate_and_trim handles any over-accent.
        used = {e["exercise_id"] for e in draft}
        added = []
        for mk in p["muscles"]:
            current = sum(e["target_sets"] for e in draft
                          if key_for_muscle(e["primary_muscle"]) == mk)
            new_target = round(current * 1.5) if current > 0 else 4
            extra = max(0, new_target - current)
            if extra > 0:
                for ex, sets in reselect_for_muscle(mk, extra, pool, used, constraints, command.seed):
                    used.add(ex.id)
                    added.append(_to_draft_ex(ex, sets, len(draft) + len(added)))
        return draft + added

    if t == CommandType.ADJUST_MUSCLE_SETS:
        # Only delta>0 reaches here (delta<0 is handled in-place by Task 5).
        delta = int(p.get("delta", 0))
        used = {e["exercise_id"] for e in draft}
        added = []
        for mk in p["muscles"]:
            remaining = delta
            targets = sorted(
                [e for e in draft if key_for_muscle(e["primary_muscle"]) == mk],
                key=lambda x: (x["fatigue_tier"], x["target_sets"]))
            # Distribute +delta across existing exercises, each up to MAX_SETS.
            progress = True
            while remaining > 0 and progress:
                progress = False
                for e in targets:
                    if remaining <= 0:
                        break
                    if e["target_sets"] < MAX_SETS:
                        e["target_sets"] += 1
                        remaining -= 1
                        progress = True
            # Any leftover sets seed new exercise(s) for the muscle.
            if remaining > 0:
                for ex, sets in reselect_for_muscle(mk, remaining, pool, used, constraints, command.seed):
                    used.add(ex.id)
                    added.append(_to_draft_ex(ex, sets, len(draft) + len(added)))
        return draft + added

    if t == CommandType.EQUIPMENT_CONSTRAINT:
        mode = p["mode"]
        eq = set(p["equipment"])
        current = constraints.get("allowed_equipment")
        if mode == "only":
            allowed = set(eq)
        else:  # "exclude"
            base = current if current is not None else set(equip.CANONICAL)
            allowed = set(base) - eq
        constraints["allowed_equipment"] = allowed  # mutate in place for later cmds
        return _reselect_violators(draft, pool, constraints, command.seed)

    if t == CommandType.ADD_INJURY:
        # Orchestrator already appended the flag to constraints["prehab_flags"].
        return _reselect_violators(draft, pool, constraints, command.seed)

    if t == CommandType.SET_BASE_ISO_RATIO:
        ratio = float(p["ratio"])
        compound_fraction = ratio / (ratio + 1.0)
        muscle_keys = []
        keep = []  # unknown-muscle exercises, cannot re-select -> keep as-is
        for e in draft:
            mk = key_for_muscle(e["primary_muscle"])
            if mk is None:
                keep.append(e)
            elif mk not in muscle_keys:
                muscle_keys.append(mk)
        used = set()
        new_draft = []
        for mk in muscle_keys:
            total = sum(e["target_sets"] for e in draft
                        if key_for_muscle(e["primary_muscle"]) == mk)
            picks = reselect_for_muscle(mk, total, pool, used, constraints, command.seed,
                                        compound_fraction=compound_fraction)
            if picks:
                for ex, sets in picks:
                    used.add(ex.id)
                    new_draft.append(_to_draft_ex(ex, sets, len(new_draft)))
            else:  # pool can't cover the muscle -> keep its existing exercises
                for e in draft:
                    if key_for_muscle(e["primary_muscle"]) == mk:
                        new_draft.append(e)
        return new_draft + keep

    raise ValueError(f"_apply_reselect does not handle {t}")


def _validate_and_trim(draft, experience_level):
    """Trim per-muscle set totals over the experience hard cap down to the cap,
    warning once per over-cap muscle. Mirrors AntiSuicideValidator.CAP_MAPPING."""
    cap = _CAPS.get(experience_level, 6)
    warns = []
    warned = set()
    by_muscle = {}
    for e in draft:
        by_muscle.setdefault(e["primary_muscle"], []).append(e)
    for muscle, exs in by_muscle.items():
        total = sum(e["target_sets"] for e in exs)
        while total > cap and exs:
            biggest = max(exs, key=lambda x: x["target_sets"])
            if biggest["target_sets"] > MIN_SETS:
                biggest["target_sets"] -= 1
                total -= 1
            else:
                # Every exercise for this muscle is already at the floor; drop the
                # smallest whole exercise so the per-muscle total can reach the cap.
                smallest = min(exs, key=lambda x: x["target_sets"])
                exs.remove(smallest)
                draft.remove(smallest)
                total -= smallest["target_sets"]
            if muscle not in warned:
                warned.add(muscle)
                warns.append(f"Объём по '{muscle}' урезан до лимита {cap}.")
    return draft, warns


_INPLACE = {CommandType.EXCLUDE_EXERCISE, CommandType.SET_ALL_SETS, CommandType.SCALE_VOLUME}


def apply(base_draft, command_log, pool, context, experience_level):
    """Replay the full command log on a deep copy of base_draft, dispatching each
    command to the in-place (Task 5) or re-select branch, then run the per-muscle
    validator clamp. Returns (final_draft, per_command_summaries, warnings)."""
    draft = copy.deepcopy(base_draft)
    constraints = {"allowed_equipment": context.get("allowed_equipment"),
                   "prehab_flags": list(context.get("prehab_flags", [])),
                   "disliked_ids": set(context.get("disliked_ids", set()))}
    summaries = []
    for cmd in command_log:
        if cmd.type == CommandType.CLARIFY:
            continue
        if cmd.type == CommandType.ADD_INJURY:
            constraints["prehab_flags"].append(cmd.params["flag"])
        if cmd.type == CommandType.EXCLUDE_EXERCISE:
            constraints["disliked_ids"].add(cmd.params["exercise_id"])
        if cmd.type in _INPLACE \
                or (cmd.type == CommandType.ADJUST_MUSCLE_SETS and int(cmd.params.get("delta", 0)) < 0) \
                or (cmd.type == CommandType.REPLACE_EXERCISE and "exercise_id" in cmd.params.get("to", {})):
            draft, _ = apply_inplace(draft, cmd)
        else:
            draft = _apply_reselect(draft, cmd, pool, constraints, len(draft))
        summaries.append(cmd.summary)
    draft, warns = _validate_and_trim(draft, experience_level)
    for i, e in enumerate(draft):
        e["order_index"] = i
    return draft, summaries, warns
