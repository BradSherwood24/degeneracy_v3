"""Passive RANGE-bucket recorder — a ZERO-ORDER, memory-light spine for the hourly KXBTC range
markets. SEPARATE process from the armed pilot (one process, one job): it NEVER places an order,
NEVER touches the box/corridor strategy, and only reads market data through the local signing proxy.

What it does, in order:

  * Discovers, via the proxy REST /markets endpoint, EVERY KXBTC range-bucket market co-settling at
    the target top-of-hour close (all widths, all live generations — so the 21:00 UTC $250 hour is
    captured alongside the ordinary $100 hours). Stands down cleanly (exit 0, one-line summary) if
    none are found.
  * Connects the WS via the proxy auth exactly like `record_window`, subscribing the range tickers
    to orderbook_delta + trade ONLY (ticker is skipped to cut frame volume — a range hour spans
    ~180 thin buckets).
  * STREAMS raw frames straight to disk (`journals_range/<close_time>.jsonl`) using the SAME record
    shape as the pilot journals ({"idx","kind":"kalshi_ws","local_ts","obj":{"type","msg"}}), so the
    existing journal readers/extractors work unchanged. It is MEMORY-LIGHT by construction: NO
    BookMirror, NO per-frame accumulation — a write-through `StreamJournal` appends each record to an
    open file handle and flushes every N frames, so RSS stays flat regardless of window length.
  * Runs from launch (default start = T-60 min if launched earlier, else immediately) until the
    close + a 10 s grace, reconnecting on lag/silence via the SAME watchdog decision `record_window`
    uses (resubscribe on every re-dial delivers a fresh snapshot). On the deadline (or Ctrl+C) it
    flushes, gzips the journal crash-safely (reusing `journal_io` helpers), writes a one-line
    summary, and exits 0.

Like `record_window`, every network edge is injected so the whole thing is smoke-tested with fakes
and NEVER dialed against the live proxy from the test suite. DO NOT run this against the live proxy
from an automated context — it opens a real signed socket.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from service.journal import _json_default
from service.journal_io import _gzip_one_crash_safe
from service.proxy_auth import ProxyAuth
from service.record_window import (
    GRACE_SECONDS,
    _append_summary,
    next_top_of_hour_iso,
    run_recording,
    watchdog_action,  # noqa: F401 — re-exported so callers/tests share the one watchdog decision
    write_standdown_summary,
)
from service.wake import (
    DEAD_STATUSES,
    MARKETS_PATH,
    StandDown,
    _group_ladders,
    _leg_is_live,
    close_epoch,
    coerce_exchange_index,
)
from service.ws_client import KalshiWebSocketClient, WsCallbacks

logger = logging.getLogger(__name__)

# The hourly BTC RANGE-bucket series (each market is a floor..cap bucket), DISTINCT from KXBTCD (the
# above/threshold ladder the armed strategy pairs). We record ALL co-settling buckets, all widths.
RANGE_SERIES = "KXBTC"

# orderbook_delta yields the initial snapshot then deltas; trade is the tape. ticker is skipped to
# save volume (the recorder never needs a mid/last — it just archives the book + tape).
RANGE_CHANNELS: tuple[str, ...] = ("orderbook_delta", "trade")

# Default: begin recording 60 minutes before the close (the full hour). Launched later -> start now.
DEFAULT_START_LEAD_SECONDS = 60 * 60
# Flush the write-through journal to the OS every this-many frames (bounded staging, flat RSS).
DEFAULT_FLUSH_EVERY = 200
_MAX_PAGES = 50  # pagination safety cap (mirrors wake._MAX_PAGES)
_PRESTART_POLL_SECONDS = 5.0

DEFAULT_JOURNAL_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "journals_range"
)


def journal_filename(close_iso: str) -> str:
    """`2026-08-20T21:00:00Z` -> `20260820T210000Z.jsonl` (fixed-width, sorts chronologically)."""
    return close_iso.replace(":", "").replace("-", "") + ".jsonl"


# === streaming, memory-light journal (write-through; no in-memory accumulation) ===


class StreamJournal:
    """Append-only JSONL writer that streams each record to an OPEN file handle and flushes every
    `flush_every` frames. Unlike `service.journal.Journal` (which buffers the whole window in memory
    and flushes once at close), this keeps RSS flat over an arbitrarily long, high-volume window —
    the spec's memory constraint. Records carry the SAME {idx, kind, local_ts, obj} shape and the
    same deterministic `json.dumps(sort_keys=True, default=_json_default)` serialization, so a
    StreamJournal file is byte-compatible with the pilot readers (`service.journal.load_journal`,
    `service.journal_io.open_journal`, the replay/extractor paths)."""

    def __init__(self, path: str, flush_every: int = DEFAULT_FLUSH_EVERY) -> None:
        self.path = path
        self.flush_every = max(1, int(flush_every))
        self._f = None  # type: ignore[assignment]
        self._idx = 0
        self._since_flush = 0

    def open(self) -> None:
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        self._f = open(self.path, "w", encoding="utf-8")

    def append(self, kind: str, obj: Any, local_ts: float) -> int:
        """Write one record (gap-free idx, assigned here) and return its index. Flushes to the OS
        every `flush_every` records so the on-disk journal is at most `flush_every` frames behind a
        crash — a strictly SAFER durability posture than the pilot's flush-only-at-close."""
        assert self._f is not None, "StreamJournal.open() must be called before append()"
        idx = self._idx
        self._f.write(json.dumps({"idx": idx, "kind": kind, "local_ts": local_ts, "obj": obj},
                                 sort_keys=True, default=_json_default))
        self._f.write("\n")
        self._idx += 1
        self._since_flush += 1
        if self._since_flush >= self.flush_every:
            self._f.flush()
            self._since_flush = 0
        return idx

    def __len__(self) -> int:
        return self._idx

    def close(self) -> None:
        if self._f is not None:
            try:
                self._f.flush()
            finally:
                self._f.close()
                self._f = None


# === discovery (pure filtering reuses wake helpers; the paged fetch is the only I/O) ===


@dataclass(frozen=True)
class Bucket:
    ticker: str
    floor: float | None
    cap: float | None
    event_ticker: str
    open_time: str
    status: str | None
    exchange_index: int | None


@dataclass(frozen=True)
class RangeDiscovery:
    """The co-settling range-bucket universe for one close (or an empty stand-down)."""

    close_time: str
    buckets: tuple[Bucket, ...] = ()
    generations: int = 0

    @property
    def tickers(self) -> list[str]:
        return [b.ticker for b in self.buckets]

    def window_meta(self) -> dict:
        """The record-0 provenance: close, series, count, and every bucket's floor/cap/routing."""
        return {
            "close_time": self.close_time,
            "series": RANGE_SERIES,
            "ticker_count": len(self.buckets),
            "generations": self.generations,
            "channels": list(RANGE_CHANNELS),
            "buckets": [
                {
                    "ticker": b.ticker,
                    "floor": b.floor,
                    "cap": b.cap,
                    "event_ticker": b.event_ticker,
                    "open_time": b.open_time,
                    "status": b.status,
                    "exchange_index": b.exchange_index,
                }
                for b in self.buckets
            ],
        }


def _fetch_range_markets(proxy: ProxyAuth, close_iso: str) -> list[dict]:
    """Paged GET /markets for the range series, narrowed to the target close (status-agnostic —
    exactly the shape WakeContext._fetch_series_markets uses; final filtering is downstream)."""
    target = close_epoch(close_iso)
    out: list[dict] = []
    cursor: str | None = None
    for _ in range(_MAX_PAGES):
        params: dict[str, Any] = {
            "series_ticker": RANGE_SERIES,
            "min_close_ts": target,
            "max_close_ts": target,
            "limit": 1000,
        }
        if cursor:
            params["cursor"] = cursor
        resp = proxy.rest_get(MARKETS_PATH, params)
        out.extend(resp.get("markets", []) or [])
        cursor = resp.get("cursor") or None
        if not cursor:
            break
    return out


def discover_range_markets(
    proxy: ProxyAuth,
    close_iso: str,
    now_epoch: float,
    dead_statuses=DEAD_STATUSES,
) -> RangeDiscovery:
    """Every LIVE range bucket co-settling at `close_iso`, across ALL widths/generations.

    Unlike wake's leg discovery (which SELECTS one smallest-window ladder), the recorder keeps them
    ALL: `_group_ladders` buckets the co-settling markets by (event_ticker, open_time); each
    all-non-dead generation contributes every one of its markets. Floor AND cap are captured per
    bucket. Returns an empty `RangeDiscovery` (stand-down) when nothing live co-settles."""
    markets = _fetch_range_markets(proxy, close_iso)
    ladders = _group_ladders(markets, close_iso)
    buckets: list[Bucket] = []
    generations = 0
    for lad in ladders:
        if not _leg_is_live(lad, now_epoch, dead_statuses):
            continue  # a fully-dead generation is dropped (fail-closed, matches wake)
        generations += 1
        for m in lad:
            tk = m.get("ticker")
            if not tk:
                continue
            buckets.append(
                Bucket(
                    ticker=str(tk),
                    floor=m.get("floor_strike"),
                    cap=m.get("cap_strike"),
                    event_ticker=str(m.get("event_ticker", "")),
                    open_time=str(m.get("open_time", "")),
                    status=m.get("status"),
                    exchange_index=coerce_exchange_index(m.get("exchange_index")),
                )
            )
    buckets.sort(key=lambda b: b.ticker)
    return RangeDiscovery(close_iso, tuple(buckets), generations)


# === recorder (memory-light: empty callbacks -> no dispatch, no books; the tap is the whole job) ===


class RangeRecorder:
    """Wires the WS recorder tap to a StreamJournal. Exposes exactly the surface `run_recording`
    drives (`ws_client`, `clock`, `record_alarm`, `mark_all_suspect`) so the pilot's proven
    connect/reconnect supervisor is reused verbatim. `callbacks` is EMPTY: with every WsCallbacks
    handler None, `on_message` records via the tap and then dispatches to nothing — no BookMirror,
    no per-frame compute, flat RSS."""

    def __init__(
        self,
        discovery: RangeDiscovery,
        journal: StreamJournal,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.discovery = discovery
        self.journal = journal
        self.clock = clock
        self.counts: dict[str, int] = defaultdict(int)
        self.ws_client: KalshiWebSocketClient | None = None
        self.callbacks = WsCallbacks()  # all None -> tap records, nothing dispatched

    def tap(self, stream: str, envelope: dict) -> None:
        """Journal the raw envelope (house law: journal before dispatch). Same 2-arg RecordCallback
        contract as record_window's tap — local_ts stamped from the injected clock."""
        self.journal.append(stream, envelope, self.clock())
        self.counts["ws_" + str(envelope.get("type"))] += 1

    def mark_all_suspect(self) -> None:
        """No books to invalidate — a re-dial's fresh orderbook_snapshot is simply recorded. Present
        so `run_recording` can call it on every reconnect without special-casing this recorder."""

    def record_alarm(self, kind: str, obj: dict) -> None:
        self.journal.append("alarm", {"alarm": kind, **obj}, self.clock())
        self.counts["alarm"] += 1


def _build_recorder_and_ws(
    discovery: RangeDiscovery,
    journal: StreamJournal,
    proxy: ProxyAuth,
    clock: Callable[[], float],
) -> RangeRecorder:
    recorder = RangeRecorder(discovery, journal, clock=clock)
    ws = KalshiWebSocketClient(
        proxy_auth=proxy,
        tickers=discovery.tickers,
        callbacks=recorder.callbacks,
        include_private=False,
        record=recorder.tap,
        clock=clock,
        channels=RANGE_CHANNELS,
    )
    recorder.ws_client = ws
    return recorder


def _sleep_until(target_epoch: float, clock: Callable[[], float], sleep: Callable[[float], None]) -> None:
    """Block (interruptibly, in <=5 s steps) until `target_epoch`. No-op if already past."""
    while True:
        remaining = target_epoch - clock()
        if remaining <= 0:
            return
        sleep(min(_PRESTART_POLL_SECONDS, remaining))


def _write_success_summary(summary_path: str, recorder: RangeRecorder, discovery: RangeDiscovery,
                           journal_path: str, gz: dict, clock: Callable[[], float]) -> dict:
    ws = recorder.ws_client
    summary = {
        "close_time": discovery.close_time,
        "stand_down": False,
        "series": RANGE_SERIES,
        "journal_path": os.path.abspath(gz.get("final_path") or journal_path),
        "records": len(recorder.journal),
        "ticker_count": len(discovery.buckets),
        "generations": discovery.generations,
        "counts": dict(recorder.counts),
        "gzipped": bool(gz.get("gzipped")),
        "raw_bytes": gz.get("raw_bytes"),
        "gz_bytes": gz.get("gz_bytes"),
        "gzip_error": gz.get("error"),
        "last_lag_seconds": ws.current_lag_seconds() if ws else None,
        "last_silence_seconds": ws.silence_seconds() if ws else None,
        "dropped_no_market": ws.dropped_no_market if ws else 0,
        "flushed_at": clock(),
    }
    _append_summary(summary_path, summary)
    return summary


def _gzip_journal(journal_path: str) -> dict:
    """Crash-safely gzip the finished journal (reusing journal_io) -> {final_path, gzipped, ...}.

    On any compression failure the raw .jsonl is left in place (fail-safe) and the error recorded;
    the recording is never lost to a gzip problem."""
    try:
        raw_bytes, gz_bytes = _gzip_one_crash_safe(journal_path)
        return {"final_path": journal_path + ".gz", "gzipped": True,
                "raw_bytes": raw_bytes, "gz_bytes": gz_bytes, "error": None}
    except Exception as e:  # noqa: BLE001 — keep the raw journal; never lose data to a gzip failure
        logger.warning("[RANGE] gzip failed (%s) — leaving raw journal %s", e, journal_path)
        return {"final_path": journal_path, "gzipped": False,
                "raw_bytes": None, "gz_bytes": None, "error": str(e)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Passive KXBTC range-bucket recorder (zero-order).")
    parser.add_argument("--close-time", default=None, help="Target close ISO (UTC). Default: next :00.")
    parser.add_argument("--journal-dir", default=DEFAULT_JOURNAL_DIR)
    parser.add_argument("--proxy-base", default=None, help="Override the proxy base URL.")
    parser.add_argument("--start-lead-minutes", type=int, default=DEFAULT_START_LEAD_SECONDS // 60,
                        help="Begin recording this many minutes before close if launched earlier "
                             "(default 60 = the full hour). Launched later -> start immediately.")
    parser.add_argument("--flush-every", type=int, default=DEFAULT_FLUSH_EVERY,
                        help="Flush the write-through journal to the OS every N frames (default 200).")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    clock = time.time
    proxy = ProxyAuth(base_url=args.proxy_base) if args.proxy_base else ProxyAuth()
    close_iso = args.close_time or next_top_of_hour_iso(clock())
    summary_path = os.path.join(args.journal_dir, "summary.jsonl")

    # Start = close - lead, but never in the past (launched later -> record immediately).
    start_epoch = close_epoch(close_iso) - max(0, args.start_lead_minutes) * 60
    try:
        _sleep_until(start_epoch, clock, time.sleep)
    except KeyboardInterrupt:
        logger.warning("[RANGE] Ctrl+C during pre-start wait — nothing recorded, standing down.")
        return 0

    discovery = discover_range_markets(proxy, close_iso, clock())
    if not discovery.buckets:
        sd = StandDown(close_iso, f"no KXBTC range markets co-settling at {close_iso}")
        s = write_standdown_summary(summary_path, sd, clock)
        logger.info("[RANGE] stand down: %s", s)
        return 0
    logger.info("[RANGE] recording %d buckets (%d generations) for close %s",
                len(discovery.buckets), discovery.generations, close_iso)

    journal_path = os.path.join(args.journal_dir, journal_filename(close_iso))
    journal = StreamJournal(journal_path, flush_every=args.flush_every)
    journal.open()
    journal.append("window_meta", discovery.window_meta(), clock())
    recorder = _build_recorder_and_ws(discovery, journal, proxy, clock)

    deadline = close_epoch(close_iso) + GRACE_SECONDS
    try:
        run_recording_sync(recorder, deadline)
    except KeyboardInterrupt:
        logger.warning("[RANGE] Ctrl+C — flushing streamed journal.")
    finally:
        journal.close()
        gz = _gzip_journal(journal_path)
        summary = _write_success_summary(summary_path, recorder, discovery, journal_path, gz, clock)
        logger.info("[RANGE] window done: %s", summary)
    return 0


def run_recording_sync(recorder: RangeRecorder, deadline: float) -> None:
    """Thin `asyncio.run` wrapper around the shared `run_recording` supervisor (kept separate so the
    unit tests drive `run_recording` directly with an injected sleep, never touching asyncio.run)."""
    import asyncio

    asyncio.run(run_recording(recorder, deadline=deadline))


if __name__ == "__main__":
    raise SystemExit(main())
