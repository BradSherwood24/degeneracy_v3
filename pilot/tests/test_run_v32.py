"""V3.2 process spine (Phase 2) — mode resolution, params sha refusal, discovery adapters, connect
gate, FrozenExecutor cycling, F-1 late-fill attribution, ClockTick cutoffs, journal record shapes,
ledger row, stand-down paths, and armed -> degrade_to_dry. FAKES ONLY — never dials the proxy, never
opens a socket, never reads a sealed/holdout date."""

from __future__ import annotations

import asyncio
import json
import os
from decimal import Decimal

import pytest

from service.book import TopOfBook
from service.record_range import StreamJournal
from service.v32 import (
    ActionKind,
    V32Params,
    V32ParamsShaMismatch,
    V32State,
    load_v32_params,
)
from service.v32.ledger import build_v32_ledger_row, load_v32_rows
from service.v32.report import build_report
import service.run_v32 as R

CLOSE = "2026-09-13T20:00:00Z"
CTS = 1789156800  # epoch of CLOSE (not a sealed/holdout date)
B_SD = "KXBTC-26SEP1316-B68200"
B_SU = "KXBTC-26SEP1316-B68300"
S_SD = "KXBTCD-26SEP1316-T68199.99"   # parse_strike_ticker -> 68200
S_SU = "KXBTCD-26SEP1316-T68299.99"   # parse_strike_ticker -> 68300
BUCKET_MAP = {B_SD: (68200.0, 68299.99), B_SU: (68300.0, 68399.99)}


def _params() -> V32Params:
    return load_v32_params()


def _top(*, yb=None, ya=None, nb=None, na=None, suspect=False) -> TopOfBook:
    return TopOfBook(
        yes_bid=yb, yes_bid_size=Decimal(100) if yb is not None else None,
        yes_ask=ya, yes_ask_size=Decimal(100) if ya is not None else None,
        no_bid=nb, no_bid_size=Decimal(100) if nb is not None else None,
        no_ask=na, no_ask_size=Decimal(100) if na is not None else None,
        suspect=suspect,
    )


def _bucket_top(mid_yes: str) -> TopOfBook:
    """A valid two-sided bucket top with the given YES mid (bid=ask=mid for simplicity)."""
    m = Decimal(mid_yes)
    return _top(yb=m, ya=m)


def _driver(tmp_path, params, *, shakedown=True):
    jpath = os.path.join(tmp_path, "w.jsonl")
    j = StreamJournal(jpath, flush_every=1)
    j.open()
    state = V32State.new(CLOSE, CTS, BUCKET_MAP, params, shakedown=shakedown)
    execu = R.FrozenExecutor(BUCKET_MAP)
    drv = R.V32Driver(params, state, j, execu, clock=lambda: 0.0)
    return drv, j, jpath


def _feed_quote(drv, ya, na, ts, *, bucket_mid="0.40"):
    """Feed a valid spot bucket + both strike books (Sd yes_ask, Su no_ask) at ``ts`` so the core has
    a fresh W (both strikes within freshness_max_age_s) and a live quote when ``ts`` is in the window."""
    drv.on_book_update(B_SD, _bucket_top(bucket_mid), ts)
    drv.on_book_update(S_SU, _top(na=Decimal(str(na))), ts)
    drv.on_book_update(S_SD, _top(ya=Decimal(str(ya))), ts)


def _seed_quote(drv, params, now):
    """Feed one fresh quote so the core places a live rest at ``now`` (t in window)."""
    _feed_quote(drv, "0.30", "0.20", now)


# ===========================================================================
# mode resolution (fail closed)
# ===========================================================================
def test_mode_resolution_cli_wins_then_file_then_failclosed(tmp_path):
    mf = os.path.join(tmp_path, "v32_mode.txt")
    with open(mf, "w") as f:
        f.write("dry\n")
    assert R.resolve_v32_mode("armed", mf) == "armed"      # CLI wins
    assert R.resolve_v32_mode(None, mf) == "dry"           # else file
    assert R.resolve_v32_mode(None, os.path.join(tmp_path, "missing.txt")) == "shakedown"  # absent
    with open(mf, "w") as f:
        f.write("nonsense\n")
    assert R.resolve_v32_mode(None, mf) == "shakedown"     # unknown -> fail closed


def test_mode_file_is_git_ignored_and_absent_fails_closed():
    """v32_mode.txt is machine-local + git-ignored (R2), exactly like the box's mode.txt: Brad's flips
    never dirty the tree, and it does NOT ship in the repo. It must be git-ignored, and an ABSENT file
    must fail closed to shakedown. If a local copy exists it must be a valid mode."""
    import subprocess
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    rel = "pilot/ops/v32_mode.txt"
    # (a) it is git-ignored
    r = subprocess.run(["git", "check-ignore", rel], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0 and rel in r.stdout, "v32_mode.txt must be git-ignored (R2)"
    # (b) it is NOT tracked in the index
    r = subprocess.run(["git", "ls-files", rel], cwd=repo, capture_output=True, text=True)
    assert r.stdout.strip() == "", "v32_mode.txt must not be tracked (R2)"
    # (c) absent -> fail closed to shakedown (the no-orders rung)
    assert R.resolve_v32_mode(None, os.path.join(repo, "pilot", "ops", "no_such_mode.txt")) == "shakedown"
    # (d) if a local copy exists, it must be a recognized mode
    p = os.path.join(repo, rel)
    if os.path.exists(p):
        with open(p) as f:
            assert f.read().strip() in R.VALID_MODES_V32


def test_effective_mode_passthrough_phase3():
    # Phase 3: effective_mode_and_degrade is a passthrough; the armed->dry DEGRADE decision moved to
    # service.v32.stops.decide_v32_arming (S5 + reconcile + day latch + S4), run in main with /health.
    assert R.effective_mode_and_degrade("armed") == ("armed", None)
    assert R.effective_mode_and_degrade("dry") == ("dry", None)
    assert R.effective_mode_and_degrade("shakedown") == ("shakedown", None)


# ===========================================================================
# params sha refusal (the spine stands down on any drift)
# ===========================================================================
def test_params_sha_mismatch_refused(tmp_path):
    good = _params()
    bad = os.path.join(tmp_path, "v32_params.json")
    with open(bad, "w") as f:
        json.dump({**good.raw, "E": 0.11, "shadow_Es": [0.08, 0.11, 0.12]}, f)
    with pytest.raises(V32ParamsShaMismatch):
        load_v32_params(bad)  # default expected_sha is the frozen pin -> mismatch


# ===========================================================================
# discovery adapters from fake /markets payloads
# ===========================================================================
class FakeProxy:
    def __init__(self, markets):
        self._m = markets
        self.calls = []

    def rest_get(self, path, params=None):
        self.calls.append((path, params))
        return {"markets": list(self._m), "cursor": None}


def _strike_mkt(ticker, exch=2, status="active", close=CLOSE):
    return {"ticker": ticker, "close_time": close, "open_time": "2026-09-13T19:00:00Z",
            "event_ticker": "KXBTCD-26SEP1316", "status": status, "exchange_index": exch}


def test_discover_strike_ladder_maps_floor_and_exchange_index():
    now = CTS - 1200
    disc = R.discover_strike_ladder(
        FakeProxy([_strike_mkt(S_SD, exch=2), _strike_mkt(S_SU, exch=2),
                   _strike_mkt("KXBTCD-26SEP1316-T99999.99", exch=None)]),
        CLOSE, now,
    )
    assert set(disc.tickers) == {S_SD, S_SU, "KXBTCD-26SEP1316-T99999.99"}
    assert disc.floor_by_ticker[S_SD] == 68200 and disc.floor_by_ticker[S_SU] == 68300
    assert disc.exchange_index_by_ticker[S_SD] == 2
    assert disc.exchange_index_by_ticker["KXBTCD-26SEP1316-T99999.99"] is None  # fail-closed
    assert disc.generations == 1


def test_discover_strike_ladder_drops_dead_generation():
    now = CTS - 1200
    disc = R.discover_strike_ladder(
        FakeProxy([_strike_mkt(S_SD, status="settled"), _strike_mkt(S_SU, status="finalized")]),
        CLOSE, now,
    )
    assert disc.tickers == ()


def test_build_bucket_map_and_observed_width():
    from service.record_range import Bucket, RangeDiscovery
    disc = RangeDiscovery(CLOSE, (
        Bucket(B_SD, 68200.0, 68299.99, "e", "o", "active", 2),
        Bucket(B_SU, 68300.0, 68399.99, "e", "o", "active", 2),
        Bucket("KXBTC-x-Bnull", None, None, "e", "o", "active", 2),  # half-populated -> dropped
    ), 1)
    bm = R.build_bucket_map(disc)
    assert set(bm) == {B_SD, B_SU}  # the None floor/cap bucket is dropped
    assert R.observed_bucket_width(bm) == 100


def test_observed_width_250_is_the_2100Z_case():
    bm = {"a": (68000.0, 68249.99), "b": (68250.0, 68499.99)}
    assert R.observed_bucket_width(bm) == 250  # != params.bucket_width (100) -> stand down upstream


# ===========================================================================
# connect-gate math
# ===========================================================================
def test_connect_gate_epoch():
    p = _params()
    assert R.connect_gate_epoch(CTS, p) == float(CTS) - p.quote_start_s - R.CONNECT_MARGIN_S
    assert R.connect_gate_epoch(CTS, p) == CTS - 900 - 5


# ===========================================================================
# FrozenExecutor cycling: place -> ack -> replace -> cancelled -> place
# ===========================================================================
def _replace_to_new_order(drv, p, now):
    """Place one rest, keep both strikes fresh across the debounce window (no |dn| replace), then move
    a wing so the core does a TRUE requote2 replace (cancel old kept-live + place new in one decide).
    Returns (first_coid, second_coid)."""
    # Deep wings (W ~ 1.33) so the BUDGET binds n (not the bucket cap); then moving a wing shifts n.
    _feed_quote(drv, "0.70", "0.60", now)
    first = drv.state.rest_live.client_order_id
    t = now
    while t < now + (p.deb_ms / 1000.0) + 0.5:  # tick both strikes fresh, prices unchanged -> no replace
        t += 0.5
        _feed_quote(drv, "0.70", "0.60", t)
    assert drv.state.rest_live.client_order_id == first, "no premature replace while |dn| < tol"
    t += 0.5
    _feed_quote(drv, "0.76", "0.60", t)  # yes_ask up -> n down by >= tol, debounce elapsed -> replace
    second = drv.state.rest_live.client_order_id
    return first, second


def test_frozen_executor_cycles_place_ack_replace_cancel(tmp_path):
    p = _params()
    drv, j, _ = _driver(tmp_path, p)
    now = CTS - 700  # t_to_close = 700, leaves room for the debounce window inside [300, 900]
    first, second = _replace_to_new_order(drv, p, now)
    assert drv.counts["would_place_rest"] == 2
    assert drv.counts["would_cancel_rest"] >= 1
    assert second != first
    # F-1: the old order is RETAINED as cancelled (not deleted); the new one is live
    assert drv.executor.rest_book[first].status == "cancelled"
    assert drv.executor.rest_book[second].status == "live"
    assert drv.state.rest_live.client_order_id == second
    j.close()


# ===========================================================================
# F-1 late-fill attribution to a retained coid
# ===========================================================================
def test_late_fill_attributed_to_retained_coid(tmp_path):
    p = _params()
    drv, j, jpath = _driver(tmp_path, p)
    now = CTS - 700
    old_coid, new_coid = _replace_to_new_order(drv, p, now)
    assert new_coid != old_coid
    assert drv.executor.rest_book[old_coid].status == "cancelled"

    # a fill lands on the OLD (replaced) order — the core no longer tracks it
    drv.on_fill(B_SD, {"client_order_id": old_coid, "count": 1}, drv.server_now())
    assert drv.counts["late_fill"] == 1
    assert drv.state.rest_fill is not None  # booked into the core (not dropped)
    assert drv.state.rest_fill.price == drv.executor.rest_book[old_coid].price
    # the pin completes (dry wing fills synthesized) -> one set done
    assert drv.state.sets_done == 1
    j.close()
    kinds = [r["kind"] for r in _read(jpath)]
    assert "late_fill" in kinds
    assert "would_take_wings" in kinds


def test_foreign_fill_is_dropped(tmp_path):
    p = _params()
    drv, j, jpath = _driver(tmp_path, p)
    now = CTS - 600
    _seed_quote(drv, p, now)
    drv.on_fill(B_SD, {"client_order_id": "not-ours", "count": 1}, now + 0.1)
    assert drv.counts["foreign_fill_ignored"] == 1
    assert drv.state.rest_fill is None  # a foreign fill never books
    j.close()


# ===========================================================================
# ClockTick pump advances the cutoffs (rest cancelled at/after quote_end_s in dry)
# ===========================================================================
def test_clocktick_cancels_rest_at_quote_end(tmp_path):
    p = _params()
    drv, j, _ = _driver(tmp_path, p)
    now = CTS - 600
    _seed_quote(drv, p, now)
    assert drv.state.rest_live is not None
    cancels_before = drv.counts["would_cancel_rest"]
    # a ClockTick past quote_end_s (t_to_close < 300) -> cancel the rest, no re-place
    drv.on_clock_tick(CTS - 250)
    assert drv.counts["would_cancel_rest"] > cancels_before
    assert drv.state.rest_live is None  # synthesized OrderCancelled cleared the slot
    j.close()


def test_server_now_none_until_first_frame(tmp_path):
    p = _params()
    drv, j, _ = _driver(tmp_path, p)
    assert drv.server_now() is None  # fail-closed: no tick driven before a timestamped frame
    drv.on_book_update(B_SD, _bucket_top("0.40"), CTS - 600)
    assert drv.server_now() is not None
    j.close()


# ===========================================================================
# journal record shapes
# ===========================================================================
def _read(path):
    from service.journal_io import open_journal
    with open_journal(path) as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def test_journal_record_shapes(tmp_path):
    p = _params()
    drv, j, jpath = _driver(tmp_path, p)
    now = CTS - 600
    _seed_quote(drv, p, now)
    j.close()
    recs = _read(jpath)
    assert all(set(r) == {"idx", "kind", "local_ts", "obj"} for r in recs)
    by_kind = {r["kind"]: r for r in recs}
    assert "would_place_rest" in by_kind
    wp = by_kind["would_place_rest"]["obj"]
    assert wp["side"] == "no" and wp["action"] == "buy"
    assert wp["client_order_id"] and wp["ticker"] == B_SD
    assert "expiration_epoch" in wp and wp["price"] is not None


# ===========================================================================
# ledger row
# ===========================================================================
def test_ledger_row_shape_and_shadow(tmp_path):
    p = _params()
    drv, j, jpath = _driver(tmp_path, p)
    now = CTS - 600
    _seed_quote(drv, p, now)
    j.close()
    row = build_v32_ledger_row(
        close_time=CLOSE, resolved_mode="dry", effective_mode="dry", degrade=None, params=p,
        state=drv.state, driver_counts=dict(drv.counts), executor_counts=dict(drv.executor.counts),
        ws_counts={}, strike_count=2, strike_generations=1, bucket_count=2, bucket_generations=1,
        strike_lag_seconds=0.3, bucket_lag_seconds=0.5, journal_path=jpath, record_count=len(j),
        stand_down_reason=None, now=1.0,
    )
    assert row["armed"] is False
    assert row["params_sha"] == p.sha256
    assert row["would_places"] == drv.counts["would_place_rest"]
    assert row["Sd"] == 68200 and row["Su"] == 68300
    assert set(row["shadow"].keys()) == {str(e) for e in p.shadow_Es}
    assert row["fills"] == [] and row["settlement"] is None  # Phase-3 slots empty
    lpath = os.path.join(tmp_path, "v32_ledger.jsonl")
    from service.v32.ledger import append_v32_ledger_row
    append_v32_ledger_row(row, lpath)
    assert len(load_v32_rows(lpath)) == 1


def test_report_builds_over_rows(tmp_path):
    p = _params()
    lpath = os.path.join(tmp_path, "v32_ledger.jsonl")
    from service.v32.ledger import append_v32_ledger_row
    row = build_v32_ledger_row(
        close_time=CLOSE, resolved_mode="dry", effective_mode="dry", degrade=None, params=p,
        state=None, driver_counts={"would_place_rest": 3}, executor_counts={}, ws_counts={},
        strike_count=2, strike_generations=1, bucket_count=2, bucket_generations=1,
        strike_lag_seconds=0.3, bucket_lag_seconds=0.5, journal_path="x", record_count=5,
        stand_down_reason=None, now=1.0,
    )
    append_v32_ledger_row(row, lpath)
    rep = build_report(load_v32_rows(lpath))
    assert rep["totals"]["windows"] == 1
    assert rep["totals"]["would_places"] == 3


# ===========================================================================
# stand-down paths (no buckets; $250 hour) + degrade ledger row
# ===========================================================================
def test_standdown_row_no_buckets(tmp_path):
    lpath = os.path.join(tmp_path, "v32_ledger.jsonl")
    spath = os.path.join(tmp_path, "summary.jsonl")
    rc = R._stand_down(spath, lpath, CLOSE, "no KXBTC range buckets co-settling",
                       resolved_mode="dry", effective_mode="dry", degrade=None,
                       params_sha=None, clock=lambda: 1.0)
    assert rc == 0
    rows = load_v32_rows(lpath)
    assert rows[0]["stand_down"] is True
    assert "no KXBTC range" in rows[0]["stand_down_reason"]


def test_standdown_row_degrade_recorded(tmp_path):
    lpath = os.path.join(tmp_path, "v32_ledger.jsonl")
    spath = os.path.join(tmp_path, "summary.jsonl")
    R._stand_down(spath, lpath, CLOSE, "bucket width 250 != params.bucket_width 100",
                  resolved_mode="armed", effective_mode="dry", degrade="phase2_no_executor",
                  params_sha="deadbeef", clock=lambda: 1.0)
    row = load_v32_rows(lpath)[0]
    assert row["mode"] == "armed" and row["effective_mode"] == "dry"
    assert row["degrade"] == "phase2_no_executor"
    assert row["armed"] is False


# ===========================================================================
# integration: two-connection run loop + clock pump + finalize
# ===========================================================================
class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeWsClient:
    """Feeds one dial's frames into the shared recorder tap + dispatch, then blocks until force_close."""

    def __init__(self, shared, frames, lag=0.4, silence=0.1):
        self.shared = shared
        self.frames = frames
        self._lag = lag
        self._silence = silence
        self.calls = 0
        self.force_closed = 0
        self.dropped_no_market = 0
        self._closed = asyncio.Event()

    async def connect(self):
        self.calls += 1
        for stream, env in self.frames:
            self.shared.tap(stream, env)
            payload = env.get("msg", {})
            mt = payload.get("market_ticker")
            typ = env.get("type")
            if typ == "orderbook_snapshot":
                self.shared.on_snapshot(mt, payload)
            elif typ == "orderbook_delta":
                self.shared.on_delta(mt, payload)
            elif typ == "trade":
                self.shared.on_trade(mt, payload)
        self._closed.clear()
        await self._closed.wait()

    async def force_close(self):
        self.force_closed += 1
        self._closed.set()

    def data_age_seconds(self):
        return self._lag

    def silence_seconds(self):
        return self._silence

    def current_lag_seconds(self):
        return self._lag


def test_two_connection_run_and_clock_pump(tmp_path):
    p = _params()
    clock = FakeClock(CTS - 610.0)
    jpath = os.path.join(tmp_path, "w.jsonl")
    j = StreamJournal(jpath, flush_every=1)
    j.open()
    state = V32State.new(CLOSE, CTS, BUCKET_MAP, p, shakedown=True)
    execu = R.FrozenExecutor(BUCKET_MAP)
    drv = R.V32Driver(p, state, j, execu, clock=clock)
    shared = R.V32Recorder(j, drv, clock=clock)

    ts = CTS - 605
    strike_frames = [
        ("kalshi_ws", {"type": "orderbook_snapshot",
                       "msg": {"market_ticker": S_SD, "ts_ms": int(ts * 1000),
                               "no_dollars_fp": [[0.70, 100]]}}),  # Sd yes_ask = 1-0.70 = 0.30
        ("kalshi_ws", {"type": "orderbook_snapshot",
                       "msg": {"market_ticker": S_SU, "ts_ms": int(ts * 1000),
                               "yes_dollars_fp": [[0.80, 100]]}}),  # Su no_ask = 1-0.80 = 0.20
    ]
    bucket_frames = [
        ("kalshi_ws", {"type": "orderbook_snapshot",
                       "msg": {"market_ticker": B_SD, "ts_ms": int(ts * 1000),
                               "yes_dollars_fp": [[0.40, 100]], "no_dollars_fp": [[0.55, 100]]}}),
    ]
    strike_ws = FakeWsClient(shared, strike_frames)
    bucket_ws = FakeWsClient(shared, bucket_frames)
    strike_conn = R._ConnRecorder(shared, strike_ws, "strikes", [S_SD, S_SU])
    bucket_conn = R._ConnRecorder(shared, bucket_ws, "buckets", [B_SD, B_SU])

    deadline = CTS + 10
    gate = R.connect_gate_epoch(CTS, p)

    async def fast_sleep(_):
        clock.t += 50.0

    asyncio.run(R.run_v32_window(shared, strike_conn, bucket_conn, drv, clock, deadline, gate,
                                 sleep=fast_sleep, pump_interval=0.5))
    j.close()
    assert strike_ws.calls >= 1 and bucket_ws.calls >= 1
    recs = _read(jpath)
    kinds = {r["kind"] for r in recs}
    assert "kalshi_ws" in kinds  # raw frames streamed
    # the pump advanced the clock past quote_end -> a cutoff decision was journaled
    assert "stand_down" in kinds or "would_cancel_rest" in kinds


def test_finalize_writes_ledger_summary_and_gzip(tmp_path):
    p = _params()
    clock = FakeClock(CTS - 610.0)
    jpath = os.path.join(tmp_path, "w.jsonl")
    j = StreamJournal(jpath, flush_every=1)
    j.open()
    j.append("window_meta", {"close_time": CLOSE}, clock())
    state = V32State.new(CLOSE, CTS, BUCKET_MAP, p, shakedown=True)
    execu = R.FrozenExecutor(BUCKET_MAP)
    drv = R.V32Driver(p, state, j, execu, clock=clock)
    shared = R.V32Recorder(j, drv, clock=clock)
    from service.run_v32 import StrikeDiscovery
    sd = StrikeDiscovery(CLOSE, (S_SD, S_SU), {S_SD: 68200, S_SU: 68300}, {S_SD: 2, S_SU: 2}, 1)
    lpath = os.path.join(tmp_path, "v32_ledger.jsonl")
    spath = os.path.join(tmp_path, "summary.jsonl")
    summary = R._finalize(
        journal=j, shared=shared, driver=drv, close_iso=CLOSE, resolved_mode="dry",
        effective_mode="dry", degrade=None, params=p, strike_disc=sd, bucket_map=BUCKET_MAP,
        bucket_generations=1, journal_path=jpath, summary_path=spath, ledger_path=lpath,
        strike_lag=0.3, bucket_lag=0.5, clock=clock,
    )
    assert summary["stand_down"] is False
    assert os.path.exists(jpath + ".gz")  # crash-safe gzip
    assert len(load_v32_rows(lpath)) == 1
    with open(spath) as f:
        s = json.loads(f.readline())
    assert s["close_time"] == CLOSE and s["effective_mode"] == "dry"
