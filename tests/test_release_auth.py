import hashlib
import hmac

import pytest
from fastapi import HTTPException

from api.security.github_webhook import verify_github_signature
from api.security.release_publisher import require_release_publisher


@pytest.mark.parametrize("header", [None, "", "Basic abc", "Bearer wrong"])
def test_release_publisher_rejects_invalid_header(monkeypatch, header):
    monkeypatch.setenv("RELEASE_PUBLISHER_TOKEN", "a" * 48)

    with pytest.raises(HTTPException) as error:
        require_release_publisher(header)

    assert error.value.status_code == 401


def test_release_publisher_accepts_exact_bearer(monkeypatch):
    token = "b" * 48
    monkeypatch.setenv("RELEASE_PUBLISHER_TOKEN", token)

    assert require_release_publisher(f"Bearer {token}") is None


def test_release_publisher_rejects_non_ascii_token_with_401(monkeypatch):
    monkeypatch.setenv("RELEASE_PUBLISHER_TOKEN", "a" * 48)

    with pytest.raises(HTTPException) as error:
        require_release_publisher("Bearer token-\u00e9")

    assert error.value.status_code == 401


def test_github_signature_accepts_valid_sha256_signature(monkeypatch):
    secret = "webhook-secret"
    body = b'{"action":"published"}'
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", secret)
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    assert verify_github_signature(body, signature) is None


def test_github_signature_rejects_one_byte_body_modification(monkeypatch):
    secret = "webhook-secret"
    body = b'{"action":"published"}'
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", secret)
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    with pytest.raises(HTTPException) as error:
        verify_github_signature(body[:-1] + b"]", signature)

    assert error.value.status_code == 401


@pytest.mark.parametrize("signature", [None, "sha256=not-hex"])
def test_github_signature_rejects_missing_or_malformed_header(monkeypatch, signature):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "webhook-secret")

    with pytest.raises(HTTPException) as error:
        verify_github_signature(b'{"action":"published"}', signature)

    assert error.value.status_code == 401


def test_github_signature_rejects_non_ascii_digest_with_401(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "webhook-secret")

    with pytest.raises(HTTPException) as error:
        verify_github_signature(b'{"action":"published"}', "sha256=" + "a" * 63 + "\u00e9")

    assert error.value.status_code == 401
