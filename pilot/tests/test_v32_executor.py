"""V3.2 armed maker executor (Phase 3). FAKES ONLY — no network, no proxy dialed, no key/holdout read.

Covers: the place/cancel/take wire bodies (post_only + GTC + expiration_time; IOC wings; NO-space price)
against the real create-response fixture; the rejection paths + 3-consecutive stand-down; the
cancel-race (filled_count read from the order status, never assumed 0); IOC no-fill -> retry; the
FrozenExecutor armed guard (P3-1); the exec-price mismatch alarm (P3-4) and fill de-dup (ws + poll)
through the driver; and the startup open-order cancel."""

from __future__ import annotations

import os
from collections import deque
from decimal import Decimal

import pytest

from service.book import TopOfBook
from service.proxy_writer import WriteResponse
from service.record_range import StreamJournal
from service.v32 import V32State, load_v32_params
from service.v32.actions import ActionKind, V32Action
from service.v32.core import WingLeg
from service.v32.executor import (
    LiveExecutor,
    ORDER_STATUS_PATH_TMPL,
    REL_BATCH_CREATE,
    REL_SINGLE_CREATE,
    cancel_path,
    cancel_stale_open_orders,
    parse_order_status,
)
import service.run_v32 as R

CLOSE = "2026-09-13T20:00:00Z"
CTS = 1789156800
B = "KXBTC-26SEP1316-B68200"
S_SD = "KXBTCD-26SEP1316-T68199.99"
S_SU = "KXBTCD-26SEP1316-T68299.99"
BUCKET_MAP = {B: (68200.0, 68299.99)}
EXCH = {B: 2, S_SD: 2, S_SU: 2}


class FakeJournal:
    def __init__(self):
        self.records: list[tuple] = []

    def append(self, kind, obj, ts):
        self.records.append((kind, obj))

    def kinds(self):
        return [k for k, _ in self.records]


class FakeWriter:
    """Duck-typed ProxyWriter: canned POST/DELETE/GET replies, capturing every call."""

    def __init__(self):
        self.posts: list[tuple] = []
        self.deletes: list[str] = []
        self.gets: list[tuple] = []
        self.post_queue: deque = deque()
        self.delete_queue: deque = deque()
        self.get_map: dict[str, object] = {}

    def rest_post(self, path, body):
        self.posts.append((path, body))
        if self.post_queue:
            return self.post_queue.popleft()
        return WriteResponse(200, {"order": {"order_id": "oid-1", "client_order_id":
                                             body.get("client_order_id"), "fill_count": "0.00",
                                             "remaining_count": "1.00"}}, True)

    def rest_delete(self, path):
        self.deletes.append(path)
        if self.delete_queue:
            return self.delete_queue.popleft()
        return WriteResponse(200, {}, True)

    def rest_get(self, path, params=None):
        self.gets.append((path, params))
        v = self.get_map.get(path)
        if callable(v):
            return v()
        return v if v is not None else {}


def _exec(writer=None, journal=None):
    return LiveExecutor(writer or FakeWriter(), BUCKET_MAP, EXCH, journal or FakeJournal(),
                        CTS, 300, clock=lambda: 0.0, sleep=lambda _s: None)


def _place_action(coid="c1", n="0.45"):
    return V32Action(kind=ActionKind.PLACE_REST, ticker=B, side="no", action="buy", count=1,
                     price=Decimal(n), expiration_epoch=CTS - 300, client_order_id=coid)


# ---------------------------------------------------------------------------
# PLACE_REST wire body
# ---------------------------------------------------------------------------
def test_place_rest_wire_body_post_only_gtc_expiration():
    w = FakeWriter()
    ex = _exec(w)
    events = ex.on_action(_place_action(n="0.45"), V32State.new(CLOSE, CTS, BUCKET_MAP,
                                                                load_v32_params()), CTS - 600)
    assert len(w.posts) == 1
    path, body = w.posts[0]
    assert path == REL_SINGLE_CREATE
    assert body["post_only"] is True
    assert body["time_in_force"] == "good_till_canceled"
    assert body["expiration_time"] == CTS - 300         # quote end (T-5); VERIFIED CreateOrderV2 field
    assert "expiration_ts" not in body                   # the non-existent field must NOT be sent
    assert body["side"] == "ask"                          # buy NO == sell YES == ask
    assert body["price"] == "0.5500"                      # NO at 0.45 == YES ask at 1-0.45
    assert body["client_order_id"] == "c1"
    assert body["exchange_index"] == 2
    assert body["count"] == "1.00"
    # a successful create -> OrderAck; the RestBook now tracks it live
    from service.v32.events import OrderAck
    assert any(isinstance(e, OrderAck) for e in events)
    assert ex.rest_book["c1"].status == "live" and ex.rest_book["c1"].order_id == "oid-1"


def test_place_rest_unrouted_is_rejected():
    ex = _exec()
    ex.exch_by_ticker = {}  # no exchange_index for the bucket -> refuse (never send unrouted)
    events = ex.on_action(_place_action(), V32State.new(CLOSE, CTS, BUCKET_MAP, load_v32_params()),
                          CTS - 600)
    from service.v32.events import OrderCancelled
    assert len(events) == 1 and isinstance(events[0], OrderCancelled)
    assert ex.rests_rejected == 1


def test_place_rest_rejection_and_three_consecutive_standdown():
    w = FakeWriter()
    j = FakeJournal()
    ex = _exec(w, j)
    # every create is a proxy cap 403 (post_only cross / budget / cap all present as non-2xx)
    for _ in range(3):
        w.post_queue.append(WriteResponse(403, {"error": "order rejected by proxy cap",
                                                "cap": "daily_order_budget"}, False, "http_403"))
    st = V32State.new(CLOSE, CTS, BUCKET_MAP, load_v32_params())
    from service.v32.events import OrderCancelled
    for i in range(3):
        events = ex.on_action(_place_action(coid=f"c{i}"), st, CTS - 600)
        assert isinstance(events[0], OrderCancelled) and events[0].filled_count_before_cancel == 0
    assert ex.rests_rejected == 3
    assert ex.stand_down_reason is not None and "rest_rejected" in ex.stand_down_reason
    assert "rest_rejected" in j.kinds()


def test_place_rest_success_resets_consecutive_rejects():
    w = FakeWriter()
    ex = _exec(w)
    w.post_queue.append(WriteResponse(403, {"error": "cap"}, False, "http_403"))
    st = V32State.new(CLOSE, CTS, BUCKET_MAP, load_v32_params())
    ex.on_action(_place_action(coid="c0"), st, CTS - 600)
    assert ex._consecutive_rejects == 1
    ex.on_action(_place_action(coid="c1"), st, CTS - 600)  # default fake reply = success
    assert ex._consecutive_rejects == 0


# ---------------------------------------------------------------------------
# CANCEL_REST — confirm via status, filled_count from the status (the race truth)
# ---------------------------------------------------------------------------
def test_cancel_confirms_filled_count_from_status_not_assumed_zero():
    w = FakeWriter()
    ex = _exec(w)
    # place first so the RestBook has the order
    ex.on_action(_place_action(coid="c1"), V32State.new(CLOSE, CTS, BUCKET_MAP, load_v32_params()),
                 CTS - 600)
    oid = ex.rest_book["c1"].order_id
    # a fill slipped in before the cancel landed: the status reports it executed with 1 filled
    w.get_map[ORDER_STATUS_PATH_TMPL.format(order_id=oid)] = {
        "order": {"order_id": oid, "status": "executed", "fill_count": "1.00",
                  "remaining_count": "0.00"}}
    cancel = V32Action(kind=ActionKind.CANCEL_REST, order_id=oid, client_order_id="c1")
    events = ex.on_action(cancel, V32State.new(CLOSE, CTS, BUCKET_MAP, load_v32_params()), CTS - 590)
    from service.v32.events import OrderCancelled
    assert len(events) == 1 and isinstance(events[0], OrderCancelled)
    assert events[0].filled_count_before_cancel == Decimal(1)   # READ from status, not assumed 0
    assert w.deletes == [cancel_path(oid, 2)]                    # shard-aware (?exchange_index=2)
    assert ex.rest_book["c1"].status == "filled"               # RETAINED (F-1); the race fill happened


def test_cancel_clean_status_zero_filled():
    w = FakeWriter()
    ex = _exec(w)
    ex.on_action(_place_action(coid="c1"), V32State.new(CLOSE, CTS, BUCKET_MAP, load_v32_params()),
                 CTS - 600)
    oid = ex.rest_book["c1"].order_id
    w.get_map[ORDER_STATUS_PATH_TMPL.format(order_id=oid)] = {
        "order": {"order_id": oid, "status": "canceled", "fill_count": "0.00",
                  "remaining_count": "0.00"}}
    cancel = V32Action(kind=ActionKind.CANCEL_REST, order_id=oid, client_order_id="c1")
    events = ex.on_action(cancel, V32State.new(CLOSE, CTS, BUCKET_MAP, load_v32_params()), CTS - 590)
    assert events[0].filled_count_before_cancel == Decimal(0)


def test_parse_order_status_derivations():
    # DOCUMENTED fixed-point fields (docs.kalshi.com get-order): fill_count_fp
    s = parse_order_status({"order": {"order_id": "o", "status": "executed", "fill_count_fp": "1.00",
                                      "remaining_count_fp": "0.00", "initial_count_fp": "1.00"}}, "o")
    assert s.filled_count == 1 and s.available and s.remaining_count == 0
    # initial - remaining (fp) fallback when no explicit fill count
    s = parse_order_status({"order": {"initial_count_fp": "1.00", "remaining_count_fp": "0.00"}}, "o")
    assert s.filled_count == 1
    # legacy explicit fill_count still honored (defensive fallback / test doubles)
    s = parse_order_status({"order": {"order_id": "o", "status": "resting", "fill_count": "1.00",
                                      "remaining_count": "0.00"}}, "o")
    assert s.filled_count == 1 and s.available
    # legacy place - remaining fallback
    s = parse_order_status({"order": {"place_count": "1", "remaining_count": "0"}}, "o")
    assert s.filled_count == 1
    # unreadable -> unavailable, filled 0
    assert parse_order_status("nope", "o").available is False


def test_cancel_race_reduced_by_from_delete_response_is_authoritative():
    # The DELETE response's reduced_by (remaining pulled off the book) decides the race even when the
    # order-status GET is unavailable (a 404 on an already-terminal order): filled = placed - reduced_by.
    w = FakeWriter()
    ex = _exec(w)
    ex.on_action(_place_action(coid="c1"), V32State.new(CLOSE, CTS, BUCKET_MAP, load_v32_params()),
                 CTS - 600)
    oid = ex.rest_book["c1"].order_id
    w.delete_queue.append(WriteResponse(200, {"order_id": oid, "reduced_by": "0.00"}, True))  # nothing
    # order-status GET returns nothing (unavailable) -> reduced_by carries the race
    cancel = V32Action(kind=ActionKind.CANCEL_REST, order_id=oid, client_order_id="c1")
    events = ex.on_action(cancel, V32State.new(CLOSE, CTS, BUCKET_MAP, load_v32_params()), CTS - 590)
    assert events[0].filled_count_before_cancel == Decimal(1)   # placed 1 - reduced_by 0 = 1 filled
    # the race fill is booked into money-math exactly once (maker fee 0), de-duped by order_id
    rest_fills = [f for f in ex.fills if f.get("leg") == "rest"]
    assert len(rest_fills) == 1 and rest_fills[0]["path"] == "cancel_race"


def test_post_unknown_outcome_records_coid_and_stands_down():
    # A transport timeout (status None) leaves the order outcome UNKNOWN: it may be live. The coid must
    # be recorded (so a phantom fill is hedged, not dropped) and a stand-down latched (no second rest).
    w = FakeWriter()
    j = FakeJournal()
    ex = _exec(w, j)
    w.post_queue.append(WriteResponse(None, {}, False, "post_exception:Timeout"))
    st = V32State.new(CLOSE, CTS, BUCKET_MAP, load_v32_params())
    from service.v32.events import OrderCancelled
    events = ex.on_action(_place_action(coid="cu"), st, CTS - 600)
    assert isinstance(events[0], OrderCancelled) and events[0].filled_count_before_cancel == 0
    assert "cu" in ex.rest_book and ex.rest_book["cu"].status == "unknown"
    assert ex.rest_book["cu"].order_id is None and ex.rest_book["cu"].price == Decimal("0.45")
    assert ex.stand_down_reason == "post_unknown_outcome"
    # the recorded coid makes a later phantom fill attributable (not foreign)
    assert ex.attribute(coid="cu") is not None


def test_post_5xx_is_treated_as_unknown_not_clean_reject():
    w = FakeWriter()
    ex = _exec(w)
    w.post_queue.append(WriteResponse(503, {"error": "upstream"}, False, "http_503"))
    ex.on_action(_place_action(coid="c5"), V32State.new(CLOSE, CTS, BUCKET_MAP, load_v32_params()),
                 CTS - 600)
    assert ex.rest_book["c5"].status == "unknown" and ex.stand_down_reason == "post_unknown_outcome"


# ---------------------------------------------------------------------------
# TAKE_WINGS / RETRY_WING — batch IOC create, synchronous fill truth
# ---------------------------------------------------------------------------
def _state_with_wings():
    p = load_v32_params()
    st = V32State.new(CLOSE, CTS, BUCKET_MAP, p, shakedown=False)
    from dataclasses import replace
    legs = (
        WingLeg(S_SD, "yes", 1, Decimal("0.32"), "wy"),
        WingLeg(S_SU, "no", 1, Decimal("0.22"), "wn"),
    )
    return replace(st, wing_legs=legs, wings_needed=True, wing_taken=True)


def test_take_wings_batch_body_ioc_and_fills():
    w = FakeWriter()
    ex = _exec(w)
    # a batch response: yes leg filled at 0.31 (YES-space), no leg filled (YES-space 0.79 -> NO 0.21)
    w.post_queue.append(WriteResponse(200, {"orders": [
        {"client_order_id": "wy", "order_id": "oy", "fill_count": "1.00", "remaining_count": "0.00",
         "average_fill_price": "0.3100", "average_fee_paid": "0.0015"},
        {"client_order_id": "wn", "order_id": "on", "fill_count": "1.00", "remaining_count": "0.00",
         "average_fill_price": "0.7900", "average_fee_paid": "0.0011"},
    ]}, True))
    events = ex.on_action(V32Action(kind=ActionKind.TAKE_WINGS), _state_with_wings(), CTS - 500)
    path, body = w.posts[0]
    assert path == REL_BATCH_CREATE
    assert len(body["orders"]) == 2
    assert all(o["time_in_force"] == "immediate_or_cancel" for o in body["orders"])
    from service.v32.events import Fill
    fills = {e.client_order_id: e for e in events if isinstance(e, Fill)}
    assert fills["wy"].count == Decimal(1) and fills["wy"].price == Decimal("0.31")
    # NO leg: parse normalizes YES 0.79 -> NO 0.21
    assert fills["wn"].price == Decimal("0.21")
    assert len(ex.fills) == 2  # money-math capture


def test_take_wings_ioc_no_fill_returns_count_zero_for_retry():
    w = FakeWriter()
    ex = _exec(w)
    w.post_queue.append(WriteResponse(200, {"orders": [
        {"client_order_id": "wy", "order_id": "oy", "fill_count": "0.00", "remaining_count": "1.00",
         "average_fill_price": None},
        {"client_order_id": "wn", "order_id": "on", "fill_count": "0.00", "remaining_count": "1.00",
         "average_fill_price": None},
    ]}, True))
    events = ex.on_action(V32Action(kind=ActionKind.TAKE_WINGS), _state_with_wings(), CTS - 500)
    from service.v32.events import Fill
    counts = {e.client_order_id: e.count for e in events if isinstance(e, Fill)}
    assert counts == {"wy": Decimal(0), "wn": Decimal(0)}  # count 0 -> core marks unfilled, retries


# ---------------------------------------------------------------------------
# P3-1: FrozenExecutor refuses a real kind; LiveExecutor refuses a WOULD_* twin
# ---------------------------------------------------------------------------
def test_frozen_executor_refuses_real_kind():
    fe = R.FrozenExecutor(BUCKET_MAP)
    st = V32State.new(CLOSE, CTS, BUCKET_MAP, load_v32_params())
    with pytest.raises(AssertionError):
        fe.on_action(_place_action(), st, CTS - 600)


def test_live_executor_refuses_would_twin():
    ex = _exec()
    st = V32State.new(CLOSE, CTS, BUCKET_MAP, load_v32_params())
    with pytest.raises(AssertionError):
        ex.on_action(V32Action(kind=ActionKind.WOULD_PLACE_REST, ticker=B, price=Decimal("0.45"),
                               count=1, client_order_id="c1"), st, CTS - 600)


def test_build_executor_selects_by_effective_mode(tmp_path):
    j = StreamJournal(os.path.join(tmp_path, "w.jsonl"), flush_every=1)
    j.open()
    p = load_v32_params()
    dry = R.build_executor("dry", bucket_map=BUCKET_MAP, exchange_index_by_ticker=EXCH, journal=j,
                           close_epoch_val=CTS, params=p, writer=None)
    assert isinstance(dry, R.FrozenExecutor)
    live = R.build_executor("armed", bucket_map=BUCKET_MAP, exchange_index_by_ticker=EXCH, journal=j,
                            close_epoch_val=CTS, params=p, writer=FakeWriter())
    assert isinstance(live, LiveExecutor)
    with pytest.raises(ValueError):
        R.build_executor("armed", bucket_map=BUCKET_MAP, exchange_index_by_ticker=EXCH, journal=j,
                         close_epoch_val=CTS, params=p, writer=None)  # armed needs a writer
    j.close()


# ---------------------------------------------------------------------------
# Startup safety: cancel our resting KXBTC* orders (a prior crash's leftovers)
# ---------------------------------------------------------------------------
def test_startup_cancel_only_kxbtc_orders():
    w = FakeWriter()
    j = FakeJournal()
    w.get_map["/portfolio/orders"] = {"orders": [
        {"ticker": "KXBTC-26SEP1316-B68200", "order_id": "o1"},
        {"ticker": "KXBTCD-26SEP1316-T68199.99", "order_id": "o2"},
        {"ticker": "KXETH-99", "order_id": "o3"},   # not ours-series -> left alone
    ]}
    res = cancel_stale_open_orders(w, j, clock=lambda: 0.0)
    assert res["found"] == 2 and res["cancelled"] == 2
    # no exchange_index on these fake orders -> no-shard fallback path
    assert set(w.deletes) == {cancel_path("o1", None), cancel_path("o2", None)}


def test_startup_cancel_skips_foreign_coid_but_clears_our_and_coidless():
    # Cross-pilot safety: never cancel a KXBTC* order carrying a FOREIGN coid (a re-armed v1.1 box
    # order shares the account); cancel our v32- orders and any coid-less crash leftover.
    w = FakeWriter()
    j = FakeJournal()
    w.get_map["/portfolio/orders"] = {"orders": [
        {"ticker": "KXBTCD-26SEP1316-T68199.99", "order_id": "v1", "client_order_id": "box-abc"},
        {"ticker": "KXBTC-26SEP1316-B68200", "order_id": "v2", "client_order_id": "v32-x-1"},
        {"ticker": "KXBTC-26SEP1316-B68300", "order_id": "v3"},  # no coid -> ours to clear
    ]}
    res = cancel_stale_open_orders(w, j, clock=lambda: 0.0)
    assert res["found"] == 2 and res["cancelled"] == 2
    assert set(w.deletes) == {cancel_path("v2", None), cancel_path("v3", None)}
    assert "v1" not in "".join(w.deletes)
    assert "startup_skip_foreign_order" in j.kinds()


# ---------------------------------------------------------------------------
# P3-4 exec-price mismatch + fill de-dup (ws + poll) — through the driver
# ---------------------------------------------------------------------------
def _armed_driver(tmp_path):
    p = load_v32_params()
    j = StreamJournal(os.path.join(tmp_path, "w.jsonl"), flush_every=1)
    j.open()
    st = V32State.new(CLOSE, CTS, BUCKET_MAP, p, shakedown=False)
    ex = _exec(journal=j)
    drv = R.V32Driver(p, st, j, ex, clock=lambda: 0.0)
    return drv, ex, j


def _seed_live_rest(drv, ex, coid="c1", n="0.45"):
    """Place a real rest so the driver's core slot + the executor RestBook both track it."""
    from service.book import TopOfBook
    def top(**kw):
        return TopOfBook(yes_bid=kw.get("yb"), yes_bid_size=None, yes_ask=kw.get("ya"),
                         yes_ask_size=None, no_bid=kw.get("nb"), no_bid_size=None,
                         no_ask=kw.get("na"), no_ask_size=None, suspect=False)
    now = CTS - 600
    drv.on_book_update(B, top(yb=Decimal("0.40"), ya=Decimal("0.40")), now)
    drv.on_book_update(S_SU, top(na=Decimal("0.20")), now)
    drv.on_book_update(S_SD, top(ya=Decimal("0.30")), now)
    return drv.state.rest_live


def test_exec_price_mismatch_raises_alarm(tmp_path):
    drv, ex, j = _armed_driver(tmp_path)
    rest = _seed_live_rest(drv, ex)
    assert rest is not None
    coid = rest.client_order_id
    oid = rest.order_id
    # a fill whose NO-space executed price != the resting price -> P3-4 alarm, still books at rest price
    payload = {"client_order_id": coid, "order_id": oid, "trade_id": "t1",
               "purchased_side": "no", "outcome_side": "no", "side": "yes",
               "yes_price_dollars": "0.5000", "count_fp": "1.00"}  # NO-space 0.50 != rest 0.55
    drv.on_fill(B, payload, CTS - 590)
    assert drv.counts["exec_price_mismatch"] == 1
    assert ex.exec_price_mismatches and ex.exec_price_mismatches[0]["order_id"] == oid
    assert drv.state.rest_fill is not None and drv.state.rest_fill.price == rest.price
    j.close()


def test_fill_dedup_ws_then_poll(tmp_path):
    drv, ex, j = _armed_driver(tmp_path)
    rest = _seed_live_rest(drv, ex)
    coid, oid = rest.client_order_id, rest.order_id
    payload = {"client_order_id": coid, "order_id": oid, "trade_id": "t1",
               "purchased_side": "no", "outcome_side": "no", "side": "yes",
               "yes_price_dollars": str(1 - Decimal(str(rest.price))), "count_fp": "1.00"}
    drv.on_fill(B, payload, CTS - 590)
    assert drv.counts["rest_fill"] == 1
    # the 1 s poll reports the SAME fill by order_id -> de-duped, not double-booked
    drv.on_poll_fill(oid, 1, CTS - 589)
    assert drv.counts.get("rest_fill_poll", 0) == 0
    j.close()


def test_driver_applies_executor_standdown(tmp_path):
    drv, ex, j = _armed_driver(tmp_path)
    ex.stand_down_reason = "rest_rejected_x3"       # executor latched after 3 consecutive rejects
    drv._apply_executor_standdown(CTS - 590)
    assert drv.state.stood_down is True
    assert drv.counts["executor_standdown"] == 1
    j.close()


def test_fill_dedup_poll_first_then_ws(tmp_path):
    drv, ex, j = _armed_driver(tmp_path)
    rest = _seed_live_rest(drv, ex)
    coid, oid = rest.client_order_id, rest.order_id
    drv.on_poll_fill(oid, 1, CTS - 590)          # poll sees it first
    assert drv.counts["rest_fill_poll"] == 1
    payload = {"client_order_id": coid, "order_id": oid, "trade_id": "t1",
               "purchased_side": "no", "outcome_side": "no", "side": "yes",
               "yes_price_dollars": str(1 - Decimal(str(rest.price))), "count_fp": "1.00"}
    drv.on_fill(B, payload, CTS - 589)           # WS echo of the same order -> de-duped by order_id
    assert drv.counts.get("rest_fill", 0) == 0
    j.close()
