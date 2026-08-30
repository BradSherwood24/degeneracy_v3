"""Pull Kalshi historical data for the corridor census, via the local signing proxy.

Fetches, for KXBTC15M (15-minute), KXBTCD (1-hour), and KXBTC (1-hour range) BTC series:
  1. metadata  — every settled market, day-windowed pagination, raw API JSON.
  2. candles15 — 1-minute candlesticks for EVERY 15-minute market (15 candles each).
  3. candles1h — 1-minute candlesticks for hourly strikes within BAND_DOLLARS of the
                 same-close-time 15-minute market's anchor (floor_strike). The full
                 188-strike ladder is ~300k calls of deep-OTM junk; the corridor only
                 ever forms near the anchor.
  4. trades15 / trades1h — the executed-trades tape (microsecond timestamps, price,
                 size, taker side) for the same market sets as the candle stages.
                 This is the sub-minute record the candles lack. API returns trades
                 newest-first; stored verbatim in received order. Trades retention is
                 shorter than candles (measured 2026-08-19: empty at 2026-06-12,
                 present from 2026-06-15) — days past the edge write empty tapes.

  RANGE series (KXBTC, "Bitcoin range", hourly, settles :00 like KXBTCD):
  5. metadata_range — every settled KXBTC market per UTC day. ~188 markets per hour
                 (~186 $100 buckets + ~2 open-ended tails); the /markets page caps at
                 100 so pagination via cursor is mandatory. Buckets carry BOTH
                 floor_strike and cap_strike; tails carry only one (e.g.
                 KXBTC-26AUG2920-T69700 = "$69,699.99 or below", floor_strike absent).
  6. candles_range — 1-minute candlesticks for EVERY KXBTC market (full ladder; the
                 range thesis is the whole bucket distribution, not a band around an
                 anchor). Span-clamped to the final hour like the KXBTCD stage.
  7. trades_range — executed-trades tape for EVERY KXBTC market. Off by default in the
                 initial backfill (heavy; most deep buckets are empty); available via
                 --stage trades_range.

House law: record the RAWS (verbatim API objects), never derived views. Retention
measured 2026-08-18: settled markets reach back to ~2026-06-11 (rolling ~68 days),
so run this regularly to keep extending the tail before it falls off the back.

SEAL: UTC days 2026-08-02..2026-08-18 are the Rung-1 read-only holdout (see
historical-data/SEAL.md). This fetcher REFUSES to write any day in that window unless
--acknowledge-sealed is passed explicitly. The range series (KXBTC) is new and NOT in
the seal manifest; leaving its sealed window unfetched keeps a clean future holdout,
and those days remain well inside Kalshi's ~68-day retention should Brad ever order a
one-shot sealed range pull.

QUIET WINDOW (house rule): the live pilot wakes at :40Z and enters at :50-:59Z. When
--quiet-guard is active, every proxy request first checks the wall clock and, if the
minute-of-hour is in [38, 59], sleeps until the top of the next hour before firing.
No bulk proxy traffic overlaps the pilot's window.

Layout (one file per UTC day, JSONL, one raw object per line):
  historical-data/15-minute/markets/YYYY-MM-DD.jsonl   raw market objects
  historical-data/15-minute/candles/YYYY-MM-DD.jsonl   {"ticker":..., "candlesticks":[...]}
  historical-data/15-minute/trades/YYYY-MM-DD.jsonl.gz {"ticker":..., "trades":[...]}
  historical-data/1-hour/markets/YYYY-MM-DD.jsonl
  historical-data/1-hour/candles/YYYY-MM-DD.jsonl
  historical-data/1-hour/trades/YYYY-MM-DD.jsonl.gz
  historical-data/1-hour-range/markets/YYYY-MM-DD.jsonl
  historical-data/1-hour-range/candles/YYYY-MM-DD.jsonl
  historical-data/1-hour-range/trades/YYYY-MM-DD.jsonl.gz

Trades are gzipped (still verbatim raw objects, losslessly compressed): the 15M tape
runs ~1M trades/~300MB per day uncompressed, which would not fit the disk raw.

Resumable: a day file that exists is skipped (written atomically via .partial rename),
so re-running only fetches missing days. Today (UTC) is never written — only complete,
settled days. A market closing exactly at 00:00:00 UTC may appear in two adjacent day
files; dedupe by ticker at load time.

Rate limit: RATE_LIMIT_SECONDS between requests (default 0.25s = 4 req/s, well under
Kalshi's documented basic tier), with exponential backoff on 429/5xx.

Data root: defaults to the tree this file lives in. Point it at the live pilot tree's
gitignored historical-data/ with --data-root or the DV3_DATA_ROOT env var, so a
worktree checkout writes into the canonical store without dirtying either tree.

Usage:
  python tools/fetch_history.py                 # legacy stages, full retention window
  python tools/fetch_history.py --stage metadata
  python tools/fetch_history.py --start 2026-08-01 --end 2026-08-17
  python tools/fetch_history.py --stage range --data-root C:/.../historical-data --quiet-guard
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

PROXY = "http://127.0.0.1:8642/trade-api/v2"
DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "historical-data"

# ROOT / SERIES are rebuilt in main() once the data root is resolved (see set_root()).
ROOT = DEFAULT_ROOT
SERIES = {
    "KXBTC15M": ROOT / "15-minute",
    "KXBTCD": ROOT / "1-hour",
    "KXBTC": ROOT / "1-hour-range",
}
# Legacy census series — stage_metadata iterates only these so existing `all`/`metadata`
# runs are unchanged; the range series has its own dedicated stages.
LEGACY_SERIES = ["KXBTC15M", "KXBTCD"]

EARLIEST = date(2026, 6, 11)  # measured retention edge, 2026-08-18
BAND_DOLLARS = 400.0  # hourly strikes within this of the 15M anchor get candles
RATE_LIMIT_SECONDS = 0.25
PAGE_LIMIT = 200
TRADE_PAGE_LIMIT = 1000  # GetTrades max page size

# SEAL: the Rung-1 read-only holdout (historical-data/SEAL.md). Registered spec — the
# fetcher enforces it rather than trusting invocation flags. Never widen without Brad.
SEALED_START = date(2026, 8, 2)
SEALED_END = date(2026, 8, 18)

# Quiet-window guard (house rule): no bulk proxy traffic in [38, 59] min-of-hour.
QUIET_START_MIN = 38
_quiet_guard_enabled = False

_session = requests.Session()
_last_request = 0.0


def set_root(root: Path) -> None:
    """Repoint the data root and rebuild the per-series output dirs."""
    global ROOT, SERIES
    ROOT = Path(root)
    SERIES = {
        "KXBTC15M": ROOT / "15-minute",
        "KXBTCD": ROOT / "1-hour",
        "KXBTC": ROOT / "1-hour-range",
    }


def is_sealed(d: date) -> bool:
    """True if UTC day `d` is inside the registered Rung-1 holdout window."""
    return SEALED_START <= d <= SEALED_END


def in_quiet_window(dt: datetime) -> bool:
    """True if `dt`'s minute-of-hour is inside the pilot's quiet window [38, 59]."""
    return dt.minute >= QUIET_START_MIN


def seconds_until_top_of_hour(dt: datetime) -> float:
    """Seconds from `dt` to the next :00:00 (exclusive of the current partial second)."""
    return (59 - dt.minute) * 60 + (60 - dt.second) - dt.microsecond / 1_000_000.0


def wait_out_quiet_window(now_fn=None, sleep_fn=time.sleep, log_fn=print) -> None:
    """If inside the quiet window, sleep past the top of the next hour.

    `now_fn`/`sleep_fn`/`log_fn` are injectable for unit tests.
    """
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    now = now_fn()
    if not in_quiet_window(now):
        return
    secs = seconds_until_top_of_hour(now) + 1.0  # +1s cushion past :00
    log_fn(f"  [quiet] {now.strftime('%H:%M:%SZ')} in pilot window; sleeping {secs:.0f}s to top of hour")
    sleep_fn(secs)


def api_get(path: str, params: dict | None = None) -> dict:
    """Rate-limited GET through the proxy with backoff on 429/5xx."""
    global _last_request
    if _quiet_guard_enabled:
        wait_out_quiet_window()
    for attempt in range(6):
        wait = RATE_LIMIT_SECONDS - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
        resp = _session.get(f"{PROXY}{path}", params=params, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429 or resp.status_code >= 500:
            backoff = 2**attempt
            print(f"  [retry] {resp.status_code} on {path}, sleeping {backoff}s")
            time.sleep(backoff)
            continue
        raise RuntimeError(f"GET {path} -> {resp.status_code}: {resp.text[:200]}")
    raise RuntimeError(f"GET {path}: exhausted retries")


def day_bounds(d: date) -> tuple[int, int]:
    start = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
    return start, start + 86400


def write_jsonl_atomic(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".partial")
    with partial.open("w", encoding="utf-8") as f:
        for obj in lines:
            f.write(json.dumps(obj, separators=(",", ":")) + "\n")
    partial.replace(path)


def write_jsonl_gz_atomic(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".partial")
    with gzip.open(partial, "wt", encoding="utf-8") as f:
        for obj in lines:
            f.write(json.dumps(obj, separators=(",", ":")) + "\n")
    partial.replace(path)


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def fetch_markets_day(series: str, d: date) -> list[dict]:
    """All settled markets of `series` closing within UTC day `d` (raw objects)."""
    start, end = day_bounds(d)
    markets: list[dict] = []
    cursor = None
    while True:
        params = {
            "series_ticker": series, "status": "settled", "limit": PAGE_LIMIT,
            "min_close_ts": start, "max_close_ts": end,
        }
        if cursor:
            params["cursor"] = cursor
        page = api_get("/markets", params)
        markets.extend(page.get("markets") or [])
        cursor = page.get("cursor")
        if not cursor or not page.get("markets"):
            return markets


def fetch_candles(series: str, market: dict, max_span_seconds: int = 3600) -> dict:
    """1-minute candlesticks for one market (raw response + ticker).

    Range is clamped to the final `max_span_seconds` before close: some KXBTCD
    markets are listed days before their hour (observed 169h open-to-close), which
    blows the API's 5000-candle cap — and the pair only exists near the close.
    Uses only ticker/open_time/close_time, so it is agnostic to floor/cap strike
    (works for range buckets and open-ended tails alike).
    """
    open_ts = int(datetime.fromisoformat(market["open_time"].replace("Z", "+00:00")).timestamp())
    close_ts = int(datetime.fromisoformat(market["close_time"].replace("Z", "+00:00")).timestamp())
    open_ts = max(open_ts, close_ts - max_span_seconds)
    resp = api_get(
        f"/series/{series}/markets/{market['ticker']}/candlesticks",
        {"start_ts": open_ts, "end_ts": close_ts, "period_interval": 1},
    )
    return {"ticker": market["ticker"], "candlesticks": resp.get("candlesticks") or []}


def fetch_trades(market: dict, max_span_seconds: int = 3600) -> dict:
    """Full executed-trades tape for one market (raw trade objects + ticker).

    Same final-hour clamp as fetch_candles: long-listed KXBTCD markets only
    matter near the close, and it bounds pagination. API pages newest-first;
    stored in received order (newest-first) — order at load time by created_time.
    Uses only ticker/open_time/close_time, so it is strike-agnostic.
    """
    open_ts = int(datetime.fromisoformat(market["open_time"].replace("Z", "+00:00")).timestamp())
    close_ts = int(datetime.fromisoformat(market["close_time"].replace("Z", "+00:00")).timestamp())
    open_ts = max(open_ts, close_ts - max_span_seconds)
    trades: list[dict] = []
    cursor = None
    while True:
        params = {
            "ticker": market["ticker"], "limit": TRADE_PAGE_LIMIT,
            "min_ts": open_ts, "max_ts": close_ts,
        }
        if cursor:
            params["cursor"] = cursor
        page = api_get("/markets/trades", params)
        batch = page.get("trades") or []
        trades.extend(batch)
        cursor = page.get("cursor")
        if not cursor or not batch:
            return {"ticker": market["ticker"], "trades": trades}


def in_band_1h(d: date) -> list[dict] | None:
    """Hourly markets within BAND_DOLLARS of the same-close-time 15M anchor."""
    meta15 = SERIES["KXBTC15M"] / "markets" / f"{d.isoformat()}.jsonl"
    meta1h = SERIES["KXBTCD"] / "markets" / f"{d.isoformat()}.jsonl"
    if not meta15.exists() or not meta1h.exists():
        return None
    anchors = {m["close_time"]: m.get("floor_strike") for m in read_jsonl(meta15)}
    selected = []
    for m in read_jsonl(meta1h):
        anchor = anchors.get(m["close_time"])
        strike = m.get("floor_strike")
        if anchor is not None and strike is not None and abs(strike - anchor) <= BAND_DOLLARS:
            selected.append(m)
    return selected


def stage_metadata(days: list[date]) -> None:
    for series in LEGACY_SERIES:
        base = SERIES[series]
        for d in days:
            out = base / "markets" / f"{d.isoformat()}.jsonl"
            if out.exists():
                continue
            markets = fetch_markets_day(series, d)
            write_jsonl_atomic(out, markets)
            print(f"[metadata] {series} {d}: {len(markets)} markets")


def stage_candles15(days: list[date]) -> None:
    base = SERIES["KXBTC15M"]
    for d in days:
        meta_path = base / "markets" / f"{d.isoformat()}.jsonl"
        out = base / "candles" / f"{d.isoformat()}.jsonl"
        if out.exists() or not meta_path.exists():
            continue
        markets = read_jsonl(meta_path)
        rows = [fetch_candles("KXBTC15M", m) for m in markets]
        write_jsonl_atomic(out, rows)
        print(f"[candles15] {d}: {len(rows)} markets")


def stage_candles1h(days: list[date]) -> None:
    base15, base1h = SERIES["KXBTC15M"], SERIES["KXBTCD"]
    for d in days:
        meta15 = base15 / "markets" / f"{d.isoformat()}.jsonl"
        meta1h = base1h / "markets" / f"{d.isoformat()}.jsonl"
        out = base1h / "candles" / f"{d.isoformat()}.jsonl"
        if out.exists() or not meta15.exists() or not meta1h.exists():
            continue
        # anchor per close_time from the 15M side; keep hourly strikes near it
        anchors = {m["close_time"]: m.get("floor_strike") for m in read_jsonl(meta15)}
        selected = []
        for m in read_jsonl(meta1h):
            anchor = anchors.get(m["close_time"])
            strike = m.get("floor_strike")
            if anchor is not None and strike is not None and abs(strike - anchor) <= BAND_DOLLARS:
                selected.append(m)
        rows = [fetch_candles("KXBTCD", m) for m in selected]
        write_jsonl_atomic(out, rows)
        print(f"[candles1h] {d}: {len(rows)} in-band markets (of {sum(1 for _ in read_jsonl(meta1h))})")


def stage_trades15(days: list[date]) -> None:
    base = SERIES["KXBTC15M"]
    for d in days:
        meta_path = base / "markets" / f"{d.isoformat()}.jsonl"
        out = base / "trades" / f"{d.isoformat()}.jsonl.gz"
        if out.exists() or not meta_path.exists():
            continue
        rows = [fetch_trades(m) for m in read_jsonl(meta_path)]
        write_jsonl_gz_atomic(out, rows)
        print(f"[trades15] {d}: {len(rows)} markets, {sum(len(r['trades']) for r in rows)} trades")


def stage_trades1h(days: list[date]) -> None:
    base = SERIES["KXBTCD"]
    for d in days:
        out = base / "trades" / f"{d.isoformat()}.jsonl.gz"
        if out.exists():
            continue
        selected = in_band_1h(d)
        if selected is None:
            continue
        rows = [fetch_trades(m) for m in selected]
        write_jsonl_gz_atomic(out, rows)
        print(f"[trades1h] {d}: {len(rows)} in-band markets, {sum(len(r['trades']) for r in rows)} trades")


# --- range series (KXBTC) stages ------------------------------------------------

def stage_metadata_range(days: list[date]) -> None:
    base = SERIES["KXBTC"]
    for d in days:
        out = base / "markets" / f"{d.isoformat()}.jsonl"
        if out.exists():
            continue
        markets = fetch_markets_day("KXBTC", d)
        write_jsonl_atomic(out, markets)
        # markets-per-hour sanity: count distinct close_times and the min per-hour count
        by_hour: dict[str, int] = {}
        for m in markets:
            by_hour[m.get("close_time", "?")] = by_hour.get(m.get("close_time", "?"), 0) + 1
        per_hour_min = min(by_hour.values()) if by_hour else 0
        print(f"[metadata_range] {d}: {len(markets)} markets, {len(by_hour)} hours, min/hour={per_hour_min}")


def stage_candles_range(days: list[date]) -> None:
    base = SERIES["KXBTC"]
    for d in days:
        meta_path = base / "markets" / f"{d.isoformat()}.jsonl"
        out = base / "candles" / f"{d.isoformat()}.jsonl"
        if out.exists() or not meta_path.exists():
            continue
        markets = read_jsonl(meta_path)
        rows = [fetch_candles("KXBTC", m) for m in markets]
        with_candles = sum(1 for r in rows if r["candlesticks"])
        write_jsonl_atomic(out, rows)
        print(f"[candles_range] {d}: {len(rows)} markets, {with_candles} with candles")


def stage_trades_range(days: list[date]) -> None:
    base = SERIES["KXBTC"]
    for d in days:
        meta_path = base / "markets" / f"{d.isoformat()}.jsonl"
        out = base / "trades" / f"{d.isoformat()}.jsonl.gz"
        if out.exists() or not meta_path.exists():
            continue
        rows = [fetch_trades(m) for m in read_jsonl(meta_path)]
        write_jsonl_gz_atomic(out, rows)
        print(f"[trades_range] {d}: {len(rows)} markets, {sum(len(r['trades']) for r in rows)} trades")


def probe_earliest(series: str, guess: date, floor: date) -> date:
    """Find the earliest UTC day (>= floor) for which `series` returns any settled
    market, walking outward from `guess`. Retention rolls ~68 days, so `guess`
    should sit near the expected edge."""
    def has_markets(d: date) -> bool:
        start, end = day_bounds(d)
        page = api_get("/markets", {
            "series_ticker": series, "status": "settled", "limit": 1,
            "min_close_ts": start, "max_close_ts": end,
        })
        return bool(page.get("markets"))

    d = guess
    if has_markets(d):
        # walk backward until empty, return last non-empty
        prev = d
        while d > floor:
            d -= timedelta(days=1)
            if has_markets(d):
                prev = d
            else:
                break
        print(f"[probe] earliest {series} day with markets: {prev}")
        return prev
    # guess empty: walk forward until markets appear
    while d < guess + timedelta(days=90):
        d += timedelta(days=1)
        if has_markets(d):
            print(f"[probe] earliest {series} day with markets: {d}")
            return d
    raise RuntimeError(f"probe_earliest({series}): no markets found near {guess}")


def build_days(start: date, end: date, acknowledge_sealed: bool) -> list[date]:
    """Inclusive day list, with sealed-window days removed unless acknowledged."""
    days: list[date] = []
    skipped: list[date] = []
    d = start
    while d <= end:
        if is_sealed(d) and not acknowledge_sealed:
            skipped.append(d)
        else:
            days.append(d)
        d += timedelta(days=1)
    if skipped:
        print(f"[seal] REFUSED {len(skipped)} sealed day(s) {skipped[0]}..{skipped[-1]} "
              f"(2026-08-02..2026-08-18 holdout; pass --acknowledge-sealed to override)")
    return days


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=[
        "metadata", "candles15", "candles1h", "trades15", "trades1h", "all",
        "metadata_range", "candles_range", "trades_range", "range",
    ], default="all")
    parser.add_argument("--start", type=date.fromisoformat, default=None,
                        help="earliest UTC day; default EARLIEST for legacy stages, "
                             "auto-probed retention edge for range stages")
    parser.add_argument("--end", type=date.fromisoformat,
                        default=datetime.now(timezone.utc).date() - timedelta(days=1))
    parser.add_argument("--data-root", default=None,
                        help="historical-data dir to write into (or set DV3_DATA_ROOT). "
                             "Defaults to this tree's historical-data/.")
    parser.add_argument("--acknowledge-sealed", action="store_true",
                        help="permit writing the 2026-08-02..18 sealed holdout window. "
                             "House law: builder agents never set this.")
    parser.add_argument("--quiet-guard", action="store_true",
                        help="sleep out the pilot quiet window [:38-:59] before each request")
    parser.add_argument("--done-marker", default=None,
                        help="path of a marker file to touch on clean completion")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

    root = args.data_root or os.environ.get("DV3_DATA_ROOT")
    if root:
        set_root(Path(root))
    print(f"[fetch] data root: {ROOT}")

    global _quiet_guard_enabled
    _quiet_guard_enabled = args.quiet_guard
    if _quiet_guard_enabled:
        print("[fetch] quiet-window guard ACTIVE (:38-:59 min-of-hour -> sleep to top of hour)")

    range_stages = args.stage in ("metadata_range", "candles_range", "trades_range", "range")

    start = args.start
    if start is None:
        if range_stages:
            # retention edge moves daily; probe near ~68 days back from today
            guess = datetime.now(timezone.utc).date() - timedelta(days=66)
            start = probe_earliest("KXBTC", guess, floor=date(2026, 6, 1))
        else:
            start = EARLIEST

    days = build_days(start, args.end, args.acknowledge_sealed)
    if not days:
        print("[fetch] no days to fetch after seal filter; nothing to do")
        return
    print(f"[fetch] {args.stage}: {days[0]} .. {days[-1]} ({len(days)} days)")

    health = _session.get("http://127.0.0.1:8642/health", timeout=5).json()
    print(f"[fetch] proxy ok: env={health['env']} signed={health['signed']}")

    if args.stage in ("metadata", "all"):
        stage_metadata(days)
    if args.stage in ("candles15", "all"):
        stage_candles15(days)
    if args.stage in ("candles1h", "all"):
        stage_candles1h(days)
    if args.stage in ("trades15", "all"):
        stage_trades15(days)
    if args.stage in ("trades1h", "all"):
        stage_trades1h(days)
    if args.stage in ("metadata_range", "range"):
        stage_metadata_range(days)
    if args.stage in ("candles_range", "range"):
        stage_candles_range(days)
    if args.stage == "trades_range":
        stage_trades_range(days)

    if args.done_marker:
        Path(args.done_marker).parent.mkdir(parents=True, exist_ok=True)
        Path(args.done_marker).write_text(
            f"done {datetime.now(timezone.utc).isoformat()} stage={args.stage} "
            f"{days[0]}..{days[-1]} ({len(days)} days)\n", encoding="utf-8")
    print("[fetch] done")


if __name__ == "__main__":
    main()
