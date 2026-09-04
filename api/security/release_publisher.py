import secrets
from typing import Annotated

from fastapi import Header, HTTPException, status

from app.config import required_env


def _require_bearer(authorization: str | None, setting_name: str) -> None:
    scheme, separator, token = (authorization or "").partition(" ")
    expected = required_env(setting_name)
    valid = separator == " " and scheme.lower() == "bearer"
    valid = valid and token.isascii() and expected.isascii()
    valid = valid and secrets.compare_digest(token, expected)
    if not valid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unauthorized")


def require_release_publisher(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    _require_bearer(authorization, "RELEASE_PUBLISHER_TOKEN")


def require_release_operator(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Authorize a manual release operator, separate from automation CI."""

    _require_bearer(authorization, "RELEASE_OPERATOR_TOKEN")
