"""V3.2 co-settling KXBTC15M RECORDING (the single-recorder change so the disabled v1.1 pilot leaves
no 15M data gap). FAKES ONLY — never dials the proxy, never opens a socket, never reads a
sealed/holdout date. Covers: discovery over a fake /markets payload (one live 15M, one dead, none);
the 15M ticker rides the bucket connection; a 15M orderbook/trade frame is journaled + counted but
drives NO core event and NO stand-down; the ledger row + report carry m15_tickers / m15_frames; a
missing 15M journals m15_missing and never stands the window down."""

from __future__ import annotations

import asyncio
import json
import os
from decimal import Decimal

from service.book import TopOfBook
from service.record_range import StreamJournal
from service.v32 import V32Params, V32State, load_v32_params
from service.v32.ledger import build_v32_ledger_row, load_v32_rows
from service.v32.report import build_report, _render
import service.run_v32 as R

CLOSE = "2026-09-13T20:00:00Z"
CTS = 1789156800  # epoch of CLOSE (not a sealed/holdout date)
B_SD = "KXBTC-26SEP1316-B68200"
B_SU = "KXBTC-26SEP1316-B68300"
S_SD = "KXBTCD-26SEP1316-T68199.99"   # parse_strike_ticker -> 68200
S_SU = "KXBTCD-26SEP1316-T68299.99"   # parse_strike_ticker -> 68300
M15 = "KXBTC15M-26SEP131600-B68250"   # a co-settling 15-minute market
M15_DEAD = "KXBTC15M-26SEP131600-B68350"
BUCKET_MAP = {B_SD: (68200.0, 68299.99), B_SU: (68300.0, 68399.99)}


def _params() -> V32Params:
    return load_v32_params()


class SeriesProxy:
    """A fake proxy that returns markets keyed by the requested series_ticker (so the strike, bucket,
    and 15M discoveries each see only their own series)."""

    def __init__(self, by_series: dict[str, list[dict]]):
        self._by = by_series
        self.calls = []

    def rest_get(self, path, params=None):
        self.calls.append((path, params))
        series = (params or {}).get("series_ticker")
        return {"markets": list(self._by.get(series, [])), "cursor": None}


def _m15_mkt(ticker, exch=2, status="active", close=CLOSE):
    return {"ticker": ticker, "close_time": close, "open_time": "2026-09-13T19:45:00Z",
            "event_ticker": "KXBTC15M-26SEP131600", "status": status, "exchange_index": exch}


# ===========================================================================
# discovery
# ===========================================================================
def test_discover_15m_one_live_one_dead():
    now = CTS - 1200
    proxy = SeriesProxy({R.FIFTEEN_SERIES: [
        _m15_mkt(M15, exch=2, status="active"),
        _m15_mkt(M15_DEAD, status="settled"),   # dead -> dropped
    ]})
    disc = R.discover_co_settling_15m(proxy, CLOSE, now)
    assert disc.tickers == (M15,)
    assert disc.exchange_index_by_ticker[M15] == 2


def test_discover_15m_none():
    now = CTS - 1200
    proxy = SeriesProxy({R.FIFTEEN_SERIES: []})
    disc = R.discover_co_settling_15m(proxy, CLOSE, now)
    assert disc.tickers == ()
    assert disc.exchange_index_by_ticker == {}


def test_discover_15m_all_dead_is_empty_not_a_crash():
    now = CTS - 1200
    proxy = SeriesProxy({R.FIFTEEN_SERIES: [_m15_mkt(M15, status="finalized")]})
    disc = R.discover_co_settling_15m(proxy, CLOSE, now)
    assert disc.tickers == ()  # empty is a journaled m15_missing upstream, NOT a stand-down


class BoomProxy:
    """A proxy whose /markets fetch raises (a 5xx / dead proxy) — the adversarial recording-only case."""

    def rest_get(self, path, params=None):
        raise RuntimeError("proxy 503 Service Unavailable")


def test_discover_15m_failure_is_non_fatal():
    # A 15M discovery failure must NEVER cost a viable trading window (recording-only leg): the safe
    # wrapper swallows the raise -> empty discovery + an error string the caller journals + continues.
    disc, err = R.discover_co_settling_15m_safe(BoomProxy(), CLOSE, CTS - 1200)
    assert disc.tickers == ()
    assert disc.close_time == CLOSE
    assert err is not None and "503" in err
    # the raw (unwrapped) discovery still raises — the safety is deliberately in the wrapper.
    import pytest
    with pytest.raises(RuntimeError):
        R.discover_co_settling_15m(BoomProxy(), CLOSE, CTS - 1200)


# ===========================================================================
# the 15M ticker rides the bucket connection, recording only (no core event)
# ===========================================================================
def _recorder(tmp_path, m15_tickers):
    p = _params()
    jpath = os.path.join(tmp_path, "w.jsonl")
    j = StreamJournal(jpath, flush_every=1)
    j.open()
    state = V32State.new(CLOSE, CTS, BUCKET_MAP, p, shakedown=True)
    execu = R.FrozenExecutor(BUCKET_MAP)
    drv = R.V32Driver(p, state, j, execu, clock=lambda: float(CTS - 600))
    shared = R.V32Recorder(j, drv, clock=lambda: float(CTS - 600),
                           m15_tickers=frozenset(m15_tickers))
    return p, j, jpath, drv, shared


def _read(path):
    from service.journal_io import open_journal
    with open_journal(path) as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def test_15m_frame_recorded_but_not_decided(tmp_path):
    _p, j, jpath, drv, shared = _recorder(tmp_path, {M15})
    ts = CTS - 605
    frame = {"market_ticker": M15, "ts_ms": int(ts * 1000),
             "yes_dollars_fp": [[0.50, 100]], "no_dollars_fp": [[0.50, 100]]}
    # tap (as the WS client does before dispatch) then dispatch snapshot/delta/trade
    shared.tap("kalshi_ws", {"type": "orderbook_snapshot", "msg": frame})
    shared.on_snapshot(M15, frame)
    shared.tap("kalshi_ws", {"type": "orderbook_delta", "msg": frame})
    shared.on_delta(M15, frame)
    shared.tap("kalshi_ws", {"type": "trade", "msg": {"market_ticker": M15, "ts_ms": int(ts * 1000),
                                                      "taker_side": "yes", "yes_price_dollars": "0.50",
                                                      "count_fp": "10.00"}})
    shared.on_trade(M15, {"market_ticker": M15, "ts_ms": int(ts * 1000), "taker_side": "yes",
                          "yes_price_dollars": "0.50", "count_fp": "10.00"})
    j.close()

    assert shared.m15_frames == 3            # 2 book frames + 1 trade counted
    assert M15 in shared.books               # folded into a BookMirror
    # the decision core never saw the 15M leg: no spot selected, no order intent, no stand-down
    assert drv.state.spot_Sd is None
    recs = _read(jpath)
    kinds = {r["kind"] for r in recs}
    assert "would_place_rest" not in kinds
    assert "stand_down" not in kinds
    assert "foreign_fill_ignored" not in kinds
    assert "v32_trade_unparsed" not in kinds  # a 15M trade is not driven, so never "unparsed" spam
    # the raw frames ARE in the tape stream
    assert sum(1 for r in recs if r["kind"] == "kalshi_ws") == 3


def test_15m_fill_frame_dropped_as_foreign(tmp_path):
    """Adversarial + impossible: a private FILL frame naming a 15M ticker (V3.2 never places a 15M
    order, so no coid/oid we placed can appear on it). on_fill attributes by client_order_id/order_id
    against the RestBook, so an unknown coid -> FOREIGN fill, journaled + dropped, never booked. Proves
    the fill path's isolation does not depend on ticker matching (a stronger guard than a name check)."""
    _p, j, jpath, drv, shared = _recorder(tmp_path, {M15})
    ts = CTS - 605
    shared.on_fill(M15, {"market_ticker": M15, "client_order_id": "not-ours-15m",
                         "order_id": "zzz", "trade_id": "t-15m", "ts_ms": int(ts * 1000),
                         "count": 10, "price": "0.50"})
    j.close()
    assert shared.m15_frames == 0                 # a fill is not a book/trade frame; the counter is untouched
    assert drv.counts.get("rest_fill", 0) == 0
    assert drv.counts.get("late_fill", 0) == 0
    assert drv.counts["foreign_fill_ignored"] == 1
    recs = _read(jpath)
    foreign = [r for r in recs if r["kind"] == "foreign_fill_ignored"]
    assert len(foreign) == 1 and foreign[0]["obj"]["market"] == M15


def test_bucket_connection_carries_the_15m_ticker():
    # main() subscribes the bucket connection to buckets UNION the co-settling 15M ticker(s).
    m15 = R.M15Discovery(CLOSE, (M15,), {M15: 2})
    bucket_sub_tickers = sorted(set(BUCKET_MAP) | set(m15.tickers))
    assert M15 in bucket_sub_tickers
    assert set(bucket_sub_tickers) == {B_SD, B_SU, M15}


def test_bucket_frames_still_decided_alongside_15m(tmp_path):
    """A strike + bucket quote still drives a would_place while a 15M frame on the same recorder is
    only recorded — proving the 15M leg does not poison the decision path."""
    _p, j, jpath, drv, shared = _recorder(tmp_path, {M15})
    ts = CTS - 605
    # a 15M frame first (recording only)
    m15_frame = {"market_ticker": M15, "ts_ms": int(ts * 1000), "yes_dollars_fp": [[0.50, 100]]}
    shared.on_snapshot(M15, m15_frame)
    # then the real strike + bucket quotes that select a spot and place a rest
    shared.on_snapshot(S_SD, {"market_ticker": S_SD, "ts_ms": int(ts * 1000),
                              "no_dollars_fp": [[0.70, 100]]})
    shared.on_snapshot(S_SU, {"market_ticker": S_SU, "ts_ms": int(ts * 1000),
                              "yes_dollars_fp": [[0.80, 100]]})
    shared.on_snapshot(B_SD, {"market_ticker": B_SD, "ts_ms": int(ts * 1000),
                              "yes_dollars_fp": [[0.40, 100]], "no_dollars_fp": [[0.55, 100]]})
    j.close()
    assert shared.m15_frames == 1
    recs = _read(jpath)
    assert any(r["kind"] == "would_place_rest" for r in recs)  # decision path unaffected


# ===========================================================================
# ledger + report carry m15_tickers / m15_frames
# ===========================================================================
def test_ledger_row_carries_m15(tmp_path):
    p = _params()
    state = V32State.new(CLOSE, CTS, BUCKET_MAP, p, shakedown=True)
    row = build_v32_ledger_row(
        close_time=CLOSE, resolved_mode="dry", effective_mode="dry", degrade=None, params=p,
        state=state, driver_counts={}, executor_counts={}, ws_counts={}, strike_count=2,
        strike_generations=1, bucket_count=2, bucket_generations=1, strike_lag_seconds=0.3,
        bucket_lag_seconds=0.5, journal_path="w.jsonl", record_count=0, stand_down_reason=None,
        now=1.0, m15_tickers=[M15], m15_frames=42,
    )
    assert row["m15_tickers"] == [M15]
    assert row["m15_frames"] == 42
    lpath = os.path.join(tmp_path, "v32_ledger.jsonl")
    from service.v32.ledger import append_v32_ledger_row
    append_v32_ledger_row(row, lpath)
    got = load_v32_rows(lpath)[0]
    assert got["m15_tickers"] == [M15] and got["m15_frames"] == 42


def test_ledger_row_defaults_m15_empty():
    # a stand-down / legacy row omits m15 kwargs -> empty list + 0, schema stays stable
    p = _params()
    row = build_v32_ledger_row(
        close_time=CLOSE, resolved_mode="dry", effective_mode="dry", degrade=None, params=p,
        state=None, driver_counts={}, executor_counts={}, ws_counts={}, strike_count=0,
        strike_generations=0, bucket_count=0, bucket_generations=0, strike_lag_seconds=None,
        bucket_lag_seconds=None, journal_path=None, record_count=0, stand_down_reason="no buckets",
        now=1.0,
    )
    assert row["m15_tickers"] == [] and row["m15_frames"] == 0


def test_report_shows_m15_frame_count():
    rows = [{
        "close_time": CLOSE, "effective_mode": "dry", "mode": "dry",
        "spot_bucket_ticker": B_SD, "replaces": 1, "would_places": 1, "late_fills": 0,
        "shadow": {}, "m15_frames": 137, "strike_lag_seconds": 0.3, "bucket_lag_seconds": 0.5,
        "stand_down": False, "stand_down_reason": None,
    }]
    report = build_report(rows)
    assert report["windows"][0]["m15_frames"] == 137
    text = _render(report)
    assert "m15" in text            # column header
    assert "137" in text            # the per-window count


# ===========================================================================
# missing 15M -> normal operation (recorder with no m15 tickers decides normally)
# ===========================================================================
def test_missing_15m_recorder_operates_normally(tmp_path):
    _p, j, jpath, drv, shared = _recorder(tmp_path, set())  # no 15M discovered
    ts = CTS - 605
    shared.on_snapshot(S_SD, {"market_ticker": S_SD, "ts_ms": int(ts * 1000),
                              "no_dollars_fp": [[0.70, 100]]})
    shared.on_snapshot(S_SU, {"market_ticker": S_SU, "ts_ms": int(ts * 1000),
                              "yes_dollars_fp": [[0.80, 100]]})
    shared.on_snapshot(B_SD, {"market_ticker": B_SD, "ts_ms": int(ts * 1000),
                              "yes_dollars_fp": [[0.40, 100]], "no_dollars_fp": [[0.55, 100]]})
    j.close()
    assert shared.m15_frames == 0
    recs = _read(jpath)
    assert any(r["kind"] == "would_place_rest" for r in recs)  # window runs normally with no 15M leg
