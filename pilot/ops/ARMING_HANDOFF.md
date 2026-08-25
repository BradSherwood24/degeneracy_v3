# ARMING HANDOFF — 2026-08-22

> **STATUS UPDATE 2026-08-24 12:04Z: STOOD DOWN to shakedown on Brad's word**
> ("yea, stand down. we'll need to deep dive and fix some issues").
> Armed span: 2026-08-23 ~13:00Z → 2026-08-24 12:04Z. 9 fires, 1 completed pair
> (+$0.0007), 8 one-legged fills; real P&L −$0.98 (Kalshi settlements), balance
> $52.97, flat at stand-down. Defects found: (1) Kalshi NO-side fills report in
> YES-space → ledger/S1/S4/A1 mismeasure; (2) unmatched flatten losses never
> booked to realized_delta; (3) S1/S2 stops do not latch the day; (4) 1c legs
> unflattenable (A_FLATTEN_NO_BID). Strategy finding: one-legged fill is the
> DOMINANT live outcome (8/9). Deep-dive + instrument fixes before any re-arm.
> Consider ALLOW_ORDERS=false at the proxy (Brad's lever) while standing down.

> **STATUS UPDATE 2026-08-23 (~13:00Z): ARMED. Both steps below are COMPLETE.**
> Brad set ALLOW_ORDERS=true; proxy restarted, /health verified
> (orders_enabled:true, caps 2/order + prefixes + 100/day, 0 used);
> mode.txt = `armed` on Brad's verbatim go ("Flip it! We set sail!").
> First armed wake: next :40 UTC after ~13:00Z. Shakedown final tally: 43 full
> windows, 18 would-fires, all floors ≥ 0, +11.57¢, 0 pins. This document now
> serves as the operating reference (expectations, stops, P1 gate below).

The falsifier is FROZEN (S5-gate verified: `falsifier_is_frozen() == True`).
Everything on the software side is done. Two physical steps remain, both Brad's.

## Step 1 — enable orders at the proxy (Brad's hands only)

1. Edit `degeneracy-proxy\.env`: set `ALLOW_ORDERS=true`
2. Restart the proxy task (PowerShell):
   ```
   Stop-ScheduledTask  -TaskName DegeneracyProxy
   Start-ScheduledTask -TaskName DegeneracyProxy
   ```
3. Verify (should show `"orders_enabled": true` plus the caps):
   ```
   curl http://127.0.0.1:8642/health
   ```
   Caps that must be present: MAX_CONTRACTS_PER_ORDER=2, ticker prefixes
   KXBTC15M/KXBTCD, DAILY_ORDER_BUDGET=100. S5 refuses to arm if any is absent.

## Step 2 — arm the pilot

Edit `pilot\ops\mode.txt`: change `shakedown` → `armed` (single word, no quotes).
The task reads it fresh at every :40 UTC wake. To stand down at any time: change
it back to `shakedown` (takes effect next wake; mid-window a running process
finishes its window under the mode it started with).

## What happens on the first armed wake

:40 Phase A — policy sha check → **S5 gate** (frozen falsifier line, proxy
health/caps/orders_enabled) → reconcile-first (any existing position in our
series → journal + stand down) → balance check → leg discovery.
:45 Phase B — anchor poll → pairing → G/σ̂/quintile → live watch. A sub-$1
executable crossing fires a batched IOC pair (limit at ask), 1 contract each leg.
Any S5 failure degrades to DRY with a loud journal record — it never half-arms.

## Expectations for the first day (from 25 shakedown windows + train tape)

- Fire rate ~40–60% of windows; entries usually late-window; floors +0.0–0.9¢.
- Fills: the falsifier's whole purpose is to measure these. Kalshi's own app
  notifications are the real-time channel; the ledger + daily paired report are
  the record.
- Alarms that may fire and are NOT emergencies: A1 (>2¢ slippage visibility),
  A2 (ladder change — strangle stands down, sub-$1 continues).
- Stops that halt the day: S1 (one net-negative filled pair on actual fees),
  S2/S3 (imbalance/reconciliation), S4 ($5 daily loss). F8 as ruled: complete
  floor pairs ride to settlement; unprotected exposure flattens reduce-only.

## Promotion

P1 (→2 pairs): ≥10 fired, fill ≥60%, 0 unresolved imbalance, 0 S1, |slip| ≤1¢,
≥1 day. Computed from the ledger (`service.pilot_ledger`); Brad rules on the
report — the gate is a floor, never an auto-promote.

## Standing state at handoff

- Shakedown: 25 full windows, 12 would-fires, all settled floors ≥ 0 (+3.26¢).
- Parity: 23/23 decision agreement, F15 PASS (`pilot/build/parity_0821_22_combined.json`).
  18:00Z window re-comparable after the Aug-22 day refetch (metadata-vintage gap
  — see `historical-data/PARTIAL_DAY_2026-08-21.marker`).
- Tests: 374 pilot + 111 proxy green (pre-freeze; falsifier.md text change
  touches no code).
- Known ops items: proxy needs a manual `Start-ScheduledTask DegeneracyProxy`
  after any reboot (logon trigger needs an elevated shell, still pending);
  Aug-22 day files are partial — delete-and-refetch before any census use.
- Stale comment note: `service/stops.py` module docstring still says F8 is
  "PENDING-BRAD" — the ruling confirmed the built defaults, so behavior is
  correct; the comment is cosmetically stale (left untouched to keep zero code
  churn between review and arming).
- Next-rung materials (NOT in this pilot): `sim/out/sizing_exploration_2026-08-21.md`
  (cap-N knee 10–20, throttle dial, neighbor-level WS expansion incl. rank +1).
