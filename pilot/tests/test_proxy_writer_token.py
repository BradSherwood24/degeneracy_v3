"""Client-side proxy shared-secret header (Phase H).

When the hosted proxy is started with PROXY_TOKEN, it 401s any non-GET lacking the
X-DV3-Token header. The pilot supplies it from the DV3_PROXY_TOKEN env var on WRITES
ONLY (POST create / DELETE cancel); GETs stay unauthenticated (the proxy leaves GETs
open). Absent env var -> no header, identical to today (the laptop proxy is tokenless).

No network: ``requests.post`` / ``requests.delete`` are monkeypatched to capture the
headers the default transport would send.
"""

from __future__ import annotations

import requests

from service.proxy_writer import _dv3_token_headers, DV3_PROXY_TOKEN_ENV, ProxyWriter


# --- the pure helper -------------------------------------------------------- #

def test_token_headers_absent_env_is_empty(monkeypatch):
    monkeypatch.delenv(DV3_PROXY_TOKEN_ENV, raising=False)
    assert _dv3_token_headers() == {}


def test_token_headers_blank_env_is_empty(monkeypatch):
    monkeypatch.setenv(DV3_PROXY_TOKEN_ENV, "   ")
    assert _dv3_token_headers() == {}


def test_token_headers_set(monkeypatch):
    monkeypatch.setenv(DV3_PROXY_TOKEN_ENV, "s3kr3t")
    assert _dv3_token_headers() == {"X-DV3-Token": "s3kr3t"}


# --- the default transport actually attaches it on writes ------------------- #

def _capture_requests(monkeypatch):
    seen: dict = {}

    def fake_post(url, json=None, timeout=None, headers=None):
        seen["post"] = {"url": url, "headers": headers}
        class R:
            status_code = 200
            def json(self):  # noqa: D401
                return {"ok": True}
            text = ""
        return R()

    def fake_delete(url, timeout=None, headers=None):
        seen["delete"] = {"url": url, "headers": headers}
        class R:
            status_code = 200
            def json(self):
                return {"ok": True}
            text = ""
        return R()

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "delete", fake_delete)
    return seen


def test_default_post_sends_token_header_when_set(monkeypatch):
    monkeypatch.setenv(DV3_PROXY_TOKEN_ENV, "abc123")
    seen = _capture_requests(monkeypatch)
    ProxyWriter._default_post("http://127.0.0.1:8642/x", {"a": 1}, 10.0)
    assert seen["post"]["headers"] == {"X-DV3-Token": "abc123"}


def test_default_post_no_header_when_unset(monkeypatch):
    monkeypatch.delenv(DV3_PROXY_TOKEN_ENV, raising=False)
    seen = _capture_requests(monkeypatch)
    ProxyWriter._default_post("http://127.0.0.1:8642/x", {"a": 1}, 10.0)
    assert seen["post"]["headers"] == {}


def test_default_delete_sends_token_header_when_set(monkeypatch):
    monkeypatch.setenv(DV3_PROXY_TOKEN_ENV, "abc123")
    seen = _capture_requests(monkeypatch)
    ProxyWriter._default_delete("http://127.0.0.1:8642/x/ORD-1", 10.0)
    assert seen["delete"]["headers"] == {"X-DV3-Token": "abc123"}


def test_default_delete_no_header_when_unset(monkeypatch):
    monkeypatch.delenv(DV3_PROXY_TOKEN_ENV, raising=False)
    seen = _capture_requests(monkeypatch)
    ProxyWriter._default_delete("http://127.0.0.1:8642/x/ORD-1", 10.0)
    assert seen["delete"]["headers"] == {}
