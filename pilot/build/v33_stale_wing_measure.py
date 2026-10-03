"""v33_stale_wing_measure.py -- gate F measurement for the V3.3 stale-wing liveness rewrite (2026-10-03).

Standalone, stdlib only. Reads the per-close V3.3 journals (``YYYYMMDDTHHMMSSZ.jsonl.gz``, records
``{"idx","kind","local_ts","obj"}``) and, per window, reports:

  * journaled ``stand_down_hold`` / ``stand_down_resume`` / ``stand_down_cancel`` counts;
  * WS reconnect evidence: reconnects are NOT journaled as a kind, so we count (a) ``watchdog_stale``
    alarms on the strikes connection and (b) strike-snapshot BURSTS after the subscribe burst (a
    reconnect re-subscribes -> a fresh orderbook_snapshot per strike market);
  * the strike connection's inter-frame gap (consecutive KXBTCD ``orderbook_delta`` server ts, running
    max) -- max and p99.9 over the quote window;
  * the strike-feed AGE as the core sees it: ``now - strike_feed_ts`` where ``now`` is the driver's
    monotone eval clock (max server ts over every driven frame, plus a ClockTick every 0.5 s of wall
    time = last server ts + wall elapsed) and ``strike_feed_ts`` is the latest KXBTCD delta's own ts.
    This is EXACTLY the quantity the new ``strike_feed_dead_s`` bound is compared against, so it
    includes cross-connection skew and feed LAG (a lagging connection stamps old server ts);
  * the two wing strikes' (derived from the ``place_rest`` bucket ticker: bucket B<c> -> Sd = c - W/2,
    yes-leg T<Sd-0.01>, no-leg T<Sd+W-0.01>) inter-delta gap, max and p99;
  * a REPLAY of the hold state machine under the OLD predicate (a wing strike's own delta age > 1.0 s)
    and the NEW predicate (wing missing OR strike-feed age > ``--feed-dead-s``): holds and
    hold-expired cancels (predicate continuously true >= ``--hold-ms``). The OLD replay validates the
    replayer against the journaled counts.

Only deltas drive the core's book state (snapshots carry no server ts and are folded but never driven;
trades drive the clock but never a strike book), and KXBTC15M frames are recording-only -- the replay
mirrors that.

Usage:
  python build/v33_stale_wing_measure.py --journals <dir> --start 2026-09-30 --end 2026-10-03 \
      [--feed-dead-s 5.0] [--hold-ms 1500] [--old-age-s 1.0] [--json out.json]

The quote window is T-900 s .. T-300 s (policy quote_start_s / quote_end_s). No network, no orders, no
writes except the optional --json.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import re
import sys
from datetime import datetime, timezone

QUOTE_START_S = 900
QUOTE_END_S = 300
CLOCK_TICK_S = 0.5
SNAP_BURST_GAP_S = 5.0
_NAME = re.compile(r"^(\d{8}T\d{6}Z)\.jsonl\.gz$")


def _pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    k = min(len(s) - 1, max(0, int(math.ceil(p / 100.0 * len(s))) - 1))
    return s[k]


def _close_epoch(name: str) -> float:
    dt = datetime.strptime(name, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _ts(msg: dict) -> float | None:
    v = msg.get("ts_ms")
    if v is not None:
        try:
            return float(v) / 1000.0
        except (TypeError, ValueError):
            return None
    return None


def _wings_for_bucket(bucket_ticker: str, width: int) -> tuple[str, str] | None:
    # KXBTC-26OCT0222-B84650 -> KXBTCD-26OCT0222-T84599.99 / T84699.99
    m = re.match(r"^KXBTC-([^-]+)-B(\d+(?:\.\d+)?)$", bucket_ticker)
    if not m:
        return None
    centre = float(m.group(2))
    sd = centre - width / 2.0
    su = sd + width
    return (f"KXBTCD-{m.group(1)}-T{sd - 0.01:.2f}", f"KXBTCD-{m.group(1)}-T{su - 0.01:.2f}")


class _Hold:
    """The core's stale-wing HOLD state machine (stand_down_hold_ms), evaluated at each _converge."""

    def __init__(self, hold_ms: float) -> None:
        self.hold_ms = hold_ms
        self.ladder = False
        self.hold_since: float | None = None
        self.holds = 0
        self.cancels = 0
        self.resumes = 0
        self.frozen = False
        self.snap: tuple[int, int] | None = None   # (holds, cancels) at the journaled terminal stand-down

    def freeze_snapshot(self) -> None:
        if self.snap is None:
            self.snap = (self.holds, self.cancels)

    def step(self, now: float, stale: bool, in_window: bool, placed_once: bool) -> None:
        if not in_window or not placed_once:
            return
        if self.frozen:
            return
        if stale:
            if not self.ladder:
                return
            if self.hold_since is None:
                self.hold_since = now
                self.holds += 1
                return
            if (now - self.hold_since) * 1000.0 >= self.hold_ms:
                self.hold_since = None
                self.cancels += 1
                self.ladder = False
            return
        if self.hold_since is not None:
            self.hold_since = None
            self.resumes += 1
        self.ladder = True   # healthy -> the ladder is (re)placed


def measure(path: str, *, feed_dead_s: float, hold_ms: float, old_age_s: float,
            wing_max_age_s: float = 15.0, sweep: tuple[float, ...] = ()) -> dict:
    name = os.path.basename(path)[:16]
    close = _close_epoch(name)
    width = 100
    kinds = {"stand_down_hold": 0, "stand_down_resume": 0, "stand_down_cancel": 0}
    watchdog_strikes = 0
    watchdog_buckets = 0
    strike_snap_local: list[float] = []

    # driver eval clock
    last_server: float | None = None
    last_wall: float | None = None
    next_tick_wall: float | None = None
    strike_feed_ts: float | None = None
    strike_ts: dict[str, float] = {}
    wings: tuple[str, str] | None = None
    placed_once = False

    feed_gaps: list[float] = []
    feed_ages: list[float] = []
    wing_gaps: dict[str, list[float]] = {}
    wing_last: dict[str, float] = {}
    wing_pairs_seen: list[str] = []
    prev_feed: float | None = None

    old_h = _Hold(hold_ms)
    new_h = _Hold(hold_ms)
    sweep_h = {d: _Hold(hold_ms) for d in sweep}
    wing_ages: list[float] = []

    def in_window(now: float) -> bool:
        return QUOTE_END_S <= close - now <= QUOTE_START_S

    def evaluate(now: float, tick: bool = False) -> None:
        iw = in_window(now)
        if tick and iw and wings is not None:
            # TIME-UNIFORM sample of each wing book's age (a ClockTick every 0.5 s wall): what a rung fill
            # landing at a random instant would see at take time.
            for t in wings:
                if t in strike_ts:
                    wing_ages.append(now - strike_ts[t])
        if strike_feed_ts is not None and iw:
            feed_ages.append(now - strike_feed_ts)
        if wings is None:
            stale_old = stale_new = True
        else:
            a = strike_ts.get(wings[0])
            b = strike_ts.get(wings[1])
            missing = a is None or b is None
            stale_old = missing or (now - a) > old_age_s or (now - b) > old_age_s
            # NEW predicate (core.py _v33_compute_W): wing missing, strike feed dead, or a wing book older
            # than the LOOSE wing_book_max_age_s. (``suspect`` is not journaled -> not replayable.)
            too_old = missing or (now - a) > wing_max_age_s or (now - b) > wing_max_age_s
            age = None if strike_feed_ts is None else now - strike_feed_ts
            dead = age is None or age > feed_dead_s
            stale_new = missing or dead or too_old
        old_h.step(now, stale_old, iw, placed_once)
        new_h.step(now, stale_new, iw, placed_once)
        for d, h in sweep_h.items():
            if wings is None:
                h.step(now, True, iw, placed_once)
            else:
                h.step(now, too_old or age is None or age > d, iw, placed_once)

    def clock_ticks_until(wall: float) -> None:
        nonlocal next_tick_wall
        if last_server is None or last_wall is None or next_tick_wall is None:
            return
        while next_tick_wall <= wall:
            evaluate(last_server + (next_tick_wall - last_wall), tick=True)
            next_tick_wall += CLOCK_TICK_S

    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            k = r.get("kind")
            obj = r.get("obj") or {}
            wall = float(r.get("local_ts") or 0.0)
            if k == "window_meta":
                width = int(obj.get("bucket_width") or width)
                continue
            if k in kinds:
                kinds[k] += 1
                continue
            if (k == "stand_down" and obj.get("reason") != "past_quote_end") or (
                    k == "alarm" and obj.get("alarm") == "executor_standdown"):
                # the live core latched a terminal stand-down: the journaled hold counts stop here.
                old_h.freeze_snapshot()
                new_h.freeze_snapshot()
                continue
            if k == "alarm" and obj.get("alarm") == "watchdog_stale":
                if obj.get("conn") == "strikes":
                    watchdog_strikes += 1
                elif obj.get("conn") == "buckets":
                    watchdog_buckets += 1
                continue
            if k in ("place_rest", "would_place_rest"):
                w = _wings_for_bucket(str(obj.get("ticker", "")), width)
                if w is not None and w != wings:
                    wings = w
                    wing_pairs_seen.append(f"{w[0].rsplit('-', 1)[1]}/{w[1].rsplit('-', 1)[1]}")
                    for t in w:
                        wing_last.pop(t, None)
                placed_once = True
                continue
            if k != "kalshi_ws":
                continue
            typ = obj.get("type")
            msg = obj.get("msg") or {}
            mt = str(msg.get("market_ticker", ""))
            if mt.startswith("KXBTC15M"):
                continue   # recording only, never driven
            if typ == "orderbook_snapshot":
                if mt.startswith("KXBTCD-"):
                    strike_snap_local.append(wall)
                continue
            if typ not in ("orderbook_delta", "trade"):
                continue
            ts = _ts(msg)
            if ts is None:
                continue
            clock_ticks_until(wall)
            last_server = ts if last_server is None else max(last_server, ts)
            last_wall = wall
            if next_tick_wall is None:
                next_tick_wall = wall + CLOCK_TICK_S
            now = last_server
            if typ == "trade":
                continue   # trades advance the clock; _converge does not run on a Trade
            if mt.startswith("KXBTCD-"):
                if in_window(ts):
                    if prev_feed is not None:
                        feed_gaps.append(max(0.0, ts - prev_feed))
                prev_feed = ts if prev_feed is None else max(prev_feed, ts)
                strike_feed_ts = ts if strike_feed_ts is None else max(strike_feed_ts, ts)
                strike_ts[mt] = ts
                if wings is not None and mt in wings and in_window(ts):
                    if mt in wing_last:
                        wing_gaps.setdefault(mt, []).append(max(0.0, ts - wing_last[mt]))
                    wing_last[mt] = ts
            evaluate(now)

    # snapshot bursts on the strike connection
    bursts = 0
    prev = None
    for t in strike_snap_local:
        if prev is None or t - prev > SNAP_BURST_GAP_S:
            if QUOTE_END_S <= close - t <= QUOTE_START_S + 60:
                bursts += 1   # a re-subscribe burst inside the quote window (the subscribe burst is ~T-905)
        prev = t
    all_wing = [g for gs in wing_gaps.values() for g in gs]
    wing_rows = {}
    for t, gs in wing_gaps.items():
        wing_rows[t.rsplit("-", 1)[1]] = {"n": len(gs), "max": max(gs), "p99": _pct(gs, 99.0),
                                          "gt1": sum(1 for g in gs if g > 1.0)}
    return {
        "window": name,
        "holds": kinds["stand_down_hold"], "resumes": kinds["stand_down_resume"],
        "cancels": kinds["stand_down_cancel"],
        "watchdog_strikes": watchdog_strikes, "watchdog_buckets": watchdog_buckets,
        "strike_resubscribe_bursts": max(0, bursts - 1),
        "old_replay_pre_sd": old_h.snap if old_h.snap is not None else (old_h.holds, old_h.cancels),
        "feed_gap_max": max(feed_gaps) if feed_gaps else None,
        "feed_gap_p999": _pct(feed_gaps, 99.9),
        "feed_age_max": max(feed_ages) if feed_ages else None,
        "feed_age_p999": _pct(feed_ages, 99.9),
        "wing_pairs": wing_pairs_seen,
        "wing_gap_max": max(all_wing) if all_wing else None,
        "wing_gap_p99": _pct(all_wing, 99.0),
        "wing_gaps_all": all_wing,
        "wing_age_p99": _pct(wing_ages, 99.0), "wing_age_p999": _pct(wing_ages, 99.9),
        "wing_age_max": max(wing_ages) if wing_ages else None,
        "wing_ages_all": wing_ages,
        "wings": wing_rows,
        "old_replay_holds": old_h.holds, "old_replay_cancels": old_h.cancels,
        "new_replay_holds": new_h.holds, "new_replay_cancels": new_h.cancels,
        "sweep": {str(d): [h.holds, h.cancels] for d, h in sweep_h.items()},
        "feed_ages_all": feed_ages,
        "feed_gaps_all": feed_gaps,
    }


def _f(x: float | None) -> str:
    return "-" if x is None else f"{x:.2f}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--journals", required=True)
    ap.add_argument("--start", required=True, help="first close date YYYY-MM-DD (inclusive)")
    ap.add_argument("--end", required=True, help="last close date YYYY-MM-DD (inclusive)")
    ap.add_argument("--feed-dead-s", type=float, default=5.0)
    ap.add_argument("--hold-ms", type=float, default=1500.0)
    ap.add_argument("--wing-max-age-s", type=float, default=15.0)
    ap.add_argument("--sweep", default="", help="comma list of extra strike_feed_dead_s to replay")
    ap.add_argument("--old-age-s", type=float, default=1.0)
    ap.add_argument("--only", default=None, help="comma list of window names (YYYYMMDDTHHMMSSZ)")
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)
    d0 = a.start.replace("-", "")
    d1 = a.end.replace("-", "")
    only = set(a.only.split(",")) if a.only else None
    files = []
    for fn in sorted(os.listdir(a.journals)):
        m = _NAME.match(fn)
        if not m:
            continue
        day = fn[:8]
        if not (d0 <= day <= d1):
            continue
        if only is not None and m.group(1) not in only:
            continue
        files.append(os.path.join(a.journals, fn))
    rows = []
    print("| window | hold | resume | cancel | wd_strk | resub | feed gap max | feed gap p99.9 | "
          "feed age max | feed age p99.9 | wing gap max | wing gap p99 | wing age p99/max | old-replay to-SD hold/cancel | "
          "old-replay full | new-replay full hold/cancel |")
    print("|" + "---|" * 16)
    for p in files:
        try:
            row = measure(p, feed_dead_s=a.feed_dead_s, hold_ms=a.hold_ms, old_age_s=a.old_age_s,
                          wing_max_age_s=a.wing_max_age_s,
                          sweep=tuple(float(x) for x in a.sweep.split(",") if x))
        except (OSError, EOFError, ValueError) as e:   # a truncated/live journal
            print(f"| {os.path.basename(p)[:16]} | unreadable: {type(e).__name__} |", flush=True)
            continue
        rows.append(row)
        print(f"| {row['window']} | {row['holds']} | {row['resumes']} | {row['cancels']} | "
              f"{row['watchdog_strikes']} | {row['strike_resubscribe_bursts']} | "
              f"{_f(row['feed_gap_max'])} | {_f(row['feed_gap_p999'])} | {_f(row['feed_age_max'])} | "
              f"{_f(row['feed_age_p999'])} | {_f(row['wing_gap_max'])} | {_f(row['wing_gap_p99'])} | "
              f"{_f(row['wing_age_p99'])}/{_f(row['wing_age_max'])} | "
              f"{row['old_replay_pre_sd'][0]}/{row['old_replay_pre_sd'][1]} | "
              f"{row['old_replay_holds']}/{row['old_replay_cancels']} | "
              f"{row['new_replay_holds']}/{row['new_replay_cancels']} |"
              + "".join(f" sweep{d}={v[0]}/{v[1]}" for d, v in row["sweep"].items()), flush=True)
    healthy = [r for r in rows if r["watchdog_strikes"] == 0 and r["strike_resubscribe_bursts"] == 0]
    ages = [x for r in healthy for x in r["feed_ages_all"]]
    gaps = [x for r in healthy for x in r["feed_gaps_all"]]
    wg = [x for r in rows for x in r["wing_gaps_all"]]
    wa = [x for r in healthy for x in r["wing_ages_all"]]
    print()
    print(f"windows: {len(rows)}  healthy (no strike watchdog, no re-subscribe): {len(healthy)}")
    print(f"healthy strike feed AGE (eval clock): n={len(ages)} p99={_f(_pct(ages, 99))} "
          f"p99.9={_f(_pct(ages, 99.9))} p99.99={_f(_pct(ages, 99.99))} max={_f(max(ages) if ages else None)}")
    print(f"healthy strike inter-frame GAP: n={len(gaps)} p99.9={_f(_pct(gaps, 99.9))} "
          f"max={_f(max(gaps) if gaps else None)}")
    print(f"wing strike inter-delta GAP (all windows): n={len(wg)} p50={_f(_pct(wg, 50))} "
          f"p99={_f(_pct(wg, 99))} p99.9={_f(_pct(wg, 99.9))} max={_f(max(wg) if wg else None)}")
    print(f"healthy wing book AGE (time-uniform, 0.5 s ticks): n={len(wa)} p99={_f(_pct(wa, 99))} "
          f"p99.9={_f(_pct(wa, 99.9))} p99.99={_f(_pct(wa, 99.99))} max={_f(max(wa) if wa else None)}")
    print(f"journaled holds total={sum(r['holds'] for r in rows)} cancels={sum(r['cancels'] for r in rows)}; "
          f"old-replay holds={sum(r['old_replay_holds'] for r in rows)} "
          f"cancels={sum(r['old_replay_cancels'] for r in rows)}; "
          f"new-replay holds={sum(r['new_replay_holds'] for r in rows)} "
          f"cancels={sum(r['new_replay_cancels'] for r in rows)}")
    for d in (a.sweep.split(",") if a.sweep else []):
        key = str(float(d))
        print(f"sweep strike_feed_dead_s={key}: holds={sum(r['sweep'][key][0] for r in rows)} "
              f"cancels={sum(r['sweep'][key][1] for r in rows)} windows_with_holds="
              f"{sum(1 for r in rows if r['sweep'][key][0])}")
    if a.json:
        slim = [{k: v for k, v in r.items() if not k.endswith("_all")} for r in rows]
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(slim, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())


# ---------------------------------------------------------------------------
# Fixture extraction (for pilot/tests/fixtures/v33/stale_wing_*.json; the tests never read journals)
# ---------------------------------------------------------------------------
def extract_fixture(path: str, t_from: float, t_to: float, thin_ms: int = 100) -> dict:
    """Extract a thinned (``thin_ms`` per stream) arrival-ordered event list from one journal for the
    window ``close - t_from .. close - t_to`` (T-minus seconds, t_from > t_to). Streams: ``y``/``n`` = the
    ladder bucket's yes/no wing strike deltas, ``s`` = any OTHER strike delta (feed liveness), ``b`` = any
    bucket-connection delta or trade (the eval clock). Each event is ``[local_ms, server_ms, stream]``
    relative to the close (ms). Thinning keeps the first frame of each stream after ``thin_ms`` since the
    last kept one, so a kept-gap is never SHORTER than the true gap (conservative for a no-hold claim)."""
    name = os.path.basename(path)[:16]
    close = _close_epoch(name)
    width = 100
    wings: tuple[str, str] | None = None
    bucket: str | None = None
    last_kept: dict[str, float] = {}
    events: list[list] = []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            k = r.get("kind")
            obj = r.get("obj") or {}
            if k == "window_meta":
                width = int(obj.get("bucket_width") or width)
                continue
            if k in ("place_rest", "would_place_rest") and wings is None:
                tk = str(obj.get("ticker", ""))
                w = _wings_for_bucket(tk, width)
                if w is not None:
                    wings, bucket = w, tk
                continue
            if k != "kalshi_ws":
                continue
            typ = obj.get("type")
            msg = obj.get("msg") or {}
            mt = str(msg.get("market_ticker", ""))
            if mt.startswith("KXBTC15M") or typ not in ("orderbook_delta", "trade"):
                continue
            ts = _ts(msg)
            if ts is None:
                continue
            wall = float(r.get("local_ts") or 0.0)
            if not (t_to <= close - wall <= t_from):
                continue
            if mt.startswith("KXBTCD-"):
                if typ != "orderbook_delta":
                    continue
                if wings is not None and mt == wings[0]:
                    s = "y"
                elif wings is not None and mt == wings[1]:
                    s = "n"
                else:
                    s = "s"
            else:
                s = "b"
            prev = last_kept.get(s)
            if prev is not None and (ts - prev) * 1000.0 < thin_ms:
                continue
            last_kept[s] = ts
            events.append([int(round((wall - close) * 1000)), int(round((ts - close) * 1000)), s])
    return {"source": name, "bucket": bucket, "wings": list(wings) if wings else None,
            "t_from": t_from, "t_to": t_to, "thin_ms": thin_ms, "events": events}
