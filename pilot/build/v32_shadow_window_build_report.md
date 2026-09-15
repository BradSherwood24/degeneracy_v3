# V3.2 build — gate the shadow fill on the live quoting window T-15..T-5 (branch `fix/shadow-quote-window`)

Builder: Opus 4.8 (Fable-delegated). Base `origin/main` 25876c7. Worktree
`C:\Users\Brads\Python_stuff\dv3_wt_v11`. No proxy, no socket, no sealed/holdout date read. The live
ledger and journals were read **read-only** for the re-count below.

## The measurement bug

The live path quotes only inside the window `params.quote_start_s >= t_to_close >= params.quote_end_s`
(T-15..T-5, i.e. `t_to_close` in [300, 900] s); outside it `_requote` cancels the rest
(`no_quote_reason = "past_quote_end"` at T-5, warmup-only before T-15). The SHADOW (the ideal no-lag
rule, E in {0.08, 0.10, 0.12}) was **not** gated by the window: `_recompute_context` solves every
shadow `n` whenever W and cap exist, and `_shadow_on_trade` recorded a shadow fill on any spot-bucket
YES print above `1 - n_shadow` — including prints BEFORE T-15 (bucket books connect ~T-20) and AFTER
T-5 (the live rest is already cancelled). Such a fill is one the live path could **never** have taken,
so it inflates shadow-vs-live fill counts and biases the pre-registered shadow observations.

### Evidence (read-only, live ledger + journals)

Close `2026-09-15T04:00:00Z`: the ledger's E=0.10 shadow fill is `print=0.9000, count=1.00,
offer=0.88`. The matching spot-bucket YES print in
`pilot/journals_v32/20260915T040000Z.jsonl.gz` is a 1-lot trade at `0.9000` on
`KXBTC-26SEP1500-B77650` (the quoted spot bucket, Sd 77600) at server time
**2026-09-15T03:55:51.2Z**, i.e. `t_to_close ≈ 248.8 s` (**T-4:09**) — past the T-5 quote end. The
live path had already cancelled its rest at T-5 by design, so live could never have taken it.

## The change (fill gate only)

1. **`pilot/service/v32/core.py` — `_shadow_on_trade`**: before recording a shadow fill, evaluate the
   trade against the **same** window the live path uses, on the trade's own clock:
   `t_to_close = st.close_epoch - event.server_ts`;
   `in_window = params.quote_start_s >= t_to_close >= params.quote_end_s`. A qualifying print outside
   the window does **not** fill the shadow (the sub is left unfilled, so a later in-window print can
   still fill it) and instead appends one observability-only action per E,
   `SHADOW_FILL_OUTSIDE_WINDOW`, carrying `shadow_E`, `offer`, `print_price`, `count`, `t_to_close`.
   `_recompute_context`'s shadow-`n` solving is **unchanged** (harmless, as instructed).
   `_shadow_complete` is **untouched**: a fill recorded inside the window still completes after T-5.

2. **`pilot/service/v32/actions.py`**: new order-free `ActionKind.SHADOW_FILL_OUTSIDE_WINDOW` (no
   WOULD_* twin — it never touches an order path; both executors return `[]` for it, and the
   FrozenExecutor's `_REAL_KINDS` refusal does not include it). Added informational fields
   `shadow_E / offer / print_price / t_to_close` to `V32Action`.

3. **`pilot/service/run_v32.py` — `_journal_action`**: new branch journals a distinct
   `shadow_fill_outside_window` record `{E, offer, print, count, t_to_close}`; the generic tail
   (`self.counts[rk] += 1`) tallies it under `shadow_fill_outside_window`, following the existing
   stand_down/place_rest action-journaling pattern.

4. **`pilot/service/v32/ledger.py` — `build_v32_ledger_row`**: additive counter
   `"shadow_fills_outside_window": int(driver_counts.get("shadow_fill_outside_window", 0))` —
   `.get`/default 0 so older rows still parse (PR #46/#50 pattern).

No change to `pilot/policy/v32_params.json`, `pilot/ceremony/v32_falsifier.md`, or the falsifier pins.

## Replay lab (requirement 4)

`sim/v32_replay/models.py` `IdealModel` is **already window-gated at the driver**, so no change was
needed: `sim/v32_replay/replay.py` `_in_window(t) = quote_end_s <= ttc <= quote_start_s` (300..900),
and both `_on_book_tick` (line ~230) and `_on_trade` (line ~318) `return` early when
`not self._in_window(...)`. So `IdealModel.on_tick`/`on_trade` never see an out-of-window frame — its
own `on_trade` has no gate, but it is never fed one. Confirmed, no lab edit.

## Tests

`pilot/tests/test_v32_core.py` — four new pure-core tests (existing golden-fixture style, `T=1_000_000`
close epoch, `_fresh_books`):

- `test_shadow_print_after_t5_is_suppressed_not_filled` — a spot-bucket YES print at `t_to_close ≈
  249 s` (after T-5) does NOT fill and emits `SHADOW_FILL_OUTSIDE_WINDOW` (offer 0.55, print 0.56,
  `t_to_close < 300`).
- `test_shadow_print_in_window_fills_and_emits_no_suppressed_record` — the same print at
  `t_to_close = 600 s` DOES fill and emits no suppressed record.
- `test_shadow_print_before_t15_is_suppressed_not_filled` — a print at `t_to_close ≈ 1000 s` (before
  T-15) does NOT fill and emits the suppressed record (`t_to_close > 900`).
- `test_shadow_completion_after_t5_for_in_window_fill_still_completes` — an in-window fill whose wings
  are stale at the fill instant defers completion, and a fresh strike book after T-5 (`t_to_close
  295 s`) still completes the lock (0.1036).

`python -m pytest pilot/tests -q` (from the worktree root): **859 passed**, 0 failed, 0 skipped
(855 prior + 4 new). `test_v32_falsifier_pins.py`: 7 passed, untouched.

## Re-count of the 5 live shadow fills on 2026-09-15 (read-only)

The live ledger cannot be recomputed from a read-only position, but each shadow fill's `print`/`count`
was matched to the spot-bucket YES trade in the journals to recover its `t_to_close` (window = [300,
900] s). The shadow fills once per E on the FIRST qualifying print, so the filling trade is the
earliest exact-price match on a quoted spot bucket; in every case that earliest match lies on a bucket
the live path actually quoted (`spot_buckets_quoted`), which is the disambiguator.

| close (Z) | E=0.10 print / count | filling trade bucket | t_to_close | in window? |
|-----------|----------------------|----------------------|-----------:|:----------:|
| 00:00 | 0.69 / 1 | B78250 (Sd 78200, quoted) | 592.8 s | IN |
| 04:00 | 0.90 / 1 | B77650 (Sd 77600, quoted) | 248.8 s | **OUT** |
| 14:00 | 0.34 / 80 | B76150 (Sd 76100, quoted) | 878.8 s | IN |
| 15:00 | 0.19 / 1 | B75850 (Sd 75800, quoted) | 888.6 s | IN |
| 17:00 | 0.69 / 1 | B76450 (Sd 76400, quoted) | 329.3 s | IN |

**Determination:** exactly one of the five shadow fills — **04:00Z** — would have been suppressed by
the window gate (all three E's for 04:00 fill on the 03:55:51Z prints, `t_to_close` 172.9–248.8 s, all
past T-5). The other four fill on their first qualifying in-window print (04:00 confirmed OUT per
requirement). So the corrected 2026-09-15 shadow-fill count is 4 (was 5); live remains 2.

**Uncertainty:** the re-count relies on matching the recorded `print`/`count`/`offer` to a journal
trade and on the once-per-E first-qualifier rule; it is not a full shadow replay (n-solve, per-tick
spot selection, freshness). The 04:00Z OUT result is unambiguous (every matching trade is past T-5).
For 00:00 and 17:00 there are also later same-price matches that fall OUT (254.6 s and 135.0 s), but
those post-date the earlier in-window fill and cannot re-fill an already-filled shadow, so the fills
are IN.
