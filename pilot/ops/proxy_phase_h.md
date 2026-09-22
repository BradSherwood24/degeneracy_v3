# Proxy change proposal — Phase H host-shaped runtime (env-driven config + optional X-DV3-Token)

**Status: PROPOSAL ONLY. Do NOT apply from this repo.** `degeneracy-proxy/proxy.py` lives on Brad's
box (a separate, non-git directory); only Brad edits and restarts it (house law: agents never touch
proxy key material / `.env` / `*.pem`). This file records the EXACT change — full unified diffs plus
the new test file verbatim — so Brad can apply it deliberately. The diffs were produced against the
LIVE `proxy.py` read READ-ONLY on 2026-09-22 (mtime 2026-08-21 02:00) and were developed and tested
against a scratchpad COPY of `proxy.py` + `tests/` (never `.env`, never any `*.pem`, never
`order_budget.json`, never a log). **Applying it to the live proxy is Brad's lever, never Claude's.**

## Why this is needed (Phase H of PLAN_V33 / RENDER_MIGRATION_PLAN §3c)

The move to a host (Render, Ohio) runs the SAME proxy in a container, configured entirely by
environment variables and Secret Files rather than a laptop `.env`. Four gaps today:

1. The bind address is hard-coded `127.0.0.1`. A Render **Private Service** must bind `0.0.0.0` to be
   reachable on the private network (it still gets no public URL).
2. The budget counter file is hard-wired beside `proxy.py`; on a host it must live on the mounted disk.
3. `.env` is always loaded — on a host there is no `.env`, config comes from real env vars.
4. On a shared private network, any neighbour could POST orders. An optional shared secret closes that.

**All four changes are behaviour-neutral on the laptop:** every new env var defaults to today's
behaviour, and `PROXY_TOKEN` unset means the token block is a pure no-op. Nothing here changes caps,
routing, signing, the budget semantics, or the read-only default.

## What changes (summary)

- **`PROXY_HOST`** (default `127.0.0.1`) — the bind address. Render sets `0.0.0.0`.
- **`PROXY_BUDGET_PATH`** (default `order_budget.json` beside `proxy.py`) — the daily-counter file.
  Render points it at the persistent disk so the count survives restarts there too (F12).
- **`.env` optional** — loaded only if the file exists; otherwise all config comes from real env vars.
- **Key path is already generic** — `PROD_KEYFILE` / `DEMO_KEYFILE` already accept an ABSOLUTE path,
  so a Render Secret File at `/etc/secrets/kalshi_render.pem` works with **no code change** (see
  `_load_signer`, which resolves an absolute keyfile as-is and only prepends `PROXY_DIR` for a relative
  one). No separate `KALSHI_KEY_PATH` var is added — it would be redundant. The docstring now says so.
  The key path variable name is `PROD_KEYFILE` (prod) / `DEMO_KEYFILE` (demo); **no key material is
  printed anywhere** by this change.
- **`PROXY_TOKEN`** (optional) — when set, every non-GET request must carry header `X-DV3-Token` equal
  to it, else **401** `{"cap":"proxy_token"}`. GETs (`/health`, `/ws-auth`, market-data reads) stay
  unauthenticated. Unset → identical to today. The token is **never logged, never returned** by any
  endpoint. The header is also **stripped before forwarding upstream** so the internal secret never
  reaches Kalshi (found during testing: the existing header filter would otherwise pass it through).
- **`/health`** gains `"host"`, `"budget_path"` (path string only), and `"token_required"` (bool) —
  never the token value itself.

The pilot client already carries the matching change (`pilot/service/proxy_writer.py`): it sends
`X-DV3-Token` from `DV3_PROXY_TOKEN` on WRITES ONLY (POST/DELETE), and no header when the env var is
absent — see the build report. That is in the git repo and merges with the pilot code; only this proxy
diff is Brad's manual apply.

## Ordering relative to `proxy_amend_cap.md`

Both are UNAPPLIED proposals against the same `proxy.py`. They touch **disjoint regions**:
`proxy_amend_cap.md` adds `is_order_amend`, edits the non-create-refusal block, and inserts an amend-cap
block inside `_handle` (all in the create/amend gating area); THIS patch edits the module docstring,
`health_payload`, `Config.__init__`, the upstream header filter, `main()`, and inserts a token gate near
the TOP of `_handle` (before the read-only check). They do not overlap semantically. Apply with
context-based tooling (`git apply` / `patch -p1`), which tolerates the line-number offsets each patch
introduces in the other; **either order works**. If a literal hunk ever fails, apply that hunk by hand
from the description — the two `_handle` insertions are at different anchors (token gate = right after
the `/trade-api/` 404 guard; amend cap = the non-create refusal + after the create-caps block).

## The diff — `proxy.py`

```diff
--- a/proxy.py	2026-08-21 02:00:22.777101600 -0400
+++ b/proxy.py	2026-09-22 19:45:56.059304500 -0400
@@ -20,12 +20,21 @@
 api.elections.kalshi.com; order WRITES (create/cancel/amend/batch) must go to
 external-api.kalshi.com. Demo is single-host.
 
-Config (.env alongside this file):
+Config (.env alongside this file is OPTIONAL — loaded only if present; on a host
+every value comes from real process env vars / Secret Files):
   KALSHI_ENV=prod|demo           (default prod)
-  PROD_KEYID=...                 PROD_KEYFILE=kalshi_prod.pem   (path, may be relative)
+  PROD_KEYID=...                 PROD_KEYFILE=kalshi_prod.pem   (path, may be relative
+                                   OR absolute — e.g. a Render Secret File
+                                   /etc/secrets/kalshi_render.pem; this IS the generic
+                                   "key path from an env var" — no separate var needed)
   DEMO_KEYID=...                 DEMO_KEYFILE=...
   ALLOW_ORDERS=true|false        (default false — read-only)
   PROXY_PORT=8642                (default)
+  PROXY_HOST=127.0.0.1           (default; a host sets 0.0.0.0 — Private Service only)
+  PROXY_BUDGET_PATH=<path>       (default order_budget.json beside this file; a host
+                                   points it at a writable/persistent path)
+  PROXY_TOKEN=<secret>           (optional; when set, every non-GET request must carry
+                                   header X-DV3-Token == it, else 401. Unset -> no check.)
 
 Run:  python proxy.py
 """
@@ -34,6 +43,7 @@
 
 import base64
 import hashlib
+import hmac
 import json
 import os
 import sys
@@ -307,6 +317,9 @@
         "signed": config.signer is not None,
         "orders_enabled": config.allow_orders,
         "key_fingerprint": config.signer.fingerprint if config.signer else None,
+        "host": config.host,
+        "budget_path": str(config.budget_path),
+        "token_required": config.proxy_token is not None,
         "caps": {
             "max_contracts_per_order": config.max_contracts_per_order,
             "ticker_prefixes": list(config.ticker_prefixes),
@@ -372,16 +385,31 @@
 
 class Config:
     def __init__(self) -> None:
-        load_dotenv(PROXY_DIR / ".env")
+        # .env is OPTIONAL: load it only if the file exists. On a host (e.g. Render)
+        # every value below comes from real process env vars / Secret Files, so a
+        # missing .env is normal and must NOT change behaviour; on the laptop the .env
+        # remains the source of truth exactly as before.
+        env_path = PROXY_DIR / ".env"
+        if env_path.is_file():
+            load_dotenv(env_path)
         self.env = os.getenv("KALSHI_ENV", "prod").strip().lower()
         if self.env not in _URLS:
             raise ValueError(f"KALSHI_ENV must be prod or demo, got {self.env!r}")
         self.http_base, self.orders_base = _URLS[self.env]
         self.allow_orders = os.getenv("ALLOW_ORDERS", "false").strip().lower() == "true"
+        # Bind address (default localhost — unchanged on the laptop). A host sets
+        # PROXY_HOST=0.0.0.0 to listen on its private network (Private Service only;
+        # Render never exposes it publicly). See README.
+        self.host = os.getenv("PROXY_HOST", "127.0.0.1").strip() or "127.0.0.1"
         self.port = int(os.getenv("PROXY_PORT", "8642"))
         self.signer = _load_signer(self.env)
         if self.signer is None:
             self.allow_orders = False  # unsigned mode is always read-only
+        # Optional shared-secret. When PROXY_TOKEN is set, every non-GET request must
+        # carry header X-DV3-Token equal to it (defence-in-depth so even a same-network
+        # neighbour cannot write orders). Unset -> no token check, behaviour identical
+        # to today. The token is NEVER logged and NEVER returned by any endpoint.
+        self.proxy_token = os.getenv("PROXY_TOKEN", "").strip() or None
 
         # Defense-in-depth order caps (startup-read; only the budget COUNTER is
         # dynamic state — no hot reload). See module docstring / PLAN Phase 0.
@@ -396,9 +424,12 @@
         self.daily_order_budget = int(
             os.getenv("DAILY_ORDER_BUDGET", _DEFAULT_DAILY_BUDGET)
         )
-        self.order_budget = OrderBudget(
-            PROXY_DIR / _BUDGET_FILENAME, self.daily_order_budget
-        )
+        # Budget counter file location. Default is beside this file (unchanged on the
+        # laptop). A host sets PROXY_BUDGET_PATH to a writable/persistent path (e.g. a
+        # mounted disk) so the daily count survives restarts there too (F12).
+        budget_env = os.getenv("PROXY_BUDGET_PATH", "").strip()
+        self.budget_path = Path(budget_env) if budget_env else (PROXY_DIR / _BUDGET_FILENAME)
+        self.order_budget = OrderBudget(self.budget_path, self.daily_order_budget)
 
 
 CONFIG = Config()
@@ -440,6 +471,23 @@
             self._respond_json(404, {"error": "only /trade-api/... paths are proxied"})
             return
 
+        # Optional shared-secret gate (defence-in-depth). When PROXY_TOKEN is set,
+        # every non-GET request must carry header X-DV3-Token equal to it, else 401.
+        # GETs (health / ws-auth / market-data reads) stay unauthenticated. Unset
+        # token -> this block is a no-op and behaviour is identical to today. The
+        # comparison result is never logged with the token value.
+        if method != "GET" and CONFIG.proxy_token is not None:
+            supplied = self.headers.get("X-DV3-Token") or ""
+            if not hmac.compare_digest(
+                supplied.encode("utf-8"), CONFIG.proxy_token.encode("utf-8")
+            ):
+                self._respond_json(401, {
+                    "error": "unauthorized: missing or invalid X-DV3-Token",
+                    "cap": "proxy_token",
+                })
+                print(f"[proxy] BLOCKED {method} {path} (bad/missing X-DV3-Token)")
+                return
+
         if method != "GET" and not CONFIG.allow_orders:
             self._respond_json(403, {
                 "error": "proxy is read-only: non-GET requests are disabled",
@@ -524,7 +572,9 @@
 
         headers = {
             k: v for k, v in self.headers.items()
-            if k.lower() not in _HOP_BY_HOP and not k.upper().startswith("KALSHI-ACCESS-")
+            if k.lower() not in _HOP_BY_HOP
+            and not k.upper().startswith("KALSHI-ACCESS-")
+            and k.lower() != "x-dv3-token"  # internal shared secret — never forward upstream
         }
         headers.setdefault("Content-Type", "application/json")
         if CONFIG.signer is not None:
@@ -562,9 +612,10 @@
     mode = "SIGNED" if CONFIG.signer else "UNSIGNED (public endpoints only)"
     orders = "ENABLED" if CONFIG.allow_orders else "read-only"
     fingerprint = f", key fp {CONFIG.signer.fingerprint}" if CONFIG.signer else ""
-    print(f"[proxy] {CONFIG.env} | {mode} | orders {orders}{fingerprint}")
-    print(f"[proxy] listening on http://127.0.0.1:{CONFIG.port}")
-    server = ThreadingHTTPServer(("127.0.0.1", CONFIG.port), Handler)
+    token = " | token-required" if CONFIG.proxy_token else ""
+    print(f"[proxy] {CONFIG.env} | {mode} | orders {orders}{fingerprint}{token}")
+    print(f"[proxy] listening on http://{CONFIG.host}:{CONFIG.port}")
+    server = ThreadingHTTPServer((CONFIG.host, CONFIG.port), Handler)
     try:
         server.serve_forever()
     except KeyboardInterrupt:
```

## The diff — `README.md` (new "Hosting / env configuration" section)

```diff
--- a/README.md	2026-08-18 22:06:12.470337800 -0400
+++ b/README.md	2026-09-22 19:23:15.726334000 -0400
@@ -35,6 +35,27 @@
 - Never logs or serves key material. `GET /health` reports env, mode, and a
   sha256 fingerprint of the **public** key only.
 
+## Hosting / env configuration (Phase H)
+
+All config is env-driven; `.env` is loaded ONLY if it exists (on a host it does not — values come from
+real env vars / Secret Files). Behaviour-neutral on the laptop (every var defaults to today):
+
+- `PROXY_HOST` (default `127.0.0.1`) — bind address. On Render set **`0.0.0.0`** so the private network
+  can reach it. The service is a **Private Service** (no public URL); `0.0.0.0` here means "all private
+  interfaces", never public exposure.
+- `PROXY_BUDGET_PATH` (default `order_budget.json` beside this file) — the daily order-budget counter
+  file. On Render point it at the mounted **persistent disk** so the count survives restarts (F12).
+- `PROD_KEYFILE` / `DEMO_KEYFILE` already accept an **absolute** path, so a Render **Secret File** at
+  `/etc/secrets/kalshi_render.pem` works with no code change. (This is the generic "key path from an env
+  var"; no separate `KALSHI_KEY_PATH` var exists or is needed.)
+- `PROXY_TOKEN` (optional) — when set, every **non-GET** request must carry header `X-DV3-Token` equal to
+  it, else **401**. GETs (`/health`, `/ws-auth`, market-data reads) stay open. Unset → no check (today).
+  The token is never logged, never returned, and is stripped before forwarding upstream. The pilot sends
+  it from its own `DV3_PROXY_TOKEN` env var on writes.
+
+`GET /health` reports `host`, `budget_path` (path string only), and `token_required` (bool) — never the
+token value or any key material.
+
 ## Client usage
 
 ```python
```

## The diff — `tests/test_proxy.py` (extend the `_fake_config` helper with the 3 new fields)

```diff
--- a/tests/test_proxy.py	2026-08-21 02:01:35.948982100 -0400
+++ b/tests/test_proxy.py	2026-09-22 19:14:14.634766000 -0400
@@ -307,13 +307,16 @@
 # health_payload — additive fields over the original shape                     #
 # --------------------------------------------------------------------------- #
 
-def _fake_config(tmp_path, *, signer=None, allow_orders=False):
+def _fake_config(tmp_path, *, signer=None, allow_orders=False, proxy_token=None):
     return SimpleNamespace(
         env="prod",
         http_base="https://api.elections.kalshi.com",
         orders_base="https://external-api.kalshi.com",
         allow_orders=allow_orders,
         signer=signer,
+        host="127.0.0.1",
+        budget_path=tmp_path / "b.json",
+        proxy_token=proxy_token,
         max_contracts_per_order=2,
         ticker_prefixes=("KXBTC15M", "KXBTCD"),
         daily_order_budget=100,
```

## The diff — `tests/test_review_probes.py` (same 3 fields on its `_fake_config`)

```diff
--- a/tests/test_review_probes.py	2026-08-21 02:01:52.964210100 -0400
+++ b/tests/test_review_probes.py	2026-09-22 19:14:21.616165500 -0400
@@ -101,13 +101,16 @@
 # Handler-level probes (real server, short-circuit paths only)                 #
 # --------------------------------------------------------------------------- #
 
-def _fake_config(tmp_path, *, signer, allow_orders):
+def _fake_config(tmp_path, *, signer, allow_orders, proxy_token=None):
     return SimpleNamespace(
         env="prod",
         http_base="https://api.elections.kalshi.com",
         orders_base="https://external-api.kalshi.com",
         allow_orders=allow_orders,
         signer=signer,
+        host="127.0.0.1",
+        budget_path=tmp_path / "b.json",
+        proxy_token=proxy_token,
         max_contracts_per_order=2,
         ticker_prefixes=PREFIXES,
         daily_order_budget=100,
```

## New test file — `tests/test_phase_h.py` (verbatim; drop it in alongside the others)

```python
"""Phase H proxy tests (host-shaped runtime): env-driven bind host, budget-file
path, optional .env, optional X-DV3-Token shared secret, and the additive /health
fields. Behaviour-neutral defaults are asserted so the laptop is unaffected.

No test makes a live upstream call. The handler tests exercise only paths that
short-circuit (health, token 401, read-only 403, cap 403) or use a fake SESSION,
so nothing leaves the process. No test reads .env or any *.pem — Config() is
constructed against the scratchpad copy, which has no .env (that IS the missing-
.env case) and no key (unsigned), and every signer used is a throwaway RSA key.
"""

from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

import proxy


# --------------------------------------------------------------------------- #
# Config — env-driven host / budget path / token, and .env optional            #
# --------------------------------------------------------------------------- #
#
# Config() reads real process env vars. We clear the ones under test so a shell
# that happens to export them cannot mask a default; PROXY_DIR here is the
# scratchpad copy, which has no .env (missing-.env case) and no key (unsigned).

_CLEARED = ("PROXY_HOST", "PROXY_BUDGET_PATH", "PROXY_TOKEN", "ALLOW_ORDERS")


@pytest.fixture
def clean_env(monkeypatch):
    for var in _CLEARED:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_config_default_host_is_localhost(clean_env):
    assert proxy.Config().host == "127.0.0.1"


def test_config_host_env_honoured(clean_env):
    clean_env.setenv("PROXY_HOST", "0.0.0.0")
    assert proxy.Config().host == "0.0.0.0"


def test_config_blank_host_falls_back_to_localhost(clean_env):
    clean_env.setenv("PROXY_HOST", "   ")
    assert proxy.Config().host == "127.0.0.1"


def test_config_default_budget_path_beside_file(clean_env):
    assert proxy.Config().budget_path == proxy.PROXY_DIR / "order_budget.json"


def test_config_budget_path_env_honoured(clean_env, tmp_path):
    target = tmp_path / "disk" / "order_budget.json"
    clean_env.setenv("PROXY_BUDGET_PATH", str(target))
    cfg = proxy.Config()
    assert cfg.budget_path == Path(str(target))
    # the OrderBudget was constructed against that same path
    assert cfg.order_budget._path == Path(str(target))


def test_config_token_unset_is_none(clean_env):
    assert proxy.Config().proxy_token is None


def test_config_blank_token_is_none(clean_env):
    clean_env.setenv("PROXY_TOKEN", "   ")
    assert proxy.Config().proxy_token is None


def test_config_token_set(clean_env):
    clean_env.setenv("PROXY_TOKEN", "s3kr3t")
    assert proxy.Config().proxy_token == "s3kr3t"


def test_config_missing_env_file_is_ok(clean_env):
    # The scratchpad copy has no .env -> Config() must still construct from real
    # env vars with the shipped defaults (env=prod). This is the Render case.
    assert not (proxy.PROXY_DIR / ".env").is_file()
    cfg = proxy.Config()
    assert cfg.env == "prod"
    assert cfg.allow_orders is False  # unsigned + no ALLOW_ORDERS


# --------------------------------------------------------------------------- #
# health_payload — additive host / budget_path / token_required (never token)  #
# --------------------------------------------------------------------------- #

def _hp_config(tmp_path, *, proxy_token=None, host="127.0.0.1"):
    return SimpleNamespace(
        env="prod",
        allow_orders=False,
        signer=None,
        host=host,
        budget_path=tmp_path / "order_budget.json",
        proxy_token=proxy_token,
        max_contracts_per_order=2,
        ticker_prefixes=("KXBTC15M", "KXBTCD"),
        daily_order_budget=100,
        order_budget=proxy.OrderBudget(tmp_path / "b.json", 100),
    )


def test_health_reports_host_and_budget_path(tmp_path):
    cfg = _hp_config(tmp_path, host="0.0.0.0")
    payload = proxy.health_payload(cfg)
    assert payload["host"] == "0.0.0.0"
    assert payload["budget_path"] == str(tmp_path / "order_budget.json")


def test_health_token_required_false_when_unset(tmp_path):
    payload = proxy.health_payload(_hp_config(tmp_path, proxy_token=None))
    assert payload["token_required"] is False


def test_health_token_required_true_but_never_leaks_token(tmp_path):
    payload = proxy.health_payload(_hp_config(tmp_path, proxy_token="topsecret"))
    assert payload["token_required"] is True
    assert "topsecret" not in json.dumps(payload)
    assert "proxy_token" not in payload and "token" not in payload


# --------------------------------------------------------------------------- #
# X-DV3-Token handler gate (real server; short-circuit + fake SESSION only)     #
# --------------------------------------------------------------------------- #

TOKEN = "correct-horse-battery-staple"


def _handler_config(tmp_path, *, signer, allow_orders, proxy_token):
    return SimpleNamespace(
        env="prod",
        http_base="https://api.elections.kalshi.com",
        orders_base="https://external-api.kalshi.com",
        allow_orders=allow_orders,
        signer=signer,
        host="127.0.0.1",
        budget_path=tmp_path / "b.json",
        proxy_token=proxy_token,
        max_contracts_per_order=2,
        ticker_prefixes=("KXBTC15M", "KXBTCD"),
        daily_order_budget=100,
        order_budget=proxy.OrderBudget(tmp_path / "b.json", 100),
    )


def _serve(cfg, monkeypatch, *, fake_session=False):
    monkeypatch.setattr(proxy, "CONFIG", cfg)
    captured: dict = {}
    if fake_session:
        class FakeResp:
            status_code = 200
            content = b'{"ok":true}'
            headers = {"Content-Type": "application/json"}

        def fake_request(method, url, headers=None, data=None, timeout=None):
            captured.update(method=method, url=url, headers=headers, data=data)
            return FakeResp()

        monkeypatch.setattr(proxy.SESSION, "request", fake_request)
    server = ThreadingHTTPServer(("127.0.0.1", 0), proxy.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    return server, base, captured


@pytest.fixture
def token_proxy(tmp_path, fake_signer, monkeypatch):
    """Real Handler, PROXY_TOKEN set, ALLOW_ORDERS on, fake SESSION so a token-valid
    create never leaves the process."""
    cfg = _handler_config(tmp_path, signer=fake_signer, allow_orders=True, proxy_token=TOKEN)
    server, base, captured = _serve(cfg, monkeypatch, fake_session=True)
    try:
        yield base, cfg, captured
    finally:
        server.shutdown()
        server.server_close()


def test_token_get_is_unauthenticated(token_proxy):
    """GETs need no token even when one is required (health / reads stay open)."""
    base, _, _ = token_proxy
    r = requests.get(f"{base}/health", timeout=5)
    assert r.status_code == 200
    assert r.json()["token_required"] is True


def test_token_post_without_header_401(token_proxy):
    base, _, captured = token_proxy
    r = requests.post(
        f"{base}/trade-api/v2/portfolio/events/orders",
        json={"ticker": "KXBTC15M-A", "count": "1"}, timeout=5,
    )
    assert r.status_code == 401
    assert r.json()["cap"] == "proxy_token"
    assert captured == {}  # never forwarded


def test_token_post_with_wrong_header_401(token_proxy):
    base, _, captured = token_proxy
    r = requests.post(
        f"{base}/trade-api/v2/portfolio/events/orders",
        json={"ticker": "KXBTC15M-A", "count": "1"},
        headers={"X-DV3-Token": "WRONG"}, timeout=5,
    )
    assert r.status_code == 401
    assert r.json()["cap"] == "proxy_token"
    assert captured == {}


def test_token_wrong_length_header_401(token_proxy):
    """A token of the WRONG LENGTH is rejected — exercises the hmac.compare_digest
    length-mismatch path (a plain `==` would also reject it, but this pins that the
    constant-time compare returns False, not raises, on unequal-length inputs)."""
    base, _, captured = token_proxy
    r = requests.post(
        f"{base}/trade-api/v2/portfolio/events/orders",
        json={"ticker": "KXBTC15M-A", "count": "1"},
        headers={"X-DV3-Token": TOKEN[:-1]}, timeout=5,  # one char short of the real token
    )
    assert r.status_code == 401
    assert r.json()["cap"] == "proxy_token"
    assert captured == {}


def test_token_delete_without_header_401(token_proxy):
    """The gate covers every non-GET verb, cancels (DELETE) included."""
    base, _, captured = token_proxy
    r = requests.delete(
        f"{base}/trade-api/v2/portfolio/events/orders/ORD-1", timeout=5)
    assert r.status_code == 401
    assert r.json()["cap"] == "proxy_token"
    assert captured == {}


def test_token_right_header_reaches_caps(token_proxy):
    """A token-valid POST passes the gate and is then subject to the EXISTING caps
    (here a cap violation, proving the request reached the cap layer, not the token
    layer)."""
    base, _, captured = token_proxy
    r = requests.post(
        f"{base}/trade-api/v2/portfolio/events/orders",
        json={"ticker": "KXBTC15M-A", "count": "9"},  # over max 2
        headers={"X-DV3-Token": TOKEN}, timeout=5,
    )
    assert r.status_code == 403
    assert r.json()["cap"] == "max_contracts_per_order"
    assert captured == {}  # blocked by caps, still never forwarded


def test_token_right_header_valid_create_forwards(token_proxy):
    """A token-valid, cap-valid create passes the gate AND the caps and forwards."""
    base, cfg, captured = token_proxy
    r = requests.post(
        f"{base}/trade-api/v2/portfolio/events/orders",
        data=json.dumps({"ticker": "KXBTC15M-A", "count": "2"}).encode(),
        headers={"X-DV3-Token": TOKEN, "Content-Type": "application/json"}, timeout=5,
    )
    assert r.status_code == 200
    assert captured["url"] == (
        "https://external-api.kalshi.com/trade-api/v2/portfolio/events/orders")
    # the client's token header is NOT forwarded upstream (only KALSHI-ACCESS-* are added)
    assert "X-DV3-Token" not in captured["headers"]


# --------------------------------------------------------------------------- #
# Token UNSET -> behaviour identical to today (no gate)                          #
# --------------------------------------------------------------------------- #

@pytest.fixture
def no_token_readonly(tmp_path, fake_signer, monkeypatch):
    cfg = _handler_config(tmp_path, signer=fake_signer, allow_orders=False, proxy_token=None)
    server, base, captured = _serve(cfg, monkeypatch)
    try:
        yield base, cfg, captured
    finally:
        server.shutdown()
        server.server_close()


def test_no_token_non_get_still_readonly_403(no_token_readonly):
    """With no token configured, a non-GET is refused by the SAME read-only 403 as
    today — the token block is a pure no-op."""
    base, _, _ = no_token_readonly
    r = requests.post(
        f"{base}/trade-api/v2/portfolio/events/orders",
        json={"ticker": "KXBTC15M-A", "count": "1"}, timeout=5,
    )
    assert r.status_code == 403
    assert "read-only" in r.json()["error"]


def test_no_token_post_needs_no_header(tmp_path, fake_signer, monkeypatch):
    """No token + ALLOW_ORDERS on: a valid create forwards with NO X-DV3-Token."""
    cfg = _handler_config(tmp_path, signer=fake_signer, allow_orders=True, proxy_token=None)
    server, base, captured = _serve(cfg, monkeypatch, fake_session=True)
    try:
        r = requests.post(
            f"{base}/trade-api/v2/portfolio/events/orders",
            data=json.dumps({"ticker": "KXBTC15M-A", "count": "1"}).encode(),
            headers={"Content-Type": "application/json"}, timeout=5,
        )
        assert r.status_code == 200
        assert captured["url"].endswith("/trade-api/v2/portfolio/events/orders")
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------- #
# Bind host actually applied                                                    #
# --------------------------------------------------------------------------- #

def test_server_binds_config_host(tmp_path, fake_signer, monkeypatch):
    """main() binds ThreadingHTTPServer to CONFIG.host; assert the server address
    matches the configured host (127.0.0.1 here — 0.0.0.0 is not bound in a unit
    test to avoid opening a listener on all interfaces)."""
    cfg = _handler_config(tmp_path, signer=fake_signer, allow_orders=False, proxy_token=None)
    server, base, _ = _serve(cfg, monkeypatch)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.shutdown()
        server.server_close()
```

## Proxy packaging for the host (not in this repo — the proxy is a separate service)

When the proxy runs on Render it needs its own `requirements.txt` beside `proxy.py` (the repo-root
`requirements.txt` covers the pilot + tools only). Pinned to the versions this box runs:

```
# degeneracy-proxy/requirements.txt  (Python 3.12.10)
cryptography==42.0.7
python-dotenv==1.0.0
requests==2.34.2
```

## Apply steps (Brad only — inside a :02–:33 UTC window, never mid-window)

1. **Back up** the live file:
   `Copy-Item degeneracy-proxy/proxy.py degeneracy-proxy/proxy.py.bak-phaseH`
   and the two test files likewise if you want a clean rollback of tests.
2. **Apply** the three diffs (from the repo root, adjusting `-p`/paths as needed), e.g. save this
   section's diffs to a `.patch` and `git apply --directory=... ` is not available (the proxy is not a
   git repo) → apply with `patch`:
   `patch degeneracy-proxy/proxy.py < proxy.patch`
   `patch degeneracy-proxy/tests/test_proxy.py < test_proxy.patch`
   `patch degeneracy-proxy/tests/test_review_probes.py < test_review_probes.patch`
   or hand-edit per the diffs (they are small). Then create `degeneracy-proxy/tests/test_phase_h.py`
   from the verbatim block above.
3. **Test in place** BEFORE restarting anything:
   `cd degeneracy-proxy; python -m pytest -q`
   Expect **133 passed** (111 existing = 86 `test_proxy` + 25 `test_review_probes`; + 22 `test_phase_h`).
   If the copy's `_fake_config` edits did not land,
   the health/handler tests will `AttributeError` on `host`/`budget_path`/`proxy_token` — re-check the
   two `_fake_config` diffs.
4. **Restart** the proxy (`.\run.ps1`, or however the service is managed) so the new code loads. On the
   laptop, set NO new env vars → behaviour is identical to today. Confirm `GET /health` now shows
   `"host"`, `"budget_path"`, and `"token_required": false`.
5. **On Render only** (later, not now): set `PROXY_HOST=0.0.0.0`, `PROXY_BUDGET_PATH=<disk path>`,
   `PROD_KEYFILE=/etc/secrets/kalshi_render.pem`, and (recommended) `PROXY_TOKEN=<secret>` on the proxy
   service AND `DV3_PROXY_TOKEN=<same secret>` on the pilot service. Confirm `/health` reports
   `"token_required": true` and that a non-GET without the header 401s.

## Rollback

Restore the backup and restart:
`Copy-Item degeneracy-proxy/proxy.py.bak-phaseH degeneracy-proxy/proxy.py -Force` (and the test files),
then restart the proxy. No state migration is involved — the budget file format is unchanged, and with
no new env vars set the old and new code are behaviourally identical, so rollback is safe at any time.

## What was verified (and what was not)

- The scratchpad copy's full suite is **green: 133 passed** (`python -m pytest -q` in the copy dir;
  111 existing + 22 `test_phase_h`, the 22nd being the wrong-length-token 401 case).
- The token compare is **constant-time** (`hmac.compare_digest` on utf-8 bytes; a missing header becomes
  `b""` and is rejected without raising).
- The token header is **stripped before upstream forwarding** (a test asserts `X-DV3-Token` is absent
  from the headers handed to `SESSION.request`).
- **Not verified here:** the LIVE proxy is untouched (read-only). The apply + restart is Brad's; the
  Render-side `0.0.0.0` bind and Secret File path are exercised only by config-unit assertions, not by a
  real datacenter deploy.

## Known gaps

- The box (wide-box) runner's own `pilot/service/executor.py:95` `_default_post` does NOT attach
  `X-DV3-Token` (it bypasses `ProxyWriter`). The V3.2 `run_v32` path — the Render migration target — IS
  covered (creates/amends via `rest_post`, cancels via `rest_delete`, all through
  `_default_post`/`_default_delete` → the token). The box path is out of scope for Render; if it is ever
  pointed at a token-gated proxy its creates/cancels would silently 401.
