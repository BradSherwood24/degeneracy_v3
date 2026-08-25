"""ProxyAuth: fresh-fetch-per-dial, 503-unsigned handling, REST retry semantics — no network."""

from __future__ import annotations

import pytest
import requests

from service.proxy_auth import ProxyAuth, ProxyError, ProxyUnsignedError


class FakeResp:
    def __init__(self, status_code: int, body=None, text: str = "") -> None:
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = text

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def make_auth(responses, sleep_calls=None):
    """responses: list of FakeResp OR Exception (raised) served in order per call."""
    calls: list[tuple] = []
    it = iter(responses)

    def http_get(url, params, timeout):
        calls.append((url, params, timeout))
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r

    def sleep(s):
        if sleep_calls is not None:
            sleep_calls.append(s)

    return ProxyAuth(http_get=http_get, sleep=sleep), calls


# === ws_connect_params ===


def test_ws_connect_params_returns_url_and_headers() -> None:
    body = {"ws_url": "wss://api.example/ws", "headers": {"KALSHI-ACCESS-KEY": "k", "X": "y"}}
    auth, calls = make_auth([FakeResp(200, body)])
    ws_url, headers = auth.ws_connect_params()
    assert ws_url == "wss://api.example/ws"
    assert headers == {"KALSHI-ACCESS-KEY": "k", "X": "y"}
    assert calls[0][0].endswith("/ws-auth")


def test_ws_connect_params_fetched_fresh_each_call() -> None:
    body1 = {"ws_url": "wss://a", "headers": {"KALSHI-ACCESS-TIMESTAMP": "1"}}
    body2 = {"ws_url": "wss://a", "headers": {"KALSHI-ACCESS-TIMESTAMP": "2"}}
    auth, calls = make_auth([FakeResp(200, body1), FakeResp(200, body2)])
    _, h1 = auth.ws_connect_params()
    _, h2 = auth.ws_connect_params()
    # A re-dial re-mints: a NEW GET is issued and the ephemeral signature timestamp differs.
    assert len(calls) == 2
    assert h1["KALSHI-ACCESS-TIMESTAMP"] == "1"
    assert h2["KALSHI-ACCESS-TIMESTAMP"] == "2"


def test_ws_connect_params_503_unsigned_raises_specific() -> None:
    auth, _ = make_auth([FakeResp(503, {"error": "proxy_unsigned"})])
    with pytest.raises(ProxyUnsignedError):
        auth.ws_connect_params()


def test_ws_connect_params_non_200_raises_and_is_not_retried() -> None:
    auth, calls = make_auth([FakeResp(500, {"error": "boom"})])
    with pytest.raises(ProxyError):
        auth.ws_connect_params()
    assert len(calls) == 1  # auth is time-sensitive: no retry, one GET


def test_ws_connect_params_missing_fields_raises() -> None:
    auth, _ = make_auth([FakeResp(200, {"ws_url": "wss://a"})])  # no headers
    with pytest.raises(ProxyError):
        auth.ws_connect_params()


# === rest_get ===


def test_rest_get_success_returns_json() -> None:
    auth, calls = make_auth([FakeResp(200, {"markets": [1, 2]})])
    assert auth.rest_get("/markets", {"status": "open"}) == {"markets": [1, 2]}
    url, params, _ = calls[0]
    assert url.endswith("/trade-api/v2/markets")
    assert params == {"status": "open"}


def test_rest_get_path_must_start_with_slash() -> None:
    auth, _ = make_auth([])
    with pytest.raises(ValueError):
        auth.rest_get("markets")


def test_rest_get_retries_on_429_then_succeeds() -> None:
    sleeps: list[float] = []
    auth, calls = make_auth([FakeResp(429), FakeResp(429), FakeResp(200, {"ok": 1})], sleeps)
    assert auth.rest_get("/x") == {"ok": 1}
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]  # backoff 1s, 2s before the 3rd (successful) attempt


def test_rest_get_retries_on_5xx_then_raises_after_four_attempts() -> None:
    sleeps: list[float] = []
    auth, calls = make_auth([FakeResp(500)] * 4, sleeps)
    with pytest.raises(ProxyError):
        auth.rest_get("/x")
    assert len(calls) == 4  # 4 attempts total
    assert sleeps == [1.0, 2.0, 4.0]  # backoff 1s, 2s, 4s


def test_rest_get_retries_on_timeout_then_succeeds() -> None:
    sleeps: list[float] = []
    auth, calls = make_auth(
        [requests.exceptions.Timeout(), FakeResp(200, {"ok": True})], sleeps
    )
    assert auth.rest_get("/x") == {"ok": True}
    assert len(calls) == 2
    assert sleeps == [1.0]


def test_rest_get_connection_error_exhausts_and_raises() -> None:
    sleeps: list[float] = []
    auth, calls = make_auth([requests.exceptions.ConnectionError()] * 4, sleeps)
    with pytest.raises(requests.exceptions.ConnectionError):
        auth.rest_get("/x")
    assert len(calls) == 4
    assert sleeps == [1.0, 2.0, 4.0]


def test_rest_get_4xx_not_retried() -> None:
    sleeps: list[float] = []
    auth, calls = make_auth([FakeResp(404, {"error": "not_found"})], sleeps)
    with pytest.raises(ProxyError):
        auth.rest_get("/x")
    assert len(calls) == 1  # 404 is not transient
    assert sleeps == []
