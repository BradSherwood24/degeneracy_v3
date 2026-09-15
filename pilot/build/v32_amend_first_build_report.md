# V3.2 build report — amend-first replace with cancel+create fallback

**Date:** 2026-09-15
**Branch:** `feat/amend-first-replace` (based on `fix/shadow-quote-window` / PR #54)
**Authority (verbatim):** Brad, 2026-09-15 ~18:10Z — *"Yea, I agree. Use the cancel and recreate flow as
a backup if our post to ammend the order fails. Go ahead and build that"*, in reply to the proposal to
replace the mid-window replace mechanics with Kalshi's Amend Order, falling back to cancel+create.

## The problem

The mid-window replace was strictly SEQUENTIAL: `CANCEL_REST → wait for OrderCancelled → PLACE_REST` on a
later tick (R-OVERLAP ruling, never two live rests). Correct, but it leaves **nothing resting for ~one
round-trip (~0.7 s) per replace** — ~60-90 s per busy hour at ~77 replaces/h. A qualifying pump print that
lands in that no-rest gap is a fill the strategy should have taken and did not.

### The miss it caused (2026-09-15 14:00Z window)

The live rest was cancelled at **13:45:20Z** for a requote; a qualifying spot-bucket YES print landed in
the cancel→recreate gap before the new rest was live, so the window took no fill it otherwise would have.

### Rest-absent share (fraction of the quoting window with NO live rest) — 2026-09-15 windows

| window (UTC close) | rest-absent share |
|--------------------|-------------------|
| 01:00Z–05:00Z      | 1–5 %             |
| 14:00Z             | 11 %              |
| 15:00Z             | 15 %              |
| 16:00Z             | 11 %              |

The busy hours (14–16Z) spent 11–15 % of the 10-minute quoting window with no order on the book purely to
the cancel→recreate gap — the direct cause of the 14:00Z miss and a standing drag on fill rate.

## The design (amend-first)

A same-bucket requote (|dn| ≥ tol and ≥ deb_ms, the existing gate) now emits `AMEND_REST` — Kalshi
**Amend Order V2** (`POST /portfolio/events/orders/{id}/amend?exchange_index=<idx>`, `exchange_index`
also in the body). The `order_id` **persists**; a price change **forfeits queue position exactly as
cancel+create did** (documented amend semantics), so the economics are unchanged — but the order stays
resting continuously and reaches the new price ONE round-trip sooner, with no no-rest gap.

- **Core** (`service.v32.core`): `_emit_amend` mints a new coid and sets `amend_in_flight` (holds all
  further requotes); `_apply_amended` updates `rest_live.price`/coid IN PLACE on `OrderAmended` and
  clears the flag. Bucket changes (different ticker — an amend cannot change the ticker) and the
  quote-end cancel are UNCHANGED (still cancel). "Never two live rests" preserved — the one order is
  amended. An amend IS a replace for `replace_count`, the A_REPLACE alarm, and the ledger `replaces`
  counter (counted on CONFIRM, mirroring the cancel+create path which counts at the create — so a
  fallback counts exactly once too).
- **Fill during the amend:** `OrderAmended.fill_count > 0` (a TAKER fill at `average_fill_price`,
  normalized to NO-space) is routed into the wings exactly like a fill-before-cancel; with count 1 the
  order is fully filled and nothing remains resting. The executor books the taker fill into money-math
  with the REAL taker fee (`average_fee_paid`), de-duped by `order_id`.
- **Executor** (`service.v32.executor`): `_amend_rest` POSTs the sharded amend; `_amend_body` sends
  `side:"ask"` (the bucket-NO buy's book side), `price` = YES-space `1 − n` (same convention as the
  create), `count:"1.00"`. On 2xx → `OrderAmended` + RestBook updated (new coid live, same order_id, old
  coid retained "amended" for late-fill attribution, F-1).

## The fallback (Brad's requirement)

On **ANY** non-2xx / timeout / exception, `_amend_rest` journals `amend_failed` (recording
`fallback:"cancel_create"`) and runs the existing sequential path: the **sharded** DELETE (PR #50
backoff / status-truth), whose `OrderCancelled` clears the core's rest so the next tick re-places via
`PLACE_REST` through the **pre-PLACE venue-truth invariant**. This is the proven cancel → confirm →
create, unchanged. A cancel-race fill discovered in the fallback is routed to the wings as before.

## Counters / journals / report

- Journals: `amend_rest` (request), `amend_confirmed` (response), `amend_failed` (+ that the fallback was
  taken), `amend_fill` (when the amend crossed).
- Ledger counters (additive, `.get` default 0): `amends_attempted`, `amends_confirmed`, `amends_failed`,
  `amend_fallbacks`, `fills_on_amend`.
- Report: an amend totals line next to `replaces`.
- Modes: `WOULD_AMEND_REST` twin in dry/shakedown (FrozenExecutor synth-amends so the dry state machine
  cycles); FrozenExecutor **refuses** a real `AMEND_REST` (P3-1, like PLACE/CANCEL).

## Falsifier

Registered as a **MECHANICS CLARIFICATION** (2026-09-15 ~18:10Z) in `ceremony/v32_falsifier.md` — no
[pin], the params sha, the `n ≥ 30` count, or the STATUS line touched (STATUS stays FROZEN). The
"What is being judged" line's `(cancel → confirm → create, never two live rests)` was edited in place to
`(amend-first, cancel → confirm → create as the fallback; never two live rests)`. The golden test
`test_core_live_fill_matches_lagging_reference` still reproduces the +10.36c reference EXACTLY (the
reference model `_ref_lagging` is itself single-lag — the order rests at its last price during one RTT —
so amend-first, which is also single-lag, matches it; the old cancel+create, a double-lag with a gap,
happened to match too on this fixture).

## Unknowns / risks (must confirm live)

1. **`post_only` on amend is UNDOCUMENTED.** The amend body inherits no `post_only`. We therefore treat
   `fill_count > 0` in the amend response as a REST FILL at `average_fill_price` and route it into the
   wings (it can only be at our amended price or better). If the venue never crosses on an amend (amend is
   maker-only), this path simply never fires — safe either way.
2. **Amend rate limits are UNDOCUMENTED.** At ~77 replaces/h the amend rate is ~0.02/s — far under any
   plausible limit — but this is unverified. Watch for amend 429s (they route to the fallback, which is
   safe, but a burst of 429s would mean the venue rate-limits amends more tightly than creates).
3. **PROXY CAP GAP (blocking for the amend benefit; safe without it).** The proxy today REFUSES every
   non-create order-write POST, so **every amend 403s and falls back to cancel+create** — armed behavior
   is identical to the old sequential replace until Brad applies the proxy change in
   `pilot/ops/proxy_amend_cap.md` and restarts. The failed amend 403 is a local proxy round-trip; it
   never reaches Kalshi and never consumes budget.
4. **Units of `average_fill_price` on an amend response** are assumed to follow the create-response
   convention (a NO order's price reported in YES-space; normalized via `normalize_fill_to_side`). If the
   amend response reported NO-space directly, the booked fill price would be wrong — the
   `exec_price_mismatch` alarm and the first `amend_fill` record are the live check. Booking is conservative
   in the meantime (a crossed amend can only fill at our amended price or better).

## Tests

`python -m pytest pilot/tests -q` — all green in the live tree (see the PR body for the count). New/updated
coverage: `tests/test_v32_amend.py` (12: wire body/path, 2xx one-order, 2xx cross → taker fill routed,
404/500/timeout → fallback cancel+create with pre-PLACE invariant + never two rests, FrozenExecutor
refuses / synth-amends, ledger counters, report totals); `tests/test_v32_core.py` (amend-first replace,
fill-during-amend, amend counts as replace, A_REPLACE via amends, quote-end/bucket-change still cancel,
shakedown WOULD_AMEND); `tests/test_v32_golden.py` (harness models `AMEND_REST → OrderAmended`; the
+10.36c reference still reproduces); `tests/test_run_v32.py` (FrozenExecutor dry amend cycle, F-1 late
fill on a retained pre-amend coid); `tests/test_v32_falsifier_pins.py` (new assertion: the MECHANICS
CLARIFICATION + "amend" are in the Registration; existing pin assertions unchanged and green).
