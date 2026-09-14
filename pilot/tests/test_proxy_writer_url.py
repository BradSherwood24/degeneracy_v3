"""Composed-URL guardrails for the proxy write/read path — the test the executor suite lacked.

The 2026-09-14 first-armed-window create-404 (three POSTs to
``/trade-api/v2/trade-api/v2/portfolio/events/orders``) slipped through because every executor test
asserted the *path handed to a fake writer*, never the URL the writer would actually dial. A fake
writer cannot double a prefix it never composes. These tests inject a fake TRANSPORT (the layer
below ``ProxyWriter`` / ``ProxyAuth``) and assert the exact URL — so a doubled-prefix regression
fails here, at the real composition site.

Every check runs WITHOUT a network (injected ``http_post`` / ``http_delete`` / ``http_get``).
"""

from __future__ import annotations

from urllib.parse import urlencode

import pytest

from service.proxy_auth import REST_PREFIX, ProxyAuth, compose_rest_url
from service.proxy_writer import ProxyWriter
from service.v32.executor import (
    OPEN_ORDERS_PATH,
    ORDER_STATUS_PATH_TMPL,
    REL_BATCH_CREATE,
    REL_SINGLE_CREATE,
    cancel_path,
)

BASE = "http://127.0.0.1:8642"

# The proxy's order-CREATE path set, copied VERBATIM from degeneracy-proxy ``_ORDER_CREATE_PATHS``
# (proxy.py). A create URL whose path is not one of these routes uncapped / 404s at the venue — the
# exact failure of the doubled-prefix bug. Kept as literal constants so this cross-check does not
# depend on importing the proxy source.
PROXY_ORDER_CREATE_PATHS = frozenset({
    "/trade-api/v2/portfolio/events/orders",
    "/trade-api/v2/portfolio/events/orders/batched",
    "/trade-api/v2/portfolio/orders",
    "/trade-api/v2/portfolio/orders/batched",
})


class FakeResp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = ""

    def json(self):
        return self._body


def _make_writer():
    """A ProxyWriter whose three transports record the exact URL they were asked to dial.

    The GET is delegated through a real ``ProxyAuth`` (as in production), so its composition is
    exercised too. ``get_urls`` records the *effective* request URL (url + urlencoded params) —
    what ``requests`` would actually put on the wire — so a ?status=resting is asserted end to end.
    """
    post_urls: list[str] = []
    delete_urls: list[str] = []
    get_urls: list[str] = []

    def http_post(url, body, timeout):
        post_urls.append(url)
        return FakeResp(200, {"order": {"order_id": "oid", "fill_count": "0", "remaining_count": "1"}})

    def http_delete(url, timeout):
        delete_urls.append(url)
        return FakeResp(200, {})

    def http_get(url, params, timeout):
        get_urls.append(url + ("?" + urlencode(params) if params else ""))
        return FakeResp(200, {})

    auth = ProxyAuth(base_url=BASE, http_get=http_get, sleep=lambda _s: None)
    writer = ProxyWriter(proxy_auth=auth, base_url=BASE, http_post=http_post,
                         http_delete=http_delete, sleep=lambda _s: None)
    return writer, post_urls, delete_urls, get_urls


# ---------------------------------------------------------------------------
# (a) Exact composed URL for EVERY writer call the executor makes.
# ---------------------------------------------------------------------------
def test_single_create_url_exact():
    w, posts, _, _ = _make_writer()
    w.rest_post(REL_SINGLE_CREATE, {"client_order_id": "c1"})
    assert posts == ["http://127.0.0.1:8642/trade-api/v2/portfolio/events/orders"]


def test_batch_create_url_exact():
    w, posts, _, _ = _make_writer()
    w.rest_post(REL_BATCH_CREATE, {"orders": []})
    assert posts == ["http://127.0.0.1:8642/trade-api/v2/portfolio/events/orders/batched"]


def test_cancel_url_exact():
    w, _, deletes, _ = _make_writer()
    # shard-aware cancel (2026-09-14 fix): the final URL carries ?exchange_index=2.
    w.rest_delete(cancel_path("ORD-1", 2))
    assert deletes == [
        "http://127.0.0.1:8642/trade-api/v2/portfolio/events/orders/ORD-1?exchange_index=2"]


def test_cancel_url_no_shard_fallback():
    w, _, deletes, _ = _make_writer()
    w.rest_delete(cancel_path("ORD-1", None))
    assert deletes == ["http://127.0.0.1:8642/trade-api/v2/portfolio/events/orders/ORD-1"]


def test_order_status_url_exact():
    w, _, _, gets = _make_writer()
    w.rest_get(ORDER_STATUS_PATH_TMPL.format(order_id="ORD-1"))
    assert gets == ["http://127.0.0.1:8642/trade-api/v2/portfolio/orders/ORD-1"]


def test_open_orders_url_exact():
    w, _, _, gets = _make_writer()
    w.rest_get(OPEN_ORDERS_PATH, {"status": "resting"})
    assert gets == ["http://127.0.0.1:8642/trade-api/v2/portfolio/orders?status=resting"]


# ---------------------------------------------------------------------------
# (b) The two create URLs' paths are members of the proxy's create-path set.
# ---------------------------------------------------------------------------
def _url_path(url: str) -> str:
    return url[len(BASE):].split("?", 1)[0]


def test_create_urls_are_proxy_recognized_creates():
    w, posts, _, _ = _make_writer()
    w.rest_post(REL_SINGLE_CREATE, {"client_order_id": "c1"})
    w.rest_post(REL_BATCH_CREATE, {"orders": []})
    for url in posts:
        assert _url_path(url) in PROXY_ORDER_CREATE_PATHS, (
            f"{_url_path(url)!r} is not a proxy-recognized create path -> uncapped/404")
    # and the specific pair the executor emits
    assert _url_path(posts[0]) == "/trade-api/v2/portfolio/events/orders"
    assert _url_path(posts[1]) == "/trade-api/v2/portfolio/events/orders/batched"


# ---------------------------------------------------------------------------
# (c) Regression: an ALREADY-prefixed path must not double the prefix.
# ---------------------------------------------------------------------------
def test_rest_post_already_prefixed_path_yields_single_prefix():
    w, posts, _, _ = _make_writer()
    # The exact bug input: the envelope's FULL /trade-api/v2/... create path.
    w.rest_post("/trade-api/v2/portfolio/events/orders", {"client_order_id": "c1"})
    assert posts == ["http://127.0.0.1:8642/trade-api/v2/portfolio/events/orders"]
    assert "/trade-api/v2/trade-api/v2/" not in posts[0]


def test_rest_delete_and_get_already_prefixed_paths_single_prefix():
    w, _, deletes, gets = _make_writer()
    w.rest_delete("/trade-api/v2/portfolio/events/orders/ORD-1")
    w.rest_get("/trade-api/v2/portfolio/orders/ORD-1")
    assert deletes == ["http://127.0.0.1:8642/trade-api/v2/portfolio/events/orders/ORD-1"]
    assert gets == ["http://127.0.0.1:8642/trade-api/v2/portfolio/orders/ORD-1"]
    assert "/trade-api/v2/trade-api/v2/" not in deletes[0]
    assert "/trade-api/v2/trade-api/v2/" not in gets[0]


def test_relative_and_prefixed_paths_compose_identically():
    # The whole point: relative and full spellings of the SAME endpoint dial the same URL.
    assert (compose_rest_url(BASE, REL_SINGLE_CREATE)
            == compose_rest_url(BASE, "/trade-api/v2/portfolio/events/orders")
            == "http://127.0.0.1:8642/trade-api/v2/portfolio/events/orders")


def test_compose_rest_url_asserts_single_prefix_and_rejects_relative_miss():
    # A path missing the leading slash is a programming error (fail closed).
    with pytest.raises(ValueError):
        compose_rest_url(BASE, "portfolio/events/orders")
    # REST_PREFIX itself is a valid (edgecase) prefixed path.
    assert compose_rest_url(BASE, REST_PREFIX + "/x") == BASE + REST_PREFIX + "/x"
