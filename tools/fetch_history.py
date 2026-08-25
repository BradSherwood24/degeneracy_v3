"""Pull Kalshi historical data for the corridor census, via the local signing proxy.

Fetches, for KXBTC15M (15-minute) and KXBTCD (1-hour) BTC series:
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

House law: record the RAWS (verbatim API objects), never derived views. Retention
measured 2026-08-18: settled markets reach back to ~2026-06-11 (rolling ~68 days),
so run this regularly to keep extending the tail before it falls off the back.

Layout (one file per UTC day, JSONL, one raw object per line):
  historical-data/15-minute/markets/YYYY-MM-DD.jsonl   raw market objects
  historical-data/15-minute/candles/YYYY-MM-DD.jsonl   {"ticker":..., "candlesticks":[...]}
  historical-data/15-minute/trades/YYYY-MM-DD.jsonl.gz {"ticker":..., "trades":[...]}
  historical-data/1-hour/markets/YYYY-MM-DD.jsonl
  historical-data/1-hour/candles/YYYY-MM-DD.jsonl
  historical-data/1-hour/trades/YYYY-MM-DD.jsonl.gz

Trades are gzipped (still verbatim raw objects, losslessly compressed): the 15M tape
runs ~1M trades/~300MB per day uncompressed, which would not fit the disk raw.

Resumable: a day file that exists is skipped (written atomically via .partial rename),
so re-running only fetches missing days. Today (UTC) is never written — only complete,
settled days. A market closing exactly at 00:00:00 UTC may appear in two adjacent day
files; dedupe by ticker at load time.

Rate limit: RATE_LIMIT_SECONDS between requests (default 0.25s = 4 req/s, well under
Kalshi's documented basic tier), with exponential backoff on 429/5xx.

Usage:
  python tools/fetch_history.py                 # all stages, full retention window
  python tools/fetch_history.py --stage metadata
  python tools/fetch_history.py --start 2026-08-01 --end 2026-08-17
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

PROXY = "http://127.0.0.1:8642/trade-api/v2"
ROOT = Path(__file__).resolve().parent.parent / "historical-data"

SERIES = {"KXBTC15M": ROOT / "15-minute", "KXBTCD": ROOT / "1-hour"}
EARLIEST = date(2026, 6, 11)  # measured retention edge, 2026-08-18
BAND_DOLLARS = 400.0  # hourly strikes within this of the 15M anchor get candles
RATE_LIMIT_SECONDS = 0.25
PAGE_LIMIT = 200
TRADE_PAGE_LIMIT = 1000  # GetTrades max page size

_session = requests.Session()
_last_request = 0.0


def api_get(path: str, params: dict | None = None) -> dict:
    """Rate-limited GET through the proxy with backoff on 429/5xx."""
    global _last_request
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
    partial = path.with_suffix(".partial")
    with partial.open("w", encoding="utf-8") as f:
        for obj in lines:
            f.write(json.dumps(obj, separators=(",", ":")) + "\n")
    partial.replace(path)


def write_jsonl_gz_atomic(path: Path, lines: list[dict]) -> None:
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
    for series, base in SERIES.items():
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=["metadata", "candles15", "candles1h", "trades15", "trades1h", "all"], default="all")
    parser.add_argument("--start", type=date.fromisoformat, default=EARLIEST)
    parser.add_argument("--end", type=date.fromisoformat,
                        default=datetime.now(timezone.utc).date() - timedelta(days=1))
    args = parser.parse_args()

    sys.stdout.reconfigure(line_buffering=True)
    days = []
    d = args.start
    while d <= args.end:
        days.append(d)
        d += timedelta(days=1)
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
    print("[fetch] done")


if __name__ == "__main__":
    main()
