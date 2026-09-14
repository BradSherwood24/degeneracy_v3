# Review — PR #43 `v32/fix-create-path` (doubled REST prefix on order CREATE)

Reviewer: Opus 4.8, in review worktree `dv3_wt_review` @ `review/fix-create-path` (37b835b, base 42d3609).

## Verdict: APPROVE — merge. The fix is correct, belt-and-braces, and the new test closes the exact seam the executor suite missed. No code defect found; two non-blocking nits below.

## Suite
- `cd pilot && python -m pytest -q` -> **829 passed, 2 skipped**. The remaining collection errors
  (4 files: `test_parity`, `test_shakedown`, `reference_impl_review`, `test_review_probes2`, plus 2
  `test_quintile` cases) are ALL missing train-data / sim build-artifacts absent from a fresh worktree
  (`sim/out/census_train.csv`, `historical-data/15-minute/markets/2026-06-11.jsonl`) — nothing to do
  with this diff. Builder's 833 comes from a worktree that had those artifacts. Zero test FAILURES.
- Money-path files alone: `test_proxy_writer_url.py` + `test_v32_executor.py` -> **31 passed**.

## Probes

1. **`compose_rest_url` (proxy_auth.py:54-77)** — idempotent and correct for all four cases:
   - relative `/portfolio/events/orders` -> single prefix;
   - already-prefixed `/trade-api/v2/portfolio/events/orders` -> NOT re-prefixed (the live bug input);
   - query string (`...orders?status=resting`) and trailing slash -> preserved verbatim;
   - composition is pure concatenation (`base + REST_PREFIX + path` or `base + path`) — nothing after
     the prefix is stripped or altered. The doubled-prefix assertion fires ONLY on a genuinely doubled
     `/trade-api/v2/trade-api/v2/...` path, never on a legitimate endpoint.

2. **Executor writer URLs** — all five compose to the documented venue paths, and the test asserts the
   FINAL URL at the true production seam. `test_proxy_writer_url.py` injects `http_post`/`http_delete`
   (below `ProxyWriter`) and `http_get` (below a real `ProxyAuth`) — the exact callables that default
   to `requests.post/delete/get(url, ...)` (`proxy_writer.py:_default_post/_default_delete`,
   `proxy_auth.py:_default_get`). The recorded `url` is byte-identical to what `requests` would dial;
   the GET records `url + urlencode(params)`, matching the wire query string.
   - single create -> `/trade-api/v2/portfolio/events/orders`
   - batch create  -> `/trade-api/v2/portfolio/events/orders/batched`
   - cancel        -> `/trade-api/v2/portfolio/events/orders/{id}`
   - status        -> `/trade-api/v2/portfolio/orders/{id}`
   - open orders   -> `/trade-api/v2/portfolio/orders?status=resting`

3. **Proxy will cap+budget the creates.** The test's `PROXY_ORDER_CREATE_PATHS` copies the four
   `_ORDER_CREATE_PATHS` literals verbatim from `degeneracy-proxy/proxy.py:77-81` — confirmed matching
   the live source. Both composed create paths are members -> proxy applies the create cap/budget. The
   cancel path `/trade-api/v2/portfolio/events/orders/{id}` starts with the `_ORDER_WRITE_PREFIXES`
   entry `/trade-api/v2/portfolio/events/orders` (proxy.py:70-72) -> routes to the orders host.

4. **`ProxyAuth.rest_get` unchanged for existing callers.** Every grepped GET caller passes a RELATIVE
   path (`/markets`, `/markets/{ticker}`, `/portfolio/positions|balance|orders*`, `MARKETS_PATH`,
   `BALANCE_PATH`). For relative paths `compose_rest_url` yields the identical string to the old
   `base + REST_PREFIX + path`; the leading-slash `ValueError` guard is preserved. New behavior is
   strictly same-or-better (an accidentally-prefixed GET would now be de-duplicated rather than
   doubled). Full suite green corroborates no message-string or behavior regression.

5. **Nothing else changed; no network.** Diff is 6 files. All tests inject transports and a no-op
   `sleep`; `requests` is never imported on the tested path.

## Nits (non-blocking, no fix applied — hotfix under arm-window time pressure; recommend follow-up)

- N1 `proxy_auth.py:64` — the `if path == REST_PREFIX` branch routes a BARE `/trade-api/v2` (no
  trailing content) to `url = base + path`, but the very next assertion (`startswith REST_PREFIX + "/"`)
  then fires on it. The branch's bare-prefix case is effectively dead / self-contradicting. Harmless
  (no caller passes a bare prefix; a real endpoint always has a tail), but the `== REST_PREFIX`
  disjunct could be dropped for clarity.
- N2 The doubled-prefix guard is an `assert`, stripped under `python -O`. The PRIMARY correctness is
  the branch logic (idempotent composition), which holds regardless of `-O`; `-O` would only remove
  the defense-in-depth net. Pilot is not launched with `-O`, so no action needed — noted for awareness.

## Proxy hardening (builder's recommendation, out of scope here) — endorsed
The doubled path was forwarded UNCAPPED because it matched neither `_ORDER_CREATE_PATHS` nor an
`_ORDER_WRITE_PREFIXES` entry (it began `/trade-api/v2/trade-api/...`). Client-side this can no longer
occur after the fix; still worth tightening the proxy to fail-closed on any non-GET `/trade-api/`
path that is not under a known order-write prefix. Defense-in-depth, separate change.
