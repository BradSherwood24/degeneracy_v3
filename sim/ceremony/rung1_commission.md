# COMMISSION — Rung 1: the pin-rate & pair census + corridor sim (frozen spec)

Commissioned 2026-08-19 by the bridge (Fable), under Brad's directive of 2026-08-18:
"run the same build process to get this sim in place... 3 training, 1 locked read only...
keep going until you're ready to unseal." Ceremony: this commission → independent design
review (opus48) → build (opus48) → independent adversarial review (opus48, separate
sessions) → one run on TRAIN only by the bridge. Nothing in this spec may be retuned after
results are seen; deviations require a written amendment in this file BEFORE the run.

## 1. Objective

Over the TRAIN half of the corpus, measure per hour: the corridor ("hole") between the
15-minute anchor and the nearest hourly threshold, the executable cost C of buying both
outsides, the realized-vol context, and the exact pinned/escaped outcome. Fit THE GATE —
the region of (hole/σ, C) where the strangle is +EV with day-clustered margin. Produce
frozen artifacts for a later one-shot sealed read (which this commission does NOT run).

## 2. Data (already on disk; read-only)

`historical-data/{15-minute,1-hour}/{markets,candles}/YYYY-MM-DD.jsonl` — raw Kalshi API
objects, one UTC day per file. Facts the builder must honor:

- **Day assignment**: a market belongs to the UTC day containing `close_ts − 1s` (a market
  closing exactly 00:00:00 UTC belongs to the PRIOR day). Files may contain boundary
  duplicates — **dedupe by ticker** after loading.
- **SEAL (absolute)**: days 2026-08-02 through 2026-08-18 are SEALED. TRAIN = 2026-06-11
  through 2026-08-01 (52 days). The loader takes `acknowledge_sealed_read: bool = False`
  and raises on any sealed-day access when False. Only `sim/unseal_runner.py` (written but
  never executed in this campaign) passes True. Tests must prove the refusal fires.
- Known data quirks: 2026-08-06 and 2026-08-13 are short days — 88 of ~96 15M markets,
  3,840 of ~4,592 hourly listings (sealed side; observed in listing counts only, amendment
  A1 2026-08-19 during SEAL manifest build);
  hourly candles exist only for strikes within ±$400 of the same-close 15M anchor;
  hourly candles are clamped to the final 3600s. Missing/short days are excluded WITH a
  per-exclusion receipt (inventory-honored, fail-closed on anything unexplained).

## 3. Pair construction (per top-of-hour close time T)

- 15M leg: the KXBTC15M market with `close_time == T`. Its `floor_strike` = the anchor A.
- Hourly leg: among KXBTCD markets with `close_time == T`, the one with `floor_strike`
  NEAREST to A (ties impossible in practice; if one occurs, exclude with receipt). Its
  strike = threshold K. **Hole G = |K − A|** (bounded ~$50 by the $100 ladder spacing).
- Lines: H = max(A, K), L = min(A, K). The strangle: **buy YES on the H-line market, buy
  NO on the L-line market** (both taker IOC, 1 lot).

## 4. Outcomes (exact — candle-independent)

Both markets settle on the same print (60s BRTI average at T; identity verified in both
series' API metadata). `result == "yes"` means the print settled above that market's line.

- **PIN** ⟺ H-market result says below H AND L-market result says above L
  (print landed inside the corridor; BOTH legs lose).
- **ESCAPE** ⟺ exactly one leg pays $1.
- The impossible combination (above H AND below L) is a data error → hard fail, not a skip.
- Truth-table unit test required, including the 2026-06/07 boundary orientation cases
  (A above K and K above A).

## 5. Economics (candle-bounded, two columns everywhere)

- Prices from 1-min candles. YES-leg ask = the H-market's `yes_ask`; NO-leg ask on the
  L-market = **1 − yes_bid** (complement identity, verified exact in V2).
- **BASE column**: minute `close` values. **WORST column**: `yes_ask.high` and
  `1 − yes_bid.low`. Every table carries both; any conclusion that holds only in BASE
  must be labeled candle-fidelity-limited.
- **Taker fee per leg per contract: `ceil(0.07·p·(1−p)·10000)/10000` dollars** — audited
  against our own four 2026-08-18 fills (0.57→0.0172, 0.46→0.0174, 0.24→0.0128,
  0.11→0.0069). These four are MANDATORY golden test literals. Settlement pays no fee.
- C = legA + fee(legA) + legB + fee(legB). Payoffs: escape → +(1 − C); pin → −C.
- **Entry snapshot (frozen, one degree of freedom)**: the primary read prices entry at the
  minute ending **T−5min** (the candle whose end_period_ts == T−300s), requiring both legs
  quoted (ask present, ask < $1). Snapshots at T−13, T−10, T−2 are computed as DESCRIPTIVE
  columns only — they are not policy candidates and must not select the gate. Hours where
  the T−5 snapshot is unquoted are recorded as NO-PAIR (with counts), not dropped silently.

## 6. Vol context (pre-declared proxy; no tuning)

σ̂(T) = the standard deviation of the trailing **8** anchor-to-anchor differences of the
15M anchor series (the anchors are spot samples every 15 min → a 2-hour lookback), in
dollars. Hours with <8 trailing anchors available on the same calendar tape: excluded with
receipt. The gate variable is **G/σ̂**. Raw-G tables are produced as descriptive context.
The proxy's limits (15-min sampling, no intraminute vol) are documented, not patched.

## 7. The census output

One row per hour: date, T, anchor, threshold, G, side-orientation, σ̂, G/σ̂, C_base, C_worst,
per-snapshot C's, quoted-flags, pin/escape, per-leg results, payoff_base, payoff_worst.
Written to `sim/out/census_train.csv` (+ a JSON receipt: row counts, exclusion inventory,
sha256 of inputs consumed). Loader, census, and gate-fit are separate modules under `sim/`
with unit tests (pin truth table, fee goldens, complement identity, seal refusal, dedupe).

## 8. Gate fit (train only)

- Bucket hours by G/σ̂ **quintiles** (5 buckets, boundaries from train). Per bucket: n,
  pin rate, mean C (both columns), EV/pair = (1−pin)(1−C) − pin·C, and a **day-clustered
  bootstrap 95% CI** on EV (resample days, 10,000 draws, fixed seed 26).
- **The gate**: the largest contiguous set of buckets from the low-G/σ̂ end whose POOLED
  day-clustered EV lower bound > 0 in the BASE column, reported alongside the same
  statistic in WORST. If no bucket clears: the gate is empty, and that is the result —
  reported plainly, no widening, no re-bucketing, no alternate proxies. Emit
  `sim/out/gate.json`: bucket boundaries, the g* cutoff, C-cap observed, fit receipts.
- Anti-overfit guard: exactly ONE bucketing (quintiles), ONE proxy (σ̂ as §6), ONE primary
  snapshot (T−5). No sweeps. Descriptive columns are never promoted mid-campaign.

## 9. What this commission does NOT authorize

No sealed-day access (beyond the loader's tested refusal). No unseal run. No live orders.
No retunes after the train read. The unseal falsifier is drafted by the bridge AFTER the
train fit, frozen in `sim/ceremony/falsifier.md`, and executed only on Brad's explicit
go after his morning review.

## 10. AMENDMENT A2 (2026-08-19, adopting design review F1–F9 — frozen BEFORE build)

Per `rung1_design_review.md` (opus48, CONDITIONAL PASS). These supersede conflicting text above.

- **A2.1 (F1/F9, σ̂ tape):** σ̂(T) uses the trailing **9 anchors → 8 differences** (2h15m span)
  on a continuous anchor tape **stitched backward across adjacent TRAIN days** (backward-only;
  never reads forward, never crosses into sealed days when evaluating sealed hours later —
  sealed-day σ̂ uses that day's own trailing tape). Exclusion only at the genuine
  head-of-corpus (first 2h15m of 2026-06-11), with receipt. "Same calendar tape" language
  is void.
- **A2.2 (F2, degenerate corridor):** pairs with **G < $0.01** (anchor within a cent of the
  threshold, including exact equality) are EXCLUDED with a per-pair receipt. The §4
  "impossible → hard fail" rule applies only to pairs with G ≥ $0.01.
- **A2.3 (F3, boundary semantics):** corrected: 15M `strike_type=greater_or_equal`
  (`result=="yes"` ⟺ print ≥ anchor); 1H `strike_type=greater` (`yes` ⟺ print > threshold).
  PIN/ESCAPE is computed **solely from the two `result` fields**. Integrity cross-check:
  `expiration_value` must be identical across both legs, and results recomputed from it
  per each market's `strike_type` must match the `result` fields with ZERO tolerance (any
  mismatch = hard fail). The truth-table unit test must include an exact-boundary print in
  BOTH orientations (A>K and K>A).
- **A2.4 (F4, σ̂ guard):** no floor is applied — a tiny σ̂ inflating G/σ̂ into the top
  (most dangerous) bucket is semantically correct. Only σ̂ == 0 exactly is excluded, with
  receipt. Any NaN anywhere is a hard fail, never a silent drop.
- **A2.5 (F6, quoting rule):** the quoted/NO-PAIR test is evaluated on **BASE close** values
  only. WORST-column values (including a NO-leg ask reaching $1.00 via `yes_bid.low=0`) are
  fidelity-bounded descriptive values and never determine pair inclusion.
- **A2.6 (F5, honest labeling):** the train gate's day-clustered CI is **selection-biased at
  the chosen boundary** and is labeled DESCRIPTIVE in all artifacts. The only inferential
  read of the gate is the sealed falsifier. `gate.json` and the report must carry this
  sentence verbatim.
- **A2.7 (F7/F8, plumbing):** day assignment parses `close_time` (ISO string) → epoch −1s →
  UTC date. Cross-file duplicate tickers must be byte-identical, else hard fail.

## 11. AMENDMENT A3 (2026-08-19, adjudicating adversarial reviews A & B — frozen BEFORE the run)

Per `rung1_adversarial_review_A.md` (FAIL: A1 critical) and `rung1_adversarial_review_B.md`
(CONDITIONAL PASS: B1–B3 blockers). The bridge adopts:

- **A3.1 (A1, strike-less products):** 15M records lacking `floor_strike` (the "up/down,
  Target price: TBD" product) are SKIPPED in the anchor-tape build and, when they occupy a
  grid hour with no strike-bearing 15M market, the hour is excluded as **`EXCL_NO_ANCHOR`**
  with receipt. Known train instances (06-15 23:00, 06-23 04:00, plus three non-top-of-hour)
  are expected receipts. The same handling applies on the sealed side at unseal.
- **A3.2 (B3/C1, σ̂ window RATIFIED):** σ̂(T) uses the 9 anchors of the 15M markets closing
  at T, T−15m, …, T−120m — **including A(T)** (its value is fixed at the window's open,
  T−15, hence known at decision time; verified 277/277 by Reviewer A). The "2h15m" phrase
  in A2.1 refers to the span of underlying observations (T−15 back to T−2h15m). This is now
  pinned by commission, not builder choice.
- **A3.3 (B2/A3-review, WORST never gates inclusion):** pair inclusion is decided ONLY on
  BASE-close quoting and C_base presence. A missing WORST field leaves the row OK with
  C_worst blank (fidelity-limited), never NO_PAIR.
- **A3.4 (B4/C6, σ̂ exclusions ratified + split):** insufficient trailing tape excludes with
  receipt wherever it occurs, under split codes **`EXCL_SIGMA_HEAD`** (2026-06-11 head only)
  and **`EXCL_SIGMA_GAP`** (mid-tape gaps, e.g. 06-25 and 07-16).
- **A3.5 (A2/B11, expiration_value robustness):** an EMPTY `expiration_value` on either leg
  excludes the hour as **`EXCL_EV_MISSING`** with receipt (fail-closed, not a crash). A
  present-but-UNEQUAL print across legs (compared as Decimal, not string) remains a hard
  fail. The `result`-field recompute check applies only when the print is present.
- **A3.6 (B1/A5, freeze mechanism):** `falsifier.md` freeze state is keyed on a dedicated
  structured line — exactly `STATUS: FROZEN` (or `STATUS: DRAFT — NOT FROZEN`) — parsed as
  a line, not a whole-file substring. A test must freeze a COPY of the real repo file and
  assert preflight passes.
- **A3.7 (A4, seal defense-in-depth):** when `acknowledge_sealed_read=True`, the LOADER
  itself asserts the falsifier is FROZEN (per A3.6) before opening any sealed file.
- **A3.8 (B5, degenerate quintiles):** gate_fit asserts strictly increasing quintile edges;
  any empty bucket or tied edge is a hard fail with a degeneracy receipt, never a silent
  play-everything gate.
- **A3.9 (B7, artifact binding):** unseal preflight hashes `census_train.csv` and requires
  equality with `gate.json["census_csv_sha256"]`.
- **A3.10 (B8, aggregates only):** the unseal result reduces sealed exclusions to
  per-reason COUNTS (no per-hour sealed rows or timestamps in any artifact).
- **A3.11 (B6, surfaced honestly):** `gate_report.md` must flag explicitly when the top
  gated bucket's individual day-clustered LB ≤ 0 while the pooled prefix clears (dilution
  admission). The gate rule itself is unchanged.

## 12. Registered disclosures (pre-run)

1. The bridge (Fable) has already seen, inside the sealed window: the 2026-08-17 14:00 UTC
   pin ($6.48 corridor) and the two 2026-08-18 live probes (~$30 escape, ~$50 pin). The
   sealed read is aggregate-level; these three known hours cannot be excluded and are
   hereby registered as peeked.
2. n=2 live fills informed the C range quoted in day_zero (68–73¢). The census measures C
   independently; no constant in this spec is derived from those fills except the audited
   fee formula (which is exchange-mechanical).
3. Candle fidelity: 1-minute OHLC without depth. Pin/escape and G are exact; C and
   fill-ability are bounded, not known. This limitation is carried into every artifact.
