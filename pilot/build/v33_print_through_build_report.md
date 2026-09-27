# V3.3 PRINT-THROUGH WINGS -- build report (2026-09-26)

Brad's idea (2026-09-26, "Go ahead and get an agent to build that out"): fire the wing takes EARLY off a
bucket TRADE print, before our own rung fill confirms, so the wings are in hand at the ask the sweep
STARTED from -- not the ask it jumped to in the same ~50 ms. Today (2026-09-26 tape) 3 of 4 fills came
with the wings jumping 12-20c in the same burst that filled us (lock -11.5c / -3.3c / -1.3c); the first
bucket print below our offer reached us 49-71 ms BEFORE our own fill message. The print is the early
warning; this feature acts on it.

Branch: `feat/print-through-wings` (worktree `dv3_wt_v11`). Live tree, V3.2 sources, `record_window.py`
untouched. `run_v33.py` edits are minimal and in distinct locations from the open PR #93.

## What ships (and what does NOT arm)

Master switch `print_through` ships **FALSE** in `policy/v33_params.json`. With it false the ladder is
byte-identical to the pre-feature one -- the whole pre-existing suite (1264 passed, 1 skipped) passes
UNCHANGED, and `_print_through_step` / `_pt_stall_step` return early on the first line. Flipping it true
(a one-line params change + this sha re-pin, Brad's hand) arms the early-hedge, exactly the lever pattern
the project uses for amend-first and batch-create. This is deliberate: it is a builder's job to build the
mechanism and prove it, and Brad's to pull the trigger on a live-adjacent roster (see Open Questions).

## Design (V3.3 only; V3.2 frozen)

TRIGGER (`core._print_through_step`, on a bucket `Trade`): when `print_through` is on and a spot-bucket
YES-taker print lands within `print_through_ticks` (default 1) of a resting rung's offer (`1 - n`) and
moving toward it -- `yes_print + ticks*1c + eps >= (1 - n)` -- fire the wing IOC batch immediately for
EVERY such live rung not already covered, in ONE `WingBatch(print_through=True, taken_count=count)`, at
the wing asks we currently see + `print_through_slack_c` (default 0 = at the ask, the pre-jump price). A
`PrintThroughTrigger` records the pre-hedged rung coids/prices, the count, the trigger-time asks / W /
n_top, and the projected lock. Gated to the same `_wing_prices` freshness the normal take needs (no
fresh wing book -> no fire). Convergence never rolls a pre-hedged rung (its coid must stay stable for
attribution); a rung in a roll or already covered is skipped.

FILL AFTER TRIGGER (`core._book_rung_fill`): a fill on a pre-hedged rung ATTACHES its `RungFill` to the
already-taken batch (no coalesce group, no second take) and marks the trigger's `filled_coids`. When both
wings filled AND every pre-hedged rung filled (`total_count == taken_count`), `_maybe_close_set` counts
`taken_count` sets and resolves the trigger "filled". The ledger attributes the batch exactly as the
normal `WingBatch` flow (it iterates `wing_batches` and matches `RungFill`s by identity).

STALL (`core._pt_stall_step`, on book/trade/clock ticks): a trigger whose wings are in hand but whose
pre-hedged rung(s) did not fill within `print_through_stall_ms` (default 1500) runs the stall policy
(`print_through_policy`, default `complete_else_unwind`):
- CANCEL our un-filled rests FIRST (sharded cancel path) and mark their margins consumed, so we can never
  end up double-filled (Brad's explicit requirement).
- `complete`: buy the shortfall bucket-NO ourselves as an IOC taker at the current NO ask if the taker
  lock `lock_value(no_ask, wings_paid)` >= `print_through_min_lock_c` (default 0) -- or unconditionally
  when some rungs already filled (do not waste the bought wings). Books the taker leg into the batch so
  the set completes; the ledger applies the venue taker fee to that (taker) bucket-NO leg.
- `unwind`: sell the pre-taken wings back IOC at the current bids, record the round-trip cost, and drop
  the (now-flat) batch + legs so the ledger never counts the unwound wings as held.

PARTIAL WING FILL (`core._pt_fail_closed`): a pre-emptive wing leg that did not fully fill (the ask moved
past our tight limit) FAILS CLOSED -- cancel the pre-hedged rests, unwind whatever wing filled, mark the
batch one-legged (the falsifier's risk count), and stand the hour down (`print_through_partial`). It never
retries and never places again this window. Simple and safe, as instructed.

Executor (`V33LiveExecutor`, behind `enable_print_through`, wired from `params.print_through`): the
pre-emptive take reuses the inherited `TAKE_WINGS` path unchanged. NEW handlers, chunked to `wing_cap` and
priority-paced: `TAKE_BUCKET_NO` (IOC bucket-NO taker buy for `complete`) and `UNWIND_WINGS` (IOC sells at
the bids for `unwind`/fail-closed). Both journal receipts (`print_through_complete` / `print_through_unwind`)
and reconcile the fill count (an alarm on a shortfall -- never silently naked). A WOULD_* twin reaching the
armed executor raises (P3-1). Two V3.3-only `ActionKind`s live in `service/v33/actions.py::V33ActionKind`
so the V3.2 `ActionKind` enum is untouched.

Dry mode: the driver's existing `_simulate_ladder_fills` books the crossing rung right after the pump, so
in DRY the pre-emptive `WOULD_TAKE_WINGS` fills via the `FrozenExecutor` and the rung fill attaches to the
same batch -- the trigger is simulated faithfully, sending nothing. The per-window `print_through`
summary rides the ledger row; the report scores it.

## Exactly what V3.2 does NOT do

Zero V3.2 behaviour change, provable by the unchanged V3.2 suite + goldens. No file under
`service/v32/` was touched. The mechanics live in the V3.3 fork (`service/v33/core.py`,
`service/v33/executor.py` as a subclass), the new action kinds in a separate `V33ActionKind` enum, and the
executor handlers behind `enable_print_through` (default OFF; enabled only by the V3.3 wiring). V3.2 has no
`print_through` param, no `PrintThroughTrigger`, no `TAKE_BUCKET_NO`/`UNWIND_WINGS`. Whether V3.2 should
get print-through via a Registration line is an Open Question for Brad (it is FROZEN; I did not touch it).

## Params + sha

`policy/v33_params.json` gained (all inert while `print_through` false):
`print_through` false, `print_through_ticks` 1, `print_through_slack_c` 0, `print_through_stall_ms` 1500,
`print_through_min_lock_c` 0, `print_through_policy` "complete_else_unwind".

- sha BEFORE (FLAP-R2): `20188bbe76b592198f2f3aa2f1b8ff8857b12cc6b5ad9f5d3f5d75f76030cc78`
- sha AFTER (PRINT-THROUGH): `2e60980762ea6531b707c1c0bc93d69577fd3257295238e63f122d53afdd995e`

`FROZEN_V33_PARAMS_SHA256` re-pinned; the prior sha kept as `PREVIOUS_V33_PARAMS_SHA256_FLAP_R2`. Loader
validates the new levers (fail-closed on negatives / an unknown policy string). The DRAFT falsifier
(`ceremony/v33_falsifier.md`) and `ops/V33_ARMING.md` sha references updated with a dated re-pin note.

## Tests + suite counts

New `tests/test_v33_print_through.py` (22 tests) + fixture `tests/fixtures/v33/print_through_sweeps.json`:
trigger fires only on a qualifying print toward the offer / not away / not when no rest is live / not when
off / not on a NO-side taker; fill-after-trigger attributes the batch once (no double take); a completed
set counts + books; stall -> complete when lock clears the floor; stall -> unwind otherwise; policy=unwind
forces unwind; partial wing -> fail-closed cancel+unwind+stand-down; stand-down blocks further triggers;
the summary shape; a 10:00Z-style FAST-sweep golden replay (pre-hedge + complete at the pre-jump ask); a
SLOW-sweep STALL golden replay (unwind); dry-mode faithful simulation; executor complete/unwind sends +
counters + twin refusal; report section builder + renderer.
Two pre-existing sha-pin tests updated for the re-pin (`test_v33_hardening`, and the doc sha in the
falsifier consumed by `test_v33_falsifier_pins`).

Full suite from `pilot/`: **1286 passed, 1 skipped** (1264 baseline + 22 new). With `print_through` false
the baseline is byte-identical.

## Scope / merge notes

`run_v33.py` edits: `build_executor_v33` passes `enable_print_through`; import `print_through_summary` +
`V33ActionKind`; `_journal_action` handles the 2 new kinds + WOULD_ twins; `_finalize` passes the
`print_through` summary to the ledger row; `_compute_v33_money` adds trigger/complete/unwind counters.
These are distinct from PR #93's recorder-deadline changes; merge should be clean.

## Known limitations / scoping decisions

- The `complete` stall books the taker bucket-NO into the batch at the current ask under the honest taker
  convention (fee applied); the executor sends the IOC and ALARMS on a fill shortfall. It does not feed a
  failed-complete back into the core. Armed-only, IOC-at-ask (marketable), rare.
- The `unwind` round-trip cost + the stall economics are carried on the `print_through` trigger summary
  (ledger row + report), not folded into `realized_delta` -- the main ledger P&L reflects the FILLED rungs
  as proper sets (a `complete` books through; an `unwind` drops the flat batch). This keeps the core ledger
  math untouched for stall edge cases while the reporter scores every trigger/stall/unwind/partial.
- A late fill in the tiny race between the stall cancel decision and its confirm is handled by the
  existing `cancel_ctx` path; cancel-first is the guard against double-fill, as instructed.

## Open questions for Brad

1. SHIP ON or keep OFF? The feature is built, tested and dry-simulatable but ships `print_through` false.
   To arm it for the DRY roster now: set `print_through: true` in `policy/v33_params.json` and re-pin the
   sha (I can do the re-pin on your word). Recommend a dry watch first (the report's PRINT-THROUGH block
   scores triggers vs the ladder scoreboard) before it rides an armed window.
2. `print_through_min_lock_c` for the `complete` branch: default 0. Completing via a bucket-NO TAKER at the
   ask is usually a NEGATIVE lock (costlier than the maker rung would have been), so at floor 0 a pure
   stall almost always UNWINDS. Do you want a negative floor (e.g. -$0.10, "complete unless deeply
   underwater") so a stalled pre-hedge salvages the bought wings rather than paying the round-trip? Or is
   unwind-by-default the right conservative choice?
3. `print_through_ticks` 1 and `print_through_slack_c` 0: fire within one tick, hedge at exactly the ask.
   Slack 0 maximises the edge but any 1-tick ask move between decision and venue -> partial -> fail-closed
   stand-down. Want a 1c slack (limit = ask + 1c) to trade a hair of edge for far fewer fail-closed hours?
4. Should V3.2 (frozen) get print-through via a dated Registration line, or does it retire as-is with V3.3
   taking over the armed slot (per the V3.3 plan Q4 flip)?
