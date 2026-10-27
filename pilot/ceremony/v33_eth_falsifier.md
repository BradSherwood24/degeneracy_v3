# V3.3 ETH FALSIFIER -- rolling K-rung ladder spot-bucket pump-fader on the ETH hourly markets

STATUS: FROZEN

FROZEN 2026-10-27 on Brad's verbatim go (Registration below). The S5 arming gate
(`service.v33.stops.decide_v33_arming` -> `service.v33.stops.v33_arming_check` ->
`service.stops.falsifier_is_frozen`, wired in `service.run_v33` at the `--falsifier` default
`pilot/ceremony/v33_eth_falsifier.md` via `ETH_LIVE_SPEC.falsifier_path_fn`) reads THIS exact path; with the
frozen marker present, S5 no longer degrades the ETH roster, and ETH arms only if every other gate passes AND
the ETH mode file reads `armed`. The `[pin]` values below do not change without a new Registration entry
carrying Brad's verbatim words.

This falsifier is the ETH analogue of the frozen BTC falsifier (`pilot/ceremony/v33_falsifier.md`). It is
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
- `[pin]` PASS at `> 30` GOOD orders over `>= 3` distinct armed sweeps (a sweep = an armed window with at
  least one rung fill). A GOOD order is a FILLED rung contract whose set completes with BOTH wings filled,
  whose ledger row reconciles to the venue fills, and that leaves nothing one-legged beyond the tolerance --
  the SAME definition the BTC Registration used for Test Fires #3 and #4. Placed / re-placed / cancelled
  rungs are NOT orders for this count. Fewer than 3 sweeps or <= 30 good orders is INCONCLUSIVE, not a pass.
- `[pin]` KILL immediately on ANY of: an unbooked fill (a venue fill the core never booked / hedged); a
  ledger-vs-executor reconcile mismatch; or an S4 CAMPAIGN latch (see below). These are the same
  defence-in-depth receipts the BTC gate E / S1 machinery already produces, read for the ETH roster.
- `[pin]` ONE-LEGGED tolerance: `<= 2` contracts left one-legged across the test fire; the 3rd KILLS. (BTC at
  1 lot, Test Fire #3, used 2; the shared BTC value 6 is sized for 3 lots and does NOT apply to ETH.)
- `[pin]` ECONOMIC KILL: mean realised true lock per GOOD order `< +2.0c` once `>= 15` good orders exist ->
  KILL (BTC's `V33_KILL_MEAN_LOCK_CENTS` / `V33_KILL_MIN_N`, borrowed as-is). Realised true lock = the set's
  $2 minus the venue-filled bucket-NO price, the venue-filled wing prices and the exact fees; the venue fills
  and the balance are the truth, not the ledger's W-at-fill estimate. ETH wings are ~10x thinner than BTC's
  and live is expected ~2c/contract under dry, so a mechanically clean test fire must not quietly bleed.
- `[pin]` ACCOUNT-WIDE S4 day-loss cap `$5.00` (the SAME cap as BTC, `service.v33.stops.V33_S4_DAY_LOSS_CAP_DOLLARS`).
  The Kalshi balance is SHARED across the BTC and ETH rosters, so each roster's balance-banded S4 already
  sees BOTH series' losses, and an S4 latch on EITHER roster's day guard stops BOTH (the ETH roster reads
  BTC's guard for an S4 latch and vice versa; `service.run_v33._sibling_s4_latched`). An S4 latch on any
  armed UTC day the ETH report covers is a CAMPAIGN kill, not merely a day halt.
- `[pin]` S1_LEGGED: each armed ETH window that ends one-legged below the lock floor (reconciled against
  executor truth) records one occurrence in the ETH day guard; the ETH DAY latches at
  `V33_ETH_S1_LEGGED_LATCH_THRESHOLD` = **2** occurrences (`ETH_LIVE_SPEC.s1_latch_threshold`; BTC stays at
  `V33_S1_LEGGED_LATCH_THRESHOLD` = 6). The ETH day guard is
  its own file (`ops/v33_eth_stops_YYYY-MM-DD.json`), SEPARATE from BTC's, so an ETH S1 latch never halts
  the BTC day (only the account-wide S4 is shared, by Brad's ruling).
- `[pin]` reconcile-first: the ETH roster refuses to arm with any inherited un-settled `KXETH*` position
  (scoped to the ETH series; a BTC position never blocks ETH and vice versa).
- `[pin]` CAPITAL RESERVE (Brad 2026-10-26: "the 50% threshold seems too conservative ... If we bumped that
  to 25% reserves"): the worst-case CONCURRENT full sweep of every armed roster must be `<= 75%` of the
  exchange_index 2 balance (a 25% reserve). At arming: BTC 3-lot ~$66 + ETH 1-lot ~$22 = ~$88 vs $143.31
  (2026-10-27) = 61%. Checked by hand (Claude + Brad) before arming and before ANY size step on either
  roster; not enforced in code.

## Scoring (honest gap)

There is no automated ETH report yet: `service.v33.report` applies the BTC pins to the BTC ledger only. ETH-TF#1
is scored BY HAND against the venue (`/portfolio/fills` + the balance), window by window, exactly as every
BTC test-fire fill was reconciled, and each fill window's tally is recorded under Registration.

The `[pin]` values above are frozen. The code-enforced ones (S4, S1_LEGGED, reconcile-first, S5) live in
`service.v33.stops` / `ETH_LIVE_SPEC`; the rest (good orders, one-legged tolerance, economic KILL, capital
reserve) are scored by hand per the Scoring section.

## Registration

- 2026-10-27 -- FREEZE. Brad reviewed the draft and its four revisions (PR #156: GOOD order definition,
  one-legged <= 2 + ETH S1 latch 2, 25% capital reserve, economic KILL), verbatim: "All four fixes sound
  good, go ahead when ready", then gave the freeze go, verbatim: "Freeze it and merge. We'll take it live
  tonight". The STATUS line was set to the frozen marker by Claude on that instruction. Phase 2 (ETH dry on
  Render beside armed BTC) ran 13 windows 2026-10-27 03:00Z..15:00Z: all dry, clean exits, Render matched the
  laptop ETH dry (same markets, spot bucket, decisions; the 12Z dry fill and the 13Z ws alarm on both), BTC
  feed-lag p99 93/156 ms and writer p50 21 ms (no CPU-contention signal). Capital at freeze: $143.31 on
  exchange_index 2; worst concurrent full sweep ~$88 = 61% (<= 75%). ETH-TF#1 begins with the first armed
  ETH window after Brad sets `/var/dv3/ops/v33_eth_mode.txt` to `armed` and adds `KXETH,KXETHD` to the
  proxy's `ORDER_TICKER_PREFIXES`.
