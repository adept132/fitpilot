import secrets
from typing import Annotated

from fastapi import Header, HTTPException, status

from app.config import required_env


def require_release_publisher(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    scheme, separator, token = (authorization or "").partition(" ")
    expected = required_env("RELEASE_PUBLISHER_TOKEN")
    valid = separator == " " and scheme.lower() == "bearer"
    valid = valid and token.isascii() and expected.isascii()
    valid = valid and secrets.compare_digest(token, expected)
    if not valid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unauthorized")
