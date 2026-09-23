"""V3.3 L3 pre-arm hardening (from the L2 review). FAKES ONLY -- no network, no proxy, no sealed read.

Covers: (R2-N1) the pacer headroom reserve + the 429-vs-business-rejection distinction; (R2-N2) the
build_executor_v33 armed belt (stand down if the proxy cap is unknown); (R2-N3) the weighted-average price
across the original + retry wing chunks; (R2-N4) the batched poll covering EVERY bucket with a live rung;
and the params sha re-pin + fail-closed loader checks for the new params."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace as dr
from decimal import Decimal

import pytest

from service.proxy_writer import WriteResponse
from service.v32.actions import ActionKind, V32Action
from service.v33 import V33State, WingLeg, load_v33_params
from service.v33.core import RestOrder
from service.v33.executor import (
    COST_CREATE,
    RATE_LIMIT_RETRY_WAIT_S,
    V33LiveExecutor,
    WriteTokenBucket,
    _RateLimitWriter,
)
import service.run_v33 as RUN

from tests.test_v33_executor import BUCKET_MAP, CTS, CLOSE, EXCH, B, S_SD, S_SU, CapWriter, FakeJournal, FakeWriter


# ---------------------------------------------------------------------------
# (R2-N1a) pacer headroom reserve
# ---------------------------------------------------------------------------
def test_pacer_reserve_holds_headroom_for_priority():
    t = [0.0]
    sleeps: list[float] = []
    clk = lambda: t[0]

    def sl(s):
        sleeps.append(s)
        t[0] += s

    b = WriteTokenBucket(rate=100.0, size=100.0, clock=clk, sleep=sl, journal=None, reserve=30.0)
    # spend down to just above the reserve: 7 creates = 70 tokens -> 30 left (== reserve). The 8th
    # non-priority create must WAIT (it may not draw below 30), but a PRIORITY cancel proceeds NOW.
    for _ in range(7):
        assert b.acquire(COST_CREATE, "create") == 0.0
    assert abs(b.tokens - 30.0) < 1e-9
    # priority: ignores the reserve, no wait even though it would dip below 30
    assert b.acquire(2, "cancel", priority=True) == 0.0
    assert b.tokens < 30.0                      # priority drew into the reserve
    # a non-priority create now must wait for the bucket to refill to cost+reserve
    w = b.acquire(COST_CREATE, "create")
    assert w > 0.0 and sleeps                    # it paced


def test_pacer_priority_wing_burst_never_waits_after_creates():
    """A full 11-create ladder depletes most of the bucket; the priority coalesced-wing burst that follows
    a sweep must still be served with ZERO wait (it draws into the reserve / transiently negative)."""
    t = [0.0]
    clk = lambda: t[0]
    b = WriteTokenBucket(rate=100.0, size=100.0, clock=clk, sleep=lambda s: None, reserve=30.0)
    # 7 creates = 70 tokens -> 30 left; the 8th would pace, but a priority wing burst does not.
    for _ in range(7):
        b.acquire(COST_CREATE, "create")
    w = b.acquire(COST_CREATE * 6, "wing_take", priority=True)   # 6-chunk wing burst = 60 tokens
    assert w == 0.0 and b.tokens < 0.0            # served now, drew the bucket negative (bounded)


def test_pacer_journals_write_paced_with_reserve():
    j = FakeJournal()
    t = [0.0]
    b = WriteTokenBucket(rate=100.0, size=100.0, clock=lambda: t[0],
                         sleep=lambda s: t.__setitem__(0, t[0] + s), journal=j, reserve=30.0)
    for _ in range(7):
        b.acquire(COST_CREATE, "create")         # down to 30
    b.acquire(COST_CREATE, "create")             # must pace (would dip below reserve)
    paced = [o for k, o in j.records if k == "write_paced"]
    assert paced and paced[0]["reserve"] == 30.0


# ---------------------------------------------------------------------------
# (R2-N1b) 429 vs business rejection in the rate-limit writer
# ---------------------------------------------------------------------------
class _Inner:
    def __init__(self, responses):
        self._q = list(responses)
        self.posts = 0

    def rest_post(self, path, body):
        self.posts += 1
        return self._q.pop(0)


def test_rate_limit_writer_retries_429_once_and_journals():
    j = FakeJournal()
    inner = _Inner([WriteResponse(429, {"retry_after": 0.25}, False, "http_429"),
                    WriteResponse(200, {"order": {"order_id": "o1"}}, True)])
    sleeps: list[float] = []
    rl = _RateLimitWriter(inner, j, clock=lambda: 0.0, sleep=lambda s: sleeps.append(s),
                          estimate_wait=lambda: 1.0)
    resp = rl.rest_post("/x", {"client_order_id": "v33-c"})
    assert resp.ok and inner.posts == 2          # retried ONCE
    assert sleeps == [0.25]                       # waited the body's retry_after
    assert "rate_limited" in j.kinds() and rl.rate_limited == 1


def test_rate_limit_writer_uses_estimate_when_no_retry_after():
    j = FakeJournal()
    inner = _Inner([WriteResponse(429, {}, False, "http_429"),
                    WriteResponse(200, {"order": {"order_id": "o1"}}, True)])
    sleeps: list[float] = []
    rl = _RateLimitWriter(inner, j, clock=lambda: 0.0, sleep=lambda s: sleeps.append(s),
                          estimate_wait=lambda: 0.0)
    rl.rest_post("/x", {"client_order_id": "v33-c"})
    assert sleeps == [RATE_LIMIT_RETRY_WAIT_S]    # falls back to the floor when estimate is 0


def test_rate_limit_writer_passes_business_4xx_through():
    j = FakeJournal()
    inner = _Inner([WriteResponse(403, {"error": "post_only_cross"}, False, "http_403")])
    rl = _RateLimitWriter(inner, j, clock=lambda: 0.0, sleep=lambda s: None, estimate_wait=lambda: 1.0)
    resp = rl.rest_post("/x", {"client_order_id": "v33-c"})
    assert resp.status_code == 403 and inner.posts == 1     # NOT retried
    assert "rate_limited" not in j.kinds() and rl.rate_limited == 0


def test_place_persistent_429_does_not_count_toward_standdown():
    """A place that stays 429 even after the writer's one retry is throttling, not a business reject: it
    does NOT increment the consecutive-reject counter and never latches the hour down."""
    w = FakeWriter()
    j = FakeJournal()
    ex = V33LiveExecutor(w, BUCKET_MAP, EXCH, j, CTS, 300, k_rungs=11, clock=lambda: 0.0,
                         sleep=lambda _s: None)
    # every POST is a 429 (the rate-limit writer retries once, still 429)
    for _ in range(6):
        w.post_queue.append(WriteResponse(429, {}, False, "http_429"))
    place = V32Action(kind=ActionKind.PLACE_REST, ticker=B, side="no", action="buy", count=1,
                      price=Decimal("0.45"), expiration_epoch=CTS - 300, client_order_id="v33-c1")
    st = V33State.new(CLOSE, CTS, BUCKET_MAP, load_v33_params())
    for i in range(3):
        w.post_queue.append(WriteResponse(429, {}, False, "http_429"))   # top up for the retry
        ex.on_action(dr(place, client_order_id=f"v33-c{i}"), st, CTS - 500)
    assert ex._consecutive_rejects == 0          # 429s never counted
    assert ex.stand_down_reason is None          # never latched
    assert "rate_limited_reject" in j.kinds()


# ---------------------------------------------------------------------------
# (R2-N3) weighted-average price across original + retry chunks
# ---------------------------------------------------------------------------
def _wing_state(count, yes_limit="0.55"):
    st = V33State.new(CLOSE, CTS, BUCKET_MAP, load_v33_params())
    legs = (WingLeg(S_SD, "yes", count, Decimal(yes_limit), "v33-wy", batch=0),
            WingLeg(S_SU, "no", count, Decimal("0.90"), "v33-wn", batch=0))
    return dr(st, wing_legs=legs)


def test_wing_retry_reports_weighted_average_across_chunks():
    # cap 2, count 4: first take fills 2 yes lots at 0.55 (one chunk rejected); the RETRY take at a NEW
    # limit 0.57 fills the remaining 2 -> the completing Fill's price is the blend (0.55*2 + 0.57*2)/4.
    w = CapWriter(cap=2, reject_coids={"v33-wc-1"})
    ex = V33LiveExecutor(w, BUCKET_MAP, EXCH, FakeJournal(), CTS, 300, k_rungs=11, clock=lambda: 0.0,
                         sleep=lambda _s: None)
    ex.wing_cap = 2
    st = _wing_state(4, yes_limit="0.55")
    ex._take_wings(st, CTS - 400)                # 2 filled at 0.55, yes leg reported unfilled (retry)
    assert ex._wing_filled[(0, "yes")] == 2
    st2 = dr(st, wing_legs=(dr(st.wing_legs[0], client_order_id="v33-wy2", limit=Decimal("0.57"),
                               status="pending"),
                            dr(st.wing_legs[1], status="filled")))
    w.chunk_counts.clear()
    w.reject_coids.clear()
    from service.v32.events import Fill
    events = ex._take_wings(st2, CTS - 390)
    yes_fill = next(e for e in events if isinstance(e, Fill) and e.side == "yes" and e.count == Decimal(4))
    # blended: (0.55*2 + 0.57*2)/4 = 0.56
    assert yes_fill.price == Decimal("0.56")


# ---------------------------------------------------------------------------
# (R2-N2) build_executor_v33 armed belt
# ---------------------------------------------------------------------------
def test_build_executor_armed_refuses_unknown_cap():
    p = load_v33_params()
    j = FakeJournal()
    with pytest.raises(ValueError, match="known proxy contract cap"):
        RUN.build_executor_v33("armed", bucket_map=BUCKET_MAP, exchange_index_by_ticker=EXCH,
                               journal=j, close_epoch_val=CTS, params=p, writer=FakeWriter(),
                               wing_cap=None)


def test_build_executor_dry_ignores_cap():
    p = load_v33_params()
    ex = RUN.build_executor_v33("dry", bucket_map=BUCKET_MAP, exchange_index_by_ticker=EXCH,
                                journal=FakeJournal(), close_epoch_val=CTS, params=p, writer=None,
                                wing_cap=None)
    assert ex.__class__.__name__ == "FrozenExecutor"    # dry never needs a cap


# ---------------------------------------------------------------------------
# (R2-N4) batched poll covers every bucket with a live rung
# ---------------------------------------------------------------------------
def _rung(coid, oid, sd, price):
    return RestOrder(client_order_id=coid, order_id=oid, price=Decimal(price), count=1,
                     placed_ts=CTS - 600, live=True, pending=False, bucket_Sd=sd, rung=0,
                     E_rung=Decimal("0.05"))


class _FakeDriver:
    def __init__(self, state, params):
        self.state = state
        self.params = params
        self.poll_fills: list = []

    def server_now(self):
        return CTS - 400

    def on_poll_fill(self, oid, fc, ts):
        self.poll_fills.append((oid, fc))


def test_batched_poll_covers_all_buckets_with_live_rungs():
    p = load_v33_params()
    B1, B2 = "KXBTC-B1", "KXBTC-B2"
    st = V33State.new(CLOSE, CTS, BUCKET_MAP, p)
    st = dr(st, rest_bucket_Sd=80400,
            bucket_tickers={80400: B1, 80500: B2},
            ladder=(_rung("v33-a", "oa", 80400, "0.44"),      # current bucket
                    _rung("v33-b", "ob", 80500, "0.43")))     # prior bucket, still resting
    drv = _FakeDriver(st, p)
    polled: list[str] = []

    class Exec:
        def poll_orders_for_bucket(self, tk):
            polled.append(tk)
            return {}

    t = [0.0]

    async def sleep(s):
        t[0] += s

    asyncio.run(RUN._order_status_poll_v33(drv, Exec(), lambda: t[0], deadline=1.0, sleep=sleep,
                                           interval=0.5))
    assert set(polled) == {B1, B2}     # BOTH the current and the prior-bucket rung's bucket polled


# ---------------------------------------------------------------------------
# params sha re-pin + fail-closed loader checks for the new L3 params
# ---------------------------------------------------------------------------
def test_params_sha_repinned_and_previous_defined():
    from service.v33.params import (
        FROZEN_V33_PARAMS_SHA256 as PIN,
        PREVIOUS_V33_PARAMS_SHA256_L2_R2 as PREV,
    )
    assert PIN == "c18197d012bea8251982e4fdb948bf85846a453a9873fbd8007a7df9639f36f3"
    assert PREV == "415b63daa2ff9dd7efa0193409b0e367b545c2ce2334fe44229484cb5395b3c2"
    assert PIN != PREV
    p = load_v33_params()
    assert p.sha256 == PIN and p.write_reserve_tokens == 30 and p.deep_obs_rungs == 10


def test_loader_fails_closed_on_bad_reserve(tmp_path):
    from service.v33.params import DEFAULT_V33_PARAMS_PATH, V33ParamsInvalid, load_v33_params as load
    with open(DEFAULT_V33_PARAMS_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    # reserve >= bucket size must fail closed (a non-priority write could never proceed)
    raw["write_reserve_tokens"] = raw["write_bucket_size"]
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(V33ParamsInvalid, match="write_reserve_tokens"):
        load(str(p), expected_sha=None)


def test_armed_executor_carries_the_reserve():
    """run_v33.build_executor_v33 threads params.write_reserve_tokens into the pacer (belt: the reserve is
    actually installed on an armed executor)."""
    p = load_v33_params()
    ex = RUN.build_executor_v33("armed", bucket_map=BUCKET_MAP, exchange_index_by_ticker=EXCH,
                                journal=FakeJournal(), close_epoch_val=CTS, params=p, writer=FakeWriter(),
                                wing_cap=2)
    assert ex._pacer.reserve == float(p.write_reserve_tokens) == 30.0
    assert ex.wing_cap == 2


def test_ledger_row_carries_deep_obs():
    from service.v33.ledger import build_v33_ledger_row
    deep = {"margins_c": list(range(16, 26)), "reached_count": 3, "rungs": []}
    row = build_v33_ledger_row(
        close_time="2026-09-24T04:00:00Z", resolved_mode="dry", effective_mode="dry", degrade=None,
        params=load_v33_params(), state=None, driver_counts={}, executor_counts={}, ws_counts={},
        strike_count=0, bucket_count=0, journal_path=None, record_count=0, stand_down_reason=None,
        now=0.0, deep_obs=deep)
    assert row["deep_obs"] == deep


def test_rate_limit_writer_delete_and_get_passthrough():
    class Inner:
        def __init__(self):
            self.deletes = []
            self.gets = []

        def rest_delete(self, p):
            self.deletes.append(p)
            return WriteResponse(200, {"reduced_by": "1.00"}, True)

        def rest_get(self, p, params=None):
            self.gets.append((p, params))
            return {"ok": True}

    inner = Inner()
    rl = _RateLimitWriter(inner, FakeJournal(), clock=lambda: 0.0, sleep=lambda s: None,
                          estimate_wait=lambda: 1.0)
    assert rl.rest_delete("/d").ok and inner.deletes == ["/d"]
    assert rl.rest_get("/g", {"x": 1}) == {"ok": True} and inner.gets == [("/g", {"x": 1})]


def test_loader_fails_closed_on_negative_deep_obs(tmp_path):
    from service.v33.params import DEFAULT_V33_PARAMS_PATH, V33ParamsInvalid, load_v33_params as load
    with open(DEFAULT_V33_PARAMS_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    raw["deep_obs_rungs"] = -1
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(V33ParamsInvalid, match="deep_obs_rungs"):
        load(str(p), expected_sha=None)


def test_loader_fails_closed_when_reserve_plus_cost_exceeds_bucket(tmp_path):
    """F3 cost-aware: reserve 95 < bucket 100, but 95 + a create's 10 tokens > 100 -> fail closed (a
    create could never proceed above the reserve)."""
    from service.v33.params import DEFAULT_V33_PARAMS_PATH, V33ParamsInvalid, load_v33_params as load
    with open(DEFAULT_V33_PARAMS_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    assert raw["write_bucket_size"] == 100
    raw["write_reserve_tokens"] = 95            # 95 < 100 but 95 + 10 > 100
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(V33ParamsInvalid, match="write_reserve_tokens"):
        load(str(p), expected_sha=None)


# ---------------------------------------------------------------------------
# (F6) amend-in-flight vs the pre-place stray check -- the amended order is NOT a stray
# ---------------------------------------------------------------------------
def _place_action(coid, n):
    return V32Action(kind=ActionKind.PLACE_REST, ticker=B, side="no", action="buy", count=1,
                     price=Decimal(n), expiration_epoch=CTS - 300, client_order_id=coid)


def test_amend_in_flight_order_not_cancelled_as_stray():
    """F6: an amend rotates coid (v33-a -> v33-b) but KEEPS the order_id. A later pre-place check that sees
    the amended order (listed under the NEW coid) must NOT flag it as a stray -- it is attributed via the
    retained new-coid record. The amended order is left resting; the ladder proceeds; no stand-down."""
    w = FakeWriter()
    j = FakeJournal()
    ex = V33LiveExecutor(w, BUCKET_MAP, EXCH, j, CTS, 300, k_rungs=11, clock=lambda: 0.0,
                         sleep=lambda _s: None)
    st = V33State.new(CLOSE, CTS, BUCKET_MAP, load_v33_params())
    ex.on_action(_place_action("v33-a", "0.45"), st, CTS - 600)      # get_map empty -> place proceeds
    w.post_queue.append(WriteResponse(200, {"order": {"order_id": "oid-1", "client_order_id": "v33-b",
                                                      "fill_count": "0.00", "remaining_count": "1.00"}},
                                      True))
    amend = V32Action(kind=ActionKind.AMEND_REST, order_id="oid-1", ticker=B, side="no", action="buy",
                      count=1, price=Decimal("0.44"), client_order_id="v33-a",
                      updated_client_order_id="v33-b")
    ex.on_action(amend, st, CTS - 590)                               # rest_book[v33-b]=oid-1
    # POST-AMEND: the venue lists the order under the NEW coid v33-b + the same oid-1
    w.get_map["/portfolio/orders"] = {"orders": [
        {"ticker": B, "order_id": "oid-1", "client_order_id": "v33-b", "exchange_index": 2}]}
    ex._pending_place_price = Decimal("0.43")                        # a fresh rung, none resting there
    assert ex._pre_place_invariant("v33-c", CTS - 580) is None       # attributed -> proceed
    assert ex.rest_stray_cancels == 0 and ex.stand_down_reason is None
    assert "stray_cancel" not in j.kinds() and "rest_stray_cancelled" not in j.kinds()


def test_venue_ahead_new_coid_attributed_by_order_id():
    """The in-flight window: the venue shows the NEW coid while our RestBook still only holds the OLD coid
    (the amend ack not yet processed). Attribution by the STABLE order_id resolves it (the old-coid record
    is retained), so it is still NOT a stray."""
    w = FakeWriter()
    j = FakeJournal()
    ex = V33LiveExecutor(w, BUCKET_MAP, EXCH, j, CTS, 300, k_rungs=11, clock=lambda: 0.0,
                         sleep=lambda _s: None)
    # seed ONLY the old coid (as if the amend ack has not landed): rest_book[v33-a]=oid-1, by_oid stable.
    from service.v32.executor import RestRecord
    ex.rest_book["v33-a"] = RestRecord("v33-a", "oid-1", Decimal("0.45"), 1, B, 80400, CTS - 600,
                                       "live", exchange_index=2)
    ex._by_order_id["oid-1"] = "v33-a"
    # venue is AHEAD: it lists the order under the new coid v33-b (not yet in our RestBook) + oid-1
    w.get_map["/portfolio/orders"] = {"orders": [
        {"ticker": B, "order_id": "oid-1", "client_order_id": "v33-b", "exchange_index": 2}]}
    ex._pending_place_price = Decimal("0.43")
    assert ex._pre_place_invariant("v33-c", CTS - 580) is None       # order_id -> v33-a -> attributed
    assert ex.rest_stray_cancels == 0 and ex.stand_down_reason is None
