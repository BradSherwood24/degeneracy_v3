# Phase box-5 build report — the wide-box daily report

Branch: `box/phase5-report` (based on `box/phase4-arm` @ 337d111). Deliverable: the daily report for
the wide box — the instrument that turns fills into knowledge. Read-only over the real journals + the
repaired pilot ledger; no socket, no order, no sealed read, no key/PEM/.env; `python` only.

---

## What shipped

### `pilot/service/box_report.py`
`python -m service.box_report [--day YYYY-MM-DD | --all] [--journal-dir] [--ledger] [--ops-dir]
[--out]` → a plain-text table to stdout + JSON (`pilot/reports/box_YYYY-MM-DD.json` per day and
`pilot/reports/box_cumulative.json`). Pure computation is split from I/O:

- **PURE** (`build_window`, `aggregate_block`, `r1_slippage`/`r2_economics`/`r3_pin_rate`/`r4_match`,
  `a1_slippage_outliers`, `a5_one_legged`, `level_bumps`, `candle_staleness`, `compute_s4`,
  `build_report`, `render_text`): dicts in, dicts/str out. Money is Decimal throughout; only
  timestamps are floats. No file/clock/network access.
- **I/O** (`load_journals`/`load_journal_file`, `load_ledger`, `load_guards`, `main`): discover the
  journals, read the ledger + `stops_*.json` day-guards, write the JSON, print the text.

**Per fired box window** (source `wide-box`, real `box_fire` only; `box_would_fire` in a separate
shadow section): close_time, t_minus at fire, side (below/above), K, A, box width |K−A|, the decided
asks (hourly + 15M), the IOC limits, displayed ask size at each ask, per-leg fill price and
**slippage = fill − decided ask** (SIGNED, side-space), fees paid, C paid (actual) vs C_mid at
decision, implied pin, fill class (both / one-legged / none), settlement outcome (pinned / not /
unsettled — from the backfill's `settlement_payoff`: $2 = pinned, $1 = not), realized per pair (close
booking + settlement backfill), and for a one-legged window the flatten attempts + outcome.

**Aggregates** (cumulative and per UTC day): fires / two-leg / one-legged / none / fill rate; R1
(mean summed slippage ¢ + SE vs +1.0¢ / 30), R2 (mean realized per FIRE ¢ — one-legged flatten
losses INCLUDED — + SE + deterministic bootstrap 95% CI vs −3¢ / 60, with the two-leg-only per_pair
mean shown beside it), R3 (pin rate vs 0.80 / 60, with the 0.90 backtest and the mean
implied pin beside it), R4 (toward 100), A1 (legs slip > 2¢, listed + running mean), A5 (one-legged
rate over the rolling last 20 fires vs 0.10), the level-bump count + bump-size distribution, the
candle-staleness proxy (C paid vs C_mid), and S4 (day-guard balance_start / latest balance / loss vs
$3.00 + `ledger_vs_balance_delta` + whether any stop is latched today). Every retirement/alarm line
prints `NOT YET (n/N)` / `HOLDING` / `TRIPPED`, computed mechanically.

### `pilot/tests/test_box_report.py` — 33 tests
Synthetic journals + ledger fixtures: both-filled pinned / not-pinned / unsettled; one-legged
flattened / held-naked / held-then-settled; the level bump (+ A1 at >2¢); A5 at 3/20 (TRIPPED) vs
2/20 (HOLDING); R1/R2/R3 status transitions across their pins (NOT YET → HOLDING/TRIPPED); R4
confirmation at 100 clean; S4 HOLDING / TRIPPED-by-loss / TRIPPED-by-latch / NOT-YET; empty day;
WOULD_FIRE excluded from the fired set; signed/summed slippage; candle-staleness; displayed ask size
carried; the day-filter with cumulative still spanning all days; and an end-to-end I/O round-trip
through real files (including a `kalshi_ws` line to prove the fast-reject).

### `pilot/reports/README.md`
Usage + what every number means. Generated JSON is gitignored (`pilot/reports/*.json`); only the
README is tracked.

### `.gitignore`
Added `pilot/reports/*.json` (generated artifacts, regenerated each run).

---

## Verification

- `python -m pytest -q` in `pilot/`: **537 passed** (524 prior + 13 new review-fix tests). Junctions
  to the real `sim/out` and `historical-data` were created for the run and removed with `rmdir` before
  finishing.
- Report over the REAL journals (120 journals, 14.5 GB; ledger 119 rows, all corridor): **0 box
  fires** — every retirement/alarm reads `NOT YET`, the shadow/empty output that is expected before
  the box ever arms. Run time ~25 s.
- A populated SYNTHETIC run (in scratch, not committed) exercised both-filled pinned/not, a one-legged
  flatten, level bumps, A1/A5 TRIPPED, and the would-fire shadow — the table renders correctly.

---

## Review fixes (Opus 4.8 review of 08c689b, applied 2026-08-26)

- **F1 (HIGH, repro-confirmed) — R2 now gates + averages over SETTLED fires only.** A wide-box
  both-filled fire is `realized_unsettled` at close, booked at the conservative $1 floor
  (`−C_paid + $1`): a fire that will settle PINNED reads −$0.90 there instead of +$0.10. R2's
  operating point sits near 0¢, so two late-settling winners at the tail of a session flipped
  HOLDING → TRIPPED on settlement TIMING alone (reviewer's `fixture_r2.py`). Fix: `r2_economics`
  restricts the mean/SE/CI **and** the 60-count gate to `realized_settled` fires (one-legged
  flattened fires are settled immediately — their round-trip P&L is final); `unsettled=` and a new
  `n_settled` are displayed, `n_fills` stays as the all-any-fill count for transparency. Reading
  note: the coordinator's "same reality with 2 unsettled → still HOLDING" is implemented as 60
  settled + 2 unsettled on top (62 total): the 2 unsettled enter neither the gate nor the mean, so
  status holds at the 60-settled mean. The reviewer's exact 60-total repro (58 settled) now reads
  `NOT YET (58/60)` — the false TRIP is gone. R1 (slippage, known at fill) and R3 (already
  settled-gated for its rate) are unchanged.
- **F2 (LOW/MED) — backfill join now requires the box source tag.** `build_backfill_entry`
  (`pilot_ledger.py`) now writes `source` (= the settled window's `fired_source`) and `strategy`
  (additive). `index_ledger` picks, among the backfill rows for a close_time, a WIDE_BOX-tagged row;
  a single untagged legacy row is tolerated only when it is the ONLY backfill for that close_time; a
  non-box-tagged row or an ambiguous untagged set yields NO box backfill (the window reads
  unsettled — never another strategy's payoff).
- **F3 (LOW) — fast-reject reads the exact top-level kind.** `load_journal_file` now finds the FIRST
  `"kind": "` token (the top-level kind, since the journal is `json.dumps(sort_keys=True)` so keys
  serialize idx, kind, local_ts, obj) and compares its value against the keep-set, instead of
  substring-testing the whole line. A kept record whose `obj` embeds `"kind": "kalshi_ws"` in a
  string is no longer dropped; a `kalshi_ws` line embedding a keep-marker is no longer parsed. Same
  ~0.3 s/150 MB throughput.
- **F5 (LOW) — a corrupt day-guard is surfaced.** `compute_s4` carries a `corrupt` flag; a corrupt
  guard renders `S4 DAILY LOSS: GUARD CORRUPT - arming refused` per day and `GUARD CORRUPT: <days>`
  in the cumulative line, distinct from `NOT YET (no balance data)`.
- **F4 / F6 (INFO)** left as-is: `decide_box` latches `entered` and fires once per window (F4), and
  per-day A5 being day-scoped while cumulative A5 matches the live rolling alarm (F6) is the intended
  behavior — both are noted, neither is a defect.

Tests added (13): the R2 settled-only scenarios (60 settled HOLDING, +2 unsettled still HOLDING, the
reviewer repro now NOT YET, all-unsettled → NOT YET, one-legged-flatten counts as settled); the F2
join (box tag wins over a foreign row appended last, legacy-single tolerated, ambiguous/non-box
skipped, and `build_backfill_entry` carries the tag); the F3 exact-kind gate; and F5 corrupt-guard
surfacing per-day and cumulative.

---

## The join that decides everything (and where the numbers live)

The falsifier's two deciding numbers — per-leg fill vs the ask we decided on, and realized per pair —
are a JOIN of two artifacts, because neither alone holds both halves:

- **Decision** (the decided ask, the IOC limit, the displayed ask size, K/A/side, C/C_mid/implied
  pin, t_minus) comes from the journal `box_fire` record's `selection` payload
  (`box_runner._selection_payload`). `BoxSelection.hourly_ask`/`m15_ask` are documented in `box.py`
  as "the OBSERVED ask (the 'decided ask' the daily report compares to)".
- **Fill** (avg fill price, actual fee, fill class, realized, floor, flatten outcome) comes from the
  pilot-ledger box row (`run_window._build_box_ledger_entry`).
- **Settlement** (pinned vs not) comes from the ledger's settlement-backfill row
  (`settlement_payoff` = $2 pinned / $1 not).

Slippage is recomputed here as **fill − decided ask** and is NOT the ledger's
`slippage_abs_per_side`: that stored field is `|fill − IOC limit|` (the limit = observed ask +
`limit_margin` 0.03), which is the wrong reference and unsigned. The report ignores it and joins the
fill to the journal's observed ask by ticker.

---

## CONFESSIONS

1. **Fields inferred from journal/ledger SHAPES, not read from a typed record.** There is no typed
   "fired box window" record; the report reconstructs one by joining the `box_fire` journal payload
   (an untyped dict built by `_action_payload`/`_selection_payload`) to the ledger row (an untyped
   dict built by `_build_box_ledger_entry`) by `close_time` and by leg `ticker`. Every field name the
   report reads (`selection.hourly_ask`, `fills[].avg_price`, `box_one_legged`, `box_flatten_filled`,
   `floor_booked`, `settlement_payoff`, `unsettled_legs`, `s4_balance_check.loss_dollars`, the
   day-guard `latched`/`balance_start_dollars`) was verified against the emitter in this branch's
   source, but nothing is enforced by a shared schema. If an emitter renames a field, the report goes
   quietly None for it rather than raising — a deliberate fail-soft (a report must not crash on a
   partial window), but it means a silent rename would show as blanks, not an error.

2. **R2 counts over the SETTLED any-fill fires (coordinator ruling + review fix F1, 2026-08-26);
   R1/R3/R4 stay two-leg-only.** I first shipped R2 over two-leg fills only; the coordinator ruled it
   must be per FIRE including one-legged flatten losses; the Opus 4.8 review then caught that counting
   UNSETTLED both-filled fires (booked at the conservative $1 floor) lets settlement timing false-trip
   R2 (F1). Final behavior: R2's STATUS is the mean realized per FIRE over the SETTLED box fires with
   any fill (two-leg + one-legged; one-legged flatten losses INCLUDED — real money; zero-fill and
   not-yet-settled fires excluded), TRIPPED after 60 SETTLED such fills if that per-fire mean < −3¢.
   The two-leg-only "per pair" mean (`per_pair_mean_realized_cents`, `n_pairs`) is still computed and
   displayed but does NOT drive the status; `n_settled` and `unsettled` are shown, `n_fills` is the
   all-any-fill count. R1 (slippage) and R3 (pin rate, already settled-gated for its rate) remain
   two-leg-only — both need both legs — and R4 counts two-leg fills toward 100. The falsifier R2 line
   was updated to match (below its DRAFT STATUS line). See the Review-fixes section for the F1 detail
   and the reading of the coordinator's "+2 unsettled → still HOLDING" test.

3. **R3 pin rate is computed over SETTLED two-leg fills; the gate count is two-leg fills.** A pinned
   vs not-pinned verdict only exists once the settlement backfill lands. The `n` shown against the
   `/60` gate is the two-leg-fill count; `pin_rate` is `pinned / n_settled` (both reported). If 60
   fills are reached but some are still unsettled, `pin_rate` uses only the settled ones and the line
   notes `settled=`. If zero are settled, `pin_rate` is `-` and the status is `HOLDING` (it cannot
   trip without data) — mechanical, not a judgment.

4. **R4 `TRIPPED` means CONFIRMED, not a stop.** R4 is a positive terminal ("the candle number stands
   live-confirmed"). To keep the three-state vocabulary uniform I map R4's firing condition (≥100
   two-leg fills with no R1–R3 trip) to `TRIPPED`, and print `(TRIPPED = live-confirmed)` beside it.
   If Brad prefers a distinct word (e.g. `CONFIRMED`) that is a literal change with no logic impact.

5. **A5 uses the rolling last-20 window with no "gate".** Matching `run_window._box_a5_alarm` /
   `pilot_ledger.box_one_legged_rate(n=20)`: the rate is over the last `min(20, fires)` box fires and
   trips whenever it exceeds 0.10 (so at exactly 2/20 = 0.10 it HOLDS; 3/20 = 0.15 TRIPS). With zero
   fires it reads `NOT YET (0/20)`. It is a rolling alarm, not a "wait for N" gate, so the `n/N`
   count semantics differ from R1–R4 — documented so the difference is not read as a bug.

6. **Candle-staleness `paid_vs_mid` is restricted to both-filled pairs.** "C paid vs C_mid at
   decision" is only apples-to-apples when both legs filled (`C_paid` = the full pair cost). A
   one-legged fire's `C_paid` is a single-leg partial, so including it would skew the mean; I gate
   `paid_vs_mid` on `len(paid_legs) == 2` and keep the decision-time `C − C_mid` gap for every fire
   (it needs no fills). The per-window `C_paid` is still printed for a one-legged fire, just excluded
   from the staleness mean.

7. **`C paid` is the ACTUAL booked cost (fills + fees), not the decision-time `C`.** The falsifier's
   line says "C paid vs C_mid"; I read "C paid" as money actually spent (`Σ fill + fee` from the
   ledger) and report it beside the decision-time `C` (the fee-inclusive limit cost from the
   selection) and `C_mid`. So the report shows all three: `C_paid`, `C_decision`, `C_mid`. If "C
   paid" was meant to be the decision `C`, the `C_decision` column already carries it.

8. **The fast-reject loader reads the EXACT top-level kind (review fix F3).** A live journal is
   ~120–200 MB and ~440k lines, dominated by `kalshi_ws` frames; parsing every line across 120
   journals is minutes. `load_journal_file` finds the FIRST `"kind": "` token and compares its value
   against the six report kinds, `json.loads`-ing only matches (~0.3 s per 150 MB file; ~25 s over all
   120). This relies on the journal being written `json.dumps(sort_keys=True)` (so the top-level keys
   serialize idx, kind, local_ts, obj and the first `"kind": "` is the record's own) — true for
   `journal.Journal.flush`. Reading the actual field value (not a whole-line substring) means a kept
   record whose `obj` embeds `"kind": "kalshi_ws"` is not dropped and a `kalshi_ws` line embedding a
   keep-marker is not parsed (both tested). An unparseable/truncated line is skipped, not raised.

9. **S4 is inherently per-day; the cumulative block summarizes, it does not sum.** `compute_s4` reads
   one UTC day's guard file + that day's `s4_balance_check` records (latest by `local_ts`). The
   cumulative section lists which days have balance data, which have any latch, and (review fix F5)
   which have a CORRUPT guard, rather than inventing a cross-day loss. A corrupt/unparseable guard is
   surfaced distinctly (`GUARD CORRUPT - arming refused`), never as "no balance data", because its
   latch state is unknown and arming is refused. On the real journals there are zero
   `s4_balance_check` records and no `stops_*.json` (the 8/23 armed corridor run predates the
   2026-08-26 balance-based S4), so S4 reads "0 days with balance data" — correct, not a miss.

10. **`--ops-dir` is an extra flag beyond the spec's four.** The spec lists `--day/--all`,
    `--journal-dir`, `--ledger`, `--out`. Locating `stops_YYYY-MM-DD.json` needs a directory; rather
    than hard-wire `pilot/ops` (which would make the S4 tests non-hermetic), I added `--ops-dir`
    defaulting to `pilot/ops`. It is additive and does not change the four spec'd flags.

11. **`fill_class` is taken from the ledger, not re-derived from the fills.** A window is classified
    `one-legged` iff the ledger row's `box_one_legged` is truthy (the entry-latched flag, never reset
    by a flatten), else `both` iff `filled`, else `none` — matching `run_window`'s own accounting. A
    successfully-flattened one-legged window still shows ONE fill leg in the report (the flatten is
    booked as a round-trip on that same leg), which is why its `C_paid` is a single-leg cost; the
    flatten outcome disambiguates it.
