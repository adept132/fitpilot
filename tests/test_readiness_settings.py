"""Тумблер отключения чек-ина (спека P0-07 §10)."""

from api.services.readiness import repository


def test_checkin_enabled_defaults_to_true():
    assert repository.checkin_enabled(None) is True
    assert repository.checkin_enabled({}) is True
    assert repository.checkin_enabled({"readiness": {}}) is True


def test_checkin_can_be_switched_off():
    assert repository.checkin_enabled({"readiness": {"checkin_enabled": False}}) is False


def test_non_dict_readiness_settings_do_not_crash():
    # settings — свободный JSONB, туда мог попасть мусор от старых сборок.
    assert repository.checkin_enabled({"readiness": "yes"}) is True
    assert repository.checkin_enabled({"readiness": None}) is True
