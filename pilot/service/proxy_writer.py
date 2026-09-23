"""proxy_writer.py — the write-side companion to ``ProxyAuth`` (POST create / DELETE cancel / GET).

House law (identical to ``ProxyAuth``): ALL Kalshi API access goes through the local signing proxy at
http://127.0.0.1:8642; this process never handles key material and never reads the proxy's .env /
*.pem. The proxy signs and enforces its own caps/budget; we just dial and read the reply.

Three verbs, with the retry discipline the order path demands:

  * ``rest_post(path, body)``   — order CREATE. Sent EXACTLY ONCE, NEVER retried. A create that
    succeeded server-side but lost its response would DUPLICATE on retry (V2 law); idempotency is the
    ``client_order_id`` in the body, not a retry. Returns a ``WriteResponse`` (status + parsed body)
    so the caller classifies a proxy cap/budget 403, an upstream 4xx/5xx, or a success itself.
  * ``rest_delete(path)``       — order CANCEL. DELETE is idempotent (cancelling an already-terminal
    order is the SAME goal achieved), so it is bounded-retried on 429/5xx/connection blips exactly
    like a GET. A 404 is returned to the caller (the V2 cancel treats a body ``error.code ==
    "not_found"`` as terminal-success — see ``service.v32.executor``).
  * ``rest_get(path, params)``  — order/position/market READS (status poll, open-orders sweep,
    settlement). Bounded-retried; delegated to a ``ProxyAuth`` so the retry semantics are shared.

Every HTTP edge is injected (``http_post`` / ``http_delete`` / the ``ProxyAuth``'s ``http_get``) so
every path is tested WITHOUT a network — the pilot never dials the live proxy from an automated
context.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from service.proxy_auth import DEFAULT_PROXY_BASE, ProxyAuth, compose_rest_url

logger = logging.getLogger(__name__)

POST_TIMEOUT = 10.0
DELETE_TIMEOUT = 10.0
_DELETE_RETRY_ATTEMPTS = 4
_INITIAL_BACKOFF = 1.0

# Optional shared-secret for the proxy's non-GET gate (Phase H). When the proxy is
# started with PROXY_TOKEN set (a hosted / shared-network deployment), it 401s any
# non-GET lacking header X-DV3-Token; the pilot supplies it from DV3_PROXY_TOKEN.
# GETs are unauthenticated at the proxy, so this header is added to WRITES ONLY
# (POST create / DELETE cancel). Absent env var -> no header, behaviour identical to
# today (the laptop proxy runs tokenless). Read at call time so a restart-free env
# change is picked up; the value is never logged.
DV3_PROXY_TOKEN_ENV = "DV3_PROXY_TOKEN"


def _dv3_token_headers() -> dict[str, str]:
    """{'X-DV3-Token': <token>} when DV3_PROXY_TOKEN is set in the environment, else {}."""
    token = os.environ.get(DV3_PROXY_TOKEN_ENV, "").strip()
    return {"X-DV3-Token": token} if token else {}

# (url, json_body, timeout) -> response-like exposing .status_code, .json(), .text
HttpPost = Callable[[str, "dict[str, Any]", float], Any]
# (url, timeout) -> response-like exposing .status_code, .json(), .text
HttpDelete = Callable[[str, float], Any]


@dataclass(frozen=True)
class WriteResponse:
    """The outcome of a non-GET proxy call. ``body`` is the parsed JSON (or ``{}`` when the reply had
    no JSON body). ``ok`` is a 2xx. ``error`` carries a classifier string on any non-2xx / transport
    failure (never raised for the order path — the caller decides how to book it)."""

    status_code: int | None
    body: dict[str, Any]
    ok: bool
    error: str | None = None


class ProxyWriter:
    """Non-GET proxy client. Constructed with an injected ``ProxyAuth`` (for GETs) and injected
    ``http_post`` / ``http_delete`` callables (defaulting to ``requests`` bound to the local proxy)."""

    def __init__(
        self,
        proxy_auth: ProxyAuth | None = None,
        base_url: str = DEFAULT_PROXY_BASE,
        http_post: HttpPost | None = None,
        http_delete: HttpDelete | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._auth = proxy_auth or ProxyAuth(base_url=base_url)
        self._http_post = http_post or self._default_post
        self._http_delete = http_delete or self._default_delete
        self._sleep = sleep

    # === GET (delegated to ProxyAuth's bounded retry) ===
    def rest_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Authenticated GET of {base}/trade-api/v2{path} (bounded retry). Reads only."""
        return self._auth.rest_get(path, params)

    # === POST create (NEVER retried) ===
    def rest_post(self, path: str, body: dict[str, Any]) -> WriteResponse:
        """POST {base}/trade-api/v2{path} with ``body`` (JSON). Sent ONCE — never retried (a lost
        response on a server-side success would duplicate the order; the body's client_order_id is the
        idempotency key). Any transport error is returned as a non-ok WriteResponse, not raised."""
        # Idempotent composition: an already-prefixed /trade-api/v2/... path is NOT
        # re-prefixed (the 2026-09-14 doubled-prefix create-404). compose_rest_url
        # also validates the leading '/' and asserts a single prefix.
        url = compose_rest_url(self._base, path)
        try:
            resp = self._http_post(url, body, POST_TIMEOUT)
        except Exception as e:  # noqa: BLE001 — POST is never retried; a transport error is a no-send
            logger.warning("[PROXY-WRITE] POST %s transport error: %s", url, e)
            return WriteResponse(status_code=None, body={}, ok=False,
                                 error=f"post_exception:{type(e).__name__}")
        return self._classify(resp, verb="POST", url=url)

    # === DELETE cancel (bounded retry — idempotent) ===
    def rest_delete(self, path: str) -> WriteResponse:
        """DELETE {base}/trade-api/v2{path} (bounded retry on 429/5xx/connection blips; DELETE is
        idempotent). A 404 is returned to the caller (not retried) — the cancel path classifies a
        not_found body as terminal-success."""
        url = compose_rest_url(self._base, path)
        backoff = _INITIAL_BACKOFF
        last: WriteResponse | None = None
        for attempt in range(_DELETE_RETRY_ATTEMPTS):
            try:
                resp = self._http_delete(url, DELETE_TIMEOUT)
            except Exception as e:  # noqa: BLE001
                last = WriteResponse(None, {}, False, f"delete_exception:{type(e).__name__}")
                if attempt < _DELETE_RETRY_ATTEMPTS - 1:
                    logger.warning("[PROXY-WRITE] DELETE %s %s; retry in %.1fs", url,
                                   type(e).__name__, backoff)
                    self._sleep(backoff)
                    backoff *= 2
                    continue
                return last
            wr = self._classify(resp, verb="DELETE", url=url)
            status = wr.status_code
            transient = status is not None and (status == 429 or status >= 500)
            if transient and attempt < _DELETE_RETRY_ATTEMPTS - 1:
                logger.warning("[PROXY-WRITE] DELETE %s status=%s; retry in %.1fs", url, status,
                               backoff)
                self._sleep(backoff)
                backoff *= 2
                last = wr
                continue
            return wr
        return last or WriteResponse(None, {}, False, "delete_retry_exhausted")

    # === helpers ===
    @staticmethod
    def _classify(resp: Any, *, verb: str, url: str) -> WriteResponse:
        status = getattr(resp, "status_code", None)
        body: dict[str, Any] = {}
        try:
            parsed = resp.json()
            if isinstance(parsed, dict):
                body = parsed
        except Exception:  # noqa: BLE001 — a non-JSON body (HTML proxy error) leaves body {}
            body = {}
        ok = status is not None and 200 <= status < 300
        error = None if ok else f"http_{status}"
        return WriteResponse(status_code=status, body=body, ok=ok, error=error)

    @staticmethod
    def _default_post(url: str, body: dict[str, Any], timeout: float) -> Any:
        import requests  # local import so importing this module never requires the network
        return requests.post(url, json=body, timeout=timeout, headers=_dv3_token_headers())

    @staticmethod
    def _default_delete(url: str, timeout: float) -> Any:
        import requests
        return requests.delete(url, timeout=timeout, headers=_dv3_token_headers())
