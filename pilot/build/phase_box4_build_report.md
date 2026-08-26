# Phase box-4 build report — FIX-THEN-SHIP fixes + box arming ceremony

Branch: `box/phase4-arm` (based on `box/phase2-wire` @ d95d3e7 + the box-falsifier DRAFT commit
511f704). Addresses the Opus-4.8 adversarial review of Phase 2
(`review_box2.md`, verdict FIX-THEN-SHIP) plus one late spec change from Brad (the strike tie-break).
House law observed: `python` only; no network in tests; no sealed reads (golden test asserts it); no
key/PEM/.env access; the corridor path is untouched when `strategy == corridor` and the whole prior
suite stays green.

Test suite: `cd pilot && python -m pytest -q` → **488 passed** (481 baseline + 7 new). No skips of
the box golden (historical-data present via the junction).

---

## Fixes

### F1 (MEDIUM, correctness) — settlement backfill can no longer book a foreign win
`run_window._parse_market_result` now requires the record's `ticker` to equal the requested ticker
**exactly, in BOTH payload shapes**. The `markets[0]` fallback is gone; the `{"market": {...}}`
branch no longer trusts an unlabelled market. A mismatch returns `None`, so the settlement backfill
waits for a later wake instead of attributing a foreign market's `result` to our leg (which fed
fictitious `count*$1` into `realized_delta` / `s4_running_total` / the promotion counters).
- Tests: `test_parse_market_result_requires_exact_ticker_F1` (the review's repro_d scenarios — foreign
  ticker in a list ⇒ None; foreign `{"market"}` ⇒ None; no-ticker record ⇒ None) + the happy path
  (exact match still returned even when a foreign market shares the list). Existing
  `test_parse_market_result` unchanged and green.

### F2 (MEDIUM, ceremony) — the box arms against ITS OWN falsifier
- New `DEFAULT_BOX_FALSIFIER_PATH = ceremony/box_falsifier.md`; `WindowService(box_falsifier_path=…)`;
  CLI `--box-falsifier`.
- `prepare()` selects the falsifier by strategy: `box` → `box_falsifier_path`, corridor →
  `falsifier_path`. For the box it calls `arming_check(..., strategy=strategy, expected_strategy="box")`.
- `stops.arming_check` gained an optional `expected_strategy`: when pinned, the RESOLVED strategy must
  equal it exactly or S5 refuses. So the box requires all of: STATUS line exactly `STATUS: FROZEN` in
  `box_falsifier.md`, box roster sha verified (carried in `policy_verified` — `load_box_policy`
  self-checks the pinned sha), `/health` caps + orders_enabled, resolved strategy exactly `box`, plus
  the existing day-guard/latch/A4/caps checks.
- The `arming` journal record now carries `falsifier_path`, `falsifier_basename`, and `strategy`.
- Tests: `test_box_arming_refuses_draft_box_falsifier` (box + DRAFT ⇒ dry), `test_box_arms_with_frozen_box_falsifier`
  (box + FROZEN temp copy ⇒ arm), `test_box_never_consults_corridor_falsifier` (corridor falsifier
  FROZEN but box falsifier DRAFT ⇒ box STILL refuses — proof it reads the box file, not `falsifier.md`),
  `test_arming_check_strategy_gate_and_falsifier_selection` (unit: strategy mismatch refuses; corridor
  path without `expected_strategy` ignores strategy). The whole armed-path wiring suite now passes a
  FROZEN box falsifier through the `_box_svc` helper (the corridor path is unaffected — its tests still
  arm against `falsifier.md`).

### F3 (LOW) — A5 counts the one-legged ENTRY event, flatten outcome recorded separately
`_box_one_legged` is now set once at entry (`exactly one entry leg filled`) and is **never reset by a
later flatten**; the flatten outcome lives in a new `_box_flatten_filled` (None = no flatten;
True = flattened flat; False = held naked). The box ledger row carries both `box_one_legged`
(what A5 counts, unchanged rolling-20-of-fires semantics) and `box_flatten_filled` (the risk/held-naked
signal). A day where every entry fills one leg but all flattens succeed now correctly shows the A5 rate
rising, instead of masking the single-leg-fill degradation.
- Tests: `test_box_one_leg_flatten_fills` (one-legged + flattened ⇒ `box_one_legged True`,
  `box_flatten_filled True`, ledger row carries both), the miss/no-bid/cutoff tests assert
  `box_flatten_filled False`. The existing `test_box_one_legged_rate_counter` (threshold 0.10 alarm)
  is unchanged and green.

### F4 (LOW) — flatten retries are EVENT-DRIVEN, 3 attempts max
The synchronous `range(1,5)` retry loop (which priced 4 attempts against the SAME frozen book under
the blocking reader) is replaced by an event-driven state machine:
- Attempt 1 fires synchronously at post-entry (the fresh entry-time bid). On a miss, `_pending_flatten`
  is recorded on the runner.
- `BoxWindowRecorder` gained an `on_book_event(market, server_ts)` callback fired after each
  server-timestamped subscribed frame; the runner's `_box_on_book_event` issues the next attempt when
  the frame is a **later** event (`server_ts > last_attempt_ts`) AND either (a) it is the held ticker
  with its best bid present, or (b) ≥ **250 ms** of ENGINE time (event `server_ts`, never wall clock)
  has passed since the last attempt — whichever comes first. The "later event" guard is what stops a
  retry from re-pricing the exact frame the previous attempt already saw (the review's F4 bug).
  **Choice documented:** the 250 ms fallback ensures a held ticker that goes quiet still gets its
  remaining attempts; using the held ticker's own fresh frame first means the common case retries
  immediately at a genuinely new bid.
- **3 attempts total** (aligns with `box_falsifier.md` "up to 3 attempts"). After the 3rd miss:
  `A_FLATTEN_EXHAUSTED` alarm + hold naked + journal. A `None` bid at an attempt is still the
  `A_FLATTEN_NO_BID` hold (falsifier text preserved). The no-orders cutoff (`t_minus <
  no_orders_after_s_to_settle`, ~1 s) short-circuits any remaining attempts to a `cutoff_hold`.
- **Max orders per box window is now 5** (2 batched entry legs + 3 flatten singles), verified
  empirically: worst case one-legged + never-flatten emits 5 order entries; **17 windows/day × 5 = 85
  < proxy daily budget 100** — the review's pathological 102 > 100 starvation is gone.
- Tests: `test_box_one_leg_flatten_misses_then_retries_then_holds` (3 attempts, bids
  `[0.93, 0.90, 0.88]` proving fresh bids per attempt, `A_FLATTEN_EXHAUSTED`, a 4th frame is a no-op),
  `test_box_flatten_retry_respects_no_orders_cutoff` (a frame at t−0.5 s ⇒ `cutoff_hold`, no order),
  `test_box_one_leg_flatten_no_bid_holds_and_alarms` (no bid ⇒ `A_FLATTEN_NO_BID`, held).

### F5 (LOW) — select_box runs once per relevant book frame (was ~2×/tick)
`decide_box` computes the selection view once, at the book fold, and caches it on
`BoxState.view` (a `field(compare=False, repr=False)` so it never perturbs golden-state equality). Step
5 of the decision reuses the cached view; the driver's `box_eval` (`_current_view`) reads the same
cache instead of re-running `select_box`. A ClockTick or an unsubscribed-market update does not change
tops, so the cached view carries forward correctly — `select_box` now runs **at most once per relevant
book frame**, never twice, and not at all on clock ticks / off-ladder frames. The pure core stays pure
(no I/O; deterministic). The tops dict-copy is left as-is (it is the correct immutable pattern).

Micro-benchmark (30-candidate ladder, all qualifying, in the entry window on a NoBox-after-scan path
so `select_box` does full work; single run of 200k ticks each, this box):

| case | µs/tick |
|---|---|
| `select_box` (30 cands) alone | 24.4 |
| tops dict-copy (31 entries) | **0.26** (negligible — review's guess confirmed, left in place) |
| BEFORE F5 (2× select_box/tick) | 64.6 |
| AFTER F5 (1× select_box/tick) | 36.5 |

Net ≈ **28 µs/tick saved** (~43% of the driver's per-tick cost), exactly one eliminated `select_box`.
30 all-qualifying candidates is a worst case; real books rarely have that many deep-ITM qualifiers on
the correct side of A within spread, so the live saving is smaller in absolute terms but the redundant
compute is gone.

---

## Spec change (Brad 2026-08-26) — strike tie-break: nearest 0.95, tie → WIDEST gap from A
`box.select_box`'s tie-break key changed from `(|mid − target|, |K − A|)` (nearer A) to
`(|mid − target|, −|K − A|)` (widest gap from A — the wider box gives the larger pin region).
Updated: the `box.py` module docstring + the `select_box` inline comment; both oracles in
`test_box_golden.py` (`_oracle_decimal`, `_oracle_float`) so golden parity stays **exact**; the
tie-break unit tests in `test_box.py` (`test_tie_break_prefers_widest_gap_from_anchor` — expectation
flipped to the far strike; new `test_tie_break_only_applies_among_equal_mid_candidates` — a strictly
nearer-mid candidate still wins, and among a tie the widest-gap one is chosen); and the "Which strike"
wording in `box_falsifier.md` (text below the still-DRAFT STATUS line).
- **No roster JSON change** — the tie rule is code, not a parameter; the canonical sha is unchanged
  (`480d4634…`, re-verified against `box_params.json`). No re-pin.
- **Impact measured** over the 1,289 golden hours: fires 1024 (nearer-A) → **1022** (widest-gap);
  **42 hours (~3.3%) changed selection** (some only in the first-qualifying minute, two stopped
  firing). Ties are uncommon but not negligible.

---

## Docs
- New `pilot/ops/BOX_ARMING.md` — the mechanical arm/stand-down runbook (tree+clean, tests, /health,
  strategy.txt=box, two shakedown windows with `box_would_fire`/`box_eval` and no
  `strategy_invalid`/`wake_error`, Brad's freeze line + roster sha, mode.txt=armed; stand-down =
  mode.txt back to shakedown; what a latched stop looks like in `ops/stops_YYYY-MM-DD.json` and how
  Brad clears a corrupt guard by hand).
- `pilot/ceremony/box_falsifier.md` — only the "Which strike" wording changed (STATUS stays DRAFT;
  Brad freezes it). The freeze line, verbatim go, and verdicts still register there and nowhere else.

## New / changed journal + record surface
- `arming` record gains `falsifier_path`, `falsifier_basename`, `strategy`.
- `box_flatten` stages: `start`, `flat`, `miss_retry`, `giveup_hold` (now with `A_FLATTEN_EXHAUSTED`),
  `no_bid_hold`, and new `cutoff_hold`.
- Box ledger row gains `box_flatten_filled` alongside the (now entry-latched) `box_one_legged`.
- New alarm kind `A_FLATTEN_EXHAUSTED`.

---

## CONFESSIONS
1. **`BoxState.view` cache field.** F5 adds a computed field to the "pure" frozen state. It is
   `compare=False` (golden-state equality and the determinism test are unaffected — verified: the
   golden suite passes) and `repr=False`. decide_box now computes the view on every *relevant book
   fold* including warmup/after-entry-adjacent frames where the old code returned earlier; this is
   net-neutral-or-better because the driver already computed the view every event via `_current_view`.
   The view is deterministic, so two replays produce identical (cached) views.
2. **F4 no-bid handling is unchanged behavior, deliberately.** The review's F4 was about the frozen-bid
   retry loop, not the no-bid case. A `None` bid at an attempt still raises `A_FLATTEN_NO_BID` and
   holds immediately (faithful to `box_falsifier.md`'s A_FLATTEN_NO_BID = "no bid to flatten into —
   held to settlement"), rather than waiting for a later bid. Documented as a choice, not a bug.
3. **The tie-break impact count uses the golden corpus, which reads `historical-data/`.** The count
   (42/1289) came from a scratch script over the same non-sealed hours the golden test uses; the
   golden loader asserts no sealed-day file was opened. The junction to real `historical-data/` was
   created only for the test run and removed before finishing.
4. **`phase_box2_build_report.md` sha is NOT stale.** Its pin `480d4634…` is still the current
   canonical box roster sha (the tie-break change is code-only). I re-verified and left that report
   unchanged; there was no stale sha to correct.
5. **Benchmark is a single 200k-tick run on this box, not a statistical average.** It isolates the one
   removed `select_box` call (BEFORE = driver monkeypatched back to recompute `_current_view`); the
   1M-tick target was reduced to 200k because 40-candidate Decimal `select_box` at 1M×8 runs exceeded
   the 2-minute tool budget. The per-tick figure is stable enough to show the ~43% cut; treat the
   absolute µs as indicative.
6. **`arming_check` signature is now keyword-extended, not broken.** The two new params
   (`strategy`, `expected_strategy`) are keyword-only with defaults, so the corridor's positional
   `arming_check(falsifier, health, verified)` call is unchanged and the corridor never passes an
   expected strategy.
