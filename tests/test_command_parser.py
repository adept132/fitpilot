from api.services.commands.parser import parse
from api.services.commands.schema import CommandType

DRAFT = [{"exercise_id": 1, "name": "Выпады с гантелями", "primary_muscle": "Квадрицепсы"},
         {"exercise_id": 2, "name": "Жим в тренажёре", "primary_muscle": "Грудь"}]


def _types(cmds):
    return [c.type for c in cmds]


def test_exclude():
    cmds = parse("убери выпады", DRAFT)
    assert _types(cmds) == [CommandType.EXCLUDE_EXERCISE]
    assert cmds[0].params["exercise_id"] == 1


def test_set_all_and_scale():
    assert parse("все по 3 подхода", DRAFT)[0].type == CommandType.SET_ALL_SETS
    assert parse("все по 3 подхода", DRAFT)[0].params["n"] == 3
    assert parse("сделай тяжелее", DRAFT)[0].type == CommandType.SCALE_VOLUME
    assert parse("сделай тяжелее", DRAFT)[0].params["factor"] == 1.2


def test_multi_command():
    cmds = parse("убери выпады и грудь потяжелее", DRAFT)
    assert CommandType.EXCLUDE_EXERCISE in _types(cmds)
    assert CommandType.ADJUST_MUSCLE_SETS in _types(cmds)


def test_equipment_only_and_exclude():
    only = parse("только гантели", DRAFT)[0]
    assert only.type == CommandType.EQUIPMENT_CONSTRAINT and only.params == {"mode": "only", "equipment": ["dumbbell"]}
    exc = parse("без штанги", DRAFT)[0]
    assert exc.params["mode"] == "exclude" and exc.params["equipment"] == ["barbell"]


def test_ambiguous_exercise_clarifies():
    draft2 = [{"exercise_id": 1, "name": "Жим штанги"}, {"exercise_id": 2, "name": "Жим гантелей"}]
    cmds = parse("замени жим на изоляцию", draft2)
    assert cmds[0].type == CommandType.CLARIFY  # two "жим" candidates


def test_muscle_decrease_routes_to_adjust():
    # Fix A: a "lighter" comment that names a muscle must route to
    # ADJUST_MUSCLE_SETS (delta -2), mirroring how "грудь потяжелее" routes to +2,
    # instead of silently falling through to a generic CLARIFY.
    cmd = parse("грудь полегче", DRAFT)[0]
    assert cmd.type == CommandType.ADJUST_MUSCLE_SETS
    assert cmd.params["delta"] == -2
    assert cmd.params["muscles"] == ["chest"]


def test_muscleless_scale_still_global():
    # Fix A must not break muscle-less global scaling.
    dec = parse("сделай легче", DRAFT)[0]
    assert dec.type == CommandType.SCALE_VOLUME and dec.params["factor"] == 0.8
    inc = parse("сделай тяжелее", DRAFT)[0]
    assert inc.type == CommandType.SCALE_VOLUME and inc.params["factor"] == 1.2


def test_near_tie_exercise_clarifies():
    # Fix B: near-tie second candidate (token-level score for "тяга" is the
    # same ~0.75 against both names) must promote to a hit so an ambiguous
    # exclude CLARIFIES instead of silently excluding just the top match.
    draft = [{"exercise_id": 1, "name": "Становая тяга"},
             {"exercise_id": 2, "name": "Тяга верхнего блока"}]
    cmd = parse("убери тягу", draft)[0]
    assert cmd.type == CommandType.CLARIFY
    # Fix 1: options must be full RE-SENDABLE comments ("убери <name>"), not
    # bare exercise names, so the mobile client can resend the tapped option
    # verbatim as new_comment and have it parse to a real command.
    assert "убери Становая тяга" in cmd.params["options"]
    assert "убери Тяга верхнего блока" in cmd.params["options"]


def test_option_roundtrip_exclude_resends_full_comment():
    # Fix 1 (CRITICAL): the CLARIFY options for an ambiguous EXCLUDE must be
    # full comments that, when resent verbatim as new_comment, resolve to a
    # single unambiguous EXCLUDE_EXERCISE for the tapped exercise (not another
    # dead-end CLARIFY).
    draft = [{"exercise_id": 1, "name": "Жим штанги"}, {"exercise_id": 2, "name": "Жим гантелей"}]
    cmds = parse("убери жим", draft)
    assert cmds[0].type == CommandType.CLARIFY
    options = cmds[0].params["options"]
    assert len(options) == 2
    option = options[0]
    reparsed = parse(option, draft)
    assert len(reparsed) == 1
    assert reparsed[0].type == CommandType.EXCLUDE_EXERCISE
    assert reparsed[0].params["exercise_id"] == 1


def test_option_roundtrip_replace_resends_full_comment():
    # Fix 1 (CRITICAL): same round-trip guarantee for an ambiguous REPLACE —
    # the option must carry the original "to" fragment so re-parsing yields a
    # single REPLACE_EXERCISE with the right criteria, not a CLARIFY.
    draft = [{"exercise_id": 1, "name": "Жим штанги"}, {"exercise_id": 2, "name": "Жим гантелей"}]
    cmds = parse("замени жим на изоляцию", draft)
    assert cmds[0].type == CommandType.CLARIFY
    options = cmds[0].params["options"]
    assert len(options) == 2
    option = options[0]
    reparsed = parse(option, draft)
    assert len(reparsed) == 1
    assert reparsed[0].type == CommandType.REPLACE_EXERCISE
    assert reparsed[0].params["from_exercise_id"] == 1
    assert reparsed[0].params["to"]["criteria"] == {"category": "isolation"}


def test_option_roundtrip_exclude_shared_word_names():
    # Fix A (round-trip closure): for exercises that SHARE a word ("Подъём на
    # бицепс" / "Подъём на трицепс"), the token-margin scorer ties both
    # candidates even for the resent full-name option. The resent option is an
    # EXACT normalized-name match for exactly one candidate, so an exact-name
    # short-circuit must resolve it to a single EXCLUDE (not re-loop to CLARIFY).
    draft = [{"exercise_id": 1, "name": "Подъём на бицепс"},
             {"exercise_id": 2, "name": "Подъём на трицепс"}]
    cmds = parse("убери подъем", draft)
    assert cmds[0].type == CommandType.CLARIFY  # first pass: still ambiguous
    option = cmds[0].params["options"][0]
    assert option == "убери Подъём на бицепс"
    reparsed = parse(option, draft)
    assert len(reparsed) == 1
    assert reparsed[0].type == CommandType.EXCLUDE_EXERCISE
    assert reparsed[0].params["exercise_id"] == 1


def test_option_roundtrip_replace_shared_word_names():
    # Fix B (greedy split): a resent REPLACE option contains TWO " на "
    # separators ("замени Подъём на бицепс на изоляцию"). Greedy from-group
    # splitting at the LAST " на " keeps the full from-name intact, then Fix A's
    # exact-name check resolves it to a single REPLACE (not a re-loop CLARIFY).
    draft = [{"exercise_id": 1, "name": "Подъём на бицепс"},
             {"exercise_id": 2, "name": "Подъём на трицепс"}]
    cmds = parse("замени подъем на изоляцию", draft)
    assert cmds[0].type == CommandType.CLARIFY  # first pass: still ambiguous
    option = cmds[0].params["options"][0]
    assert option == "замени Подъём на бицепс на изоляцию"
    reparsed = parse(option, draft)
    assert len(reparsed) == 1
    assert reparsed[0].type == CommandType.REPLACE_EXERCISE
    assert reparsed[0].params["from_exercise_id"] == 1
    assert reparsed[0].params["to"]["criteria"] == {"category": "isolation"}


def test_clear_winner_squats_not_ambiguous_with_pullups():
    # Fix 2 regression: whole-name char-overlap over-triggered CLARIFY here
    # ("Подтягивания" scored ~0.545 against "убери приседания" under the old
    # whole-name SequenceMatcher scoring). Token-level matching + a
    # clear-winner margin must collapse this to a single confident hit.
    draft = [{"exercise_id": 1, "name": "Приседания со штангой"},
             {"exercise_id": 2, "name": "Подтягивания"}]
    cmd = parse("убери приседания", draft)[0]
    assert cmd.type == CommandType.EXCLUDE_EXERCISE
    assert cmd.params["exercise_id"] == 1
