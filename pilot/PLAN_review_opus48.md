# PLAN review — degeneracy_v3 live-pilot (adversarial)

Reviewer: opus48 (independent adversarial review, Brad's standing mandate)
Date: 2026-08-20
Target: `C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\PLAN.md`
Grounding read: `degeneracy-proxy/proxy.py`, `degeneracy_v2/kalshi/ws.py`, `claudes-corner/sealed_read_2026_08_20.md`

Scope: design review of the plan. NOT a code review. Brad's locked decisions (local host,
IOC-over-FOK, app notifications, log+fix-fast imbalance, 1:1-or-0:0, scaling ladder,
auto-fire-within-caps, ALLOW_ORDERS is Brad's) are treated as given; findings flag only
where the plan implements a locked decision incorrectly/incompletely, or where the plan
is internally unsound.

Severity key: BLOCKER (must fix before the affected phase builds) / MAJOR / MINOR / NOTE.

---

## BLOCKERs

### F1 — BLOCKER — The retry-buy branch of the imbalance protocol can break the sub-$1 arithmetic floor
Plan text: imbalance protocol step (1) "retry-buy the deficient leg to match size, bounded
by a max chase price (pinned in the falsifier)"; and the whole justification of the pilot:
"sub-$1 flip ... Arithmetic floor; cannot lose if filled as priced."

The floor guarantee is *C (both legs, fees in) < $1.00*. If leg A fills and leg B orphans,
"retry-buy leg B bounded by a max chase price" completes the pair at whatever the chase
bound allows — which can push total C **above $1.00**, converting a riskless pair into a
losing one. The plan then has a stop that fires on exactly that outcome ("any filled sub-$1
pair realizing negative — n=1 suffices"). So the fix-fast protocol as written can *manufacture
the very arithmetic violation that halts the pilot.* The live IOC "limit-at-max-price" for the
first fire has the identical hazard if max-price is a fillability pad rather than the floor
price.

The bound on any buy (first fire OR retry) must not be an independent "max chase price"; it
must be the **floor budget** = ($1.00 − already-paid − est. fees) per remaining contract. If
the current ask exceeds that budget, retry-buy is *impossible without breaking the floor* and
the protocol MUST fall through to sell-down. As written the plan permits a floor-violating
completion.
Resolution: pin the retry/limit bound AS the floor-preserving price (budget-to-$1 net fees),
not a separate chase number; if ask > budget, sell-down is the only legal branch.

### F2 — BLOCKER (measurement validity) — The pilot's own fills contaminate the tape the paired sim replays; confound is unnamed
Plan text: "Every live day gets a paired replay — the fetcher pulls that day's tape, the
frozen sim runs it"; recycling row "Paired replay | tools/fetch_history.py + sim/tape_sim.py".

Our orders are **IOC takers**: every fill becomes a public print. `fetch_history.py` pulls the
public tape → that tape now contains our own prints → the tape-sim replays liquidity/prints
that existed *only because we traded*. V2 already learned aggressor-hits-bid fills are visible
only on the trade channel, so the sim's fillability signal is exactly the channel we pollute.
Result: bin 3 (fillability) and bin 4 (slippage) are biased toward agreement by our own
participation, and the bias **grows with the scaling ladder** (1→2→5 pairs). This silently
undermines the single question the pilot exists to answer. The confound is named nowhere in
the plan.
Resolution: tag our own fills (client_order_id / trade ids / price-time) and exclude them from
the replayed tape; state this exclusion in the commission and verify it in the comparison
script's tests. See also F3 (prefer replaying the recorded book, not the fetched print tape).

---

## MAJORs

### F3 — MAJOR — Daily paired replay compares live-book fills against a print-based sim; bin 4 "slippage" is contaminated by methodology, not just execution
Plan text: signals "compute C from live asks, not from other people's prints"; but the daily
comparison runs `tape_sim` over `fetch_history` (prints). Phase 2 exit even concedes
"book-vs-prints C" as a known delta. Live C is computed from the **book (asks)**; the fetched
sim computes C from **prints**. Bin 4 ("both filled / different price — slippage, measured
exactly") therefore carries a permanent structural offset that is the book/print methodology
gap, NOT slippage. The pilot cannot claim to "measure slippage exactly" while the two sides
use different price sources.
Resolution: for the daily paired report, run the frozen sim against the **recorded journal
book** (Phase-1 journal), which is the same data source live decided on; keep the
fetched-tape sim only as a secondary corroborator. This also sidesteps most of F2.

### F4 — MAJOR — "Crash never costs a position" is false: a crash mid-window leaves a position that settles unmanaged before the next :40 reconcile
Plan text: "a crash costs one window, never a position"; "Reconciler-at-startup ... an
inherited position means flatten + stop, never resume"; wake ":40 each hour."

15m and hourly markets settle at the top of the hour (and 15m every :00/:15/:30/:45). If a
window process crashes after a fill, the *next* process does not wake until :40 of the next
hour — by which time the crashed position has **already settled** on the top-of-hour BRTI
print. "Flatten if held" is then a no-op; the position was carried through settlement with no
supervision. For a floor-protected sub-$1 pair that is benign; for a Q1-strangle orphan it is
a naked directional bet held to expiry. The plan's crash-safety claim overstates what
next-window reconcile can deliver.
Resolution: add an in-window supervisor / auto-restart within the same window (fast restart →
reconcile-first → resume management or flatten before settlement), and state explicitly that
next-window reconcile only cleans up, it does not prevent settlement of a crashed position.

### F5 — MAJOR — Sell-down can itself partial-fill and has no defined termination; interaction with settlement undefined
Plan text: "(2) if the buy window has closed too far, SELL the overfilled side down to match.
End state of every entry is 1:1 or 0:0"; stop: "imbalance the protocol fails to restore
within its time bound."

The sell-down is an IOC order and can partial-fill, especially into a thinning late-window
book — leaving the pair still imbalanced and requiring another sell-down, which can partial
again. The protocol defines no iteration count, no numeric time bound, and no behavior when
the bound collides with settlement (seconds-to-:00). If sell-down cannot complete before
settlement you settle imbalanced. "1:1 or 0:0" is asserted as an invariant but the mechanism
cannot guarantee it.
Resolution: define max iterations, a numeric time bound tied to seconds-before-settlement, and
the terminal action if the bound is hit (STOP + alert; accept the unavoidable imbalance for a
floor-protected leg, flatten-attempt for a strangle leg).

### F6 — MAJOR — Order authority race: Executor is declared "the ONLY code path that touches order endpoints," yet the Reconciler places rebalance orders
Plan text (architecture): "Executor the ONLY code path that touches order endpoints"; and
"Reconciler ... retry-buy the deficient leg ... else sell the overfilled side down."

Direct contradiction, and a live race: two threads can submit orders. Concrete failure — the
Reconciler is selling leg-1 of pair-1 down while a late delta drives `decide()` to fire pair-2
(2-pair stage); both submit; positions land in an unplanned state that reconcile then reads as
a mismatch → STOP. `single-flight per (window, side)` is not defined as a lock shared across
both threads.
Resolution: make the Executor the sole order gateway (Reconciler *decides* rebalances,
Executor *dispatches* them under one shared order-authority mutex); state the mutex explicitly.

### F7 — MAJOR — count=1 vs count=2 target ambiguity: "match size" defaults to buying UP, which is the floor-unsafe direction
Plan text: "retry-buy the deficient leg to match size ... End state ... is 1:1 or 0:0."

At size 2, partials produce (2,1),(1,2),(1,0),(2,0)... "match size" reads as "buy the short leg
up to the higher count," which maximizes capital at risk and is exactly the direction that can
break the floor (F1). "1:1 or 0:0" permits *any* equal count, so selling down to the lower
count is equally valid and is floor-safe. The plan does not say which target is preferred.
Resolution: prefer the *lower* count target (sell/round down) unless a floor-preserving
retry-buy is available; define the target selection deterministically.

### F8 — MAJOR — Stop "flatten if held" contradicts holding a floor-protected pair to settlement, and can convert guaranteed wins into realized losses
Plan text: stops "freeze orders, flatten if held, alert loudly," triggered by e.g. "daily
realized-loss cap."

A complete sub-$1 flip pair is strictly safe held to settlement (pays $1 floor, $2 on pin).
"Flatten if held" would SELL both legs into a possibly thin late book at a loss — turning a
guaranteed payout into a realized loss — precisely when a daily-loss-cap stop has fired. The
stop action is undifferentiated across position types.
Resolution: distinguish stop actions by position type — *hold to settlement* for a complete
floor-protected flip pair; *flatten* only for unprotected/strangle exposure and orphaned legs.

### F9 — MAJOR — No clock discipline specified; the "fresh ≤1s" gate and the lag watchdog both depend on a trustworthy local wall clock on a Windows machine
Plan text: "fresh ≤ 1 s"; recycled ws.py uses `local_wall − server_ts` for lag and floors
staleness with it. V2's own note (verified in ws.py) assumes "Render NTP skew <<1s."

The pilot moves to Brad's always-on **Windows** box. Default Windows time sync (w32time) can
drift multiple seconds and resync in jumps. A 1s freshness gate and a 30s lag threshold are
both corrupted by an untended local clock — a drifted-fast clock makes every book look stale
(fail-closed, no orders — merely lost pilot days), a drifted-slow clock makes stale books look
fresh (fail-OPEN — fires on stale data). Nowhere addressed.
Resolution: require NTP discipline as a Phase 4 ops item (enable/verify w32time against a
reliable source, alert on skew); document assumed max skew relative to the 1s gate.

### F10 — MAJOR — Rate-limit / token budget across 2 legs × retries × reconciler polling is not budgeted; 429 behavior undefined
Plan text (API facts): "10 tokens per order, billed per item"; Reconciler polls "every few
seconds." No global token budget.

One fire = 2 orders = 20 tokens; a retry-buy adds more; the Reconciler read-polls continuously;
WakeContext does REST reads. At the 5-pair rung a single fire is up to 10 orders (100 tokens)
plus polling. A 429 mid-batch or on the second leg = orphan. The plan defines no token budget,
no backoff, and no fail-closed rule for 429 on the order path.
Resolution: budget tokens per window across legs+retries+reconciler; define 429 handling
(order path fails closed → treat as no-fill → imbalance protocol; read path backs off).

### F11 — MAJOR — Missing hourly leg (06–09 UTC) and market halt/pause are not handled; either yields an un-completable pair
Plan text: mentions neither the 06–09 UTC hourly gap nor Kalshi halts. WakeContext lists
"ladders both legs" but no missing-leg / not_active / halted stand-down.

If the hourly (KXBTCD) leg is not listed for a window, or either leg is halted/paused, a pair
cannot form or cannot be completed → guaranteed orphan risk. Fail-closed requires standing down
the whole entry when either leg is absent or not active.
Resolution: WakeContext must verify both legs exist and are active; either leg missing/halted →
stand down (both policies); add a halt-mid-window handler (freeze new fires, run imbalance/stop
logic on any open leg).

### F12 — MAJOR — Proxy-level caps reset on restart; ALLOW_ORDERS toggling *requires* a restart, so the defense-in-depth budget silently re-zeros exactly when Brad arms
Verified in `proxy.py`: config (including ALLOW_ORDERS) is read once at startup;
docstring confirms "Brad sets ALLOW_ORDERS=true ... and restarts." Proxy is
`ThreadingHTTPServer`. Plan: "Caps live in two places ... the proxy refuses ... daily order
budget."

A daily order budget held only in proxy memory resets to zero on every proxy restart — and
arming the pilot *is* a restart. Any mid-day restart (reboot, re-arm, crash) silently refills
the daily budget → the second cap fails open. Also the threaded server needs a thread-safe
counter or concurrent orders race the cap.
Resolution: persist the proxy's daily budget/counters to disk keyed by UTC date (survive
restart); make counters thread-safe; expose remaining budget on /health.

### F13 — MAJOR — Order-response and order-intent journaling vs "the journal reconstructs everything" — determinism gap on the order path
Plan text: "Journal before dispatch" (stated for inbound envelopes); "the order/batch response
is synchronous fill truth ... orphan detection happens in the same response"; "the journal IS
the state; replay reconstructs everything."

The synchronous order response is an *input* to the Reconciler/decide-adjacent logic, but the
plan only guarantees journaling for inbound WS envelopes. If order intents and responses are
not journaled-before-consumed, (a) golden replay cannot reproduce the imbalance-protocol branch
taken, and (b) a crash between POST and journal leaves a fill at the exchange with no journal
record → "replay reconstructs everything" is false (reconcile-first covers live safety but not
determinism).
Resolution: journal the order intent (with client_order_id) BEFORE POST and the full response
BEFORE acting on it; include order events in the golden-replay corpus.

### F14 — MAJOR — Recycling scope understated: ws.py is single-ticker and depends on a KalshiAuth that V3 does not have
Verified in `degeneracy_v2/kalshi/ws.py`: `self.ticker` (singular),
`market_tickers:[self.ticker]`, seq state per-sid, and `from kalshi.auth import KalshiAuth`
with `self.auth.ws_base_url` + `self.auth.request_headers`. Plan: "lifts nearly whole; needs
auth-via-proxy + multi-ticker subscribe."

"Nearly whole" understates: (a) multi-ticker means either two client instances (two sockets,
two auth fetches, two seq machines) or one socket with both tickers per channel — in which case
snapshot/delta callbacks currently carry no market disambiguation and BookMirror must key by
market_ticker (a signature change through the callback layer); (b) KalshiAuth does not exist —
a `ProxyAuth` shim must supply `ws_base_url` pointing at **Kalshi's WS host directly** (the
proxy is HTTP-only; verified — it only proxies `/trade-api/...` over HTTP) while
`request_headers` fetches from proxy `/ws-auth`; (c) the client re-dials on every seq gap, so
fresh signed headers must be re-minted per dial with adequate signature freshness. This is real
adaptation, not a lift.
Resolution: scope ws.py as "adapt," not "lift nearly whole"; specify the ProxyAuth split
(direct Kalshi WS host + proxy-minted headers per dial) and the market-keyed callback change.

### F15 — MAJOR — Phase 2 exit is rubber-stampable
Plan text: Phase 2 exit "every divergence explained and either fixed or **documented as a
known live/sim delta**."

"Documented as a known delta" lets any divergence pass by writing it down. No bound on count or
magnitude, and no requirement that a documented delta be fire/no-fire-neutral. A signal-engine
bug can be waved through as a "known delta."
Resolution: quantify — e.g., zero unexplained divergences; every documented delta must not flip
a fire/no-fire decision and must be bounded to ≤ X¢ on C, verified in tests.

---

## MINORs

### F16 — MINOR — Task Scheduler ":40 each hour" — UTC vs local, and DST
The hourly/15m markets settle on the UTC top-of-hour; Brad's box is ET. If the scheduler and
`decide()` gates key off local time, the DST transition drops/doubles a window and misaligns
with the venue's UTC schedule twice a year. Resolution: schedule and reason in UTC; state it.

### F17 — MINOR — Which 15m contract pairs against the hourly is never stated
Only the top-of-hour 15m window (:45→:00) co-settles with the hourly on the same BRTI print.
"Both legs" is left implicit; picking the wrong 15m leg = no co-settlement = the floor
arithmetic does not hold. Resolution: state the leg-pairing rule explicitly in WakeContext.

### F18 — MINOR — No pre-fire affordability/balance gate
WakeContext "reads balance" but the plan wires no check that funds cover both legs before
firing; a leg rejected for insufficient balance = orphan. Low blast radius (~$10/day) but a
real orphan source. Resolution: gate the fire on sufficient balance for both legs.

### F19 — MINOR — order_translate "lifts verbatim" while the order envelope around it is new
API facts require `self_trade_prevention_type` (new), fixed-point STRING count/price,
`client_order_id`, and the batch-create-orders-v2 shape — none of which V2's order_translate
built. The direction mapping is verbatim; the envelope construction is new. Resolution: scope
order_translate as "direction mapping verbatim; envelope construction new + tested."

### F20 — MINOR — Coarse-ladder strike co-settlement not verified (21:00 UTC $250 / Friday $500)
The ladder map handles step *size* (strangle stands down, sub-$1 continues) but not whether the
15m and hourly legs still land on the *same* strike granularity at $250/$500 steps such that the
flip legs remain co-settling. Resolution: verify strike alignment of the two legs at coarse
steps, not just the step-size branch.

### F21 — MINOR — Shakedown "extended until ≥1 would-fire" has no backstop and self-references the code under test
If the market never presents a would-fire print for many days, the shakedown never exits
(schedule stall / loosening pressure); and a `decide()` bug that never fires would extend the
shakedown forever rather than being caught. Resolution: add a max-days backstop that escalates
to Brad, and a synthetic-journal test proving `decide()` *can* fire.

---

## NOTES

### F22 — NOTE — Demo-env dress rehearsal assumes parity that may not hold
Demo BTC markets may lack the same 15m/hourly listings, ladder, or liquidity; "parity" is
asserted. Fine as a plumbing rehearsal, not as a fillability signal. Say so.

### F23 — NOTE — Service should refuse-to-arm on /health mismatch
The service can read proxy `/health` (`orders_enabled`, env, key fingerprint, remaining budget
per F12) at startup and refuse to run if env/orders state disagree with the pilot's expectation.
Cheap belt-and-suspenders on top of ALLOW_ORDERS.

### F24 — NOTE — "single-flight per (window, side)" vs batch-both-legs-in-one-POST
Terminology: the batch is per-window (both sides in one request), not per-side. Reconcile the
wording so the single-flight lock is defined at the right granularity (per-window fire +
per-side rebalance), which also feeds the F6 mutex.

---

## Verdict: REVISE

The spine is sound — pure `decide()` + I/O shells, journal-as-state, process-per-window,
caps in two places, actual-fee arithmetic stop, and a phased ladder with an independent
reimplementation gate. Phases 0–2 and the architecture can proceed largely as written.

But two design-level BLOCKERs sit in the parts that matter most, and both must be redesigned
in the plan (and pinned in the falsifier) before their phases build:

- **F1**: the imbalance protocol's retry-buy, as specified, can complete a pair above $1.00 and
  break the exact arithmetic floor the pilot rests on — the fix-fast path can manufacture the
  violation that halts the pilot.
- **F2/F3**: the headline paired-replay is contaminated — our own IOC prints re-enter the
  fetched tape the sim replays, and live-book C is being compared against print-based sim C —
  so the pilot's one question (does observed match simulated) is measured on a biased instrument,
  with the confound unnamed.

Add the material safety gaps (F4 settlement-during-crash, F5 sell-down non-termination, F6
order-authority race, F8 flatten-vs-hold, F9 clock discipline, F10 rate budget, F11 missing/
halted legs, F12 proxy cap reset) and the honest call is REVISE, not APPROVE-WITH-FIXES: the
imbalance protocol and the measurement design need a second pass on paper before code, even
though the data spine can start now.
