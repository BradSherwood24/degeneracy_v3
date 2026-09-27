# Daily Replay + Tripwire

Brad, 2026-09-27: "lets get an automated process to run the replay every morning."

An OFFLINE, incremental morning job that re-runs the V3.2 pump-fader over the ms journals and reports a
falsifier **tripwire**. It only REPORTS. Standing V3.2 down remains Brad's lever.

- Runner: `tools/daily_replay.py` (run from the REPO ROOT: `python tools/daily_replay.py`)
- Engine: `sim/v32_replay/lab.py` (unchanged math; a new `--only` / `--files-from` subset option lets
  the runner replay just the new windows)
- Never touches the proxy. Reads only `pilot/journals_v32/*.jsonl.gz` (never the live `.jsonl` a window
  is still writing), and the lab refuses the sealed / holdout dates (the runner never sets the
  acknowledge flag).

## What one run does

1. Lists `pilot/journals_v32/*.jsonl.gz` and compares against the manifest
   `sim/out/v32_replay/daily/manifest.json` (basename + size). Runs the lab over ONLY the new windows
   (oldest first, capped by `--max-windows`, default 60), writing that run's outputs to
   `sim/out/v32_replay/daily/<YYYYMMDD_HHMM>/`.
2. Merges the run's `fills.jsonl` into `sim/out/v32_replay/fills_all.jsonl` (dedupe on
   close+model+E+tol+deb+trade_ts) and `per_window.jsonl` into `per_window_all.jsonl` (dedupe on
   close+cell). Marks the replayed journals done in the manifest (only after the lab exits 0).
3. Computes the tripwire from the base cell (`model="lag"`, `E="0.10"`) and writes:
   - `pilot/ops/replay_tripwire.txt` (verdict line + a small ASCII table incl. per-day rows for the
     last 7 days),
   - `pilot/ops/replay_tripwire.json` (machine-readable),
   - one appended line to `pilot/ops/replay_daily.log`.

All three ops files are runtime artifacts (gitignored), regenerated each run.

## Reading `replay_tripwire.txt`

```
TRIPWIRE: WATCH  (3-day n=27 (>= 8) and mean -5.68c < +2.0c (early warning; ...))

as of 2026-09-26 UTC | base cell model=lag E=0.10 | kill line = mean < +2.0c at n>=15 (7d)
  7-day: n=81  mean=+5.01c median=+9.40c neg=24/81 drift_p90=+19.15c
  3-day: n=27  mean=-5.68c median=-6.56c neg=24/27 drift_p90=+19.15c

  date        fills   mean      negatives
  ----------  -----   -------   ---------
  2026-09-20      ...
  ...
  2026-09-26      ...
```

- **verdict** (first line): `OK`, `WATCH`, or `TRIP` with the reason.
- **7-day / 3-day** rows: n fills, mean / median lock (cents), negatives, and the **wing-drift p90**.
  Wing drift = `W_completion - (2 - E - n)` in cents: it spikes when sweeps arrive with a big wing
  jump (the 2026-09-25/26 regime showed drift_p90 jumping to ~+19c). Watch it alongside the mean.
- **per-day table**: the last 7 UTC days, one row each (fills / mean / negatives), so a bad day stands
  out even before it drags the trailing mean under the line.

The trailing windows are anchored on **today (UTC)** by default (`--as-of` overrides for a backfill).
7-day = `[today-6, today]`, 3-day = `[today-2, today]`, inclusive.

## Verdict rules (and why +2.0c)

`+2.0c` is the **frozen V3.2 falsifier kill line**: mean lock < +2.0c at n >= 15 is the standing kill
condition for the edge. The tripwire mirrors it and adds a 3-day early warning:

- **TRIP** -- 7-day n >= 15 AND 7-day mean < +2.0c. The falsifier's own condition, on the trailing
  week. Exit code 3.
- **WATCH** -- 3-day n >= 8 AND 3-day mean < +2.0c. Fires days earlier than TRIP when a regime turns
  (as on 2026-09-26, when the 3-day mean was already -5.7c while the 7-day still held +5.0c). Exit
  code 2.
- **OK** -- neither. Exit code 0.

Exit codes: 0 OK, 2 WATCH, 3 TRIP, 1 error. `main()` never raises -- on any error it logs and
returns 1 (a scheduled task must not crash).

The tripwire does NOT stand anything down. It reports; Brad decides.

## Register / unregister the scheduled task

Runs daily at **10:10 UTC** (chosen so the previous UTC day's :40 windows have all closed and rotated
to `.gz`). Task Scheduler fires on LOCAL wall-clock, so the register script converts 10:10 UTC to local
time AT REGISTRATION. The box is currently **UTC-4 (EDT) -> 06:10 local**.

Register from the **LIVE tree** (`C:\Users\Brads\Python_stuff\degeneracy_v3`) after this branch is
merged, so the task's repo root -- and thus `pilot/journals_v32` -- is the live one that `run_v32`
writes to. The script resolves the repo root from its own location (or pass `-RepoRoot`).

```
# from the LIVE repo root, in a normal (non-admin) PowerShell:
powershell -NoProfile -ExecutionPolicy Bypass -File pilot\ops\register_daily_replay.ps1 -DryRun   # preview
powershell -NoProfile -ExecutionPolicy Bypass -File pilot\ops\register_daily_replay.ps1           # register (Interactive)

# unregister:
powershell -NoProfile -ExecutionPolicy Bypass -File pilot\ops\unregister_daily_replay.ps1
```

- Task name: `DegeneracyReplayDaily`. Working dir: the repo root. Command: `python tools\daily_replay.py`.
- `-LogonType Interactive` (default) runs when you are logged on and needs NO admin shell. `-LogonType
  S4U` runs whether logged on or not but needs an ADMIN PowerShell to register.
- Settings: `-StartWhenAvailable` (a missed run fires when the box next wakes), battery flags allowed,
  `ExecutionTimeLimit 2h`, `MultipleInstances IgnoreNew`.
- Scheduler stdout/stderr -> `sim/out/v32_replay/daily/scheduler.out`.
- **DST caveat**: a daily trigger keeps its local time across a DST change, so the effective UTC firing
  time shifts by an hour when EDT<->EST. Re-run the register script after a DST change to re-pin
  10:10 UTC.

## Seeding from an existing full run

A full run's `fills.jsonl` + `per_window.jsonl` (e.g. a scratchpad `v32_replay_all/`) seeds the rolling
history so the first scheduled run only picks up NEW windows:

```
python tools\daily_replay.py --seed-from "<dir with fills.jsonl + per_window.jsonl>"
```

Seeding imports both files into `fills_all.jsonl` / `per_window_all.jsonl` (dedupe), marks every journal
in `pilot/journals_v32/` whose close is in the seeded per_window as done in the manifest, and writes an
initial tripwire. It is idempotent (re-seeding adds nothing).

## Flags

- `--dry-run` -- list the windows that would be replayed, then exit (no run).
- `--full` -- ignore the manifest and replay all discovered windows (still capped by `--max-windows`).
- `--max-windows N` -- cap windows per run (default 60) so a first run after a long gap can't run for
  hours; the rest carry to the next morning.
- `--seed-from <dir>` -- import an existing lab output dir (see above).
- `--as-of YYYY-MM-DD` -- anchor date for the trailing tripwire windows (default: today UTC).
- `--journals <dir>` -- journals dir (default `pilot/journals_v32`; only `*.jsonl.gz` are read).

## Robustness notes

- A partial / unreadable journal (no `window_meta`, or a read error) is SKIPPED with a warning; the run
  continues over the rest. A sealed / holdout journal is still hard-refused (never skipped silently).
- If the lab exits non-zero, the manifest is NOT updated (those windows retry next run) and the run
  returns exit code 1.
- Timing: a full run over ~245 windows is ~3.5 h (~50 s/window); a daily increment of ~24 windows is
  ~20 min -- well inside the 2 h execution limit and the `--max-windows` guard.
