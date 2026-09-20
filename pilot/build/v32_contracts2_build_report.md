# V3.2 AMENDMENT 1 build report — contracts 1 -> 2 (params sha re-pin)

Branch `amend/contracts-2` off origin/main `8543ad4`. Opus 4.8 builder. Worktree
`C:\Users\Brads\Python_stuff\dv3_wt_v11`. `python` only. No order writes; the only proxy call made was a
read-only `curl -s 127.0.0.1:8642/health`.

## What changed and why

Brad's dated verbatim go (2026-09-20 ~00:21Z, first message after a context compaction):
> "Hey! Welcome back after the compact! All memory of whats next carry over>? You have my go to build the multi-contract. Lets size up!"

His reasoning two days earlier (2026-09-18):
> "Yea, I think waiting for n to hit 30 before sizing was the play when we were entering roughly half the windows. This strategy is different and should be judged differently. It's not "Do we win in profitable rates" anymore, we in theory never lose, right? Waiting for a specific n should be to answer questions about slippage and edge cases, not proof of a coin flip is weighted on one side. Make sense? I'd vote to double soon, but want your insight too"

This is AMENDMENT 1 of the FROZEN V3.2 falsifier. The ONLY value that changes is `params.contracts`
(1 -> 2). Every other param, every verdict gate, the STATUS line, and the proxy cap stay exactly as they
are. The Promotion clause (ALIVE at n>=60 AND thinner-wing depth >=10 lots) is SUPERSEDED for this size
step by pointer: Brad's 2026-09-18 ruling re-frames the lock as structural (every set pays $2.00), so n
is for slippage/edge-case questions, not for proving a weighted coin. The promotion pins stay DEFINED in
code (add-only law) and the Promotion section text is not edited.

## SHA computation (verified)

Canonical sha = `sha256(json.dumps(obj, sort_keys=True, separators=(",",":")).encode("utf-8"))`.

- OLD (contracts 1, 2026-09-14 freeze): `0ac697957c69a004e45d49505cce1084aaeb2e50bbaea45fe60bfbe0911c80dc` — verified against the pre-edit file.
- NEW (contracts 2): `a2a58787bb88a6ded644c2ff6a22c5e75fbb1b41882ca7f76e40d9405a139a9c` — matches Fable's precomputed target.

Sanity outputs (from `dv3_wt_v11/pilot`):

```
$ python -c "from service.v32.params import load_v32_params; p=load_v32_params(); print(p.contracts, p.sha256)"
2 a2a58787bb88a6ded644c2ff6a22c5e75fbb1b41882ca7f76e40d9405a139a9c

$ python -c "import json,hashlib; o=json.load(open('policy/v32_params.json')); print(hashlib.sha256(json.dumps(o,sort_keys=True,separators=(',',':')).encode()).hexdigest())"
a2a58787bb88a6ded644c2ff6a22c5e75fbb1b41882ca7f76e40d9405a139a9c
```

`git diff policy/v32_params.json` shows exactly one changed line (`"contracts": 1,` -> `"contracts": 2,`);
the file is byte-identical otherwise.

## Code changes

- `pilot/policy/v32_params.json`: `contracts` 1 -> 2 (only change).
- `pilot/service/v32/params.py`:
  - `FROZEN_V32_PARAMS_SHA256` re-pinned to the new sha (comment records AMENDMENT 1).
  - ADDED `PREVIOUS_V32_PARAMS_SHA256_2026_09_14 = "0ac697...80dc"` with a history comment (add-only law).
  - docstring line `contracts    order size (1; proxy cap 2 stands)` -> `order size (2; proxy cap 2 stands -- AMENDMENT 1 2026-09-20, was 1)`.
  - `load_v32_params` self-verification unchanged.
- `pilot/ceremony/v32_falsifier.md`: APPENDED one Registration entry titled
  `2026-09-20 ~00:21Z -- AMENDMENT 1 (params sha; contracts 1 -> 2; Brad's dated go)`, placed after
  MEASUREMENT CLARIFICATION 3 and before `## Pre-registered shadow observations`. Nothing above the
  "change NOTHING above this line" marker was touched; the header provenance sha, the "1 contract" line,
  the Policy sha/"contracts 1" line, and the Promotion section are left as-is and superseded by pointer.
- `pilot/ops/V32_ARMING.md` (not frozen): contracts reference updated (params.contracts now 2, equals the
  proxy cap); item 6 now expects the NEW sha; MUST CONFIRM item 10 (first size-2 windows) added with
  sub-items (a)-(e).

## Every test assertion touched (file:line, old -> new, reason)

Mechanical mirrors of the amendment (VALUE assertions on the real params):

1. `pilot/tests/test_v32_falsifier_pins.py:123` — `test_partial_fill_contracts_lever_unchanged_at_one`.
   Function kept (history); docstring + body rewritten. `assert p.contracts == 1` -> `assert p.contracts == 2   # AMENDMENT 1 (was 1)`; still `assert p.sha256 == FROZEN_V32_PARAMS_SHA256`.
   Reason: the lever moved; the doc's 2026-09-18 "stays 1" text is unchanged so the neighbouring
   assertion at line 110 (`"params.contracts` remains BRAD'S lever and stays 1" in reg`) still passes.

2. `pilot/tests/test_v32_partial_fill.py` — SIZE-1 REGRESSION kept byte-identical via a new
   `_params1(**over)` helper (real params, `contracts` forced to 1):
   - `:282` `test_contracts1_single_batch_mirrors`: `p = _params()` -> `p = _params1()` (line 283 `assert p.contracts == 1` now passes because `_params1()` forces 1).
   - `:379` `test_contracts1_ledger_row_additive_keys_single_set`: `_params()` -> `_params1()`.
   - `:668` `test_poll_noop_at_contracts1_after_ws_fill`: `_params()` -> `_params1()`.
   - `:683` `test_poll_first_when_ws_missed_books_the_lot`: `_params()` -> `_params1()`.
   - `:170-171` `test_both_batches_complete_counts_two_sets`: `p = _params(contracts=2)` -> `p = _params()`
     + `assert p.contracts == 2 and p.sha256 == load_v32_params().sha256`. Reason: proves the REAL
     shipped file now drives contracts=2. All other size-2 tests keep `_params(contracts=2, ...)` (which
     equals the real value now) — the size-2 suite is comprehensive (partial fills, cumulative->delta,
     amend-cross echo guards, poll backstop, per-batch one-legged).

3. Behavioral tests that implicitly assumed contracts==1 from the real file — size-1 behaviour PRESERVED
   under an explicit override (size-2 counterparts already exist in `test_v32_partial_fill.py`):
   - `pilot/tests/test_v32_core.py:414` `test_fill_during_amend_takes_wings_not_place`:
     `_params(tol=0.01, deb_ms=0)` -> `_params(tol=0.01, deb_ms=0, contracts=1)`.
   - `pilot/tests/test_v32_core.py:437` `test_amend_confirm_with_cross_fill_takes_wings_once_no_second_rest`:
     `_params(tol=0.01, deb_ms=0)` -> `_params(tol=0.01, deb_ms=0, contracts=1)`.
   - `pilot/tests/test_v32_amend.py:190` `test_core_routes_amend_cross_fill_into_wings`:
     `p = load_v32_params()` -> `p = dreplace(load_v32_params(), contracts=1)`.
   - `pilot/tests/test_v32_executor.py:389` `_armed_driver(tmp_path)` -> `_armed_driver(tmp_path, contracts=None)`
     (optional override); `:463` `test_fill_dedup_poll_first_then_ws` now calls `_armed_driver(tmp_path, contracts=1)`.

4. `pilot/tests/test_v32_golden.py:242` `_run_core`: added `contracts=1` to the `dreplace(...)` so the
   golden remains the SIZE-1 economic reference. `test_v32_golden.py` passed at contracts=2 already (the
   per-contract lock is size-invariant), but pinning contracts=1 keeps it byte-identical to the
   pre-amendment build; the +10.36c reference reproduces exactly at contracts=1 (5 passed).

5. `pilot/tests/test_v32_falsifier_pins.py` — ADD-ONLY section `AMENDMENT 1` (6 new tests, no existing
   assertion edited beyond item 1 above):
   - `test_amendment1_params_sha_repinned_and_previous_defined`: `FROZEN_V32_PARAMS_SHA256` == new
     literal; `PREVIOUS_V32_PARAMS_SHA256_2026_09_14` == old literal; the two differ.
   - `test_amendment1_loader_self_verifies_contracts_two`: `load_v32_params().contracts == 2` and
     `.sha256 == FROZEN_V32_PARAMS_SHA256`.
   - `test_amendment1_registration_entry_present`: the Registration section carries the entry title,
     BOTH shas, Brad's exact words "You have my go to build the multi-contract. Lets size up!" and "not
     proof of a coin flip is weighted on one side"; STATUS line still `STATUS: FROZEN`; the entry sits
     after MEASUREMENT CLARIFICATION 3.
   - `test_amendment1_promotion_pins_still_defined_and_in_doc`: `n >= 60` and `10 lots` still in the doc;
     `V32_PROMOTION_MIN_N == 60`, `V32_PROMOTION_MIN_DEPTH_LOTS == 10`.
   - `test_amendment1_caps_agree_at_two_refuses_above`: `v32_caps_agree(max 2, contracts 2)` ok;
     `(max 1, contracts 2)` refuses; `(max 2, contracts 3)` refuses (params.contracts > proxy max);
     `(max 3, contracts 3)` refuses (V3.2 ceiling `V32_MAX_CONTRACTS_PER_ORDER == 2`).
   - `test_amendment1_arming_check_passes_at_contracts_two`: `v32_arming_check(real frozen doc, healthy
     /health shape, params_verified=True, contracts=2)` arms.

The doc-sha tests that failed only because the doc lacked the new sha (`test_stop_pins_and_sha_match_doc`,
`test_bucket_freshness_pin_matches_params_and_doc`, `test_registration_carries_2026_09_18_partial_fill_clarification`,
`test_registration_carries_2026_09_19_capture_ratio_clarification`) pass once the Registration entry —
which contains the new sha — is appended; no assertion in them was edited.

`pilot/tests/test_box.py:442` (`assert ... p.contracts == 1`) is the BOX roster (fields `hourly_ask_min`,
`min15_ask`, etc.), NOT v32 — left untouched, and it passes.

## Suite result

`cd C:\Users\Brads\Python_stuff\dv3_wt_v11\pilot && python -m pytest -q` -> **941 passed, 0 failed** in
~23 s. Before the amendment this worktree collected 935 (924 passed + 11 failed on the intentional value
drift); +6 is the AMENDMENT 1 add-only section. No environmental skips were observed. Baseline on main
(live tree) is 935 passed; the count matches (935 + 6 new = 941). Zero failures — the hard requirement.

## /health read (00:2xZ, read-only)

```
{"status":"ok","env":"prod","signed":true,"orders_enabled":true,"key_fingerprint":"d9fac8c8ad48e676",
 "caps":{"max_contracts_per_order":2,"ticker_prefixes":["KXBTC15M","KXBTCD","KXBTC"],"daily_order_budget":4000},
 "orders_used_today":0,"orders_remaining_today":4000}
```

`max_contracts_per_order` 2 == `params.contracts` 2 == the V3.2 ceiling `V32_MAX_CONTRACTS_PER_ORDER`.
`v32_caps_agree` passes; a params.contracts of 3 would refuse.

## Report/ledger pool across params_sha (verified)

- `service.v32.report` has ZERO references to `params_sha` (`grep -c` = 0): the scoreboard pools EVERY
  armed row into one `n`; the size-1 history (n=7) and the coming size-2 sets pool toward the n>=30 verdict.
- `service.v32.ledger.build_v32_ledger_row` records `"params_sha": params.sha256` on each row (line 190)
  and never filters on it, so the two regimes (old sha `0ac6979...`, new sha `a2a5878...`) remain
  separable after the fact.

## Blockers / nits

None. No runtime code hard-assumes `contracts == 1` (grep of `service/v32/*.py` + `run_v32.py` for
`contracts == 1` / `== 1` is clean); the known-good primitives (`core._rest_size`, per-batch `WingBatch`,
`_book_rest_delta`, `cancel_ctx`, `amend_cross_pending`, `run_v32._compute_money_math(contracts=...)`,
`stops.v32_caps_agree`) all size on `params.contracts`.
