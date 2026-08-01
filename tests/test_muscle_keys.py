from api.services.muscle_keys import key_for_muscle, to_system_key, RU_TO_KEY, KEY_TO_RU


def test_to_system_key_normalizes_all_encodings():
    # EN system keys pass through
    assert to_system_key("chest") == "chest"
    assert to_system_key("front_delts") == "front_delts"
    # UPPERCASE UI codes (custom splits)
    assert to_system_key("ANTERIOR_DELT") == "front_delts"
    assert to_system_key("LATERAL_DELT") == "side_delts"
    assert to_system_key("POSTERIOR_DELT") == "rear_delts"
    assert to_system_key("LATISSIMUS") == "lats"
    assert to_system_key("MIDDLE_BACK") == "mid_back"
    assert to_system_key("BICEPS") == "biceps"
    # Russian display names (system-split seed), incl. non-standard variants
    assert to_system_key("Грудь") == "chest"
    assert to_system_key("Передняя дельта") == "front_delts"
    assert to_system_key("Трапеции") == "traps"
    assert to_system_key("Бицепс бедра") == "hamstrings"
    assert to_system_key("Ягодичные") == "glutes"
    # unknown / unmappable
    assert to_system_key("Поясница") is None
    assert to_system_key("LOWER_BACK") is None
    assert to_system_key(None) is None


def test_key_for_muscle_translates_russian_to_system_key():
    assert key_for_muscle("Грудь") == "chest"
    assert key_for_muscle("широчайшие") == "lats"          # case-insensitive
    assert key_for_muscle(" Средняя часть спины ") == "mid_back"  # trims


def test_key_for_muscle_unknown_returns_none():
    assert key_for_muscle("Поясница") is None
    assert key_for_muscle("") is None
    assert key_for_muscle(None) is None


def test_maps_are_inverses_for_known_keys():
    assert KEY_TO_RU["chest"] == "грудь"
    assert RU_TO_KEY["грудь"] == "chest"
