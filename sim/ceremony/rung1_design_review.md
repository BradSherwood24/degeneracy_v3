I've completed the paper attack. Everything reconciles on real train data (fee goldens reproduce exactly, anchor identity 96/96, pin truth-table 25/25 with 0 impossibles, no lookahead in the entry indexing) — but I found **three build-blockers and several ambiguities that would silently corrupt the census or abort the single frozen run.** Full review below.

---

# DESIGN REVIEW — Rung 1 Commission (rung1_commission.md)

**Reviewer:** opus48, independent. **Verdict: CONDITIONAL PASS — do not build until F1–F3 are amended in the commission.** The spec's core mechanics are sound and I verified them against `2026-07-15` (train). But three issues are load-bearing, and one of them can abort the one-shot sealed run.

## What I verified GREEN (hand-checked on train data)

| Claim | Result |
|---|---|
| Fee `ceil(0.07·p(1−p)·10000)/10000` | All four goldens reproduce exactly (0.57→0.0172, 0.46→0.0174, 0.24→0.0128, 0.11→0.0069) |
| Anchor A = 15M `floor_strike` = prior window's settle print | 96/96 exact, continuous 15-min tape (all gaps = 900s) |
| Pin truth table `H:no ∧ L:yes` | 25/25 hours classified correctly; 7 pins, 18 escapes, **0 impossibles** |
| Both orientations exist (A>K and K>A) | Both present same day; classification holds in both |
| Entry indexing `end_period_ts == T−300s` | Indexes the minute **ending** at T−5:00 → known at decision time → **no lookahead** |
| σ̂ / nearest-K inputs | Anchor known at T−15, strike ladder static → **no lookahead** |
| `expiration_value` = exact print, identical across both legs | Confirmed — a stronger cross-check than the `result` field alone |
| Example C at T−5 (08:00 hr) | C_base=0.7239, C_worst=0.8564 — matches disclosed 68–73¢ range |

## BLOCKERS — amend before build

**F1 — σ̂ "same calendar tape" silently deletes early-UTC hours (systematic, non-random).**
The anchor tape is continuous every 15 min, but σ̂(T) needs 9 trailing anchors (8 diffs = 2h15m back). If the builder reads "excluded... on the same calendar tape" as *same day-file* (the natural per-file implementation), then **T=01:00 UTC (and the first ~2h of the earliest train day) is dropped every day** for a data-plumbing reason, not a market reason. That is a recurring hole at one wall-clock slot (Asia-session regime) that biases the pin-rate census. Also an internal off-by-one: §6 says "trailing **8** anchor-to-anchor differences" and "a 2-hour lookback," but 8 diffs need 9 anchors spanning 2h15m, and then says "Hours with <8 trailing anchors" (should be <9 anchors / <8 diffs).
**Fix:** state that σ̂ is computed over a **continuous anchor tape stitched across adjacent TRAIN days** (backward-only — no seal leak, since σ̂ never reads forward), pin the exact count (9 anchors → 8 diffs), and reserve exclusion only for the genuine head-of-corpus (2026-06-11 first ~2h).

**F2 — G=0 (anchor exactly on a threshold) is unhandled and can ABORT the frozen run.**
KXBTCD strikes are `x99.99`; the anchor is a 2-decimal print. `A == strike` (G=0) has ~1/10⁴ odds per hour → **~12% chance across the ~1,250 train hours that at least one occurs.** When it does, H=L (no corridor), and a print landing exactly on the line makes the 15M(`>=`) leg pay AND the 1H(`>`) leg pay — read from `result` fields as `H:yes ∧ L:no`, which §4 routes to **"impossible → hard fail."** A degenerate-but-legal pair would then hard-abort the single sealed read. (0 occurrences on 07-15, but the tail is real.)
**Fix:** add an explicit pre-run rule — **exclude pairs with G below a small epsilon / anchor-on-strike, with a receipt** — decided now, before any results are seen.

**F3 — The `>=` vs `>` boundary asymmetry is real and unstated; §4's wording is wrong for the 15M leg.**
Confirmed from data: 15M `strike_type = greater_or_equal`, 1H `strike_type = greater`. §4 says `result=="yes"` means "settled **above** that market's line" — false for 15M, which is *at-or-above*. Consequence: the pin corridor is **open at both ends** when A>K but **closed at both ends** when K>A. Classifying from the two `result` fields is SAFE (I verified 0 mismatches vs a per-market recompute across all 25 hours), but any print-based truth-check the builder writes MUST use per-market `strike_type`, never a uniform inequality.
**Fix:** correct §4's wording; mandate that PIN/ESCAPE come solely from `result` fields (verified against `expiration_value`); and require the truth-table unit test to include an **exact-boundary print in BOTH orientations** (not just the general orientation cases §4 already asks for).

## MEDIUM

**F4 — G/σ̂ has no σ̂-floor / divide-by-zero guard.** A flat trailing window (or a data gap making 8 diffs near-identical) sends G/σ̂ → ∞ and dumps the hour into the top quintile (or NaNs the bucketing). Specify a σ̂ floor or an explicit σ̂≈0 exclusion.

**F5 — The gate's train CI is selection-biased and should be labeled descriptive.** §8 picks "the largest low-end contiguous bucket set whose pooled day-clustered LB>0" — selecting the boundary on the *same* statistic used for the CI makes "LB>0" near-tautological at g*. The honest inference is the sealed falsifier, not this interval. The ceremony implies this; the spec should say it outright so the train CI is never read as out-of-sample evidence.

**F6 — "quoted / ask<$1" column is ambiguous for NO-PAIR.** WORST NO-ask = `1 − yes_bid.low` can hit exactly $1.00 within the T−5 minute (I saw `yes_bid.low=0` in a real candle). State that the **quoted flag / NO-PAIR test is evaluated on the BASE close**; a WORST leg exceeding $1 is a fidelity-limited column value, not a dropped pair.

## LOW / verification nits for the builder

- **F7** — There is no numeric `close_ts`; the field is `close_time` (ISO string). The day rule must parse→epoch→subtract 1s. Recommend `expiration_value` as the authoritative print for the result-integrity cross-check (identical across both legs and both series — verified).
- **F8** — Dedupe-by-ticker is correct and necessary: the `2026-07-15` file carries both `2026-07-15T00:00Z` (a 07-14 market by the −1s rule) and `2026-07-16T00:00Z` (a 07-15 market). No intra-file dup tickers, but cross-file dups are real. Add: assert the two copies are byte-identical, fail-closed otherwise.
- **F9** — Cosmetic: fix the "2-hour lookback / 8 anchors" arithmetic to match F1's pinned count.

---

The blockers are all cheap to fix and none require re-tuning any economic constant — they're specification tightenings that must land **before** the build so they're frozen, not chosen after seeing results. Want me to draft the amendment text for F1–F3 (and F4/F6) as a written addendum to `rung1_commission.md` per the §Ceremony "written amendment before the run" rule?
