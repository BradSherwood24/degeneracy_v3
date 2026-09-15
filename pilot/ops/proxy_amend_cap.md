# Proxy change proposal — contract-cap + ticker-check the Amend Order endpoint

**Status: PROPOSAL ONLY. Do NOT apply from this repo.** `degeneracy-proxy/proxy.py` lives on Brad's
box; only Brad edits and restarts it (house law: agents never touch proxy key material / .env / *.pem).
This file records the exact change so Brad can apply it deliberately. Line numbers are as read on
2026-09-15 (READ ONLY).

## The finding (why this is needed)

The amend-first replace (Brad 2026-09-15) sends `POST /trade-api/v2/portfolio/events/orders/{id}/amend`.
The proxy today **REFUSES every non-create order-write POST**, amends included:

- `proxy.py` ~L459-478: any POST under an `_ORDER_WRITE_PREFIXES` prefix that is **not** an
  `is_order_create` path is rejected `403 {"cap":"order_write_not_create"}` ("amend/decrease and other
  order-write POSTs are refused (uncappable body shape)"), BEFORE any upstream contact or budget touch.
- `is_order_create` (~L168-178) returns True only for the exact create/batch paths in
  `_ORDER_CREATE_PATHS` (~L77-82); the `/amend` subpath is not among them.

**Consequence today (safe):** every amend the executor sends gets a local `403` from the proxy →
`LiveExecutor._amend_rest` sees `resp.ok == False` → it journals `amend_failed` and runs the proven
cancel → confirm → create FALLBACK. So with the current proxy, armed behavior is IDENTICAL to the old
sequential replace (the amend is a wasted ~1 ms localhost round-trip that never reaches Kalshi and never
consumes budget). The amend benefit (continuous resting, no ~0.7 s no-rest gap) only turns on once the
change below is applied and the proxy restarted. **Nothing forces this change; the pilot is correct
without it — it just keeps falling back.**

## The change

The amend body is a single JSON object with top-level `ticker` and `count` (our executor sends
`count:"1.00"`, a `ticker`, `side:"ask"`, `price`, both coids, `exchange_index`). That is the SAME shape
`check_order_caps` already caps for a single create, so the cap logic is reused verbatim.

1. **Recognize the amend path** (near `is_order_create`, ~L168). Add a pure helper:

   ```python
   def is_order_amend(method: str, path: str) -> bool:
       """True iff this is a POST to the Amend Order V2 endpoint
       (/portfolio/events/orders/{id}/amend, or the legacy /portfolio/orders/{id}/amend)."""
       if method != "POST":
           return False
       clean = path.split("?", 1)[0].rstrip("/")
       return clean.endswith("/amend") and any(
           clean.startswith(p + "/") for p in _ORDER_WRITE_PREFIXES
       )
   ```

2. **Stop blanket-refusing amends** (the non-create refusal, ~L464-478). Exempt amends from the refusal
   so they are capped instead of blocked:

   ```python
   if (
       method == "POST"
       and any(clean_path.startswith(p) for p in _ORDER_WRITE_PREFIXES)
       and not is_order_create(method, path)
       and not is_order_amend(method, path)          # <-- ADD: amends are capped below, not refused
   ):
       ... existing 403 order_write_not_create ...
   ```

3. **Contract-cap + ticker-check the amend** (right after the create-caps block, ~L483-517). An amend is
   always a single object, never batched:

   ```python
   if is_order_amend(method, path):
       try:
           entries = parse_order_entries(body, is_batch=False)   # [the single amend object]
       except BodyParseError as e:
           self._respond_json(400, {"error": "amend body rejected (fail closed)",
                                    "cap": "body_parse", "detail": str(e)})
           print(f"[proxy] BLOCKED {method} {path} (unparseable amend body)")
           return
       violation = check_order_caps(
           entries, CONFIG.max_contracts_per_order, CONFIG.ticker_prefixes
       )
       if violation is not None:
           self._respond_json(403, violation)
           print(f"[proxy] BLOCKED {method} {path} (cap {violation['cap']}: {violation['detail']})")
           return
       # BUDGET: see the trade-off below. RECOMMENDED — count the amend against the daily budget:
       allowed, used, remaining = CONFIG.order_budget.try_consume(1)
       if not allowed:
           self._respond_json(403, {"error": "order rejected by proxy cap",
                                    "cap": "daily_order_budget",
                                    "detail": f"amend; {remaining} of {CONFIG.daily_order_budget} left"})
           print(f"[proxy] BLOCKED {method} {path} (daily budget: {remaining} remaining)")
           return
   ```

   Routing already works: `is_order_write_path` (~L91) matches the `/amend` subpath (it's under
   `_ORDER_WRITE_PREFIXES`), so the request already routes to the orders host (~L520-523). No routing
   edit needed.

## Budget trade-off (pick one — the block above uses "count")

- **COUNT amends against `DAILY_ORDER_BUDGET` (recommended, shown above).** An amend is a real wire write
  to Kalshi that forfeits queue position and can cross/fill; counting it keeps the budget's meaning as
  "order-establishing writes per day". With amend-first a requote is now ONE amend where it used to be one
  create (+one uncapped cancel), so the ~1,860/day create math is preserved and the S5
  self-degrade-to-dry safety (`orders_remaining_today >= 200`) still bounds a runaway requote loop.
- **DON'T count amends (alternative).** Amends modify an existing order rather than adding net exposure,
  so one could argue they should not draw down the create budget. RISK: a boundary-flap or gate bug could
  then requote unboundedly without ever tripping the daily budget → no automatic degrade-to-dry. If you
  choose this, add a SEPARATE amend-rate guard so a flap cannot hammer the venue uncapped.

The contract cap (`max_contracts_per_order`) and ticker-prefix whitelist are NON-optional either way — the
amend must never establish more than the capped size on a non-KXBTC ticker.

## After applying

- Brad restarts the proxy (`ALLOW_ORDERS` + `ORDER_TICKER_PREFIXES` incl. `KXBTC` +
  `DAILY_ORDER_BUDGET` unchanged) so the new code loads. Until the restart, amends keep 403-ing and the
  executor keeps falling back to cancel+create (safe).
- Confirm with one dry/armed window: the first `amend_confirmed` shows a 2xx and `amends_failed` stays low
  (per `ops/V32_ARMING.md` MUST CONFIRM item 7). If amends still 403 with `order_write_not_create`, the
  exemption in step 2 did not load.
- Add proxy unit tests mirroring the create-cap tests: an amend over `max_contracts_per_order` → 403
  `max_contracts_per_order`; an amend on a non-whitelisted ticker → 403 `order_ticker_prefixes`; a
  well-formed 1-contract KXBTC amend → forwarded (and budget decremented, if counting).
