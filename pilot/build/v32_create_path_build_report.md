# V3.2 hotfix — doubled REST prefix on order CREATE (first-armed-window 404)

Branch: `v32/fix-create-path` (off `origin/main` @ 42d3609). Build only; no push, no PR.

## What happened live (2026-09-14 19:44:59Z, first armed window)

The first armed window attempted three real order creates. The proxy log shows, three times:

```
POST /trade-api/v2/trade-api/v2/portfolio/events/orders -> 404
```

The venue 404'd, each create came back with an empty body, the executor journaled
`rest_rejected http_404` x3 and stood down. Net effect: safety held — **no order reached the
book** — but the strategy could not fire. The proxy's order budget shows **0 used**: the proxy did
not recognize the doubled path as a create, so it never applied (or consumed) a create cap; it
forwarded the request uncapped and the venue rejected it.

## Root cause

`service/v32/executor.py` passed `SINGLE_CREATE_PATH` / `BATCH_CREATE_PATH` (from
`service/orders/envelope.py`) into `ProxyWriter.rest_post(path, body)`. Those envelope constants are
**FULL** paths already beginning with `/trade-api/v2/...`:

```
SINGLE_CREATE_PATH = "/trade-api/v2/portfolio/events/orders"
BATCH_CREATE_PATH  = "/trade-api/v2/portfolio/events/orders/batched"
```

`ProxyWriter.rest_post` (and `rest_delete`) built the URL as `self._base + REST_PREFIX + path` with
`REST_PREFIX = "/trade-api/v2"`. Feeding a full path in therefore produced
`.../trade-api/v2` + `/trade-api/v2/portfolio/events/orders` = a **doubled** prefix.

The cancel/status/open-orders templates are RELATIVE
(`CANCEL_PATH_TMPL = "/portfolio/events/orders/{order_id}"`, `ORDER_STATUS_PATH_TMPL`,
`OPEN_ORDERS_PATH`), so they composed correctly — only the two CREATE constants were full paths.
The sibling `service/executor.py` (HarnessExecutor) was unaffected because its `_default_post` dials
`proxy_base + path` with the full path and never re-prepends the prefix.

## Fix (belt and braces)

1. **`service/proxy_auth.py`** — new `compose_rest_url(base, path)`: idempotent composition. If
   `path` is already under `REST_PREFIX` it is not prefixed again; the function asserts the final URL
   path starts with **exactly one** `/trade-api/v2/`. `ProxyAuth.rest_get` now uses it (previously it
   had the same unguarded `base + REST_PREFIX + path`).
2. **`service/proxy_writer.py`** — `rest_post` and `rest_delete` compose via `compose_rest_url`
   (the leading-slash validation moved into the helper). `rest_get` already delegated to
   `ProxyAuth.rest_get`, so it inherits the guard. `REST_PREFIX` import dropped (now unused here).
3. **`service/v32/executor.py`** — introduced explicit RELATIVE create paths
   `REL_SINGLE_CREATE = "/portfolio/events/orders"`, `REL_BATCH_CREATE = "/portfolio/events/orders/batched"`,
   each pinned by a **module-level assertion** that it equals the envelope constant minus `REST_PREFIX`
   (so a future envelope edit cannot silently diverge). All three `rest_post` call sites now pass the
   relative constants.

Both layers are now correct independently: the executor passes relative paths (right by convention)
AND the writer refuses to double a prefix even if handed a full one (right by construction).

## Why the existing tests missed it

`tests/test_v32_executor.py` drives the executor with a duck-typed `FakeWriter` whose `rest_post`
records `(path, body)` and asserted `path == SINGLE_CREATE_PATH`. That checks the string **handed
to** the writer — never the URL the writer would actually dial. A fake writer cannot double a prefix
it never composes, so the doubled-prefix bug lived entirely below the seam the executor suite tested.
There was also no dedicated `ProxyWriter` URL-composition test at all.

Fix: new `tests/test_proxy_writer_url.py` injects a fake **transport** (the `http_post` /
`http_delete` / `http_get` layer *below* `ProxyWriter`/`ProxyAuth`) and asserts the exact composed
URL for every writer call the executor makes:

- single create -> `http://127.0.0.1:8642/trade-api/v2/portfolio/events/orders`
- batch create  -> `.../trade-api/v2/portfolio/events/orders/batched`
- cancel        -> `.../trade-api/v2/portfolio/events/orders/ORD-1`
- order status  -> `.../trade-api/v2/portfolio/orders/ORD-1`
- open orders   -> `.../trade-api/v2/portfolio/orders?status=resting`

Plus: (b) the two create URL paths are members of the proxy's create-path set (the four
`_ORDER_CREATE_PATHS` literals copied verbatim into the test), and (c) a regression test that passing
an already-prefixed path to `rest_post` yields a single prefix (the exact live bug input). The two
executor assertions were updated to `REL_SINGLE_CREATE` / `REL_BATCH_CREATE`.

## Proxy hardening note (recommendation, not changed here)

The doubled path `/trade-api/v2/trade-api/v2/portfolio/events/orders` was forwarded to the venue
**uncapped**. Tracing `degeneracy-proxy/proxy.py`: it is not in `_ORDER_CREATE_PATHS` (so no
create cap / budget), and it does not start with any `_ORDER_WRITE_PREFIXES` entry
(`/trade-api/v2/portfolio/events/orders`, `/trade-api/v2/portfolio/orders`) — because the doubled
string begins `/trade-api/v2/trade-api/...`. So the existing non-create-order-write refusal
(`order_write_not_create`) did not trigger either. It merely starts with `/trade-api/`, so it passed
the "only /trade-api/... paths are proxied" gate and was forwarded uncapped; the venue 404'd.

Recommendation: the proxy should **refuse any non-GET request whose path is not under a known
order-WRITE prefix** (i.e. tighten the fail-closed check to cover every non-GET under `/trade-api/`,
not only those already matching `_ORDER_WRITE_PREFIXES`). A malformed or unexpected write path should
be rejected at the proxy rather than forwarded uncapped. (Client-side, the doubled prefix can no
longer occur after this fix; the proxy hardening is defense-in-depth.)

## Receipts

- Files changed: `service/proxy_auth.py`, `service/proxy_writer.py`, `service/v32/executor.py`,
  `tests/test_v32_executor.py`, and new `tests/test_proxy_writer_url.py`.
- `cd pilot && python -m pytest -q` -> **833 passed** (baseline 823 + 10 new URL/regression tests).
- No network touched; no `.env`/`*.pem` read; no sealed/holdout data accessed.
