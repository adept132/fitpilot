from api.services.commands import slots


def test_resolve_muscle_single_and_group():
    assert slots.resolve_muscles("меньше груди") == ["chest"]
    assert set(slots.resolve_muscles("больше ног")) == {"quads", "hamstrings", "glutes", "calves"}
    assert set(slots.resolve_muscles("акцент на плечи")) == {"front_delts", "side_delts", "rear_delts"}


def test_resolve_equipment_and_injury():
    assert slots.resolve_equipment("только гантели") == ["dumbbell"]
    assert slots.resolve_equipment("без штанги") == ["barbell"]
    assert slots.resolve_injury("болят колени") == "knees"


def test_resolve_equipment_multiword_synonyms():
    assert slots.resolve_equipment("вес тела") == ["bodyweight"]
    # Must resolve to smith only, not fall through to generic "машина" -> block_machine
    assert slots.resolve_equipment("машина смита") == ["smith"]
    # Longer phrase must win over the "свободн" colloquial branch and over generic
    # "тренажер" -> block_machine substring match.
    assert slots.resolve_equipment("тренажер со свободным весом") == ["free_machine"]


def test_resolve_muscles_compound_does_not_drop_individual_muscle():
    assert set(slots.resolve_muscles("меньше груди, больше плеч")) == {
        "chest", "front_delts", "side_delts", "rear_delts",
    }


def test_numbers():
    assert slots.extract_number("все по 3 подхода") == 3
    assert slots.extract_range("все по 2-3 подхода") == (2, 3)
    assert slots.extract_number("нет чисел") is None


def test_match_exercise_fuzzy():
    cands = [{"name": "Жим гантелей лёжа"}, {"name": "Подъём на бицепс"}]
    assert slots.match_exercise("жим гантелей", cands)["name"] == "Жим гантелей лёжа"
    assert slots.match_exercise("присед", cands) is None
