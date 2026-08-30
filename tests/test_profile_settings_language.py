import pytest
from pydantic import ValidationError

from api.schemas.оnboarding import UpdateSettingsRequest


def test_settings_request_keeps_a_supported_language_value():
    assert UpdateSettingsRequest(language="en").language == "en"


def test_settings_request_rejects_an_unsupported_language_value():
    with pytest.raises(ValidationError):
        UpdateSettingsRequest(language="de")
