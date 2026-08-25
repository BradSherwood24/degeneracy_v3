# PILOT PLAN — live auto-fire dual-contract pilot (floor-arithmetic policy)

Drafted 2026-08-20 on Brad's go. This is the build plan; the binding ceremony documents
(commission + frozen falsifier) are Phase 0 deliverables and gate Phase 5. Nothing in
this file overrides them.

## What the pilot is

A live measurement, not a strategy validation. The question: **how close does observed
behavior with real fills match simulated behavior over the same data?** Every live day
gets a paired replay — the fetcher pulls that day's tape, the frozen sim runs it, and a
comparison script bins every discrepancy:

1. sim fired / live never saw the signal (feed gap, latency)
2. live saw a signal the sim didn't (signal-engine divergence)
3. both fired / live didn't fill (fillability — the reason this pilot exists)
4. both filled / different price (slippage, measured exactly)
5. both filled, same price, payoff matches (the sim tells the truth)

Known confounds, ruled on 2026-08-20 (review F2/F3): our own fills re-enter the fetched
tape the sim replays. At pilot size (1–2 contracts vs ~20k prints per entered window)
this is assessed as noise — accepted, not fixed. Contingency if the paired report shows
anomalies: exclude our prints by size+price+time match against our fill records. The
book-vs-prints C difference (live decides on asks, sim on prints) is a known structural
delta, reported as such — journal-based replay (same book live saw) is available for
bin-1/2 parity from Phase 2 onward.

## Frozen policy roster (from the sealed-read survivors — no flip-EV anywhere)

- **sub-$1 flip**, all quintiles: first moment C < $1.00 (fees in), both legs. Arithmetic
  floor; cannot lose if filled as priced.
- **Q1-strangle EV−5**: G/σ̂ in the bottom train quintile, fresh pair, ev ≥ 5¢.
- Same params as the sim: fresh ≤ 1 s, A15.9 refutation, A15.10 dwell, audited fee
  formula, census EV curve sha-pinned. Policy config = frozen JSON + sha, logged at
  startup; the service refuses to run on sha mismatch.

## Decisions already made (Brad, 2026-08-20)

- **Hosting**: local, on the machine where the proxy lives. Auto-update off, awake 24/7 (Brad).
- **Orders**: cross, capped, never resting — realized as IOC limit orders at the max
  price (the venue has no market type; see API facts below). **IOC over FOK** (Brad,
  2026-08-20): partial fills are acceptable; keeping the order type constant across
  sizes means a sizing problem is never confounded with an order-type change. Revisit
  FOK only if unpaired entries become a problem in practice.
- **Notifications**: Kalshi's own app. Nothing to build.
- **Trigger mode**: auto-fire within hard caps. ALLOW_ORDERS is Brad's lever alone.
- **Scaling ladder**: shakedown (no orders) → 1 pair → 2 pairs → readiness report for 5.
  Each stage's goal is readiness for the next.
- **Shakedown length**: minimum 2 runs, extended until ≥ 1 would-fire signal has been
  produced and logged (no order).
- PEMs revoked except the live proxy key (done 2026-08-20).

## Alarm / stop taxonomy (feeds the falsifier; summary here)

- **Alarms** (notify, keep running): slippage > 2¢ per fill side; ladder-map deviation
  (see below); guard trips; entry-rate drift.
- **Imbalance protocol** (partial fill or orphaned leg — logged, then FIXED FAST;
  hedge ratio is 1:1, always). Brad's rulings 2026-08-20 (review F1/F5/F6/F7 folded):
  (1) retry-buy the deficient leg to match size, bounded by the **pair-cost ceiling =
  $1.00 + average window return** (pinned in the falsifier from train, sub-$1 mean
  ≈ +3.2¢ → ceiling ≈ $1.032; a net-zero pair beats chasing a fleeting price; strangle
  analog: total pair cost ≤ bucket-fair, i.e. EV ≥ 0, pinned likewise); (2) **max 5
  retries per side**, each also bounded by seconds-to-settlement; (3) if the ceiling
  or retry budget is exhausted, SELL the overfilled side down to match — target count
  always rounds DOWN (equal-at-lower is the safe direction; buying up is how the floor
  breaks). End state of every entry is 1:1 or 0:0. The Reconciler DECIDES rebalances;
  the Executor DISPATCHES them (single order-authority mutex — one code path touches
  order endpoints). Every imbalance event is its own bin in the paired report.
- **Stops** (freeze orders, flatten if held, alert loudly): imbalance the protocol
  fails to restore within its time bound; a non-fill the system believes is a fill,
  or any reconciliation mismatch vs exchange truth; daily realized-loss cap; any
  filled sub-$1 pair realizing negative (arithmetic violation — n=1 suffices).
- **Ladder map** (verified over all 69 corpus days): $100 steps for all hours except
  21:00 UTC = $250, or **$500 on Fridays**. 2026-07-31 showed a dual-generation listing
  (both ladders in one hour) — validate the ladder the 15m market actually pairs
  against. Deviation from the map → alarm + strangle stands down; sub-$1 may continue
  (structure-independent).

---

## Architecture

Not a monolith, but close to Brad's instinct: **one pure decision core, invoked on every
normalized event**, wrapped by thin I/O shells. The shape that makes the paired replay
possible is the shape of the whole service:

```
Task Scheduler (:40 each hour)
  └─ window process (one process per window — crash isolation, no long-lived drift)
       ├─ WakeContext     REST via proxy: query ALL markets for the event and select the
       │                  leg pair by smallest window co-settling at the top of the hour
       │                  (dynamic discovery — no hardcoded pairing rule; no co-settling
       │                  pair found → stand down, which also covers the 06–09 UTC
       │                  missing-hourly days). Ladder-map check, σ̂ + quintile
       │                  assignment, balance + affordability gate, policy sha check.
       │                  Wake EARLY relative to the window; local clock trusted
       │                  (Brad's ruling), server-ts deltas journaled so skew is
       │                  observable after the fact.
       ├─ MarketFeed      lifted V2 ws.py: multi-ticker subscribe (both legs),
       │                  orderbook_delta + trade + fill channels, seq-gap → resnapshot,
       │                  lag + silence watchdogs, recorder tap
       ├─ BookMirror      full-depth book per leg from snapshot+deltas (depth kept even
       │                  though the pilot reads top-of-book — 5-pair scaling and every
       │                  future implementation reads depth)
       ├─ Journal         every raw envelope + local recv timestamp + order intents and
       │                  responses, buffered IN MEMORY during the window, flushed to
       │                  JSONL once the window closes (Brad's ruling: no disk I/O in the
       │                  hot path). Accepted cost: a crash loses that window's replay
       │                  record — positions stay safe because exchange truth (reconcile)
       │                  is authoritative, but that window can't be diagnosed offline.
       ├─ decide()        PURE function: (frozen_params, window_state, event) → actions.
       │                  No clock reads, no network, no I/O. Time comes from events.
       │                  The same function runs live and in replay — bit-identical.
       ├─ Executor        the ONLY code path that touches order endpoints. Batched IOC
       │                  limit-at-max-price legs, client_order_id per intent,
       │                  single-flight per (window, side), POST never retried (V2 law).
       └─ Reconciler      the second thread. Two jobs: (a) IMBALANCE PROTOCOL — on any
                          leg-size mismatch, retry-buy the deficient leg (bounded by max
                          chase price), else sell the overfilled side down; 1:1 or 0:0,
                          nothing between; (b) positions/fills via proxy (read endpoints)
                          diffed against the local ledger every few seconds — reality vs
                          what the main thread thinks. Unrestorable mismatch → STOP.
```

**Why orderbook WS and not the trades WS**: the book is what we can actually fill
against — signals compute C from live asks, not from other people's prints. The trades
channel is still subscribed and journaled: (a) replay parity with the print-based
historical sim needs it, (b) V2 learned the delta stream can't see aggressor-hits-bid
fills, (c) it's free. Book decides, trades corroborate.

**Why process-per-window**: state cannot rot across windows; a crash costs one window,
never a position (Reconciler-at-startup rule: on start, reconcile against the exchange
BEFORE any signal logic — an inherited position means flatten + stop, never resume).

## Coding standards (house law + this service's specifics)

1. **Pure core, I/O shells.** All strategy logic in `decide()` and pure helpers.
   Anything that touches network, clock, or disk lives in a shell and is injected.
2. **Determinism is a test.** Golden replay tests: feed a recorded journal through the
   core and assert the identical action sequence, byte for byte. Any nondeterminism
   (dict ordering, float wobble, wall-clock leak) is a bug.
3. **Decimal for money** (sim law), floats only in σ̂/statistics. No pandas. Stdlib +
   `websockets` + `requests` only.
4. **Fail closed.** Unknown lag = stale. Unmeasured age = stale. Unrecognized ladder =
   stand down. Exception in the signal path = window over, alarm, no order.
5. **Idempotency on the order path.** UUID client_order_id per intent; single-flight per
   (window, side); POST never retried; the answer to "did it go through?" is always the
   exchange (Reconciler), never our own memory.
6. **Caps live in two places**: the Executor AND the proxy (defense-in-depth — a service
   bug cannot oversize because the proxy refuses: max contracts/order, series whitelist,
   daily order budget).
7. **Journal before dispatch** (V2 recorder discipline): the raw envelope is on disk
   before the callback runs, so replay can always reproduce what live saw.
8. **Tests for everything**: unit (book builder deltas/gaps, fee math, decide() cases,
   order translation — V2's direction-mapping tests carry over verbatim), golden replay,
   executor against a fake proxy, integration against recorded journals. The build is
   opus48's; the adversarial review (independent reimplementation of decide()) gates
   ALLOW_ORDERS, same as the unseal runner.
9. **One-line structured log per event class**; per-window summary JSON appended to a
   pilot ledger — the paired-replay report reads only these artifacts.

## Recycling inventory (verified 2026-08-20)

| asset | source | state |
|---|---|---|
| WS client (seq-gap, lag/silence watchdogs, recorder tap, callback injection) | `degeneracy_v2/kalshi/ws.py` | lifts nearly whole; needs auth-via-proxy + multi-ticker subscribe |
| Book builder | `degeneracy_v2/signals/order_book.py` + tests | lift + keep full depth |
| Order direction mapping (buy NO = ask @ 100−p etc.) + its unit tests | `degeneracy_v2/kalshi/order_translate.py` | lifts verbatim — the direction-critical pure function |
| REST retry semantics (GET retried, POST never) | `degeneracy_v2/kalshi/rest.py` | lift, pointed at the proxy |
| 2s book-age gate, 45s watchdog | V2 orchestrator | encoded in ws.py's `data_age_seconds`; wire into decide() gates |
| Signing proxy | `degeneracy-proxy/proxy.py` | running; needs Phase 0 extensions |
| σ̂ / quintile edges / fee / EV curve | V3 `sim/` (census, gate_fit, tape_sim) | import the frozen law, never reimplement |
| Paired replay | V3 `tools/fetch_history.py` + `sim/tape_sim.py` | exists; comparison script is new |

**New builds**: proxy `/ws-auth` endpoint + proxy-level order caps; BookMirror-to-signal
plumbing; `decide()`; Executor; Reconciler; window process harness; five-bin comparison
script.

## API facts (verified against docs.kalshi.com, 2026-08-20)

- **No market order type exists.** The V2 order shape is limit-only with
  `time_in_force` ∈ {`fill_or_kill`, `good_till_canceled`, `immediate_or_cancel`}.
  "IOC with max price" = **limit at max price + IOC**: crosses as taker up to the
  limit, unfilled remainder cancels server-side. IOC cannot rest and cannot be
  combined with `expiration_time`. FOK available if all-or-nothing per leg is
  preferred at count ≥ 2 (at count 1, IOC ≡ FOK).
- **Batch create (`batch-create-orders-v2`)**: both legs in ONE request — one HTTP
  round trip minimizes inter-leg latency. NOT atomic: per-entry success/error; one
  leg can fail while the other fills → the flatten path handles it. Rate cost:
  10 tokens per order, billed per item.
- **The order/batch response is synchronous fill truth**: `fill_count`,
  `remaining_count`, `average_fill_price`, `average_fee_paid`, `ts_ms` per entry —
  orphan detection happens in the same response that placed the legs; the `fill`
  WS channel and the Reconciler are corroboration, not the primary signal.
  `average_fee_paid` feeds the sub-$1 arithmetic stop with ACTUAL fees.
- **Wire details**: `side` is `bid`/`ask` from the YES perspective (V2's
  `order_translate` maps this exactly — buy NO = ask @ 100−p); `count` and `price`
  are fixed-point STRINGS; `self_trade_prevention_type` is required
  (`taker_at_cross`/`maker`); `client_order_id` supported (idempotency key).

## Review disposition (opus48 REVISE, 2026-08-20 — `PLAN_review_opus48.md`)

Brad's rulings folded into this rev: F1 (pair-cost ceiling $1 + avg window return,
≤5 retries/side), F2/F3 (own-fill contamination accepted as noise at pilot size;
size+price+time exclusion as contingency), F5/F6/F7 (retry/time bounds, decide/dispatch
split, round-down), F9 (clock trusted, early wake), F13-as-modified (in-memory journal,
flush at window close), F11/F17 (dynamic leg discovery via event query), F18
(affordability gate), F16 (schedule in UTC). Engineering findings F10 (rate-token
budget, 429 fail-closed), F12 (proxy caps persist to disk, thread-safe), F14 (ws.py
"adapt" not "lift": ProxyAuth shim, market-keyed callbacks, re-mint headers on
re-dial), F15 (Phase 2 exit: no documented delta may flip a fire/no-fire decision),
F19–F21, F23–F24 accepted as written.

**DEFERRED (owed before Phase 3 freeze): stop semantics — F4 (crash-mid-window
settles unmanaged before next wake; in-window supervisor?) and F8 (stops should HOLD
complete floor-protected pairs to settlement, flatten only unprotected exposure).
Brad will come back to these.**

## Verifications still owed before Phase 3

- WS auth handshake via proxy-minted headers (signature freshness tolerance).
- `fill` WS channel payload on partial fills at 1–2 contracts.
- Batch max size for Brad's account tier ("scales with your tier's write budget").
- Demo-env parity (proxy already routes demo) — candidate dress-rehearsal venue for
  Phase 3 exit.

---

## Phases

### Phase 0 — Ceremony + proxy extensions
Commission + falsifier for the pilot (measurement protocol, five-bin definitions,
alarm/stop taxonomy, promotion gates, caps as frozen numbers) — **falsifier freezes on
Brad's go before Phase 5 fires a single order.** Proxy: `/ws-auth`, per-order contract
cap, series whitelist, daily order budget; tests.
**Exit**: proxy tests green; Brad has read the commission; API verifications above answered.

### Phase 1 — Market data spine
Lift ws.py + book builder; multi-ticker subscribe (both legs); Journal; WakeContext with
ladder-map check. Run passively over live windows (no signal logic).
**Exit**: golden replay reconstructs live book states exactly from the journal; watchdogs
demonstrated firing on injected faults.

### Phase 2 — Signal engine + parity harness
`decide()` with the frozen roster; σ̂/quintile from REST at wake reproducing sim values;
C-from-asks + fee math. The parity harness: replay Phase-1 journals AND run the tape-sim
over the same hours; compare would-fire moments (bins 1–2 before money exists).
**Exit**: signal parity on recorded windows — every divergence explained and either fixed
or documented as a known live/sim delta (e.g. book-vs-prints C).

### Phase 3 — Execution + safety
Executor (batched IOC legs at max price), Reconciler with the imbalance protocol
(retry-buy to match → else sell-down to match → 1:1 or 0:0), stops/alarms, caps wired
end-to-end. Tested against fake proxy; optional demo-env dress rehearsal.
**Exit**: adversarial review (opus48, independent decide() reimplementation) APPROVES;
fault-injection suite green (orphan, partial, sell-down path, phantom fill, seq gap
mid-entry, crash mid-order, crash mid-rebalance).

### Phase 4 — Service harness + ops
Task Scheduler wake :40 hourly; process-per-window lifecycle; reconcile-first startup;
pilot ledger; runbook (start/stop/kill, what each alarm means, where the day's report
lands).
**Exit**: 24h unattended dry cycle (no orders): every window woke, ran, journaled, slept.

### Phase 5 — The ladder (ceremony live)
Falsifier frozen. Brad sets ALLOW_ORDERS.
1. **Shakedown**: ≥ 2 runs, extended until ≥ 1 would-fire logged. No orders.
2. **Day 1 — 1 pair**: paired replay report that evening. Goal: ready for 2?
3. **2 pairs**: adds the second-pair question (walk the book?). Goal: ready for 5?
4. **Readiness report for 5** goes to Brad with the accumulated five-bin evidence.
Promotion gates and stop conditions per the frozen falsifier; ~$10/day capital at
dual-contract size.

---

*Build/review agents: opus48 (Brad's standing mandate). The sealed days 08-02..18 are
spent-as-train; the pilot's paired replays run on live forward days only, which also
accrue toward revival clause (a).*
