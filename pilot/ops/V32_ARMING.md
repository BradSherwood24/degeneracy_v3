# V32_ARMING.md -- the mechanical runbook for arming the V3.2 pump-fader

Scope: how to take V3.2 (`DegeneracyV3_2`, roster `DegeneracyV3_2`, the continuous-requote spot-bucket
pump-fader) from a clean tree to live maker resting + taker completion, and how to stand it down or
repair a jammed guard. The authority for WHAT is being judged is `pilot/ceremony/v32_falsifier.md`;
this file is only the sequence of hands-on steps its checklist implies. House law is unchanged: `python`
only (never `python3`/`py` on this box), all Kalshi access through the proxy at `127.0.0.1:8642`, never
touch the key/.env/PEM, never read the sealed holdout. V3.2 and v1.1 (the box) are SEPARATE tasks,
SEPARATE mode files, SEPARATE day-guard files -- arming one never touches the other.

The S5 gate (`service.v32.stops.v32_arming_check` + `decide_v32_arming`, wired in `service.run_v32`) is
what actually enforces this. For V3.2 it checks, in one place: this file's falsifier
(`ceremony/v32_falsifier.md`) carries a line exactly `STATUS: FROZEN`; the params sha is verified
(`load_v32_params` self-checks `FROZEN_V32_PARAMS_SHA256`); proxy `/health` shows `orders_enabled: true`
with caps that allow `params.contracts` (1) and no more than 2 per order, cover BOTH `KXBTC-` (buckets)
and `KXBTCD-` (strikes) via startswith, and leave >= 200 creates in today's budget; reconcile-first sees
no inherited un-settled KXBTC* position; the SEPARATE v32 day guard is neither corrupt nor latched; and
the banded S4 is not `latch`/`pending`. Any miss => the window runs DRY (orders frozen) and journals
`degrade_to_dry` with the reasons. You cannot arm past a failed gate by hand.

---

## A. Arm (do these in order)

1. **Proxy prefixes.** Brad edits `degeneracy-proxy/.env`: `ORDER_TICKER_PREFIXES` must include `KXBTC`
   (one prefix; `KXBTC` covers both `KXBTC-` buckets and `KXBTCD-` strikes, since the proxy allows a
   ticker T iff `T.startswith(prefix)`). Never touch the key/PEM.
2. **Order budget.** In the same `.env`, `DAILY_ORDER_BUDGET` >= 4000 (the requote policy issues ~1,900
   creates/day; 2x margin). Amend is not used (cancel+create only).
3. **Restart + health.** Brad restarts the proxy, then:
   ```
   Invoke-RestMethod http://127.0.0.1:8642/health | ConvertTo-Json -Depth 5
   ```
   Confirm `orders_enabled = true`; a `caps` block with `max_contracts_per_order` (in [1,2]),
   `ticker_prefixes` (covering `KXBTC`), `daily_order_budget`; and `orders_remaining_today` >= 200.
4. **Tree + tests.** Working tree on `main` (V3.2 phases merged), `git status` clean. Then:
   ```
   cd pilot
   python -m pytest -q
   ```
   Green. A red suite is a stop; do not arm.
5. **Dry proof.** Run at least TWO dry windows and confirm the journal carries `would_place_rest` +
   shadow records and NO discovery errors (see `ops/V32_DRY_RUN.md`). Dry sends no orders, so it is
   safe:
   ```
   python -m service.run_v32 --mode dry
   python -m service.v32.report --days 1
   ```
   The report's FALSIFIER SCOREBOARD will show `n<30 pending` (no live sets yet), the dry-run shadow
   locks, and the `capture ratio = live X / shadow Y = Z%` line (MEASUREMENT CLARIFICATION 3) -- that is
   expected.
6. **Confirm the roster sha** matches the pinned constant
   `c6715fc7fd8339e0cc8877bd39bb78b04239eda9c490bde71a53333a48bdfb92`:
   ```
   python -c "import json,hashlib; o=json.load(open('policy/v32_params.json')); print(hashlib.sha256(json.dumps(o,sort_keys=True,separators=(',',':')).encode()).hexdigest())"
   ```
7. **Freeze the falsifier.** On Brad's verbatim go, change the STATUS line of
   `pilot/ceremony/v32_falsifier.md` from `STATUS: DRAFT` to exactly `STATUS: FROZEN`, and append the go
   + roster sha under Registration. **Only Brad authorises the freeze**; an agent never flips it.
8. **Arm.** Brad sets `pilot/ops/v32_mode.txt` to exactly `armed` (one line):
   ```
   Set-Content -Path ops\v32_mode.txt -Value "armed" -NoNewline -Encoding ascii
   ```
   The next `:40` process reads `v32_mode.txt` + params fresh, loads the roster, runs S5 + reconcile +
   S4, and (if every gate passes) quotes + completes one set in the T-15..T-5 window.

After the first armed window, read the journal: `arming` (`armed: true`), then `place_rest` ->
`order_ack` -> (on fill) `take_wings` -> `wing_fill`, and the ledger row's money-math slots. See section
D for exactly where each MUST-CONFIRM item shows up.

---

## B. Stand down

Set `pilot/ops/v32_mode.txt` back to `dry` (or `shakedown`):
```
Set-Content -Path ops\v32_mode.txt -Value "dry" -NoNewline -Encoding ascii
```
Takes effect at the next `:40` process (mode is read once per fresh process; a window already running is
unaffected). The running process cancels its own resting order at the quote end (T-5) with a DELETE (the
primary path); the order also carries an `expiration_time` set EXPIRATION_GRACE_S (60 s) PAST the quote
end (T-4) as a crash backstop, so standing down mid-window leaves nothing resting past the window either
way. Standing down does NOT un-freeze the falsifier -- only Brad edits that line.

**Cancel a stray resting order by hand** (only if you have confirmed one is genuinely still resting --
e.g. a crashed process before its `expiration_time`; normally the venue auto-expires it just after the
quote end, at T-4). Find
the order id from the journal (`place_rest`/`order_ack`) or from the proxy, then:
```
Invoke-RestMethod -Method Delete http://127.0.0.1:8642/trade-api/v2/portfolio/events/orders/<id>
```
(This is the cancel path the docs pin: `DELETE /trade-api/v2/portfolio/events/orders/{order_id}`; the
reply carries `reduced_by` = the count pulled off the book.) Never place an order by hand.

---

## C. What a latched stop looks like, and clearing a corrupt guard

The day-halting stops latch into a **day-scoped** guard file SEPARATE from the box's, so a V3.2 stop
never halts the box and vice versa: `pilot/ops/v32_stops_YYYY-MM-DD.json`. One file per UTC day:

```
pilot/ops/v32_stops_2026-09-13.json
{
  "utc_day": "2026-09-13",
  "balance_start_dollars": "1234.56",          # S4 baseline: first clean-wake balance snapshot
  "latched": [                                  # empty list = nothing latched (guard present, clean)
    {"kind": "S1_LEGGED", "reason": "...", "window": "2026-09-13T18:00:00Z", "ts": 1690000000.0}
  ]
}
```

- A day-halting latch => `arming` degrades to DRY for the rest of the UTC day. S4 latches on a real
  balance loss (banded, see the falsifier); S1_LEGGED latches the DAY only after **2** one-legged-below-
  floor occurrences (one occurrence stands only the HOUR down). The guard clears on its own at the next
  UTC day (the path is day-scoped).
- A corrupt / unreadable / wrong-day / wrong-shape guard fails **closed**: S5 treats it as "cannot
  confirm no latch" and refuses to arm; the window journals `day_guard_corrupt`. It keeps refusing every
  wake that UTC day until a human repairs it.

**Manual repair of a corrupt guard** (Brad, deliberate):
1. Inspect `pilot/ops/v32_stops_YYYY-MM-DD.json`. If a REAL stop is latched, do NOT clear it -- the day
   is meant to be halted; investigate the day's journals + ledger rows first.
2. If it is merely corrupt (bad JSON / truncated) and you have confirmed no genuine stop fired today,
   either DELETE the file (a missing file = a fresh empty guard, not corrupt) or rewrite it with the
   shape above, `"latched": []`, the correct `utc_day`, and the true `balance_start_dollars` (or omit it
   as `null` to re-snapshot at the next clean wake). Key order is irrelevant; the reader validates shape.
3. Re-run one dry window and confirm `day_guard_corrupt` is gone before re-arming.

Never delete a guard that carries a real latch to "get past" a halt -- that is the one move the whole
day-scoped design exists to prevent.

---

## D. What to watch in the first armed window (the MUST CONFIRM list -> where it shows up)

The falsifier's "FIRST ARMED WINDOW MUST CONFIRM" list, and where each item appears:

1. **`expiration_time` honored (rest gone by the quote end).** `place_rest` record carries
   `expiration_time` (Unix seconds = EXPIRATION_GRACE_S (60 s) past the quote end, i.e. T-4 — the crash
   backstop). The running process's quote-end DELETE pulls the rest at T-5 (the primary path); after the
   backstop (T-4) the venue reports no resting order regardless (poll/status shows the rest gone). If a
   rest lingers past T-4, the field name is wrong at the venue -- STOP.
2. **get-order fp fields present.** The order-status poll/cancel-confirm records carry `fill_count_fp`,
   `remaining_count_fp`, `initial_count_fp` and a `status` the confirm treats as terminal
   (`status not in ("resting", None)`). If a filled order still parses `filled=0`, the fp fields are
   missing -- STOP.
3. **cancel reply `reduced_by`.** Every `cancel_rest` record carries the DELETE reply's `reduced_by`;
   on a real cancel race, `placed - reduced_by` equals the WS fill count.
4. **reconcile-first zero sizes.** The `arming` record shows the previous hour's SETTLED positions at
   size 0 / absent (a fresh window is not blocked by a stale settled row).
5. **fill channel delivering.** A rest fill arrives on the `fill` WS channel (and is corroborated by the
   1-s status poll); `take_wings` fires immediately after.
6. **exec price == resting price.** `wing_fill` / rest-fill records show the executed price equal to the
   resting/decided price within the wing margin; the `exec_price_mismatch` alarm (A_EXEC_PRICE) stays
   quiet in the ledger row's `exec_price_mismatches` (empty).
7. **invariant phantom handled without stand-down (counter > 0 acceptable; violations counter stays 0).**
   The pre-PLACE venue-truth invariant reads `/portfolio/orders?status=resting`, which lags the matching
   engine by up to ~1 s (2026-09-15 18:00Z incident). A `rest_invariant_phantom` record (a just-confirmed
   cancel still on the LIST, or a stray whose cancel 404s with a terminal/not-found status) is EXPECTED
   read-path lag and does NOT stand the hour down: PLACE proceeds. `rest_invariant_phantoms` > 0 in the
   ledger row is acceptable; a `rest_invariant_recheck` (one re-read after 0.5 s) is normal. Only
   `rest_invariant_violations` > 0 (a stray still genuinely resting after the re-read) is a real event --
   it cancels + alarms + stands the hour down, and that counter must stay 0 in a clean window.
8. **amend-first replace lands (Brad 2026-09-15).** The FIRST `amend_confirmed` record carries a 2xx with
   the SAME `order_id` as the pre-amend rest persisted and `remaining_count` 1 (an amend that did NOT
   cross); the ledger row's `amends_confirmed` climbs with `replaces` and `amends_failed` stays low. The
   cancel+create FALLBACK path is exercised at least once in the tests (`test_v32_amend.py`
   `test_amend_failure_falls_back_to_cancel_create`) so an amend outage degrades to the proven sequential
   cancel -> confirm -> create, never to a naked or doubled rest. If `amends_failed` spikes or an
   `amend_confirmed` shows a CHANGED `order_id`, the amend semantics are wrong at the venue -- STOP.

9. **capture ratio printed on the scoreboard (MEASUREMENT CLARIFICATION 3, Brad 2026-09-19).** The
   FALSIFIER SCOREBOARD block of `python -m service.v32.report` prints a line
   `capture ratio = live X / shadow Y = Z%  (>= 50% [pin] Registration 3)` -- (live completed sets) /
   (ideal-shadow E=0.10 fills inside the T-15..T-5 quoting window), both over armed windows carrying a
   spot bucket. At n >= 30 the VERDICT gates on `capture ratio >= 50%` IN PLACE OF the old
   `fill rate >= 2.0/day`; the fill rate is still printed but labelled "(info, superseded as a gate by
   Registration 3)". Confirm both lines are present.

Also watch: no `A_REPLACE` (replaces/min under 60 -- an amend counts as a replace), no `A_STALE` bursts
(strike/bucket data-age under 1.0 s), and the ledger row's `realized_lock` positive and near the shadow
E=0.10 lock.

---

## E. Budget math (creates/day vs DAILY_ORDER_BUDGET)

- With amend-first (Brad 2026-09-15) a requote is normally ONE POST (the amend; budgeted like a create)
  with NO cancel — cheaper on the wire than the old cancel(DELETE)+create(POST) pair. Only the FALLBACK
  (an amend that fails) spends a DELETE (cancel; uncapped, unbudgeted) + a POST (create; budgeted). At the
  chosen gate (tol 2c, deb 5000 ms) the sim measures ~77 replaces/hour over the 10-minute quoting window,
  plus the completion batch. Across ~24 armed hours that is ~1,860 amend/create POSTs/day; a completion is
  2 more per set. NOTE the proxy does not yet contract-cap or ticker-check the amend endpoint (see
  `pilot/ops/proxy_amend_cap.md`) — apply that proxy change before arming leans on amends at volume.
- `DAILY_ORDER_BUDGET` must be >= 4000 (2x margin) before arming. Each armed window's S5 refuses to arm
  unless `orders_remaining_today` >= 200 (`V32_MIN_ORDER_BUDGET_AT_ARM`) at :40 -- i.e. one hour needs
  headroom of at least 200 creates. If a day's requoting runs the budget down toward 200, later windows
  self-degrade to dry (fail-closed) rather than error mid-window.
- Exchange write rate at ~77 replaces/window is ~0.15/s -- far under Kalshi limits. If the live
  replaces/hour (SO-2 in the report) runs far above 77, raise `DAILY_ORDER_BUDGET` accordingly and
  investigate a noisier book or a gate bug before the next arm.

---

> `pilot/ops/v32_mode.txt` is a machine-local lever (git-ignored) so flipping it never dirties the tree
> the scheduled task checks out. Keep it present with exactly `shakedown` | `dry` | `armed`; a missing or
> invalid value resolves to `shakedown` (fail closed -- the no-orders rung). `armed` additionally
> requires the frozen falsifier (S5), so a stray `armed` with a DRAFT falsifier still runs dry.
