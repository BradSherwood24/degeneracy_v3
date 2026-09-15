# Review — PR #54 `fix/shadow-quote-window`: gate the shadow fill on the live quoting window (T-15..T-5)

Reviewer: Opus 4.8 (Fable-delegated). Review worktree `C:\Users\Brads\Python_stuff\dv3_wt_review`.
Base `origin/main` 25876c7. PR head 0e68a9d. No proxy, no socket, no sealed/holdout date read. Live
journals were read **read-only** for the spot-check below.

## Verdict: APPROVE — no blocking items

The change is a correct, minimal, order-free fix. The shadow (the ideal no-lag fill rule) now takes a
print only inside the same T-15..T-5 window the live path quotes in, matching the live gate expression
exactly. It cannot touch any order path in either mode. Two nits and one falsifier recommendation
below; none block merge.

## What the diff does (confirmed line by line)

- `core.py` `_shadow_on_trade`: before recording a shadow fill, `t_to_close = st.close_epoch -
  event.server_ts`; `in_window = params.quote_start_s >= t_to_close >= params.quote_end_s`. A
  qualifying print outside the window does `continue` (no fill, sub left open) and appends one
  observability action `SHADOW_FILL_OUTSIDE_WINDOW` per open E. `_recompute_context` n-solving and
  `_shadow_complete` untouched.
- `actions.py`: new `ActionKind.SHADOW_FILL_OUTSIDE_WINDOW` (no WOULD_* twin) + informational fields
  `shadow_E / offer / print_price / t_to_close` on `V32Action`.
- `run_v32.py` `_journal_action`: new branch, `rk = "shadow_fill_outside_window"`, generic tail tallies
  `self.counts[rk] += 1` and journals the record.
- `ledger.py`: additive `"shadow_fills_outside_window": int(driver_counts.get("shadow_fill_outside_window", 0))`.

## 1. Money safety — PASS

- `SHADOW_FILL_OUTSIDE_WINDOW` is **not** in `FrozenExecutor._REAL_KINDS` (= PLACE_REST, CANCEL_REST,
  TAKE_WINGS, RETRY_WING), so the P3-1 mis-wire `AssertionError` is not triggered, and it matches none
  of the WOULD_*/PLACE/CANCEL/TAKE branches → falls through to `return []` (run_v32.py ~565).
- `LiveExecutor.on_action` (executor.py 318): matches none of PLACE/CANCEL/TAKE/RETRY and is not a
  WOULD_* twin → `return []` (line 333). **No `_place_rest` / `_cancel_rest` / `_take_wings` path; no
  venue write is reachable.**
- `twin_kind` (`_WOULD_TWIN.get(kind, kind)`) leaves it unchanged; and `_shadow_on_trade` builds the
  action directly (not via the shakedown `_action` helper), so it is identical and order-free in dry,
  shakedown, and armed modes.
- `_shadow_on_trade` mutates only `st.shadows` (the shadow substate) and returns actions; the
  out-of-window branch only appends an action and `continue`s. It never reads or writes `rest_live`,
  `rest_pending`, wing legs, or any live order state — confirmed unchanged from origin/main.

## 2. Clock correctness — PASS (with one benign asymmetry noted)

The build report's phrase "the TRADE's own clock (`event.server_ts`)" is slightly misleading in
isolation, but the code is correct: **the driver folds the trade onto the monotone evaluation clock
before the core sees it.** `run_v32.V32Driver.on_trade` (line 759) calls `self._stamp(server_ts)` then
`ev = replace(ev, server_ts=self._last_server_ts)` (line 768) — `_last_server_ts` is `max` over every
frame's ts on **both** connections (the PR #38 clock-flap fix). So the `event.server_ts` inside
`_shadow_on_trade` is the **same** monotone eval clock family the live `_requote` consumes via
`decide_v32`'s `now = event.server_ts` (line 379). The window gate is evaluated against the identical
clock the live gate uses. The core stays pure (time only from event timestamps).

- **Edge-inclusive bounds match exactly**: both `_requote` (line 769) and `_shadow_on_trade` (line 894)
  use the byte-identical `params.quote_start_s >= t_to_close >= params.quote_end_s`. A trade at
  `t_to_close = 300.0` is IN for both; at `900.0` IN for both.
- **`server_ts` is never None in the core**: the WS callback `V32Recorder.on_trade` (run_v32.py 1131)
  parses `_parse_server_ts` and, when None, journals `ws_frame_no_server_ts` and **returns** — the
  trade never reaches `driver.on_trade` or the core. Belt-and-braces, `_stamp` then guarantees
  `_last_server_ts` is non-None on the driver path. And the pre-existing `_fresh(event.server_ts, …)`
  guard (line 883) already runs on `server_ts` upstream of the new subtraction and cannot return True
  with a None `now`. So `st.close_epoch - event.server_ts` adds **no** new crash path.
- **Benign asymmetry (note, not a defect)**: the live path evaluates the window on book/clock events
  (the ClockTick pump is ~0.5 s), while the shadow evaluates it at the trade instant. Near the T-5
  edge the live rest's cancel is tick-latent, so within the ~[299.5, 300) s sliver live *could* still
  catch a fill the shadow now suppresses; near T-15 the first live place is likewise tick-latent. The
  shadow gate is therefore marginally **more conservative** than live at both boundaries — it can
  under-count, never inflate. This is the right direction for a counterfactual whose job is to bound
  the execution-gap, and the effect is sub-tick. No change requested.

## 3. Falsifier integrity — recommend a dated measurement-clarification registration (PR #52 pattern)

The frozen doc **does not** state a shadow window anywhere. SO-1 says the shadow "re-solves and scores
E in {0.08, 0.10, 0.12} **every tick on the live tape**"; "What is being judged" says it is "run on the
SAME feed." The `core.py` module docstring (lines ~44–49) likewise says the shadow re-solves "each book
tick with **NO lag and NO requote gate** … records a shadow fill (once per hour per E)" — with no window
qualifier on the fill. So gating the shadow fill **narrows the registered measurement** rather than
merely restating it.

Two reasons this rises to a registration, not a silent bug fix:
1. The shadow fill count and its per-E locks feed **SO-1** and, materially, the `n>=30` KILL gate
   *"mean execution gap (shadow E=0.10 lock − live lock) <= 3.0c [pin]"*. Changing which prints can
   fill the shadow changes a measurement input to a pinned verdict gate — exactly the class of change
   PR #52 (armed-days denominator) was registered for.
2. On 2026-09-15 the change moves the reported shadow count 5 → 4; that is a visible, verdict-adjacent
   number.

It is nonetheless the **correct** reading — the shadow's stated purpose (the no-lag counterfactual of a
LIVE fill; "does the venue's latency eat the edge?") is only honored if the shadow is confined to
opportunities the live path could take; a print outside the window is not a lagged live fill, it is a
fill live structurally never had. So the recommendation is a *clarification*, not a re-spec.

**Recommendation (do not edit the frozen doc yourself):** ask Brad for a dated verbatim go, then append
a Registration line — mirroring the 2026-09-15 13:30Z armed-days entry — stating that the in-process
shadow fill is gated to the same T-15..T-5 window as the live path, because the shadow is the no-lag
counterfactual of a live fill. Register it now at n=1 (well before n=30) so the registered-specs rule
holds — the measurement is pinned before the window it will judge closes. No threshold, sha, or pin
changes. (Params and sha are untouched by this PR — confirmed.)

Separately, a **doc-drift nit** (code, not the frozen falsifier): the `core.py` module docstring still
says the shadow has "NO requote gate" and fills "each book tick" with no window mention. It should note
the fill is now window-gated so code and comment agree. Non-blocking; belongs on a follow-up (I did not
push to the builder's branch).

## 4. Tests — PASS; 855 passed here (859 in a data-present tree)

`python -m pytest pilot/tests -q` in this worktree: **855 passed, 2 skipped, 2 errors**. The 2 errors
are `test_quintile.py` (`FileNotFoundError` on absent `historical-data/`), the known environmental
data-absence in the review worktree; with the 2 skips (box golden) these are the 4 data-dependent tests
that reconcile to the builder's 859 in a data-present tree. The 4 new tests pass in isolation
(`-k "shadow_print or shadow_completion_after_t5"` → 4 passed).

The 4 tests drive the real `decide_v32` (`_feed` = `decide_v32`) with a `Trade` event and assert both
the **non-fill** (`not filled`, `fill is None`) and the **emitted action** (`SHADOW_FILL_OUTSIDE_WINDOW`
with the right `offer`/`print_price`/`t_to_close`). They cover after-T-5, in-window (control),
before-T-15, and in-window-fill-completes-after-T-5. **Gap (nit, non-blocking):** being pure-core tests
they assert the *action* but not the *journal* record `shadow_fill_outside_window` nor the ledger
`shadow_fills_outside_window` counter — the driver→ledger plumbing is untested. A small driver-level
test (or a `report`/ledger assertion) would close the loop.

## 5. Ledger / report — additive, parses; report does not surface it (nice-to-have)

The ledger counter is additive with `.get(..., 0)`, so older rows still parse (PR #46/#50 pattern) —
confirmed. `report.py` aggregates `late_fills`, `stand_downs`, and the shadow cells but does **not**
surface `shadow_fills_outside_window` in its totals. The datum is captured durably in each ledger row,
so nothing is lost; adding a totals column is a nice-to-have, not blocking (as the task framed it).

## 6. Re-count spot-check — CONFIRMED on both windows (read-only journals)

Read-only from `C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\journals_v32\` (trades live in
`kalshi_ws` frames, `obj.msg.yes_price_dollars` / `count_fp` / `ts_ms`; `t_to_close = close_epoch −
ts_ms/1000`):

- **04:00Z (claimed OUT)**: on `KXBTC-26SEP1500-B77650` the first YES print strictly above the E=0.10
  offer 0.88 is **0.9000 / count 1.00 at t_to_close = 248.8 s** (T-4:09, < 300 → OUT). Every earlier
  YES print on that bucket is ≤ 0.80; every qualifying print (0.90 and up) is at 248.8 s or later, all
  OUT. Matches the report exactly.
- **00:00Z (claimed IN)**: the spot bucket is `KXBTC-26SEP1420-B78250`; the first YES print ≥ 0.69 is
  **0.69 / count 1.00 at t_to_close = 592.8 s** (300 ≤ 592.8 ≤ 900 → IN). Matches the report exactly.

The builder's determination — exactly one of the five 2026-09-15 shadow fills (04:00Z) is outside the
window, corrected count 4 (was 5), live unchanged at 2 — is corroborated on the two spot-checked
windows.

## Summary

| Item | Result |
|------|--------|
| Money safety (order-free both executors, no venue write, no live-rest touch) | PASS |
| Clock correctness (monotone eval clock, edge bounds, None-safe) | PASS |
| Falsifier integrity | APPROVE + recommend a dated measurement-clarification registration |
| Tests (4 new, real `decide_v32`) | PASS; 855/2skip/2err (data-absence), 859 data-present |
| Ledger additive / report surfacing | PASS / report surfacing = nice-to-have |
| Re-count spot-check | CONFIRMED (04:00Z OUT 248.8 s; 00:00Z IN 592.8 s) |

**Blocking items: none.** Recommend merge after Brad decides on the item-3 registration (a doc act, not
a code change).
