import hashlib
import hmac
import secrets

from fastapi import HTTPException, status

from app.config import required_env


def verify_github_signature(body: bytes, signature: str | None) -> None:
    secret = required_env("GITHUB_WEBHOOK_SECRET")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    algorithm, separator, digest = (signature or "").partition("=")
    valid = separator == "=" and algorithm == "sha256"
    valid = valid and secrets.compare_digest(digest, expected)
    if not valid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unauthorized")
