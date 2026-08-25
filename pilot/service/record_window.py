"""Passive window recorder — Phase 1's live deliverable (NO signal logic, NO orders).

Wakes for a top-of-hour close, discovers the co-settling leg pair (stands down cleanly if none),
connects the WS via the proxy, subscribes both legs, drives BookMirror + Journal until the close +
a 10s grace, flushes the journal to pilot/journals/<close_time>.jsonl, appends a one-line window
summary to pilot/journals/summary.jsonl, and exits. Robust to Ctrl+C (flushes what is buffered).

This module is genuinely runnable (`python -m service.record_window`), but is written so every
network edge is injected: `WindowRecorder` holds the pure wiring (callbacks feed books, the tap
journals, tops are captured for golden-replay parity), `watchdog_action` is a pure decision, and
`run_recording` drives the connect/reconnect loop with an injectable supervisor sleep — so the whole
thing is smoke-tested with fakes and NEVER dialed against the live proxy from the test suite.

DO NOT run this against the live proxy from an automated context — it opens a real signed socket.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import logging
import os
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from service.book import BookMirror, TopOfBook
from service.journal import Journal
from service.proxy_auth import ProxyAuth
from service.wake import StandDown, WakeContext, WakeResult, close_epoch
from service.ws_client import KalshiWebSocketClient, WsCallbacks

logger = logging.getLogger(__name__)

GRACE_SECONDS = 10
# Force-close (reconnect) watchdog thresholds for the PASSIVE spine. Generous — they catch a
# dead/badly-lagged stream, not the tight entry-gate freshness (that is a Phase 2 decide() concern).
DEFAULT_LAG_THRESHOLD = 30.0
DEFAULT_SILENCE_THRESHOLD = 45.0
DEFAULT_POLL_SECONDS = 0.5

CONTINUE = "continue"
FORCE_CLOSE = "force_close"
DEADLINE = "deadline"

DEFAULT_JOURNAL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "journals")


def next_top_of_hour_iso(now_epoch: float) -> str:
    """The next :00:00 UTC strictly after `now_epoch`, as an ISO 'Z' string."""
    now = _dt.datetime.fromtimestamp(now_epoch, tz=_dt.timezone.utc)
    top = now.replace(minute=0, second=0, microsecond=0) + _dt.timedelta(hours=1)
    return top.strftime("%Y-%m-%dT%H:%M:%SZ")


def watchdog_action(
    now: float,
    deadline: float,
    data_age: float | None,
    silence: float,
    lag_threshold: float,
    silence_threshold: float,
) -> str:
    """Pure watchdog decision for the supervisor.

    - `now >= deadline` -> DEADLINE (stop the window).
    - measured data_age (lag-or-silence, silence-aware) over the lag threshold -> FORCE_CLOSE.
    - raw silence over the silence threshold -> FORCE_CLOSE (catches a dead TCP session).
    - data_age None (unmeasured — the per-dial startup grace before the first timestamped frame) is
      NOT force-closed here; the silence gauge covers a stream that never delivers. Everything else
      -> CONTINUE.
    """
    if now >= deadline:
        return DEADLINE
    if data_age is not None and data_age > lag_threshold:
        return FORCE_CLOSE
    if silence > silence_threshold:
        return FORCE_CLOSE
    return CONTINUE


class WindowRecorder:
    """Pure-ish wiring around a Journal + per-market BookMirrors for one window.

    Feeds each orderbook frame into its market's book and captures the resulting top-of-book keyed to
    the journal index the recorder tap just assigned — so `book_tops` is exactly what
    `replay.replay_books(journal)` reconstructs (Phase 1 golden-replay parity)."""

    def __init__(
        self,
        wake_result: WakeResult,
        journal: Journal,
        clock: Callable[[], float] = time.time,
        capture_tops: bool = True,
    ) -> None:
        self.wake = wake_result
        self.journal = journal
        self.clock = clock
        self.capture_tops = capture_tops
        self.books: dict[str, BookMirror] = {}
        self.book_tops: list[tuple[int, str, TopOfBook]] = []
        self.counts: dict[str, int] = defaultdict(int)
        self.ws_client: KalshiWebSocketClient | None = None
        self.callbacks = WsCallbacks(
            on_orderbook_snapshot=self._on_snapshot,
            on_orderbook_delta=self._on_delta,
            on_trade=self._on_trade,
            on_ticker=self._on_ticker,
        )

    # --- recorder tap (wired as the WS client's `record` callback) ---
    def tap(self, stream: str, envelope: dict) -> None:
        """Journal the raw envelope BEFORE dispatch (house law: journal before dispatch)."""
        self.journal.append(stream, envelope, self.clock())
        self.counts["ws_" + str(envelope.get("type"))] += 1

    # --- per-channel dispatch (market-keyed) ---
    def _book(self, market: str) -> BookMirror:
        book = self.books.get(market)
        if book is None:
            book = BookMirror()
            self.books[market] = book
        return book

    def _capture(self, market: str, book: BookMirror) -> None:
        if not self.capture_tops:
            return
        # The tap already appended this envelope, so its idx is the current last index.
        idx = len(self.journal) - 1
        self.book_tops.append((idx, market, book.top_of_book()))

    def _on_snapshot(self, market: str, payload: dict) -> None:
        book = self._book(market)
        book.apply_snapshot(payload)
        self._capture(market, book)

    def _on_delta(self, market: str, payload: dict) -> None:
        book = self._book(market)
        book.apply_delta(payload)
        self._capture(market, book)

    def _on_trade(self, market: str, payload: dict) -> None:
        self.counts["trade"] += 1  # journaled by the tap; corroboration only

    def _on_ticker(self, market: str, payload: dict) -> None:
        self.counts["ticker"] += 1

    def mark_all_suspect(self) -> None:
        """A reconnect is imminent (seq gap / dropped socket): every book is suspect until its next
        fresh snapshot rebuilds it (fail closed)."""
        for book in self.books.values():
            book.mark_suspect()

    def record_alarm(self, kind: str, obj: dict) -> None:
        self.journal.append("alarm", {"alarm": kind, **obj}, self.clock())
        self.counts["alarm"] += 1

    # --- finalize ---
    def flush(self, journal_path: str, summary_path: str) -> dict:
        """Flush the journal and append a one-line window summary. Returns the summary dict."""
        n = self.journal.flush(journal_path)
        ws = self.ws_client
        summary = {
            "close_time": self.wake.close_time,
            "stand_down": False,
            "journal_path": os.path.abspath(journal_path),
            "records": n,
            "tickers": self.wake.subscribe_tickers(),
            "counts": dict(self.counts),
            "tops_captured": len(self.book_tops),
            "ladder_ok": self.wake.ladder.ok,
            "strangle_disabled": self.wake.ladder.strangle_disabled,
            "ladder_alarm": self.wake.ladder.alarm,
            "last_lag_seconds": ws.current_lag_seconds() if ws else None,
            "last_silence_seconds": ws.silence_seconds() if ws else None,
            "dropped_no_market": ws.dropped_no_market if ws else 0,
            "flushed_at": self.clock(),
        }
        _append_summary(summary_path, summary)
        return summary


def _append_summary(summary_path: str, summary: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(summary_path)), exist_ok=True)
    with open(summary_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(summary, sort_keys=True))
        f.write("\n")


def write_standdown_summary(summary_path: str, stand_down: StandDown, clock: Callable[[], float]) -> dict:
    summary = {
        "close_time": stand_down.close_time,
        "stand_down": True,
        "reason": stand_down.reason,
        "flushed_at": clock(),
    }
    _append_summary(summary_path, summary)
    return summary


async def run_recording(
    recorder: WindowRecorder,
    deadline: float,
    *,
    lag_threshold: float = DEFAULT_LAG_THRESHOLD,
    silence_threshold: float = DEFAULT_SILENCE_THRESHOLD,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> WindowRecorder:
    """Drive connect/reconnect until the deadline, with a supervisor that force-closes on a watchdog
    trip or at the deadline. `sleep` is injected so tests drive the supervisor deterministically."""
    ws = recorder.ws_client
    assert ws is not None, "recorder.ws_client must be set before run_recording"
    while recorder.clock() < deadline:
        stop = asyncio.Event()

        async def supervise() -> None:
            while not stop.is_set():
                await sleep(poll_seconds)
                if stop.is_set():
                    return
                action = watchdog_action(
                    recorder.clock(),
                    deadline,
                    ws.data_age_seconds(),
                    ws.silence_seconds(),
                    lag_threshold,
                    silence_threshold,
                )
                if action == CONTINUE:
                    continue
                if action == FORCE_CLOSE:
                    recorder.record_alarm(
                        "watchdog_stale",
                        {
                            "data_age_seconds": ws.data_age_seconds(),
                            "silence_seconds": ws.silence_seconds(),
                        },
                    )
                await ws.force_close()
                return

        sup = asyncio.create_task(supervise())
        try:
            await ws.connect()
        except Exception as e:  # noqa: BLE001 - a dial failure is logged + journaled, then retried
            logger.warning("[RECORD] connection error: %s", e)
            recorder.record_alarm("ws_error", {"error": str(e)})
        finally:
            stop.set()
            await sup
        if recorder.clock() < deadline:
            recorder.mark_all_suspect()  # reconnect imminent: books suspect until fresh snapshots
    return recorder


def _build_recorder_and_ws(
    result: WakeResult,
    proxy: ProxyAuth,
    clock: Callable[[], float],
    max_hourly: int | None,
) -> WindowRecorder:
    journal = Journal()
    # Journal the wake context first (record 0) so the window's provenance is in the replay file.
    journal.append(
        "window_meta",
        {
            "close_time": result.close_time,
            "fifteen_ticker": result.fifteen_leg.primary_ticker,
            "hourly_event": result.hourly_leg.event_ticker,
            "ladder_ok": result.ladder.ok,
            "expected_step": str(result.ladder.expected_step),
            "observed_step": None if result.ladder.observed_step is None else str(result.ladder.observed_step),
            "strangle_disabled": result.ladder.strangle_disabled,
        },
        clock(),
    )
    recorder = WindowRecorder(result, journal, clock=clock)
    ws = KalshiWebSocketClient(
        proxy_auth=proxy,
        tickers=result.subscribe_tickers(hourly_limit=max_hourly),
        callbacks=recorder.callbacks,
        include_private=False,
        record=recorder.tap,
        clock=clock,
    )
    recorder.ws_client = ws
    return recorder


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Passive window recorder (Phase 1 spine).")
    parser.add_argument("--close-time", default=None, help="Target close ISO (UTC). Default: next :00.")
    parser.add_argument("--max-hourly-strikes", type=int, default=None,
                        help="Cap the hourly ladder subscription to N tickers (default: full ladder).")
    parser.add_argument("--journal-dir", default=DEFAULT_JOURNAL_DIR)
    parser.add_argument("--proxy-base", default=None, help="Override the proxy base URL.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    clock = time.time
    proxy = ProxyAuth(base_url=args.proxy_base) if args.proxy_base else ProxyAuth()
    wake = WakeContext(proxy, clock=clock)
    close_iso = args.close_time or next_top_of_hour_iso(clock())
    summary_path = os.path.join(args.journal_dir, "summary.jsonl")

    result = wake.sweep(close_iso)
    if isinstance(result, StandDown):
        s = write_standdown_summary(summary_path, result, clock)
        logger.info("[RECORD] stand down: %s", s)
        return 0

    recorder = _build_recorder_and_ws(result, proxy, clock, args.max_hourly_strikes)
    deadline = close_epoch(close_iso) + GRACE_SECONDS
    journal_path = os.path.join(args.journal_dir, close_iso.replace(":", "").replace("-", "") + ".jsonl")
    try:
        asyncio.run(run_recording(recorder, deadline=deadline))
    except KeyboardInterrupt:
        logger.warning("[RECORD] Ctrl+C — flushing buffered journal.")
    finally:
        summary = recorder.flush(journal_path, summary_path)
        logger.info("[RECORD] window done: %s", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
