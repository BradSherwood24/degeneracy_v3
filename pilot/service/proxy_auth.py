"""ProxyAuth — the auth shim that replaces V2's KalshiAuth, routed through the local signing proxy.

House law: ALL Kalshi API access goes through the local proxy at http://127.0.0.1:8642; this
process never handles key material and never reads the proxy's .env / *.pem. The proxy signs; we
just dial and read.

Two responsibilities:

  1. `ws_connect_params()` -> (ws_url, headers): GET {base}/ws-auth. The proxy returns
     {"ws_url": "wss://...", "headers": {KALSHI-ACCESS-KEY, KALSHI-ACCESS-SIGNATURE,
     KALSHI-ACCESS-TIMESTAMP}} on 200, or a 503 JSON error when the proxy is UNSIGNED (no key
     loaded). The signature is EPHEMERAL, so this is fetched FRESH ON EVERY CALL — the WS client
     re-mints on every dial and every re-dial (PLAN review F14). Deliberately NOT retried: a
     signature is time-sensitive and a 503-unsigned is a proxy state, not a transient blip; the
     caller's reconnect loop re-invokes this fresh rather than us serving a stale mint.

  2. `rest_get(path, params)` -> parsed JSON: GET {base}/trade-api/v2{path} with V2's bounded retry
     semantics (429 / 5xx / ConnectionError / Timeout -> backoff 1s, 2s, 4s; 4 attempts total).
     GETs only — the pilot's REST use here is reads (markets/events discovery, balance, positions).
     Order WRITES are the Executor's job (Phase 3) and are never retried (duplicate-order hazard).

Both HTTP verbs go through an injectable `http_get` so every path is testable without a network:
the injected callable takes (url, params, timeout) and returns an object exposing `.status_code`,
`.json()`, and `.text` (the shape `requests.Response` already has).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import requests

logger = logging.getLogger(__name__)

DEFAULT_PROXY_BASE = "http://127.0.0.1:8642"
WS_AUTH_PATH = "/ws-auth"
REST_PREFIX = "/trade-api/v2"

WS_AUTH_TIMEOUT = 5.0
REST_TIMEOUT = 5.0
_RETRY_ATTEMPTS = 4
_INITIAL_BACKOFF = 1.0

# The proxy returns this when no key is loaded (it cannot sign). Distinguished from other errors so
# the caller can surface "start/sign the proxy" rather than a generic failure.
PROXY_UNSIGNED_STATUS = 503

HttpGet = Callable[[str, "dict[str, Any] | None", float], Any]


class ProxyError(Exception):
    """Any non-success response from the proxy that is not classified more specifically."""


class ProxyUnsignedError(ProxyError):
    """The proxy is running but unsigned (503) — no key loaded, so it cannot mint WS auth."""


class ProxyAuth:
    def __init__(
        self,
        base_url: str = DEFAULT_PROXY_BASE,
        http_get: HttpGet | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._http_get = http_get or self._default_get
        self._sleep = sleep

    @staticmethod
    def _default_get(url: str, params: dict[str, Any] | None, timeout: float) -> requests.Response:
        return requests.get(url, params=params or {}, timeout=timeout)

    # === WS auth (fresh every call) ===

    def ws_connect_params(self) -> tuple[str, dict[str, str]]:
        """Return (ws_url, headers) minted fresh by the proxy. Raises on any non-200 (fail closed)."""
        url = self._base + WS_AUTH_PATH
        resp = self._http_get(url, None, WS_AUTH_TIMEOUT)
        status = resp.status_code
        if status == PROXY_UNSIGNED_STATUS:
            raise ProxyUnsignedError(
                f"proxy unsigned (503) at {url}: {self._body_snippet(resp)} — "
                f"the proxy has no key loaded and cannot mint WS auth."
            )
        if status != 200:
            raise ProxyError(f"ws-auth GET {url} returned status={status}: {self._body_snippet(resp)}")
        data = resp.json()
        try:
            ws_url = data["ws_url"]
            headers = dict(data["headers"])
        except (KeyError, TypeError) as e:
            raise ProxyError(f"ws-auth response missing ws_url/headers: {data!r}") from e
        return ws_url, headers

    # === REST reads (bounded retry, V2 semantics) ===

    def rest_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Authenticated (proxy-signed) GET of {base}/trade-api/v2{path}; returns parsed JSON.

        `path` is the API path AFTER /trade-api/v2 and MUST begin with '/', e.g. "/markets",
        "/events/KXBTCD-26JUL3120". Bounded retry on 429/5xx/ConnectionError/Timeout (backoff
        1s -> 2s -> 4s, 4 attempts); the final attempt propagates the real failure so callers fail
        closed rather than looping forever.
        """
        if not path.startswith("/"):
            raise ValueError(f"rest_get path must start with '/': {path!r}")
        url = self._base + REST_PREFIX + path
        backoff = _INITIAL_BACKOFF
        last_exc: Exception | None = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                resp = self._http_get(url, params, REST_TIMEOUT)
                status = resp.status_code
                transient = status == 429 or status >= 500
                if transient and attempt < _RETRY_ATTEMPTS - 1:
                    logger.warning(
                        "[REST RETRY] attempt %d/%d status=%s url=%s sleeping %.1fs",
                        attempt + 1,
                        _RETRY_ATTEMPTS,
                        status,
                        url,
                        backoff,
                    )
                    self._sleep(backoff)
                    backoff *= 2
                    continue
                if status < 200 or status >= 300:
                    raise ProxyError(f"GET {url} returned status={status}: {self._body_snippet(resp)}")
                return resp.json()
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                last_exc = e
                if attempt < _RETRY_ATTEMPTS - 1:
                    logger.warning(
                        "[REST RETRY] attempt %d/%d %s url=%s sleeping %.1fs",
                        attempt + 1,
                        _RETRY_ATTEMPTS,
                        type(e).__name__,
                        url,
                        backoff,
                    )
                    self._sleep(backoff)
                    backoff *= 2
                    continue
                raise
        if last_exc is not None:  # defensive: unreachable (loop either returns or raises above)
            raise last_exc
        raise RuntimeError("rest_get retry loop exhausted without success or exception")

    @staticmethod
    def _body_snippet(resp: Any) -> str:
        try:
            return str(resp.json())[:300]
        except Exception:  # noqa: BLE001 - best-effort diagnostics only
            try:
                return str(resp.text)[:300]
            except Exception:  # noqa: BLE001
                return "<no body>"
