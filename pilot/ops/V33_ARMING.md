# V33_ARMING.md -- the mechanical runbook for arming the V3.3 rolling-ladder pump-fader

Scope: how to take V3.3 (`DegeneracyV3_3`, roster `DegeneracyV3_3`, the rolling K-rung ladder) from DRY
side-by-side to LIVE, the FLIP that arms V3.3 and stands V3.2 down in one window, the MUST CONFIRM list
for the first armed windows, and the rollback. The authority for WHAT is being judged is
`pilot/ceremony/v33_falsifier.md`; this file is only the sequence of hands-on steps. House law is
unchanged: `python` only (never `python3`/`py`), all Kalshi access through the proxy at `127.0.0.1:8642`,
never touch the key/.env/PEM, never read the sealed holdout, NEVER between :38 and :59. V3.2, V3.3 and
v1.1 (the box) are SEPARATE tasks, SEPARATE mode files, SEPARATE day-guard files.

The S5 gate (`service.v33.stops.v33_arming_check` + `decide_v33_arming`, wired in `service.run_v33`) is
what actually enforces arming. For V3.3 it checks, in one place: the falsifier
(`ceremony/v33_falsifier.md`) carries a line exactly `STATUS: FROZEN`; the params sha is verified
(`load_v33_params` self-checks `FROZEN_V33_PARAMS_SHA256` =
`c18197d012bea8251982e4fdb948bf85846a453a9873fbd8007a7df9639f36f3`); proxy `/health` shows
`orders_enabled: true` with caps that allow `max_contracts_per_order` in `[lots_per_rung, K*lots_per_rung]`
= `[1, 11]` (so a proxy cap of **2** OR **11** both arm -- the wings chunk to <= cap), cover BOTH `KXBTC-`
and `KXBTCD-`, and leave >= 500 creates in today's budget; reconcile-first sees no inherited un-settled
KXBTC* position; the SEPARATE v33 day guard is neither corrupt nor latched; and the banded S4 is not
`latch`/`pending`. A BELT (R2-N2): an armed window whose `/health` contract cap is unreadable degrades to
dry rather than sizing wings against a guess. Any miss => the window runs DRY and journals `degrade_to_dry`.

---

## A. Prerequisites (all must hold before the flip)

1. **Falsifier FROZEN.** On Brad's verbatim go, Brad ALONE changes the STATUS line of
   `pilot/ceremony/v33_falsifier.md` from `STATUS: DRAFT -- NOT FROZEN` to exactly `STATUS: FROZEN` and
   appends the go + roster sha under Registration. An agent NEVER flips it.
2. **Proxy levers (Brad's `.env` + restart).**
   - `ORDER_TICKER_PREFIXES` includes `KXBTC` (covers `KXBTC-` buckets and `KXBTCD-` strikes).
   - `DAILY_ORDER_BUDGET` -> **8000** (the roll issues ~200 orders/window + K per bucket change; PLAN_V33
     Q6/step 1).
   - The **amend cap** (`ops/proxy_amend_cap.md`): until applied, the roll runs cancel->create per order
     (the executor's default fallback -- correct, just more orders); applying it flips the roll to
     amend-first with NO code change.
   - `MAX_CONTRACTS_PER_ORDER`: **2** (wings go as `ceil(K/2) = 6` IOC chunks per side, one lot per rung
     guard intact) OR **11** (each wing goes as 1 order, fewer writes, but it lifts the one-lot-per-rung
     guard -- weigh it). BOTH values arm; the executor's `wing_cap` = `min(params
     max_contracts_per_order_hint = 11, the live /health cap)`, read at window start.
   - Restart, then confirm:
     ```
     Invoke-RestMethod http://127.0.0.1:8642/health | ConvertTo-Json -Depth 5
     ```
     `orders_enabled = true`; caps `max_contracts_per_order` (2 or 11), `ticker_prefixes` (covering
     `KXBTC`), `daily_order_budget` 8000; `orders_remaining_today` >= 500.
3. **Task registered.** `DegeneracyV3_3` runs (via `ops/register_supervisor_tasks.ps1 -WithV33`, Brad's
   hand). It has been running DRY.
4. **>= 2 dry days reviewed.** `python -m service.v33.report --days 3` -- the SIDE-BY-SIDE tracked V3.2,
   the LADDER SCOREBOARD's DRY section shows the ladder's dry_sim per-rung locks, one-order rolls
   (single-order ratio ~100%), and the DEEP END (SO-3) block is populating. "V3.3 is running exactly as
   expected" (Brad's words). Confirm the dry journals sent NOTHING (only `would_*`/`dry_sim_fill`).
5. **Clean tree + green suite.** `cd pilot && python -m pytest -q` green; the V3.3 params sha:
   ```
   python -c "import json,hashlib; o=json.load(open('policy/v33_params.json')); print(hashlib.sha256(json.dumps(o,sort_keys=True,separators=(',',':')).encode()).hexdigest())"
   ```
   must print `c18197d012bea8251982e4fdb948bf85846a453a9873fbd8007a7df9639f36f3` and equal
   `service.v33.params.FROZEN_V33_PARAMS_SHA256`.

An OPTIONAL smaller first armed step is `rungs` 3 (~$6 in flight) -- a params amendment (its own dated
Registration entry + a new pinned sha), not a mode flip.

---

## B. The flip (Brad's hands only, in a :02-:33 UTC window; NEVER between :38 and :59)

Arm V3.3 and stand V3.2 down IN THE SAME WINDOW so only ONE roster is armed per bucket (Q4 refined,
Brad 2026-09-23: "flip V3.3 to contracts 10 and V3.2 to 0"):

```
Set-Content -Path ops\v33_mode.txt -Value "armed" -NoNewline -Encoding ascii   # (or the DV3_DATA_DIR copy)
Set-Content -Path ops\v32_mode.txt -Value "dry"   -NoNewline -Encoding ascii   # V3.2 stops placing
```

The next `:40` V3.3 process reads `v33_mode.txt` + params fresh, runs S5 + reconcile + S4, and (if every
gate passes) rests the K-rung ladder in the T-15..T-5 window. V3.2 keeps running and REPORTING with no
orders, so the side-by-side continues in the other direction. When Brad flips V3.2 to dry he ALSO appends
its falsifier's Q4 close line (the TEMPLATE in section D below) -- that is the ONE edit to
`v32_falsifier.md`, done at the flip, by Brad's hand.

---

## C. FIRST ARMED WINDOWS -- MUST CONFIRM (mirrors the falsifier's list)

Read the V3.3 journal + `python -m service.v33.report --days 1`:

1. **K orders accepted.** The first placement lays up to K = 11 `place_rest` records the proxy returns
   201 for (the K-aware pre-place invariant never flags the healthy ladder; `rest_invariant_violations`
   stays 0). A stray v33-* order that is otherwise-consistent is CANCELLED + alarmed (`stray_cancel`)
   and the ladder proceeds (NOT a whole-window stand-down); only an overflow/dup stands the hour down.
2. **One-order rolls.** A 1c W move issues exactly ONE `amend_rest` (or one cancel+create fallback); the
   other K-1 rungs keep `order_id`. The ledger's single-order-roll ratio stays >= 90% [pin].
3. **Coalesced / chunked wings sized to fills.** A full sweep takes ONE wing pair sized to the total,
   chunked into `ceil(count/cap)` IOC orders; no rung left naked; count taken == count filled.
4. **Hand-reconcile the first full sweep** against `GET /portfolio/fills`: the per-rung `realized_lock`
   agrees with the venue fills (the falsifier reads CORE state, not the money-math; confirm by hand once).
5. **Pacer never 429'd.** No `rate_limited` records under normal latency; `write_paced` waits are fine
   (the Basic-tier bucket working); the priority cancel/wing burst always finds `write_reserve_tokens`
   (30) headroom. A `rate_limited` record means a 429 WAS retried once (not counted as a reject).
6. **Per-rung bucket correct across a change.** A rest-and-fill spanning a mid-window bucket change lands
   the held bucket-NO leg on the RIGHT market (`RungFill.bucket_ticker`); the batched order poll covers
   BOTH buckets (R2-N4) so a prior-bucket rung's fill is never missed.

Also watch: no `A_REPLACE` (rolls/min under 120), no `A_STALE` bursts, S4 not tripping on a complete-but-
unsettled ladder (the banded, count-aware S4 nets the guaranteed floor).

---

## D. V3.2 falsifier Q4 CLOSE LINE -- a TEMPLATE, appended ONLY at the flip, by Brad

Do NOT append this to `pilot/ceremony/v32_falsifier.md` now. It is appended under V3.2's `## Registration`
by the arming step, in the same :02-:33 window V3.2 flips to dry, by Brad's hand -- filling in the live
`<n>`, `<record>` and Brad's dated words:

```
- 2026-09-<DD> ~<HH:MM>Z -- Q4 CLOSE (V3.2 superseded by V3.3; V3.2 continues in DRY). Brad, verbatim
  (2026-09-22 ~22:00Z: "Lets keep V3.2 running during this build out, so n=>12. Hopefully capture
  another"; 2026-09-23 ~00:20Z: "I'd like to run it along side V3.2 without it trading, then flip V3.3 to
  contracts 10 and V3.2 to 0. Just to watch and compare. Make sure V3.3 is running exactly as expected
  before $20+ are on the line"). At the flip, `ops/v32_mode.txt` -> dry and `ops/v33_mode.txt` -> armed in
  one :02-:33 window; V3.2 keeps running + reporting with NO orders. V3.2's record at close: n=<n> live
  completed sets, <record> (e.g. "11/11 positive, real -$0.98 net across the armed campaign"); the n>=30
  verdict is NOT reached and is SUPERSEDED by V3.3 (different mechanics -- V3.3's 10c rung is NOT pooled
  into V3.2's n). No [pin], the params sha, or -- other than the roster now running dry -- the STATUS line
  is touched by this entry; it is a Registration record of the supersession, not a threshold change.
```

---

## E. Rollback (V3.3 -> dry, V3.2 -> armed), Brad's hands, in a :02-:33 window

If the first armed windows show a defect, reverse the flip in one window:
```
Set-Content -Path ops\v33_mode.txt -Value "dry"   -NoNewline -Encoding ascii
Set-Content -Path ops\v32_mode.txt -Value "armed" -NoNewline -Encoding ascii
```
The running V3.3 process cancels its own rests at the quote end (T-5); each rung also carries an
`expiration_time` at T-4 as the crash backstop, so nothing rests past the window. Rolling back does NOT
un-freeze the V3.3 falsifier. If V3.2's Q4 close line was already appended, that Registration record
STANDS (add-only law) -- V3.2 simply resumes arming; note the resumption in a follow-up Registration line
if the campaign continues.

---

## F. Standing V3.3 down (without touching V3.2)

Set `pilot/ops/v33_mode.txt` back to `dry`:
```
Set-Content -Path ops\v33_mode.txt -Value "dry" -NoNewline -Encoding ascii
```
Takes effect at the next `:40` process. A corrupt/latched v33 day guard (`ops/v33_stops_<day>.json`) is
repaired exactly as V3.2's (see V32_ARMING.md section C) -- it is a SEPARATE file; repairing one never
touches the other.

---

> `pilot/ops/v33_mode.txt` is a machine-local lever (git-ignored). Keep it present with exactly
> `shakedown` | `dry` | `armed`; a MISSING or invalid value resolves to `dry` (fail closed -- V3.3 never
> arms by default). `armed` additionally requires the frozen falsifier (S5), so a stray `armed` with a
> DRAFT falsifier still runs dry.
