# ADVERSARIAL REVIEW A ΓÇö Rung 1 build (correctness & causality)

**Reviewer:** Opus 4.8, independent, fresh session, no authorship stake. **Mandate:** refute the build.
**Verdict: FAIL ΓÇö DO NOT RUN. One CRITICAL defect: the frozen TRAIN census aborts with an uncaught `KeyError` and produces no output.** I reproduced it on three separate TRAIN days. Separately, my full independent replication of 72 hours across 3 untouched TRAIN days reconciled to the census **to the character in all 15 shared columns (zero diffs)** ΓÇö the arithmetic the builder claims is correct *for the hours that survive*. The problem is a whole class of real market records the pipeline never anticipated, which no confession covers.

---

## 1. Independent replication (the core deliverable)

I wrote a throwaway recompute (`_reviewA_tmp/recompute.py`) that reads the raw JSONL directly and **imports nothing from `sim/`** ΓÇö it re-implements day-assignment, dedupe, pairing, nearest-K, A2.3 outcomes+integrity, ┬º5 economics (BASE/WORST, fees, complement), the A2.1/C1 ╧â╠é window, quoting, and payoffs from scratch.

- **Days chosen (untouched ΓÇö not 07-14..18):** **2026-06-25, 2026-06-26, 2026-06-27** (3 consecutive, so cross-day ╧â╠é stitching is genuinely exercised). **72 grid hours**, well over the 15-hour / 3-day floor.
- I then ran `sim/census.py --dates 2026-06-25 2026-06-26 2026-06-27` and diffed every row ├ù every shared column.

**Result: `total field diffs: 0 over 72 hours ├ù 15 cols`.** Status counts identical: `OK=65, EXCL_SIGMA_TAPE=3, EXCL_NO_1H_LEG=1, EXCL_NO_15M_LEG=3`; OK = 7 pins / 58 escapes on both sides.

Sample hours (my recompute == census, exact):

| date | T (UTC) | A | K | G | ╧â╠é | G/╧â╠é | orient | out | C_base | C_worst | payoff_base |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 06-26 | 12:00 | 59342.78 | 59299.99 | 42.79 | 156.853933 | 0.272802 | A>K | PIN | 0.8906 | 1.0085 | ΓêÆ0.8906 |
| 06-26 | 03:00 | 58740.32 | 58699.99 | 40.33 | 249.803162 | 0.161447 | A>K | ESCAPE | 1.0074 | 1.0295 | ΓêÆ0.0074 |
| 06-25 | 14:00 | 60091.51 | 60099.99 | 8.48 | 392.481630 | 0.021606 | K>A | ESCAPE | 1.0205 | 1.0205 | ΓêÆ0.0205 |
| 06-26 | **00:00 (of 06-27)** | 59997.63 | 59999.99 | 2.36 | 69.364915 | 0.034023 | K>A | ESCAPE | 0.9944 | 1.4585 | +0.0056 |
| 06-27 | 20:00 | 60108.30 | 60099.99 | 8.31 | 107.969076 | 0.076966 | A>K | ESCAPE | 0.8930 | 1.2525 | +0.1070 |

The 4th row (census `date=2026-06-26`, `close_time=2026-06-27T00:00:00Z`) independently confirms the A2.7 boundary rule: the D+1 00:00 close is filed under day D. (Note in passing: many real hours carry **C_base > \$1** ΓÇö e.g. 1.0074, 1.0205 ΓÇö a guaranteed-loss cost; recorded faithfully, correct, but a sobering context for the gate.)

---

## 2. THE FINDING ΓÇö CRITICAL

### A1 ΓÇö The frozen TRAIN run aborts with an uncaught `KeyError: 'floor_strike'`. No output is produced. (UNCONFESSED)

`census.py:229`, inside the anchor-tape build that runs **before** the grid loop:

```python
for r in m15:
    ep = close_epoch(r["close_time"])
    a = r["floor_strike"]                       # ΓåÉ line 229: bare subscript, no guard
    if a is None or (isinstance(a, float) and math.isnan(a)):
        raise IntegrityError(...)               # guard only catches None/NaN, never a MISSING key
    anchor_by_epoch[ep] = float(a)
```

The corpus mixes a **structurally different product** into the 15-minute markets files: `"BTC price up in next 15 mins?"` markets (`yes_sub_title: "Target price: TBD"`) that have **no `floor_strike`, no `strike_type`, no `cap_strike`** ΓÇö the key is entirely absent, status `finalized`. The tape loop iterates *every* 15M record, so `r["floor_strike"]` raises `KeyError` the moment one is loaded. The guard on the next line is dead ΓÇö it only fires for a present-but-`None`/`NaN` value.

**Reproduced (receipts):**

```
$ python census.py --dates 2026-06-15
  ...
  File "census.py", line 229, in build_census
    a = r["floor_strike"]
KeyError: 'floor_strike'
```

Identical crash on **2026-06-23** and **2026-07-22**. All three are TRAIN days; the bridge's one frozen run loads **2026-06-11..2026-08-01**, so it hits `2026-06-15` during tape construction and aborts before a single row, receipt, or sha is written. The build report's "BUILD-COMPLETEΓÇª reconciles" rests entirely on a 5-day smoke window (07-14..18) and one hand-checked hour (07-15 08:00) ΓÇö none of which touch these days. **This is precisely the "plausible failure that reaches the frozen run" the ceremony exists to stop, in its most total form: not a wrong number, a non-starting run.**

I enumerated the whole TRAIN exposure (read-only scan, `_reviewA_tmp/floor_scan.py`, no census/EV computed):

```
15M markets missing floor_strike (TRAIN):
  2026-06-15 23:00:00Z  KXBTC15M-26JUN151900-00  (top-of-hour, strike_type absent)
  2026-06-23 04:00:00Z  KXBTC15M-26JUN230000-00  (top-of-hour)
  2026-06-23 03:45:00Z  KXBTC15M-26JUN222345-45
  2026-07-22 18:45:00Z  KXBTC15M-26JUL221445-45
  2026-07-22 18:30:00Z  KXBTC15M-26JUL221430-30
```

Two are **top-of-hour**, i.e. they occupy a census grid slot with *no accompanying threshold market* (verified: at `2026-06-15T23:00:00Z` the "up/down" market is the **only** 15M record ΓÇö a genuine missing-anchor hour). So even after the tape is guarded, the pairing path (`census.py:278 A = float(m["floor_strike"])`) hits the same wall and must route these to an exclusion-with-receipt (e.g. `EXCL_NO_ANCHOR`), not a crash ΓÇö exactly the fail-closed-with-receipt discipline ┬º2 mandates for unexplained/short data. The builder handled "no 15M market at T" (`EXCL_NO_15M_LEG`, C7) but never the third state: **a 15M market that exists but carries no strike.**

**Severity:** blocks the entire train read. **Fix before any run.** And note the sealed side is a standing venue product too ΓÇö the short sealed days (08-06/08-13 missing listings) raise, not lower, the odds the same class appears there; a train-only patch that isn't strike-aware will re-fail on unseal.

---

## 3. Secondary findings

### A2 ΓÇö Empty `expiration_value` on a settled top-of-hour hourly market routes to a hard-fail, not a receipt (UNCONFESSED; latent, sealed-run risk)

`census.py:339` `if ev_h != ev_l: raise IntegrityError(...)` ΓÇö and `recompute_result` would `Decimal("")`-crash even sooner. **37** top-of-hour 1H markets in TRAIN carry an **empty `expiration_value`** with a real `result` (e.g. `KXBTCD-26JUN2716-T61099.99` at `2026-06-27T20:00`, `result=no`, `ev=""`). If such a market were ever the *nearest* strike, the run hard-fails on a data-completeness quirk rather than excluding it.

I verified this **does not bite TRAIN**: across all ~1,200 train hours the nearest strike is *never* one of the empty-EV markets (`_reviewA_tmp/ev_scan.py`: `HOURS where NEAREST hourly strike is empty-EV = 0`; the empty ones sit hundreds of dollars OOM). So A1 is the only thing crashing train. But A2.3's "any mismatch = hard fail" was written for genuine cross-leg print *disagreement*, not a blank field on a settled market ΓÇö and the sealed set's geometry is unknown. Recommend: treat empty `expiration_value` as `EXCL`-with-receipt (fail-closed), distinct from a true value mismatch. C16 ("both legs in every OK smoke hour agreed") never probed the empty case.

### A3 ΓÇö NO-PAIR inclusion is partly decided on a WORST-column value, contra A2.5 (LOW)

`census.py:391`: `if not q or primary_C.get("cb") is None or primary_C.get("cw") is None: NO_PAIR`. A2.5 is explicit: WORST values "**never determine pair inclusion**" ΓÇö only BASE close does. Here a missing `cw` (WORST) forces `NO_PAIR` even when BASE is fully quoted. It only bites if a candle has `yes_ask.close`/`yes_bid.close` but a missing `high`/`low` ΓÇö not observed in my sample, so immaterial to numbers today, but it is a literal deviation from a frozen amendment and should read `if not q or cb is None`.

### A4 ΓÇö "unreachable until preflight passes" overstates the seal's enforcement (LOW, defense-in-depth)

Grep confirms `acknowledge_sealed_read=True` appears in exactly one production path (`unseal_runner.py:106`); `SEALED_DATES` matches `SEAL.md` exactly (17 days, 2026-08-02..08-18); `_guard_seal` fires before any open; CLI `census.main` has no acknowledgement path. **But** the preflight lives only in `unseal_runner`, not at the loader boundary ΓÇö nothing binds `acknowledge_sealed_read=True` to a passed preflight. A careless bridge that calls `census.build_census(list(SEALED_DATES), acknowledge_sealed_read=True)` directly (the very call `unseal_runner.run()` makes) reads sealed days with **no** gate/falsifier/go-flag check. The build report's "that path is unreachable until a fail-closed preflight passes" is therefore inaccurate; the real teeth are the boolean + human discipline + harness Read-deny (which does not stop a Python `open()`). Consider moving the frozen-falsifier assertion into the loader when `acknowledge_sealed_read=True`.

### A5 ΓÇö Falsifier "frozen" gate is a fragile substring test (LOW)

`unseal_runner.preflight` accepts any file where `"NOT FROZEN" not in text and "FROZEN" in text`. A draft that happens to contain the uppercase token "FROZEN" in prose but lacks the exact literal "NOT FROZEN" would pass. Fail-closed on lowercase drafts (good), but the marker should be a structured line, not a bare `in` test.

---

## 4. Confession & mandate-item verification

- **C1 (╧â╠é window includes the anchor closing at T) ΓÇö VERIFIED, no lookahead.** The code (`sigma_hat`, `census.py:110-126`) reads 9 anchors at T, TΓêÆ900,ΓÇª,TΓêÆ7200 incl. A(T), consecutive diffs ΓÇö exactly what C1 states. No lookahead: I confirmed on real data (`_reviewA_tmp/anchor_identity.py`) that `floor_strike(T) == expiration_value(TΓêÆ15)` for **277/277** consecutive pairs ΓÇö the anchor for the window closing at T is the TΓêÆ15 settle, known by decision time TΓêÆ5. **Rank sensitivity (as C1 admits): NOT invariant.** On my sample T=2026-06-26 12:00, the C1 window gives ╧â╠é=**156.8539**; the rejected alternative (exclude A(T), use TΓêÆ15ΓÇªTΓêÆ135) gives **154.5879** ΓÇö a different G/╧â╠é, which can move quintile membership. C1's characterization is honest and the choice is defensible, but it is genuinely outcome-affecting; the bridge should ratify it consciously. (C1's "2h15m span of observations" is loose ΓÇö 9 obs at TΓêÆ15ΓÇªTΓêÆ135 span 2h; cosmetic, count is right.)
- **C7 (fixed 24-h grid; no hour silently vanishes) ΓÇö VERIFIED on a gap day I found (2026-06-25, untouched).** Real 15M tape gap 06:45ΓåÆ09:15 (9000 s). Both receipts fire correctly and independently matched my recompute: `EXCL_NO_15M_LEG` at 07/08/09:00, `EXCL_NO_1H_LEG` at 06:00 (the 1H side is also short there), and consequent `EXCL_SIGMA_TAPE` at 10/11:00 whose windows reach into the gap. No silent drop. (This is the same failure shape as the documented 07-16; I confirmed it on a second, independent day.)
- **C2 (sample vs population ╧â╠é) ΓÇö rank-invariant, confirmed:** every window has exactly 8 diffs, 156.8539/146.7234 = ΓêÜ(8/7); uniform scale ΓåÆ identical quintiles. Immaterial (and Reviewer B's domain).
- **C5 (EOL-normalized byte-identity) ΓÇö safe and exercised.** Real cross-file duplicate at `2026-06-26T00:00:00Z` (present in both 06-25 and 06-26 files) is byte-identical after `rstrip("\r\n")`; the dedupe keeps one. Trailing non-EOL whitespace differences would still hard-fail (conservative).
- **Snapshot indexing (mandate #4) ΓÇö VERIFIED no off-by-one.** `end_period_ts == TΓêÆ300` is minute-aligned ((TΓêÆ300) % 60 == 0) and selected by **exact equality**; that bar covers (TΓêÆ360, TΓêÆ300] = the minute *ending* at TΓêÆ5:00. Later bars (ending TΓêÆ240ΓÇªT, which do exist up to the close) are never picked. No window extends past TΓêÆ300.
- **Day assignment (mandate #6) ΓÇö VERIFIED:** `assign_day(2026-06-26T00:00:00Z)=2026-06-25`, `ΓÇª00:00:01Z=2026-06-26`; the 00:00 boundary market files to the prior day, so it is not double-counted (it is day D's 24th grid hour, not day D-1's).
- **Seal / SEALED_DATES vs SEAL.md (mandate #5) ΓÇö match exactly** (see A4 for the one caveat). Sealed files are not even present on disk (corpus stops at 2026-08-01); the test that passes `acknowledge_sealed_read=True` (`test_loader.py:77`) uses a **synthetic** `data_root`, not the real 2026-08-05 file ΓÇö no real sealed byte is touched. I opened no file dated 2026-08-02..18.

---

## 5. Bottom line

The economic engine is correct where it runs ΓÇö 72/72 hours, 15/15 columns, exact ΓÇö and the seal, the pin/escape truth table (incl. A2.3 exact-boundary both orientations), the fee goldens, C1's no-lookahead, and C7's grid receipts all hold. But **the build cannot deliver the artifact it exists to deliver:** `A1` aborts the frozen train run outright on real data the smoke window never sampled, and it is unconfessed. Fix `A1` (strike-aware exclusion-with-receipt for `floor_strike`-less markets, in both the tape build and the pairing path), then `A2` before unseal, then re-review. An empty confessions section was flagged as a red flag; here the *populated* confessions still missed the one deviation that stops the run ΓÇö because it lives in the corpus, not in the diff.

*Scope note: all work in `_reviewA_tmp/` (recompute, diffs, read-only scans, and census outputs redirected there); no file under `sim/` was modified; the full train census was not run; no sealed day was accessed by any means.*
