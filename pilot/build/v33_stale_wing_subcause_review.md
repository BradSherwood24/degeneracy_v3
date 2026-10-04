# PR #127 review -- V3.3 stale-wing stand-down sub-cause labels (label only)

**Reviewer:** Opus 4.8 (did not write the PR). Branch `review/v33-stale-wing-subcause` off
`origin/fix/v33-stale-wing-subcause` @ 6c52f38.

## VERDICT: APPROVE WITH NITS

The change is genuinely LABEL-ONLY. The classifier is a faithful mirror of the REST W gate, the state
plumbing has no stale read, the episode tally counts episodes (not ticks), and the reason strings / dedup /
falsifier / economics / v32 are all untouched. No BLOCKER, no DEFECT. I added reviewer hardening tests for
paths the builder left uncovered (all passed as written -- the code was already correct). Two NITs below are
documented design choices, not fixed.

## What I verified (evidence)

**1. Truly label-only.**
- `git diff origin/main...HEAD -- pilot/service/v32` is EMPTY.
- All six changed files are additive: new pure fn `wing_unavailable_cause`, two new `V33State` fields
  (`wing_sub_cause`, `hold_sub_cause`, both default None), `sub_cause` added to existing journal payloads,
  a new ledger key `stand_down_sub_causes` (defaults `{}`), a report line guarded on non-empty, measure-tool
  columns. The journalled `reason` string stays `"stale_or_missing_wing"` on every path
  (`run_v33.py:683,689,692,704`) -- dedup, falsifier and the measure tool key on it unchanged.
- Goldens unchanged and green: `test_v33_golden`, `test_v32_golden`, `test_v33_run`,
  `test_v33_stale_wing_liveness`, `test_v33_bucket_flap`, `test_v33_standdown_ownership`,
  `test_dedupe_v33_ledger`, `test_v33_falsifier_pins` all pass (89) on the branch.

**2. Classifier correctness (`core.py:821-878`).** Mirrors `_v33_compute_W` (feed-alive check at
`core.py:805`, then v32 `_compute_W` at `v32/core.py:403`) in EXACT order: feed_dead -> missing(Sd,Su) ->
suspect(Sd,Su) -> too_old(Sd,Su) -> no_ask(Sd,Su) -> price_invalid(Sd,Su) -> unknown. Sd before Su in each
class, matching the gate's short-circuit order. Confirmed by probe:
- two checks failing at once -> label is the FIRST in gate order (Su-missing + Sd-no-ask -> `wing_missing_su`;
  feed-dead + Sd-missing -> `feed_dead`; Sd-suspect + Su-too-old -> `wing_suspect_sd`);
- None spot pair, None book tops, both asks None -> never raises, returns a label / `unknown`;
- the age bound is `params.wing_book_max_age_s` -- the SAME loose bound `_v33_compute_W` hands the v32 law via
  `_WingLawView` (`core.py:811-818`), not v32's 1.0 s;
- the open interval `_ZERO < ask < _ONE` is bit-identical to `_compute_W` (`v32/core.py:421`). An ask at
  exactly 1.00 fails the REST gate AND is labelled `wing_price_invalid_*` -- I asserted the two agree.
- **Parity (the key property):** `wing_cost` never returns None for in-range asks (`v32/core.py:132`), so
  `_v33_compute_W` returns None ONLY at a check the classifier mirrors. Therefore whenever W is None the
  classifier returns a specific label; the fall-through `unknown` is unreachable on a real None (only the
  except guard yields it). No stand-down can be left with a None/`unknown` label by accident.

**3. State plumbing (`core.py:983-993`, `2551-2554`; `run_v33.py:681-706`).**
- `wing_sub_cause` is set to None in the per-tick main `replace`, then overwritten ONLY when
  `gate_ran and st.W is None` -- so it is a label exactly when a stale wing is the cause, None otherwise.
- `hold_sub_cause` is captured from the live `wing_sub_cause` at the instant a hold begins and read back by
  the hold/cancel/resume journaling. The driver assigns `self.state` from `decide_v33` BEFORE journaling
  (`run_v33.py:519`), so no stale read.
- Hold -> resume -> (no new hold) -> later PLAIN stale stand_down: the plain path reads the CURRENT
  `wing_sub_cause` (`run_v33.py:703`), not the stale `hold_sub_cause` -- so the journal shows the current
  cause. Confirmed by trace.
- `now` consistency: the classifier is called with the same `now` as the gate within one
  `_recompute_context`; replay-deterministic.
- Per-tick cost: the extra `replace` runs only on stand-down ticks (`gate_ran and W is None`), not the hot
  quoting path; the hot path only gains `wing_sub_cause=None` in the existing replace. Negligible.

**4. Journal / ledger / report / tool.**
- Payload shapes correct; `_standdown` dedups on reason transition (`core.py` `_standdown`), so a multi-tick
  stale run emits ONE stand_down -> the tally bumps once per EPISODE (hold-start, or plain-stale transition),
  never per tick, never on resume/cancel. Hold -> cancel -> empty-stale does not double-count (the cancel
  sets `last_standdown_reason`, so the trailing empty-stale ticks dedup silently).
- Report renders only when non-empty (`report.py:1032`, `.get(...) or {}`) -- old rows without the field are
  safe.
- Measure tool: ran it READ-ONLY on the motivating journal
  `journals_v33/20261003T230000Z.jsonl.gz` (pre-label) -> `stale-wing sub_cause -> n/a (pre-labeling
  journal)`, and it reproduced the real episode (1 hold / 1 cancel / 0 resume). Labelled journals tabulate
  per sub_cause.

**5. Tonight's golden** (`test_v33_stale_wing_subcause.py::test_golden_yes_bids_only_sd_...`): Sd wing with
YES bids only -> hold `wing_no_ask_sd`, cancel after `stand_down_hold_ms`, and zero re-place across all 11
cancel confirms. Reproduces the real 23:00Z case.

## Findings

- **NIT-1 (diagnostic imprecision, documented):** `core.py:2553`. If the spot bucket CHANGES mid-hold, the
  terminal `stand_down_cancel` still names `hold_sub_cause` captured at hold start (the OLD bucket's wing),
  not the current bucket's cause. This is the PR's stated design ("captured at hold start, carried to the
  resume/cancel"). Acceptable for a diagnostic label; noted so a future reader is not surprised.
- **NIT-2 (coverage, addressed):** the builder's tests left the `wing_price_invalid_su` twin, the
  two-checks-fail ordering, the never-raises paths, and the PLAIN stale stand_down tally (hold disabled)
  untested. Verified correct by probe and added as `tests/test_v33_stale_wing_subcause_review.py` (10 tests).

## Changes made on the review branch

- Added `pilot/tests/test_v33_stale_wing_subcause_review.py` (10 tests): gate-order under multiple
  simultaneous failures; `wing_price_invalid_su`; ask==1.0 label/gate parity; classifier never-raises
  (None spot, None tops, both-asks-None); and the plain stale stand_down path carrying `wing_sub_cause`
  with a one-per-episode tally. No source change -- the code was correct; these pin the contract.

## Tests
Full suite in the worktree: **1557 passed / 5 skipped** (builder's 1547 + 10 review tests), 36.7 s.
Ran outside the :38-:00 armed band.
