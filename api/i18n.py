from typing import Literal


SupportedLanguage = Literal["ru", "en"]
SUPPORTED_LANGUAGES = {"ru", "en"}

# Server-originated strings are intentionally kept separate from mobile UI
# catalogs. Routes can add only the keys they own here as they become localized.
TRANSLATIONS: dict[SupportedLanguage, dict[str, str]] = {
    "ru": {},
    "en": {},
}


def normalize_language(value: str | None) -> SupportedLanguage | None:
    """Return a supported primary language tag, if one is present."""
    if not isinstance(value, str) or not value:
        return None

    primary = value.split(",", 1)[0].split("-", 1)[0].strip().lower()
    return primary if primary in SUPPORTED_LANGUAGES else None


def resolve_language(
    accept_language: str | None, profile_settings: dict | None
) -> SupportedLanguage:
    """Resolve language from the persisted preference, request, then default."""
    profile_language = normalize_language((profile_settings or {}).get("language"))
    return profile_language or normalize_language(accept_language) or "en"


def tr(language: SupportedLanguage, key: str, **params: object) -> str:
    """Translate a server string, using Russian when the chosen catalog lacks it."""
    catalog_language = normalize_language(language) or "en"
    template = TRANSLATIONS[catalog_language].get(key)
    if template is None:
        template = TRANSLATIONS["ru"].get(key, key)
    return template.format_map(dict(params))
