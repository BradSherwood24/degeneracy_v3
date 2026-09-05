# Range recorder (KXBTC hourly range-bucket tape)

A **passive, zero-order** recorder for the hourly **KXBTC range-bucket** markets. It is a **separate
process from the armed pilot** — one process, one job. It **never places an order**, never touches
the box/corridor strategy or any lever, and only reads market data through the local signing proxy.
It is the range-market analogue of `record_window` (the corridor spine's passive recorder).

Module: `pilot/service/record_range.py` — run as `python -m service.record_range` **from `pilot/`**.

---

## What it records

- **Discovery (REST, via the proxy).** For the target top-of-hour close it queries
  `GET /trade-api/v2/markets?series_ticker=KXBTC&min_close_ts=<epoch>&max_close_ts=<epoch>`
  (paged, status-agnostic) and keeps **every live range bucket co-settling at that close, across all
  widths and all generations** — so the 21:00 UTC `$250` hour is captured alongside ordinary `$100`
  hours. It reuses the pilot's own discovery helpers (`wake._group_ladders`, `wake._leg_is_live`,
  `wake.close_epoch`, `wake.coerce_exchange_index`). A fully-dead generation is dropped (fail-closed,
  same rule as `wake`). Each bucket's **floor and cap** (and `exchange_index`, `status`, `open_time`,
  `event_ticker`) are captured in a **`window_meta`** record (idx 0) at the top of the journal.
- **Stream (WebSocket, via the proxy).** Connects exactly like `record_window` (fresh proxy-minted
  auth on every dial; no key material here) and subscribes the range tickers to **`orderbook_delta`
  and `trade` only**. `ticker` is deliberately skipped to cut frame volume — the recorder archives
  the book + tape, it never needs a mid/last. (`KalshiWebSocketClient` gained an optional `channels`
  parameter defaulting to the existing public set, so every other caller is unchanged.)
- **Record shape.** Identical to the pilot journals:
  `{"idx", "kind": "kalshi_ws", "local_ts", "obj": {"type", "msg"}}`, deterministic
  `json.dumps(sort_keys=True)`. The record-0 `window_meta` and any watchdog `alarm` records use the
  same envelope with `kind` `"window_meta"` / `"alarm"`. Existing readers work unchanged:
  `service.journal.load_journal`, `service.journal_io.open_journal`, `service.replay`, and the
  `obj.type` / `obj.msg.market_ticker` / `*_dollars_fp` / `delta_fp` / trade-field extractors.

## Where it writes

- Journal: `pilot/journals_range/<close_time>.jsonl` (e.g. `journals_range/20260905T170000Z.jsonl`),
  fixed-width UTC stem so files sort chronologically. **Separate directory from the armed pilot's
  `pilot/journals/`** — the two processes never share a file.
- Summary: one line appended to `pilot/journals_range/summary.jsonl` per window (a stand-down writes
  `{"stand_down": true, "reason": ...}`; a real window writes counts, byte sizes, last lag/silence,
  `dropped_no_market`, and the final journal path).

## Memory-light by construction (RAM)

- **No `BookMirror`, no per-frame accumulation.** The recorder's WS callbacks are all empty, so
  `on_message` records via the tap and dispatches to nothing. A **write-through `StreamJournal`**
  appends each record straight to an open file handle and **flushes every 200 frames** (`--flush-every`),
  so RSS stays flat regardless of how long or busy the window is.
- **Measured:** ~34 MB RSS after imports (interpreter + `websockets`), and **still ~34 MB after
  streaming 60,001 frames** (+0.1 MB) — no growth with frame count. A live hour sits around
  **35-45 MB**, well under the 100 MB budget on this RAM-short box. (The armed pilot's in-memory
  `Journal` buffers a whole window; this recorder intentionally does not, precisely because a
  180-bucket hour is too much to hold.)

## Disk expectations

~180 buckets x 60 min. Measured in a quiet mid-afternoon smoke (188 buckets, 75 s): **4,739 records
= 1.6 MB raw -> 101 KB gzipped**, i.e. ~63 frames/s. Extrapolated to a full quiet hour that is
roughly **~230k records, ~75-80 MB raw, ~5 MB gzipped**. Range buckets are **thinner** than the
armed pilot's ladder (the pilot logs ~950k msgs / 15 min for its 188-strike threshold+15M set with
the `ticker` channel on); dropping `ticker` and the range books being quieter keeps this well below
that. Busy hours (news, the 21 UTC step change) can run several times the quiet rate — budget on the
order of **a few hundred MB raw per busy hour before gzip**, and gzip (~15x here) brings the archived
file to single-digit MB. The journal is written raw during the window, then gzipped at close.

## Rotation / gzip

At the deadline (close + 10 s grace) — or on Ctrl+C — the recorder closes the stream, then
**crash-safely gzips** the raw journal to `<close_time>.jsonl.gz` and removes the raw file, reusing
`service.journal_io._gzip_one_crash_safe` (stream into `.gz.tmp`, atomic rename, best-effort remove
of the raw). A crash before the rename leaves the intact raw journal; a crash after leaves the
finished `.gz` — never a half-written `.gz` in place of the raw. If gzip fails for any reason the raw
`.jsonl` is left in place (data is never lost to a gzip problem) and the error is recorded in the
summary. `journal_io.open_journal` reads `.jsonl` and `.jsonl.gz` transparently, so downstream tools
don't care which form is on disk.

## Watchdog / reconnect

Reuses `record_window`'s exact `watchdog_action` decision and `run_recording` supervisor: force-close
+ re-dial on lag over 30 s or silence over 45 s (generous, passive thresholds), and every re-dial
re-subscribes (a fresh `orderbook_snapshot` arrives — nothing to rebuild, since there are no books).
Watchdog trips are recorded as `alarm` records in the journal.

## Command line

Run **from `pilot/`** (the live tree: `C:\Users\Brads\Python_stuff\degeneracy_v3\pilot`):

```
python -m service.record_range
```

Defaults: target close = next `:00` UTC; start = **T-60 min** if launched earlier, else immediately;
journal dir = `journals_range/`; flush every 200 frames. Useful flags:

- `--close-time 2026-09-05T17:00:00Z` — target a specific close (UTC ISO).
- `--start-lead-minutes 60` — begin this many minutes before close if launched earlier (default 60 =
  the full hour). Launched later than that -> start immediately and record the remainder.
- `--journal-dir <dir>` / `--flush-every <N>` / `--proxy-base <url>` — overrides.

Stands down cleanly (exit 0, one summary line) if no range markets co-settle at the target close.

## Register the hourly task (Windows Task Scheduler)

The task fires at **UTC :00 every hour** and launches one recorder for the next `:00` close. Because
the recorder waits until T-60 itself and each instance writes a distinct `<close_time>.jsonl`,
concurrent instances (the 10 s grace tail of one hour overlapping the next launch) never collide.

**First, create the log directory** (the redirect target must exist or the first launch fails):

```
mkdir "C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\logs"
```

**schtasks one-liner** (run in an elevated prompt; the `degeneracy_v3` path has no spaces so no inner
quoting is needed):

```
schtasks /create /tn "DegeneracyV3RangeRecorder" /sc HOURLY /mo 1 /st 00:00 /f /tr "cmd /c cd /d C:\Users\Brads\Python_stuff\degeneracy_v3\pilot && python -m service.record_range 1>> C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\logs\range_scheduler.out 2>&1"
```

Notes:
- `/st 00:00` + `/sc HOURLY /mo 1` fires at minute `:00` of every local hour. This equals UTC `:00`
  **only for whole-hour-offset time zones** (as on this box). If the machine's UTC offset has a
  `:30`/`:45` component, set `/st` to the local minute that corresponds to UTC `:00`.
- **Multiple-instances policy.** The 10 s grace of the closing hour overlaps the next `:00` launch.
  If your Task Scheduler default is "do not start a new instance", that overlap would *skip* the new
  hour — so verify the policy allows it. The robust way is to register via PowerShell with an
  explicit **Parallel** policy (mirrors `ops/register_task.ps1`), which also auto-computes the
  UTC-`:00` local minute:

  ```powershell
  $pilot = "C:\Users\Brads\Python_stuff\degeneracy_v3\pilot"
  New-Item -ItemType Directory -Force -Path (Join-Path $pilot "logs") | Out-Null
  $log = Join-Path $pilot "logs\range_scheduler.out"
  $arg = '/c cd /d "' + $pilot + '" && python -m service.record_range >> "' + $log + '" 2>&1'
  $offMin = [int][System.TimeZoneInfo]::Local.GetUtcOffset([DateTime]::Now).TotalMinutes
  $localMin = ((0 + $offMin) % 60 + 60) % 60      # local minute matching UTC :00
  $now = Get-Date
  $start = $now.Date.AddHours($now.Hour).AddMinutes($localMin)
  if ($start -le $now) { $start = $start.AddHours(1) }
  Register-ScheduledTask -TaskName "DegeneracyV3RangeRecorder" -Force `
    -Action  (New-ScheduledTaskAction  -Execute "cmd.exe" -Argument $arg -WorkingDirectory $pilot) `
    -Trigger (New-ScheduledTaskTrigger -Once -At $start -RepetitionInterval (New-TimeSpan -Hours 1)) `
    -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
                 -DontStopIfGoingOnBatteries -MultipleInstances Parallel)
  ```

Remove the task with `schtasks /delete /tn "DegeneracyV3RangeRecorder" /f` (or
`Unregister-ScheduledTask -TaskName "DegeneracyV3RangeRecorder" -Confirm:$false`).

> Register the task only **after this branch is merged to main and the live tree
> (`C:\Users\Brads\Python_stuff\degeneracy_v3`) is updated** — the scheduled command runs from the
> live tree, not this worktree.

## Verify a recording

```
# Newest journal + its summary line
ls journals_range
python -c "from service.journal import load_journal; j=load_journal('journals_range/20260905T170000Z.jsonl.gz'); print(len(j),'records'); print(next(j.iter_records())['obj']['ticker_count'],'buckets')"
tail -1 journals_range/summary.jsonl
```

A healthy summary shows `stand_down: false`, `records` in the hundreds-of-thousands for a busy hour,
`counts` dominated by `ws_orderbook_delta` with some `ws_orderbook_snapshot` (one per bucket per dial)
and `ws_trade`, `dropped_no_market: 0`, and a small `last_lag_seconds`. A gzipped `.jsonl.gz` in
`journals_range/` with a matching summary line is a good window.

## Standing rule

This process **never places an order** — no executor, no order path, empty WS callbacks. It exists
only to archive the range-bucket book + tape for offline study. It is entirely independent of the
armed v1.1 corridor/box pilot; running or stopping it cannot affect the armed strategy.
