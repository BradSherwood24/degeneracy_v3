# BOX_ARMING.md — the mechanical runbook for arming the wide box

Scope: how to take the box (`strategy=box`, roster `box-v1`) from a clean tree to live single-pair
taker firing, and how to stand it down or repair a jammed guard. The authority for WHAT is being
judged is `pilot/ceremony/box_falsifier.md`; this file is only the sequence of hands-on steps that
its checklist implies. House law is unchanged: `python` only, all Kalshi access through the proxy at
`127.0.0.1:8642`, never touch the key/.env/PEM, never read the sealed holdout.

The S5 gate (`stops.arming_check`, wired in `run_window.prepare`) is what actually enforces this. For
the box it checks, in one place, all of: the resolved strategy is exactly `box`; the BOX falsifier
(`ceremony/box_falsifier.md`, NOT the corridor's `falsifier.md`) has a line that is exactly
`STATUS: FROZEN`; the box roster sha is verified (`load_box_policy` self-checks the pinned sha and
refuses drift); `/health` shows `orders_enabled: true` with the caps block; no day-halting stop is
latched for today and the day-guard file is not corrupt. Any miss ⇒ the window runs **DRY** (orders
frozen) and journals `degrade_to_dry` with the reasons. You cannot arm past a failed gate by hand.

---

## A. Arm (do these in order)

1. **Tree.** Working tree on `main`, `git status` clean. The scheduled task checks out `main`; it does
   not run a tag or a branch. If the box code is on a branch, it must be merged to `main` first
   (Brad merges PRs; agents never push `main`).
2. **Tests.** `cd pilot && python -m pytest -q` → green. A red suite is a stop; do not arm.
3. **Health.** `curl 127.0.0.1:8642/health` → `orders_enabled: true` and a `caps` block carrying
   `max_contracts_per_order`, `ticker_prefixes`, `daily_order_budget`. Absent caps or
   `orders_enabled:false` ⇒ S5 refuses.
4. **Strategy lever.** `pilot/ops/strategy.txt` contains exactly `box` (one line, no trailing junk).
   An unknown/missing value fails closed to the corridor core, DRY.
5. **Shakedown proof.** Run at least **two** shakedown windows (`mode.txt` = `shakedown`, or
   `--mode shakedown`) and confirm in the journals: at least one `box_would_fire`, or a full window of
   `box_eval` records, AND **no** `strategy_invalid` and **no** `wake_error`. Shakedown/dry never
   place orders (`on_box_action` is a no-op unless armed), so this is safe.
6. **Freeze the falsifier.** On Brad's verbatim go, change the STATUS line of
   `pilot/ceremony/box_falsifier.md` from `STATUS: DRAFT` to exactly `STATUS: FROZEN`, and append the
   go + roster sha under Registration. **Only Brad authorises the freeze**; an agent never flips it.
   Confirm the roster sha in the file matches the pinned constant
   `480d46347c6d5e5b136d34df1555516cf1b3d3899b41611a2f0dafb786305eb3`
   (`python -c "import json,hashlib;o=json.load(open('pilot/policy/box_params.json'));print(hashlib.sha256(json.dumps(o,sort_keys=True,separators=(',',':')).encode()).hexdigest())"`).
7. **Arm.** Brad sets `pilot/ops/mode.txt` → `armed`. The next `:40` process reads mode.txt +
   strategy.txt fresh, loads the box roster, runs S5, and (if every gate passes) fires the single pair
   in the T−600s..T−60s window.

After the first armed window, read the journal: `arming` (`armed: true`, `falsifier_basename:
box_falsifier.md`, `strategy: box`), then `box_fire` → `box_post_fill`. A one-legged fire triggers
`box_flatten` (up to 3 event-driven attempts) and an `A5` alarm on the ledger row.

---

## B. Stand down

Set `pilot/ops/mode.txt` back to `shakedown` (or `dry`). Takes effect at the next `:40` process; a
window already running is unaffected (mode is read once per fresh process). Standing down does NOT
un-freeze the falsifier — the freeze line stays; only Brad edits it. To fully retire, follow the box
falsifier's R1–R4 verdict path.

---

## C. What a latched stop looks like, and clearing a corrupt guard

The day-halting stops (S1_box arithmetic, S3 reconciliation, S4 daily-loss-on-balance) latch into a
**day-scoped** guard file so the next `:40` process refuses to arm for the rest of that UTC day (a
stop halts the DAY). One file per UTC day:

```
pilot/ops/stops_YYYY-MM-DD.json
{
  "utc_day": "2026-08-26",
  "balance_start_dollars": "1234.56",          # S4 baseline: first clean-wake balance snapshot
  "latched": [                                  # empty list = nothing latched (guard present, clean)
    {"kind": "S4", "reason": "daily loss ...", "window": "2026-08-26T18:00:00Z", "ts": 1690000000.0}
  ]
}
```

- A **non-empty `latched`** ⇒ `arming` journals `stop_latched: S4` (etc.) and the window degrades to
  DRY. This is intended: the day is halted. It clears on its own at the next UTC day (the path is
  day-scoped, so a new day gets a fresh file).
- A **corrupt / unreadable / wrong-day / wrong-shape** guard file fails **closed**: S5 treats it as
  "cannot confirm no latch" and refuses to arm, and nothing self-heals it. The window journals
  `day_guard_corrupt: true`. It will keep refusing every wake that UTC day until a human repairs it.

**Clearing a corrupt guard by hand** (Brad, deliberate):
1. Inspect `pilot/ops/stops_YYYY-MM-DD.json`. If a real stop is latched, do **not** clear it — the
   day is meant to be halted; investigate first.
2. If the file is merely corrupt (bad JSON / truncated) and you have confirmed no genuine stop fired
   today (read the day's journals + ledger rows), either delete the file (a missing file = fresh
   empty guard, not corrupt) or rewrite it with the correct shape above and `"latched": []`, the
   correct `utc_day`, and the true `balance_start_dollars` (or omit it as `null` to re-snapshot at the
   next clean wake). Keep `sort_keys` order irrelevant — the reader validates shape, not key order.
3. Re-run one shakedown window and confirm `day_guard_corrupt` is gone before re-arming.

Never delete a guard that carries a real latch to "get past" a halt — that is the one move the whole
day-scoped design exists to prevent.
