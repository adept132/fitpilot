from api.services.commands.schema import Command, CommandType


def test_command_roundtrip():
    c = Command(type=CommandType.SET_ALL_SETS, params={"n": 3}, seed=7, summary="все по 3")
    d = c.to_dict()
    assert d["type"] == "SET_ALL_SETS"
    c2 = Command.from_dict(d)
    assert c2.type == CommandType.SET_ALL_SETS and c2.params == {"n": 3} and c2.seed == 7


def test_all_command_types_exist():
    for name in ["EXCLUDE_EXERCISE", "REPLACE_EXERCISE", "ADJUST_MUSCLE_SETS",
                 "ACCENT_MUSCLE", "ADD_MUSCLE", "SET_ALL_SETS", "SCALE_VOLUME",
                 "EQUIPMENT_CONSTRAINT", "ADD_INJURY", "SET_BASE_ISO_RATIO", "CLARIFY"]:
        assert CommandType[name].value == name
