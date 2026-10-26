# V3.3 ETH LIVE (armable) — runbook

Status: **SHIPPED DRY-SAFE. Does NOT arm until Brad acts (levers d + e below).** This is the ETH ladder
running LIVE-capable on the Render worker ALONGSIDE the armed BTC ladder, through the SAME `run_v33` armed
spine (`service.run_v33_eth_live` -> `run_v33.main(spec=ETH_LIVE_SPEC)`). There is no forked armed logic;
the BTC roster is byte-identical. Every step that could place an order is gated — the ETH mode file must say
`armed` AND the ETH falsifier must be frozen AND S5/reconcile/S4 must pass, or every ETH window degrades to
dry.

This is NOT the laptop DRY logger (`service.run_v33_eth`, `V33_ETH_DRY_RUNBOOK.md`), which never arms by
construction and keeps running on the laptop. This runbook is the Render-hosted ETH roster.

## What it is

One process (`python -m service.supervisor --rosters v33,v33_eth`) co-hosts BOTH rosters: one signal
handler, one loop, and at each UTC :40 it spawns BOTH children in parallel (BTC first) — `run_v33` for BTC
and `run_v33_eth_live` for ETH — each its own process with its own per-window watchdog. An ETH child crash
never affects the BTC child. SIGTERM is forwarded to both.

Shared-account safety (BTC and ETH run the same hour on ONE Kalshi account):
- ETH coids are `v33e-*`, BTC coids `v33-*`. `"v33e-".startswith("v33-")` is False and vice versa, so
  neither roster's startup sweep / fill attribution can ever match the other's orders.
- ETH boot/stale-order sweep scopes to `KXETH*` tickers + `v33e-*` coids; BTC to `KXBTC*` + `v33-*`.
- Reconcile-first scopes to each roster's own series (`KXETH*` / `KXBTC*`).
- `/portfolio/fills` ingestion is filtered to THIS window's own acked order ids (a shared-account fills
  list containing the other series' fills is never ingested).

ETH writable files (separate from BTC V3.2/V3.3; `DV3_DATA_DIR` relocates them to `/var/dv3` on Render):
- journals: `journals_v33_eth/`
- ledger:   `ledger/v33_eth_ledger.jsonl`
- logs:     `logs_v33_eth/`
- mode file: `ops/v33_eth_mode.txt` — governs ETH arming (absent/unknown → **dry**, fail-closed).
- day guard: `ops/v33_eth_stops_YYYY-MM-DD.json` — the ETH day guard (S1_LEGGED latch, S4 baseline).
- falsifier: `ceremony/v33_eth_falsifier.md` — **ships as a DRAFT**; S5 refuses to arm until Brad freezes it.

## Account-wide S4 (Brad's one-account ruling)

The $5 day-loss cap (`V33_S4_DAY_LOSS_CAP_DOLLARS`) is ACCOUNT-WIDE because the balance is shared:
- Each roster's balance-banded S4 already sees BOTH series' losses (it reads the account balance).
- The S4 pending-settlement band is account-wide: each roster adds the OTHER roster's unsettled held-set
  credit into its band (fail-closed: a missing sibling ledger contributes nothing).
- An S4 latch on EITHER roster's day guard stops BOTH (the ETH roster reads BTC's guard for an S4 latch and
  vice versa; a missing/absent sibling guard has no effect — BTC stays byte-identical until ETH latches).

S1_LEGGED stays PER-roster (ETH has its own `v33_eth_stops_*` guard); only S4 is shared.

## Brad's levers, IN ORDER

### (a) Deploy the co-hosted worker in DRY (24–48 h Phase-2 gates)

Change the Render worker start command to:

```
cd pilot && python -m service.supervisor --rosters v33,v33_eth
```

Set the ETH mode file to dry on the persistent disk (do NOT arm yet):

```
# in the Render worker Shell:
echo dry > /var/dv3/ops/v33_eth_mode.txt
```

Leave BTC's mode file exactly as it is (BTC stays armed at 3 lots). Watch for 24–48 h:
- BTC wing p50 stays near the TF#4 baseline (**~198 ms**) — the shared CPU must not slow BTC's sends.
- Worker CPU / RAM headroom on the Standard 1 CPU / 2 GB instance (two Python children per hour, each a
  ~22-min window; the ETH child subscribes fewer markets — no 15M leg).
- ETH Render-dry decisions match the laptop ETH dry for the same hours (same core, same series).
- Proxy daily order-budget counter headroom vs `DAILY_ORDER_BUDGET 30000`. ETH DRY sends nothing, but ETH
  ARMED implies ~11k order actions/day (1-lot ladder) on top of BTC's ~6k; confirm the budget has room
  BEFORE arming ETH.

### (b) Proxy: add the ETH ticker prefixes

Add `KXETH,KXETHD` to the proxy's `ORDER_TICKER_PREFIXES` env (the proxy's allowlist must cover an ETH
ticker via startswith, exactly as S5's `v33_caps_agree` checks). Without this, ETH would be refused at the
proxy even if armed. BTC's existing prefixes are untouched.

### (c) Kalshi balance top-up

A BTC 3-lot full sweep is ~$66 and an ETH 1-lot full sweep is ~$22 → ~$88 worst-case concurrent vs a
balance of ~$147 (>50% utilization). Deposit ~$30–40 and move the funds to **exchange_index 2** via
kalshi.com/account/exchange-indexes (crypto lives on index 2; deposits land on index 0).

Disk note: ETH adds ~**75 MB/day** of journals on the 20 GB persistent disk. Keep the journal rotation /
offload cadence (the supervisor rotates leftover journals per roster at each wake).

### (d) Freeze the ETH falsifier

Only Brad, on his verbatim go, edits `ceremony/v33_eth_falsifier.md`: replace the `STATUS:` line with the
exact frozen marker and append the go under Registration. Until then S5 keeps every ETH window dry. (An
agent never flips it; a `test_v33_eth_live` test asserts the shipped draft is NOT frozen.)

### (e) Arm ETH

```
# in the Render worker Shell, AFTER (a)-(d):
echo armed > /var/dv3/ops/v33_eth_mode.txt
```

ETH then arms at the next :40 IF S5 (frozen falsifier + ETH params sha + proxy caps/budget), reconcile-first
(no inherited KXETH* position), the ETH day guard, and the account-wide S4 all pass; otherwise it degrades
to dry and journals `degrade_to_dry` with the reasons. BTC is unaffected.

## Worker Shell checks

```
# ETH ledger — last few window rows (roster/series self-labelled):
tail -n 5 /var/dv3/ledger/v33_eth_ledger.jsonl

# ETH stops — today's guard; "latched": [] means no stop latched:
cat /var/dv3/ops/v33_eth_stops_$(date -u +%F).json

# BTC stops (for the account-wide S4 cross-check) — same format:
cat /var/dv3/ops/v33_stops_$(date -u +%F).json

# supervisor events (the co-hosted loop writes to the multi log):
tail -n 20 /var/dv3/logs_v33/supervisor_multi.out
```

## Rollback

Set `echo dry > /var/dv3/ops/v33_eth_mode.txt` (ETH stops arming at the next :40; in-flight ETH window
finishes). To stop co-hosting entirely, revert the start command to `--roster v33` (BTC only). BTC is never
touched by any ETH lever.
