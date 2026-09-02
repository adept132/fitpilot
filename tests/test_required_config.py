import pytest

from app.config import required_env


def test_required_env_rejects_missing_and_blank_values():
    for env in ({}, {"DATABASE_URL": "   "}):
        with pytest.raises(RuntimeError, match="DATABASE_URL is required"):
            required_env("DATABASE_URL", env)


def test_required_env_returns_stripped_value():
    assert required_env("DATABASE_URL", {"DATABASE_URL": " postgres://db "}) == "postgres://db"
