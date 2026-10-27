# V3.3 ETH FALSIFIER (DRAFT) -- rolling K-rung ladder spot-bucket pump-fader on the ETH hourly markets

STATUS: DRAFT -- NOT FROZEN

This file is a DRAFT. It is NOT frozen. It becomes frozen ONLY when Brad ALONE, on his verbatim go,
replaces the STATUS line above with the exact frozen marker (the one the BTC falsifier carries) and appends
his go under Registration. An agent NEVER flips it. Until then the ETH V3.3 roster cannot arm: the S5
arming gate (`service.v33.stops.decide_v33_arming` -> `service.v33.stops.v33_arming_check` ->
`service.stops.falsifier_is_frozen`, wired in `service.run_v33` at the `--falsifier` default
`pilot/ceremony/v33_eth_falsifier.md` via `ETH_LIVE_SPEC.falsifier_path_fn`) requires THIS exact path to
carry a line that is exactly the frozen marker. While this draft stands, `falsifier_is_frozen` returns
False and every ETH window degrades to dry -- an agent can never arm ETH by editing code.

This draft is the ETH analogue of the frozen BTC falsifier (`pilot/ceremony/v33_falsifier.md`). It is
written for ETH-TEST FIRE #1 (the first armed ETH shakedown, 1 lot per rung). The BTC falsifier is
untouched and stays the sole governing document for the BTC roster.

## Provenance

- Build: pilot V3.3 ETH-live adapter on Opus 4.8, disclosed branch `feat/v33-eth-live`, reviewed and
  merged to `main` by Brad. The ETH ladder arms through the SAME `service.run_v33` armed spine as BTC
  (parameterised by `service.v33.series.ETH_LIVE_SPEC`); there is no forked armed logic. The live BTC
  roster is untouched.
- Policy: roster `DegeneracyV3_3_ETH`, series `KXETH` (range) + `KXETHD` (strike), both CF Benchmarks,
  hourly, exchange_index 2, the SAME exact-fee law as BTC. Policy file `pilot/policy/v33_eth_params.json`,
  canonical sha `7be646101cc6bbe99432ee630dfd1048729354b91f23116348faa1bda7e96969` (pinned in code as
  `service.v33.eth.FROZEN_V33_ETH_PARAMS_SHA256`; the loader self-verifies and refuses drift). The ETH
  policy is BYTE-IDENTICAL to the BTC V3.3 policy EXCEPT `bucket_width` 100 -> 5 and
  `bucket_switch_hysteresis_usd` 15 -> 1 (the two geometry constants the ETH $5 bucket forces) and
  `lots_per_rung` 1 (ETH starts at ONE lot, NOT BTC's 3). Margins / rungs / shadow_Es / n_min / lock_floor
  / wing_margin are unchanged from BTC (8..18c, 11 rungs).

## What this falsifier commits to (ETH-TEST FIRE #1)

ETH-TF#1 is MECHANICAL: it proves the ETH plumbing is correct on the real venue at 1 lot, not that the
edge clears a profit bar. It is the ETH analogue of BTC Test Fire #3 (mechanical, pass on order count).

- Sizing: 1 lot per rung, 11 rungs, margins 8..18c (the shipped ETH policy, sha above).
- `[pin]` PASS at `> 30` GOOD orders over `>= 3` distinct armed sweeps. A GOOD order is a placed rung or a
  booked wing-pair leg that reconciles against executor truth with no defect (mirrors the BTC "good order"
  count the Registration uses). Fewer than 3 sweeps or <= 30 good orders is INCONCLUSIVE, not a pass.
- `[pin]` KILL immediately on ANY of: an unbooked fill (a venue fill the core never booked / hedged); a
  ledger-vs-executor reconcile mismatch; or an S4 CAMPAIGN latch (see below). These are the same
  defence-in-depth receipts the BTC gate E / S1 machinery already produces, read for the ETH roster.
- `[pin]` ACCOUNT-WIDE S4 day-loss cap `$5.00` (the SAME cap as BTC, `service.v33.stops.V33_S4_DAY_LOSS_CAP_DOLLARS`).
  The Kalshi balance is SHARED across the BTC and ETH rosters, so each roster's balance-banded S4 already
  sees BOTH series' losses, and an S4 latch on EITHER roster's day guard stops BOTH (the ETH roster reads
  BTC's guard for an S4 latch and vice versa; `service.run_v33._sibling_s4_latched`). An S4 latch on any
  armed UTC day the ETH report covers is a CAMPAIGN kill, not merely a day halt.
- `[pin]` S1_LEGGED: a one-legged-below-floor set is counted PER CONTRACT; the ETH DAY latches at
  `V33_S1_LEGGED_LATCH_THRESHOLD` occurrences (the same threshold constant as BTC). The ETH day guard is
  its own file (`ops/v33_eth_stops_YYYY-MM-DD.json`), SEPARATE from BTC's, so an ETH S1 latch never halts
  the BTC day (only the account-wide S4 is shared, by Brad's ruling).
- `[pin]` reconcile-first: the ETH roster refuses to arm with any inherited un-settled `KXETH*` position
  (scoped to the ETH series; a BTC position never blocks ETH and vice versa).

The thresholds above are DRAFT and may change until Brad freezes this document. On freeze, the pre-registered
`[pin]` values here are the ones the ETH report / stops enforce, exactly as the BTC falsifier's `[pin]`
values are mirrored in `service.v33.falsifier_pins` / `service.v33.stops`.

## Registration

(DRAFT -- empty. Brad appends his verbatim freeze go here, and only then the STATUS line changes.)
