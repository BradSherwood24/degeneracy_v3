# Range-series backfill (KXBTC "Bitcoin range", hourly)

Extends `tools/fetch_history.py` to the Kalshi **KXBTC** hourly *range* series and stores it
under `historical-data/1-hour-range/`, mirroring the existing 15-minute / 1-hour layout.

## What KXBTC is
- Hourly, settles on the `:00` like KXBTCD. ~188 markets per settled hour:
  - ~186 **$100 buckets** — `KXBTC-<26AUG2916>-B<floor>` — carry BOTH `floor_strike` and
    `cap_strike` (e.g. floor 78800 / cap 78899.99).
  - ~2 open-ended **tails** — `KXBTC-...-T<strike>` — carry only ONE side
    (`T88299.99` = "$88,300 or above", `floor_strike` set, `cap_strike` absent;
    `T69700` = "$69,699.99 or below", `cap_strike` set, `floor_strike` absent).
- The `/markets` page caps at 100, so day pagination via `cursor` is mandatory
  (verified: 2 pages per hour, all 188 collected).
- Real data has occasional **short hours** (fewer than 188 listed buckets); this is a Kalshi
  listing property, not a fetch bug (e.g. 2026-08-29T21:00:00Z listed 80). SEAL.md records the
  same class of short hours on the KXBTCD side.

## Layout
```
historical-data/1-hour-range/markets/YYYY-MM-DD.jsonl   raw market objects
historical-data/1-hour-range/candles/YYYY-MM-DD.jsonl   {"ticker":..., "candlesticks":[...]}
historical-data/1-hour-range/trades/YYYY-MM-DD.jsonl.gz {"ticker":..., "trades":[...]}   (opt-in)
historical-data/1-hour-range/fetch.log                  detached backfill stdout
historical-data/1-hour-range/fetch.err                  detached backfill stderr
historical-data/1-hour-range/fetch_range.done           written on clean completion
```
One raw API object per line; today's (incomplete) UTC day is never written. A market closing
exactly at `00:00:00Z` appears in two adjacent day files — dedupe by ticker at load time
(same convention as the legacy series).

## Stages (`--stage`)
- `metadata_range` — all settled KXBTC markets per UTC day (fast; ~7 min for the full window).
- `candles_range`  — 1-minute candlesticks for **every** KXBTC market (full ladder, not a band —
  the range thesis is the whole bucket distribution). Span-clamped to the final hour before
  close, so long-listed markets don't blow the candle cap. This is the slow stage
  (~4.4k markets/day x 0.25s ~= 18 min/day of pure fetch).
- `range` = `metadata_range` + `candles_range` (what the backfill runs).
- `trades_range` — executed-trades tape for every KXBTC market. **Opt-in**, not in `range`:
  heavy and mostly empty for deep buckets. Run separately if/when the tape is needed.

Legacy stages (`metadata`, `candles15`, `candles1h`, `trades15`, `trades1h`, `all`) are
unchanged — `stage_metadata` iterates only `LEGACY_SERIES`, so a plain `all` run does not
touch the range dir.

## House-law guards baked into the fetcher
- **SEAL refusal (registered spec).** `historical-data/SEAL.md` pins UTC days
  **2026-08-02..2026-08-18** as the Rung-1 read-only holdout. `build_days()` drops every day
  in that window and logs the refusal, unless `--acknowledge-sealed` is passed. Builder agents
  never set that flag. The range series is new and NOT in the seal manifest — leaving its
  sealed window unfetched keeps a clean future holdout, and those days stay well inside
  Kalshi's ~68-day retention should a one-shot sealed pull ever be ordered.
- **Quiet-window guard (house rule).** With `--quiet-guard`, every proxy request first checks
  the wall clock; if the minute-of-hour is in `[38, 59]` it sleeps to the top of the next hour
  before firing. The live pilot wakes at `:40Z` and enters `:50-:59Z`; no bulk proxy traffic
  overlaps it. Pure predicates (`in_quiet_window`, `seconds_until_top_of_hour`,
  `wait_out_quiet_window`) are unit-tested with an injected clock in
  `tools/tests/test_fetch_history.py`.
- **Read-only.** Only GETs through the proxy at `http://127.0.0.1:8642/trade-api/v2`. No key
  material, no `.env`, no `*.pem`.

## Data root
The fetcher defaults to the tree it lives in. Point it at the **live pilot tree's** gitignored
store so a worktree checkout writes into the canonical location without dirtying either tree:
```
--data-root C:/Users/Brads/Python_stuff/degeneracy_v3/historical-data
# or: setx / export DV3_DATA_ROOT=...
```

## Run the backfill
```
python C:/Users/Brads/Python_stuff/dv3_wt_range/tools/fetch_history.py \
  --stage range \
  --data-root C:/Users/Brads/Python_stuff/degeneracy_v3/historical-data \
  --quiet-guard \
  --done-marker C:/Users/Brads/Python_stuff/degeneracy_v3/historical-data/1-hour-range/fetch_range.done
```
`--start` is auto-probed to the current retention edge (`probe_earliest`, ~68 days back);
`--end` defaults to yesterday UTC.

Detached (PowerShell), stdout/stderr to the log files, PID returned:
```powershell
Start-Process -FilePath python `
  -ArgumentList @('C:/Users/Brads/Python_stuff/dv3_wt_range/tools/fetch_history.py',
    '--stage','range',
    '--data-root','C:/Users/Brads/Python_stuff/degeneracy_v3/historical-data',
    '--quiet-guard',
    '--done-marker','C:/Users/Brads/Python_stuff/degeneracy_v3/historical-data/1-hour-range/fetch_range.done') `
  -WindowStyle Hidden `
  -RedirectStandardOutput 'C:/Users/Brads/Python_stuff/degeneracy_v3/historical-data/1-hour-range/fetch.log' `
  -RedirectStandardError  'C:/Users/Brads/Python_stuff/degeneracy_v3/historical-data/1-hour-range/fetch.err' `
  -PassThru | Select-Object Id
```

## Check progress / resume
```powershell
Get-Content .../1-hour-range/fetch.log -Tail 20        # live progress
(Get-ChildItem .../1-hour-range/markets).Count          # days of metadata done
(Get-ChildItem .../1-hour-range/candles).Count          # days of candles done
Test-Path .../1-hour-range/fetch_range.done             # complete?
```
**Resumable at day granularity:** a day file that already exists is skipped (written atomically
via `.partial` rename), so re-running the exact same command continues where it stopped. If the
process dies, just relaunch it — it re-fetches only the missing days. (A day interrupted
mid-fetch has no file yet and is redone in full.)

## Never
- Never kill a python process you did not start — the live pilot is python. No broad
  `taskkill` by name or command-line pattern. Stop *this* backfill only by its specific PID.
- Never write the sealed window without an explicit, Brad-authorized `--acknowledge-sealed`.
