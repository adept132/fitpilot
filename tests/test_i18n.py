import pytest

from api.i18n import TRANSLATIONS, normalize_language, resolve_language, tr


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ru-RU,ru;q=0.9", "ru"),
        ("en-US", "en"),
        ("de-DE", None),
        (None, None),
    ],
)
def test_normalize_language_returns_supported_primary_tag(raw, expected):
    assert normalize_language(raw) == expected


@pytest.mark.parametrize(
    ("accept_language", "expected"),
    [
        ("RU;q=1", "ru"),
        ("de;q=.9, ru;q=.8", "ru"),
        ("ru;q=.2, en;q=.8", "en"),
        ("ru;q=.8, en;q=.8", "ru"),
        ("ru;q=0, en;q=.8", "en"),
        ("ru;q=not-a-number, en;q=.5", "en"),
        ("ru;q=not-a-number", "en"),
    ],
)
def test_resolve_language_selects_highest_quality_supported_header_range(
    accept_language, expected
):
    assert resolve_language(accept_language, None) == expected


def test_profile_language_overrides_accept_language_header():
    assert resolve_language("en-US", {"language": "ru"}) == "ru"


def test_accept_language_is_used_when_profile_has_no_valid_language():
    assert resolve_language("ru-RU", {"language": "de"}) == "ru"


def test_non_string_profile_language_is_ignored():
    assert resolve_language("en-US", {"language": 7}) == "en"


def test_profile_language_precedes_backend_fallback():
    assert resolve_language(None, {"language": "ru"}) == "ru"


def test_resolve_language_falls_back_to_english():
    assert resolve_language(None, None) == "en"


def test_translation_uses_requested_catalog_and_interpolates_named_parameters(
    monkeypatch,
):
    monkeypatch.setitem(TRANSLATIONS, "en", {"welcome": "Welcome, {name}!"})

    assert tr("en", "welcome", name="Ada") == "Welcome, Ada!"


def test_translation_falls_back_to_russian_for_missing_requested_catalog_key(
    monkeypatch,
):
    monkeypatch.setitem(TRANSLATIONS, "en", {})
    monkeypatch.setitem(TRANSLATIONS, "ru", {"welcome": "Привет, {name}!"})

    assert tr("en", "welcome", name="Ада") == "Привет, Ада!"
