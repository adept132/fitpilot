from __future__ import annotations

import http.client
from types import SimpleNamespace

import pytest

from tests.deploy import caddy_harness


def test_large_publish_reads_early_413_after_broken_pipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EarlyCloseConnection:
        def __init__(self) -> None:
            self.closed = False

        def request(self, *args, **kwargs) -> None:
            raise BrokenPipeError("server closed after rejecting Content-Length")

        def getresponse(self):
            return SimpleNamespace(
                status=413,
                getheaders=lambda: [("Content-Length", "0")],
                read=lambda: b"",
            )

        def close(self) -> None:
            self.closed = True

    connection = EarlyCloseConnection()
    monkeypatch.setattr(
        caddy_harness.http.client,
        "HTTPConnection",
        lambda *args, **kwargs: connection,
    )
    harness = object.__new__(caddy_harness.CaddyHarness)
    harness.caddy_port = 8443

    response = harness.request_large_publish(256 * 1024 * 1024 + 1)

    assert response.status == 413
    assert response.body == b""
    assert connection.closed


def test_large_publish_still_fails_and_closes_when_no_response_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ClosedWithoutResponseConnection:
        def __init__(self) -> None:
            self.closed = False

        def request(self, *args, **kwargs) -> None:
            raise BrokenPipeError("server closed without a response")

        def getresponse(self):
            raise http.client.RemoteDisconnected("no response")

        def close(self) -> None:
            self.closed = True

    connection = ClosedWithoutResponseConnection()
    monkeypatch.setattr(
        caddy_harness.http.client,
        "HTTPConnection",
        lambda *args, **kwargs: connection,
    )
    harness = object.__new__(caddy_harness.CaddyHarness)
    harness.caddy_port = 8443

    with pytest.raises(http.client.RemoteDisconnected, match="no response"):
        harness.request_large_publish(256 * 1024 * 1024 + 1)

    assert connection.closed
