# Phase 0 proxy — adversarial review

Reviewer: opus48 (SEPARATE from the builder). Date: 2026-08-21.
Target: `C:\Users\Brads\Python_stuff\degeneracy-proxy\proxy.py` + tests.
Constraints honored: `.env` and `*.pem` never read; no live network calls; all new
tests network-free. Fixes applied in-tree during this review cycle; full suite re-run
after every change.

**Verdict: APPROVE-WITH-FIXES (all fixes applied, suite green). Equivalent to APPROVE
FOR ARMING once Brad restarts the proxy to load this code** — the service reads config
and code once at startup; nothing hot-reloads, so the review's fixes take effect only on
`python proxy.py` restart.

Test count: **89 passed** (was 61). 69 in `tests/test_proxy.py` (8 net added by
restructuring the parse tests to the fixed contract) + 20 new in
`tests/test_review_probes.py`. ~7.5s, no network, no skips.

---

## Findings

### R1 — BLOCKER (FIXED): single-endpoint `orders`-key oversize bypass
`parse_order_entries` decided single-vs-batch by the **presence of an `"orders"` key in
the body**, not by the endpoint path. Confirmed exploit:

```
POST /trade-api/v2/portfolio/orders
{"ticker":"KXBTC15M-A","count":"999","orders":[]}
```

The proxy read this as an empty batch → `entries == []` → `check_order_caps` looped zero
times → **caps trivially passed** → budget consumed 0 → the body was forwarded verbatim.
Kalshi's single `/orders` endpoint acts on the top-level `{ticker,count}` and ignores an
unknown `orders` field, so this would have created **999 contracts uncapped**. This is
exactly the "even if the pilot service is maximally wrong, the proxy must make oversizing
impossible" property, defeated by a body-shape trick.

Root cause: the parser guessed intent from the body instead of the known endpoint.
Fix: `parse_order_entries(body, is_batch)` — the caller passes `is_batch` derived from the
normalized path (`clean_path == ".../orders/batched"`). The single endpoint now always
caps the top-level object Kalshi will act on; the batch endpoint requires a real
`orders` list. Regression tests: `test_single_endpoint_with_orders_key_is_capped_on_toplevel`,
`test_single_endpoint_orders_key_hiding_oversize_entry_is_capped`,
`test_single_endpoint_orders_key_bypass_blocked_end_to_end` (full-handler 403, never forwarded).

### R2 — MAJOR (FIXED): `NaN`/`Infinity` count crashed the request thread
`parse_count("NaN")` constructs a valid quiet-NaN `Decimal` without error, but the
downstream `count <= 0` comparison raises `decimal.InvalidOperation`, which
`check_order_caps` does not catch. Confirmed: it propagated out of `do_POST` →
unhandled exception in the handler thread → dropped connection / 500, **no clean JSON**,
violating the "error paths return proper JSON" and "fail closed cleanly" contract. (The
order was not forwarded, so it failed toward safety, but via a crash, not a decision.)
Fix: `parse_count` now rejects non-finite Decimals (`is_finite()` check) → clean 403
`max_contracts_per_order`. `Infinity` was already caught by `> max`, but is now rejected
explicitly too. Tests: `test_parse_count_rejects_non_finite`,
`test_caps_reject_non_finite_count_as_403`, `test_nan_count_returns_clean_403_not_500`.

### R3 — MAJOR (FIXED — closes confession #1): amend/decrease POSTs forwarded uncapped
Confirmed: `POST .../orders/{id}/amend` and `.../decrease` route to the orders **write**
host (they start with `_ORDER_WRITE_PREFIX`) and were forwarded with **no contract/prefix/
budget cap** — a real oversize channel (amend can raise `count`). The builder confessed
and invited closure; the pilot's Executor never amends (sell-down is a fresh capped
create), so closing it breaks nothing. Fix: a fail-closed gate refuses any **POST** under
the orders prefix that is not a recognized create (`cap: order_write_not_create`, 403,
never forwarded). DELETE cancels are unaffected (not POST). Tests:
`test_amend_post_is_blocked`, `test_decrease_post_is_blocked`.

### R4 — NOTE (FIXED, hardening): body not drained on early-return
My new amend gate (and, pre-existing, the read-only gate) returned 403 without reading
the request body, which can desync an HTTP/1.1 keep-alive connection (leftover body bytes
parsed as the next request line). I moved the body read to the top of the non-GET path so
the amend and cap early-returns always drain it. The **pre-existing read-only 403 gate**
(hit on every non-GET when `ALLOW_ORDERS` is off) still returns before the drain — see R9;
left as-is to keep the change minimal, and it self-heals via client retry on a fresh
connection.

### R5 — MINOR (FLAGGED, not fixed): corrupt budget file fails OPEN
`OrderBudget._load` treats any unreadable/invalid JSON as `{}` → today's used = 0 → **full
budget restored**. This is fail-**open** on the budget: a corrupted `order_budget.json`
re-grants up to the full daily allowance even if orders were already placed today. Blast
radius is bounded — the contract cap (2/entry) and ticker whitelist are startup config,
unaffected by the file, so the worst case is ≤ `DAILY_ORDER_BUDGET` entries × 2 contracts,
not unbounded. I did NOT change it because (a) it is a deliberate design choice with a
passing test (`test_budget_corrupt_file_recovers`), (b) the only writer is the proxy's own
atomic `os.replace`, so corruption is near-nil, and (c) failing closed on an
existing-but-unparseable file would brick the pilot until Brad deletes the file. **Brad's
call**: if house-law "fail closed everywhere" is read strictly, change `_load` to refuse
(raise) on `JSONDecodeError` for an existing file while still allowing `FileNotFoundError`
(first run). I recommend leaving as-is; flagging for the record.

### R6 — NOTE (FLAGGED): case-variant / URL-encoded create paths
- **Case variant** (`/Trade-API/...`): fails the lowercase `/trade-api/` guard → **404,
  never forwarded** (verified: `test_case_variant_order_path_does_not_reach_orders_host`).
  A post-prefix case variant like `.../portfolio/Orders` routes to the **read** host (not
  the write host) and is not a create → uncapped forward to a host that will 404 it. Not
  exploitable given Kalshi path case-sensitivity, but it is a defense-in-depth seam.
- **URL-encoding** (`%6frders`): `http.server` does not decode `self.path`, so the encoded
  path misses both the create set and the write-prefix routing → forwarded to the **read**
  host with a signature over the encoded path. For this to place an order, Kalshi would
  have to decode the path, accept order creation on the read host, and validate the
  signature over the encoded form — three unlikely conjunctions. Low real risk; not fixed
  (robust path canonicalization risks breaking legitimate forwarding and is speculative).
  The pilot's Executor sends canonical paths.

### R7 — NOTE (FLAGGED, intended): ticker whitelist is a true prefix
`"KXBTC15MFOO"` passes prefix `KXBTC15M` (verified). This is intended prefix semantics and
acceptable given Kalshi's ticker namespace, but note it is not an exact-series match: any
ticker starting with an allowed prefix passes. Lowercase (`kxbtc15m-...`) correctly FAILS
(case-sensitive) — verified `test_lowercase_ticker_fails_prefix`.

### R8 — NOTE (FLAGGED, intended): `/ws-auth` mints signed headers regardless of ALLOW_ORDERS
`/ws-auth` returns `KALSHI-ACCESS-KEY` (the key **id**, not the private key), a fresh
signature, and a timestamp — usable to open the **authenticated** WS (fills/positions) from
localhost even when the proxy is read-only. This is by design (WS is read/subscribe scope;
the pilot needs it in shakedown before orders are armed) and the bind is **127.0.0.1 only**
(verified in `main()` and the tests). The minted signature is **path-scoped** to
`GET /trade-api/ws/v2` and cannot be replayed against any other path or method (confirmed
`test_ws_auth_signs_the_ws_path`). No private-key material appears in the body
(`test_ws_auth_no_private_key_material`). Accepted.

### R9 — NOTE (FLAGGED): pre-existing read-only 403 gate does not drain body
Same keep-alive seam as R4, on the pre-existing read-only path (line 418). Out of Phase 0
scope and self-healing via client retry; noting for completeness. If tightened later, move
that gate below the body-drain too.

### R10 — NOTE (FLAGGED, = confession #8): importing `proxy` loads the real signer
Module-level `CONFIG = Config()` runs the real `load_dotenv`/`_load_signer` in the pytest
process, so the test run holds the real private key in memory (never read by any test,
never serialized — every test uses a throwaway RSA key). Sanctioned and leak-free. Hygiene
suggestion, not a defect: set `KALSHI_ENV=demo` (or monkeypatch) for CI so the prod signer
is never instantiated in a test process.

---

## Confession rulings

1. **Amend/decrease uncapped** — was a **real MAJOR hole; CLOSED** (R3).
2. **400 vs 403 taxonomy** — **ACCEPTABLE.** Structural→400, semantic-cap→403; both fail
   closed and never forward. Classifying a missing/unparseable `count` inside otherwise
   valid JSON as a 403 cap failure is defensible (it is structurally parseable, cap-failing).
3. **Budget counts attempts pre-forward** — **ACCEPTABLE.** Consuming before the forward is
   the fail-closed direction (over-counts on a 502, never under-counts) and avoids a
   forward-then-untangle race. Correctly documented as attempts, not fills.
4. **Budget cost = entries, not contracts** — **ACCEPTABLE.** The per-entry contract cap is
   the real oversize guard; the budget is a coarse daily backstop. Note the theoretical
   daily max is `DAILY_ORDER_BUDGET` entries × `MAX_CONTRACTS_PER_ORDER` = 100×2 = 200
   contracts — far above pilot need, fine as a backstop.
5. **Non-positive/fractional rejected, no rounding** — **CORRECT** and fail-closed.
6. **Keeps all past UTC dates** — **ACCEPTABLE** (one small key/day; auditable).
7. **Empty batch `{"orders":[]}` forwarded** — **ACCEPTABLE** (0 entries, 0 budget; upstream
   rejects). No oversize path.
8. **Test import runs `Config()`** — **ACCEPTABLE** (R10); hygiene suggestion only.

## Regression checks (all intact)
127.0.0.1-only bind; read-only-by-default 403; two-host routing (write→orders host,
verified via `test_valid_create_forwards_to_orders_host_signed_and_consumes_budget`);
hop-by-hop + `KALSHI-ACCESS-*` client-header stripping and proxy re-signing (same test,
asserts a spoofed client `KALSHI-ACCESS-KEY` is replaced by the real signer's);
`/health` additive-only; unsigned mode serves public GETs and forces read-only; error
paths return JSON (now including the former NaN crash).
