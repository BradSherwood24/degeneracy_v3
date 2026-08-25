# Phase 1 spine — adversarial review (SEPARATE opus48 REVIEWER)

Reviewed 2026-08-21. Scope: `pilot/service/*` + `pilot/tests/*` as built, against `pilot/PLAN.md`,
`pilot/ceremony/commission.md`, the Phase-1 build report (11 confessions), and the V2 originals
(`degeneracy_v2/kalshi/ws.py`, `degeneracy_v2/signals/order_book.py`). No `.env`/`*.pem` read; no
network dialed; no `sim/out/sealed_eval/` read; no sealed-day (08-02..18) file read. Historical-data
reads were REST market records only (2026-06/07, non-sealed), for fixture grounding.

**Verdict: APPROVE-WITH-FIXES.** One HIGH fail-closed defect found and FIXED in-tree (mine to own),
with regression tests. The remaining items are flags/handoffs; exactly one (F2) must reach Brad
before the *strangle* trades live, but none blocks the Phase 1 passive spine (which fires no orders).
Phase 1 exit criteria — golden replay reconstructs live books from the journal, watchdogs demonstrated
firing — are met. Test count: **116 passed** (110 builder + 6 review probes), 0 failed.

---

## Findings

### F1 — HIGH — FIXED — non-finite wire values failed OPEN (and crashed replay)
`service/book.py::_to_decimal` coerced via `Decimal(str(value))` and returned the result unchecked.
For a wire `"nan"`/`"inf"` (or a Python float `nan`/`inf`), that builds `Decimal('NaN')` /
`Decimal('Infinity')` — **not** `None`. Two harms, both in the exact direction the review targets
("garbage that yields a plausible-looking top / fires an order"):

- **Fail-open on the book.** A NaN/Inf price or size never trips the fail-closed `_mark_malformed`
  path. An `Infinity`-priced level tops `best_bid` (`max(...)`), so `best_no_ask = 1 − ∞ = −∞` — a
  non-`None`, plausible-looking-but-garbage top-of-book. In Phase 1 this only pollutes the record; in
  Phase 2 it is a candidate to fire an order on garbage.
- **Replay crash.** A NaN `delta_fp` makes `new_size <= 0` raise `decimal.InvalidOperation` out of
  `apply_delta`. `replay.replay_records` has **no** per-record guard, so a single poisoned frame in a
  journal aborts that whole window's golden replay / paired report. Confirmed by probe:
  `REPLAY CRASHED: InvalidOperation`.

**Fix (minimal, at the source):** `_to_decimal` now returns `None` when the parsed Decimal is not
`is_finite()`. Snapshot then drops the level; delta then hits `_mark_malformed()` → `suspect=True`
(no exception). This removes the crash at its root (price/delta are guaranteed finite before the
`<= 0` compare) and restores fail-closed. Regression tests: `test_to_decimal_rejects_nonfinite`,
`test_nan_delta_marks_suspect_not_raises`, `test_inf_snapshot_level_dropped_no_garbage_top`,
`test_nan_delta_in_journal_does_not_crash_replay` (all in `tests/test_review_probes.py`). No signature
change — behavior-only.

### F2 — MEDIUM — RESOLVED 2026-08-21 — pilot pairing model diverges from the census authority
**RESOLVED (opus48 builder, 2026-08-21, via timing-review F1):** the pilot now pools ALL live
co-settling hourly generations at phase B (`wake.hourly_ladders`/`WakeResult.hourly_pool_markets`) and
pairs the anchor against that pool with the existing `nearest_hourly_strike` — reproducing census
`h1_by_ct` nearest-to-anchor selection AND its equidistant tie-exclusion (an identical strike in two
generations now stands the window down as a cross-generation TIE). The strike census would pair can no
longer live in an unsubscribed generation: the WS subscription and the ladder-map check both follow the
CHOSEN market's generation. The dual-generation bin-2 discovery confound named below is closed. See
`build/timing_review.md` F1 (RESOLVED) for the full fix + tests. Original finding retained below.

The census (`sim/census.py`, the authority the paired replay is measured against) pairs the hourly
leg by pooling **all** hourly markets that share a `close_time` — across generations — into
`h1_by_ct[close_time]`, then picking the single strike **nearest the anchor A** (exact ties →
`EXCL_NEAREST_TIE`, excluded). It never uses "generation" or "step" for pairing. The pilot's
`wake.py` instead selects **one whole generation** by smallest window duration and subscribes only
that generation's strikes.

On a dual-generation window (the 2026-07-31 21:00 shape), the strike the census would pair can live
in the generation the pilot did **not** subscribe. Phase 2's C-from-asks would then price a
*different strike* than the sim → a systematic **bin-2 divergence** (live fired / sim didn't, or vice
versa) that is a discovery artifact, not a fill artifact — i.e. it corrupts the very measurement the
pilot exists to make. This is invisible on normal single-generation days (the common case) and only
bites on dual-generation days, which makes it the kind of silent confound worth naming now. It is not
a Phase-1 blocker (no orders, no signal engine yet), but it must be resolved with Brad / the census
owner before the strangle trades live. Ruling belongs in confession #1, below.

### F3 — LOW — FLAGGED (do NOT implement the confessed alternative) — expected-step-from-window would invent an unregistered spec and blunt the guard
Confession #1 asks whether `expected_step` should derive from the selected generation's own window
length instead of `(hour, weekday)`. **My judgment: no, do not implement that.** Grounds:
- The ladder step ($100 / $250 / $500) is a **live-only structural guard**. Verified it is NOT a
  pricing input to `sim/tape_sim.py`, `sim/census.py`, or `sim/gate_fit.py` (grep returned nothing).
  There is therefore **no pinned window→step table** to derive from; inventing one would be an
  unregistered spec (house law: registered specs rule).
- Deriving the expectation from the selected generation would make the check tautological and mask
  genuine structural deviations — the opposite of its purpose.
- The current behavior (deviation → `strangle_disabled=True` + alarm, sub-$1 continues) is fail-closed
  and correct in spirit ("unrecognized ladder = stand down"). The Friday-21:00 "weekly false alarm"
  is (a) acceptable fail-closed noise and (b) not even weekly — the corpus shows dual-generation as a
  one-off (07-31), not every Friday. The real issue is F2 (which strikes are subscribed), not the
  step label. Resolve via F2; leave `expected_step` as written.

### F4 — LOW — FLAGGED + covered — golden replay was only tested IN-MEMORY
The builder's determinism/parity tests (`test_replay_deterministic_same_journal_twice`,
`test_recorder_builds_books_and_captures_tops_matching_replay`) compare replays of the *same
in-memory Journal* (identical Python objects). The path that actually runs days later —
`flush → load_journal → replay` from disk, with wire numerics that arrived as Python floats — was
**untested**. Probed: parity holds (floats round-trip losslessly through `json` and both live and
replay coerce via `Decimal(str(x))`), so the confession's byte-identity claim is defensible. Added
`test_flush_reload_replay_matches_inmemory_with_float_wire_values` and
`test_replay_two_processes_would_match_via_stable_serialization` as regression guards.

### F5 — LOW — FLAGGED — `replay_records` has no per-record isolation
With F1 fixed, wire garbage no longer crashes replay, but a hand-corrupted / truncated journal record
(missing `kind`/`obj`, malformed line) can still raise mid-stream and sink the whole window's report.
Acceptable for Phase 1 (corruption should be loud, and determinism-of-failure is a virtue here — I did
NOT wrap it in a broad try/except, which would silently skip real corruption). The Phase 2+ paired-
report harness should isolate per-window replay failures so one bad journal doesn't lose a day. Harness
concern, not a book/replay defect.

### F6 — LOW — FLAGGED — dial-failure reconnect throttle is coarse
When `/ws-auth` is unsigned/down, `run_recording` re-dials every ~`poll_seconds` (0.5s) until the
deadline — ~7,200 attempts across an hour window. Bounded (not a busy-loop; the `await sup` on the
supervisor's sleep paces it, matching confession #5), but a persistently-unsigned proxy hammers the
endpoint for the full window. Add backoff on repeated dial failures in Phase 4 ops.

### F7 — LOW/informational — recorder books ANY ticker the wire sends
`record_window.WindowRecorder._book(market)` lazily creates a `BookMirror` for any `market_ticker` in
a dispatched frame, including tickers outside the subscribed leg set. Benign for Phase-1 recording
(faithfully journaled). Interface note for Phase 2: `decide()` must key strictly off the discovered
legs — an unexpected/unsubscribed ticker must never feed a decision.

### F8 — informational (interface/handoff) — `data_age None` semantics must NOT be reused as the entry gate
The passive watchdog (`watchdog_action`) treats `data_age is None` as startup grace → `CONTINUE`.
Correct for Phase 1 (no orders; confession #9). **Phase 2 `decide()` MUST treat
`data_age_seconds() is None` (and `current_lag_seconds() is None`) as STALE / fail-closed at the entry
gate — do not reuse the watchdog's grace.** Independently, `TopOfBook.suspect is True` must gate out
entry regardless of age. This preserves the V2 `data_age None = stale` contract end-to-end, which the
Phase-1 passive watchdog deliberately relaxes only because it fires no orders.

---

## Confession rulings (all 11)

1. **Leg selection / dual-generation smallest-window.** ACCEPT the literal, fail-closed reading for
   Phase 1. But see **F2/F3**: do NOT adopt the window-derived-step alternative, and escalate the
   deeper pairing-model mismatch (census pools all co-settling strikes → nearest-to-anchor; pilot
   subscribes one generation) to Brad before the strangle trades live. Implementation is defensible;
   the *semantics* need Brad.
2. **Ladder map keyed on close hour/weekday directly.** ACCEPT. 21:00 is unambiguous; weekday
   arithmetic verified (07-31 = Fri, 08-20 = Thu, 08-21 = Fri → $500/$250/$500 as intended).
3. **Active-status semantics (`{"active","open"}`, ≥1 active + future close).** ACCEPT as fail-closed.
   Grounded that historical records read `"finalized"`; ≥1-active-with-future-close is a sound
   fail-closed rule. Live status strings → live-checklist item.
4. **WS payload field names assumed from V2 (`yes_dollars_fp`/`no_dollars_fp`/`delta_fp`/`price_dollars`
   /`ts_ms`/`ts`).** ACCEPT — and BETTER-GROUNDED than the confession claims. V2's
   `scratch/ws_reconstruct_audit.py` replays REAL recorded windows: it reads `price_dollars`/`delta_fp`
   for deltas and passes `msg` straight into `apply_snapshot` (which reads `yes_dollars_fp`/
   `no_dollars_fp`), and that reconstruction succeeded over the recorded corpus (WP-2's 328,799-frame
   figure) — a wrong snapshot key would have produced empty books. So the orderbook field names are
   evidence-backed, not merely assumed; the `ts` ISO form is likewise grounded (07-12). **The ONE field
   V2 could not exercise is `market_ticker` on every dispatched frame** — V2 was single-ticker, so the
   multi-leg attribution the pilot fail-closes on is the genuinely new surface. Narrow the live-checklist
   to that (below); the golden tests surface any residual mismatch on the first passive run.
5. **`ws_connect_params` not retried.** ACCEPT (ephemeral signature; outer loop re-mints). See F6 on
   throttle.
6. **`rest_get` path convention (`/…` after `/trade-api/v2`).** ACCEPT.
7. **Series tickers hardcoded + `/markets` param support.** ACCEPT. Market-record fields the discovery
   relies on (`event_ticker`, `ticker`, `close_time`, `open_time`, `floor_strike`, `status`,
   `strike_type`) all verified present in real non-sealed historical market data. Param support
   (`min/max_close_ts`, cursor pagination) → live-checklist item.
8. **Full hourly ladder subscribed by default (~188 strikes).** ACCEPT with the builder's own ops
   caveat — socket comfort at ~188×3 channels is a first-passive-run measurement. `--max-hourly-strikes`
   is the lever.
9. **Watchdog thresholds (lag 30s / silence 45s) are reconnect-only.** ACCEPT; correctly distinguished
   from the tight Phase-2 entry gate. See F8.
10. **suspect-after-seq-gap not replay-reconstructable.** ACCEPT — verified the parity argument holds:
    the gap frame is dispatched then the socket force-closes, so NO book-frame tops are captured
    between the gap and the healing snapshot; `mark_all_suspect` runs between dials (captures nothing);
    the post-reconnect snapshot rebuilds wholesale (suspect→False) in both live and replay. Replayed
    `suspect` reflects only journaled malformed deltas — as documented.
11. **Journal not thread-safe.** ACCEPT for Phase 1 (single WS/asyncio thread appends). The Phase 3
    Executor/Reconciler thread MUST serialize appends (documented in the docstring).

---

## Live-verification checklist (first passive runs MUST tick these off)

- [ ] **`market_ticker` on EVERY dispatched frame** (the one genuinely new field — V2 was
      single-ticker). If any orderbook/trade/ticker frame omits it, the pilot fail-closes and drops it
      (`dropped_no_market` climbs) — a high `dropped_no_market` on the first run means the live wire
      attributes frames differently and dispatch is silently starving. Watch this counter closely.
- [ ] **WS orderbook payload shape** (evidence-backed via V2 recordings, confirm anyway):
      `orderbook_snapshot` carries `yes_dollars_fp`/`no_dollars_fp` as `[price_dollars, size]` pairs;
      `orderbook_delta` carries `side` ∈ {`yes`,`no`}, `price_dollars`, `delta_fp`. (A mismatch shows
      instantly as empty/suspect books in the golden replay.)
- [ ] **Server timestamps**: deltas carry `ts` (ISO) and/or `ts_ms` (epoch-ms); lag gauge reads
      non-`None` within the first second of a dial.
- [ ] **Live open-market status string** (`"active"` vs `"open"` vs other) for both series — confirm it
      is inside `ACTIVE_STATUSES`, else the window stands down erroneously.
- [ ] **Per-strike ladder status**: confirm ≥1-active-suffices does not admit an all-but-one-settled
      ladder in practice.
- [ ] **`/markets` params**: `series_ticker`, `status=open`, `min_close_ts`/`max_close_ts` window,
      `limit`, `cursor` pagination all honored; the co-settling set returned is complete.
- [ ] **`/ws-auth` contract**: 200 body `{ws_url, headers{…}}`; 503 body when unsigned.
- [ ] **Signature freshness tolerance**: a fresh mint survives a slow dial.
- [ ] **Full ~188-strike hourly subscription** is comfortable on one socket (else use
      `--max-hourly-strikes`).
- [ ] **Dual-generation reality** (F2): when two hourly generations co-settle, record which one the
      15m market's nearest-strike actually belongs to — the input Brad needs to rule on F2.

---

## Interface changes (LOUD — for the Phase 2 builder)

- **NONE to public signatures.** The only code change is behavior-internal to `book._to_decimal`
  (now rejects non-finite → `None`; snapshot drops the level, delta marks the book suspect). Every
  interface documented in the build report stands unchanged: `BookMirror`, `TopOfBook`,
  `KalshiWebSocketClient`, `WsCallbacks(market_ticker, payload)`, `Journal`, `replay_books`,
  `WakeContext`/`WakeResult`/`Leg`/`StandDown`/`LadderCheck`, `record_window`.
- **Handoff constraints for `decide()`** (not interface changes, but binding): (F8) treat
  `data_age_seconds()`/`current_lag_seconds()` `None` as STALE and any `TopOfBook.suspect` as
  no-fire; (F7) key strictly off the discovered legs, never off an arbitrary wire `market_ticker`;
  (F2) do not assume the subscribed single generation contains the census's nearest-strike pair on
  dual-generation windows — pending Brad's ruling.

## Files
- Fixed: `C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\service\book.py` (`_to_decimal`)
- Added: `C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\tests\test_review_probes.py` (6 tests)
- Reviewed unchanged: `pilot\service\{proxy_auth,ws_client,journal,wake,record_window,replay}.py`
