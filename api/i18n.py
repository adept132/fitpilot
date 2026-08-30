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

    primary = value.split(",", 1)[0].split(";", 1)[0].split("-", 1)[0]
    primary = primary.strip().lower()
    return primary if primary in SUPPORTED_LANGUAGES else None


def _quality_value(parameters: list[str]) -> float | None:
    """Return a valid HTTP quality value, or None for a malformed range."""
    quality = 1.0
    seen_quality = False
    for parameter in parameters:
        name, separator, raw_value = parameter.partition("=")
        if name.strip().lower() != "q":
            continue
        if not separator or seen_quality:
            return None
        seen_quality = True
        try:
            quality = float(raw_value.strip())
        except ValueError:
            return None
        if not 0 <= quality <= 1:
            return None
    return quality


def _header_language(accept_language: str | None) -> SupportedLanguage | None:
    if not isinstance(accept_language, str):
        return None

    selected: SupportedLanguage | None = None
    selected_quality = -1.0
    for language_range in accept_language.split(","):
        parts = language_range.split(";")
        language = normalize_language(parts[0])
        quality = _quality_value(parts[1:])
        if language is None or quality is None or quality == 0:
            continue
        if quality > selected_quality:
            selected = language
            selected_quality = quality
    return selected


def resolve_language(
    accept_language: str | None, profile_settings: dict | None
) -> SupportedLanguage:
    """Resolve language from the persisted preference, request, then default."""
    profile_language = normalize_language((profile_settings or {}).get("language"))
    return profile_language or _header_language(accept_language) or "en"


def tr(language: SupportedLanguage, key: str, **params: object) -> str:
    """Translate a server string, using Russian when the chosen catalog lacks it."""
    catalog_language = normalize_language(language) or "en"
    template = TRANSLATIONS[catalog_language].get(key)
    if template is None:
        template = TRANSLATIONS["ru"].get(key, key)
    return template.format_map(dict(params))
