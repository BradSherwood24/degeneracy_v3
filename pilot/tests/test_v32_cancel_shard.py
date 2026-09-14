"""V3.2 cancel-shard hotfix (2026-09-14 first-armed-window incident). FAKES ONLY — no network, no
proxy dialed, no key/holdout read.

The incident: crypto orders live on ``exchange_index: 2`` (Kalshi exchange sharding). A cancel WITHOUT
the shard query param returns HTTP 404 {"error":{"code":"not_found"}} while the order stays LIVE and
resting; the executor misread that 404 as "already gone", so the core placed the NEXT rest and 21 of
our orders accumulated on the venue in ~2.5 minutes. These tests pin the fix: the cancel URL carries
``?exchange_index=2``; a DELETE 404 is verified against the order status (never assumed gone) and
retried shard-aware, then stands the hour down; a pre-PLACE venue-truth invariant refuses to place
while any of our orders rests; the startup sweep routes each order's own shard; and a REPLAY of the
incident's first order-path records shows the fix bounds live rests to at most one."""

from __future__ import annotations

import gzip
import json
import os
import re
from collections import deque
from decimal import Decimal

from service.proxy_auth import DEFAULT_PROXY_BASE, REST_PREFIX
from service.proxy_writer import ProxyWriter, WriteResponse
from service.v32 import V32State, load_v32_params
from service.v32.actions import ActionKind, V32Action
from service.v32.events import OrderCancelled
from service.v32.executor import (
    CANCEL_RETRY_ATTEMPTS,
    LiveExecutor,
    OPEN_ORDERS_PATH,
    ORDER_STATUS_PATH_TMPL,
    cancel_path,
    cancel_stale_open_orders,
)

CLOSE = "2026-09-14T22:00:00Z"
CTS = 1789423200
B = "KXBTC-26SEP1418-B78850"
S_SD = "KXBTCD-26SEP1418-T78849.99"
S_SU = "KXBTCD-26SEP1418-T78949.99"
BUCKET_MAP = {B: (78850.0, 78949.99)}
EXCH = {B: 2, S_SD: 2, S_SU: 2}
FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "v32",
                       "incident_20260914T220000Z_orderpath.jsonl.gz")


class FakeJournal:
    def __init__(self):
        self.records: list[tuple] = []

    def append(self, kind, obj, ts):
        self.records.append((kind, obj))

    def kinds(self):
        return [k for k, _ in self.records]


def _st():
    return V32State.new(CLOSE, CTS, BUCKET_MAP, load_v32_params())


def _exec(writer, journal=None):
    return LiveExecutor(writer, BUCKET_MAP, EXCH, journal or FakeJournal(), CTS, 300,
                        clock=lambda: 0.0, sleep=lambda _s: None)


def _place(coid="c1", n="0.54"):
    return V32Action(kind=ActionKind.PLACE_REST, ticker=B, side="no", action="buy", count=1,
                     price=Decimal(n), expiration_epoch=CTS - 300, client_order_id=coid)


# ===========================================================================
# 1. Cancel URL composition carries ?exchange_index=2 (final URL recorded)
# ===========================================================================
def test_cancel_path_helper_carries_shard():
    assert cancel_path("oid-9", 2) == "/portfolio/events/orders/oid-9?exchange_index=2"
    assert cancel_path("oid-9", None) == "/portfolio/events/orders/oid-9"  # no-shard fallback


def test_rest_delete_final_url_has_exchange_index_param():
    seen: list[str] = []

    class _Resp:
        status_code = 200

        def json(self):
            return {"order_id": "oid-9", "reduced_by": "1.00"}

        text = ""

    def http_delete(url, timeout):
        seen.append(url)
        return _Resp()

    w = ProxyWriter(base_url=DEFAULT_PROXY_BASE, http_delete=http_delete, sleep=lambda _s: None)
    wr = w.rest_delete(cancel_path("oid-9", 2))
    assert wr.ok
    # the FINAL composed URL (single /trade-api/v2 prefix) carries the shard query param verbatim.
    assert seen == [f"{DEFAULT_PROXY_BASE}{REST_PREFIX}/portfolio/events/orders/oid-9?exchange_index=2"]


def test_executor_cancel_sends_sharded_delete():
    w = _RecordingWriter()
    ex = _exec(w)
    ex.on_action(_place(coid="c1"), _st(), CTS - 600)
    oid = ex.rest_book["c1"].order_id
    ex.on_action(V32Action(kind=ActionKind.CANCEL_REST, client_order_id="c1"), _st(), CTS - 590)
    assert w.deletes and w.deletes[-1] == cancel_path(oid, 2)
    assert "exchange_index=2" in w.deletes[-1]


# ===========================================================================
# A minimal recording writer with scriptable POST/DELETE/GET
# ===========================================================================
class _RecordingWriter:
    def __init__(self):
        self.posts: list[tuple] = []
        self.deletes: list[str] = []
        self.gets: list[tuple] = []
        self.post_queue: deque = deque()
        self.delete_fn = None          # (path)->WriteResponse, else default 200
        self.get_map: dict[str, object] = {}
        self._oid = 0

    def rest_post(self, path, body):
        self.posts.append((path, body))
        if self.post_queue:
            return self.post_queue.popleft()
        self._oid += 1
        oid = f"oid-{self._oid}"
        return WriteResponse(201, {"order": {"order_id": oid, "client_order_id":
                                             body.get("client_order_id"), "fill_count": "0.00",
                                             "remaining_count": "1.00"}}, True)

    def rest_delete(self, path):
        self.deletes.append(path)
        if self.delete_fn is not None:
            return self.delete_fn(path)
        return WriteResponse(200, {"reduced_by": "1.00"}, True)

    def rest_get(self, path, params=None):
        self.gets.append((path, params))
        v = self.get_map.get(path)
        return v() if callable(v) else (v if v is not None else {})


# ===========================================================================
# 2. DELETE 404 -> status GET path: resting -> retry -> cancel_failed -> stand-down (never PLACE)
# ===========================================================================
def test_delete_404_still_resting_retries_then_cancel_failed_stands_down():
    w = _RecordingWriter()
    j = FakeJournal()
    ex = _exec(w, j)
    ex.on_action(_place(coid="c1"), _st(), CTS - 600)
    oid = ex.rest_book["c1"].order_id
    # EVERY delete (even sharded) 404s; the order-status GET says it is STILL resting.
    w.delete_fn = lambda p: WriteResponse(404, {"error": {"code": "not_found"}}, False, "http_404")
    w.get_map[ORDER_STATUS_PATH_TMPL.format(order_id=oid)] = {
        "order": {"order_id": oid, "status": "resting", "fill_count_fp": "0.00",
                  "remaining_count_fp": "1.00"}}
    deletes_before = len(w.deletes)
    events = ex.on_action(V32Action(kind=ActionKind.CANCEL_REST, client_order_id="c1"), _st(),
                          CTS - 590)
    # a 404 is NOT "gone": the initial DELETE + CANCEL_RETRY_ATTEMPTS retries, all shard-aware.
    assert len(w.deletes) - deletes_before == 1 + CANCEL_RETRY_ATTEMPTS
    assert all("exchange_index=2" in d for d in w.deletes[deletes_before:])
    # still resting after retries -> cancel_failed + stand down; the RestRecord is NOT "cancelled".
    assert ex.cancel_failed_count == 1
    assert ex.stand_down_reason == "cancel_failed"
    assert ex.rest_book["c1"].status == "cancel_failed"
    assert "cancel_failed" in j.kinds()
    assert ex.cancel_404s >= 1
    # the confirm event carries filled 0 (no fill happened); the stand-down blocks any replacement.
    assert len(events) == 1 and isinstance(events[0], OrderCancelled)
    assert events[0].filled_count_before_cancel == Decimal(0)
    # and NO create was sent as part of resolving this failed cancel.
    assert len(w.posts) == 1  # only the original place


def test_delete_404_then_sharded_retry_succeeds():
    # A first (transiently 404-ing) DELETE that then succeeds on a shard-aware retry resolves cleanly.
    w = _RecordingWriter()
    ex = _exec(w)
    ex.on_action(_place(coid="c1"), _st(), CTS - 600)
    oid = ex.rest_book["c1"].order_id
    calls = {"n": 0}

    def deletes(path):
        calls["n"] += 1
        if calls["n"] == 1:
            return WriteResponse(404, {"error": {"code": "not_found"}}, False, "http_404")
        return WriteResponse(200, {"order_id": oid, "reduced_by": "1.00"}, True)

    w.delete_fn = deletes
    # status GET keeps saying resting until the retry lands (so we go down the retry path, not terminal)
    w.get_map[ORDER_STATUS_PATH_TMPL.format(order_id=oid)] = {
        "order": {"order_id": oid, "status": "resting", "remaining_count_fp": "1.00"}}
    events = ex.on_action(V32Action(kind=ActionKind.CANCEL_REST, client_order_id="c1"), _st(),
                          CTS - 590)
    assert ex.cancels_confirmed == 1 and ex.cancel_failed_count == 0
    assert ex.stand_down_reason is None
    assert ex.rest_book["c1"].status == "cancelled"
    assert events[0].filled_count_before_cancel == Decimal(0)


# ===========================================================================
# 3. DELETE 404 with terminal status 'executed' -> the race Fill is routed (wings then taken)
# ===========================================================================
def test_delete_404_status_executed_routes_fill():
    w = _RecordingWriter()
    ex = _exec(w)
    ex.on_action(_place(coid="c1"), _st(), CTS - 600)
    oid = ex.rest_book["c1"].order_id
    w.delete_fn = lambda p: WriteResponse(404, {"error": {"code": "not_found"}}, False, "http_404")
    # the venue says the order EXECUTED (a fill slipped in before we could cancel).
    w.get_map[ORDER_STATUS_PATH_TMPL.format(order_id=oid)] = {
        "order": {"order_id": oid, "status": "executed", "fill_count_fp": "1.00",
                  "remaining_count_fp": "0.00"}}
    events = ex.on_action(V32Action(kind=ActionKind.CANCEL_REST, client_order_id="c1"), _st(),
                          CTS - 590)
    # no retry storm: a terminal status resolves immediately (initial DELETE only).
    assert ex.cancels_confirmed == 1 and ex.cancel_failed_count == 0
    # the fill is routed to the core (OrderCancelled.filled>0 -> core takes the wings) and booked once.
    assert len(events) == 1 and events[0].filled_count_before_cancel == Decimal(1)
    rest_fills = [f for f in ex.fills if f.get("leg") == "rest"]
    assert len(rest_fills) == 1 and rest_fills[0]["path"] == "cancel_race"
    assert ex.rest_book["c1"].status == "filled"


def test_core_routes_cancel_race_fill_as_the_entry():
    # The cancel-race Fill the executor surfaces (OrderCancelled.filled>0) becomes THE hourly entry in
    # the core — the state from which the wings are taken (the wing take itself needs live wing books,
    # exercised by the core's own suite; here we pin that filled>0 is booked as the rest fill).
    from dataclasses import replace

    from service.v32.core import RestOrder, decide_v32
    p = load_v32_params()
    st = replace(_st(), spot_Sd=78850, spot_Su=78950,
                 rest_live=RestOrder(client_order_id="c1", order_id="oid-1", price=Decimal("0.54"),
                                     count=1, placed_ts=CTS - 600, live=True, pending=False,
                                     bucket_Sd=78850))
    st2, _actions = decide_v32(p, st, OrderCancelled(order_id="oid-1", server_ts=CTS - 590,
                                                     filled_count_before_cancel=Decimal(1)))
    assert st2.rest_fill is not None and int(st2.rest_fill.count) == 1  # the race fill = the entry
    assert st2.wings_needed  # wings are now owed on this entry


# ===========================================================================
# 4. Pre-PLACE venue-truth invariant: one of ours already resting -> no PLACE, cancel + stand down
# ===========================================================================
def test_pre_place_invariant_blocks_when_venue_holds_our_resting_order():
    w = _RecordingWriter()
    j = FakeJournal()
    ex = _exec(w, j)
    # the venue already shows one of OUR orders resting (a coid we never confirmed gone) — internal
    # state disagrees with the venue. The invariant must refuse to place.
    w.get_map[OPEN_ORDERS_PATH] = {"orders": [
        {"order_id": "ghost-1", "client_order_id": "v32-ghost", "ticker": B, "exchange_index": 2,
         "status": "resting"}]}
    events = ex.on_action(_place(coid="v32-new"), _st(), CTS - 600)
    assert ex.rest_invariant_violations == 1
    assert len(w.posts) == 0                       # NO create was sent
    assert w.deletes == [cancel_path("ghost-1", 2)]  # the straggler was cancelled, shard-aware
    assert ex.stand_down_reason == "rest_invariant_violation"
    assert "rest_invariant_violation" in j.kinds()
    assert isinstance(events[0], OrderCancelled)   # core slot cleared; stand-down blocks re-place


def test_pre_place_invariant_ignores_foreign_and_proceeds_when_clean():
    w = _RecordingWriter()
    ex = _exec(w)
    # only a FOREIGN (non-v32) order rests -> not ours -> place proceeds normally.
    w.get_map[OPEN_ORDERS_PATH] = {"orders": [
        {"order_id": "box-1", "client_order_id": "box-abc", "ticker": B, "exchange_index": 2,
         "status": "resting"}]}
    ex.on_action(_place(coid="v32-1"), _st(), CTS - 600)
    assert ex.rest_invariant_violations == 0
    assert len(w.posts) == 1 and "v32-1" in ex.rest_book


# ===========================================================================
# 5. Startup sweep passes each order's OWN exchange_index
# ===========================================================================
def test_startup_sweep_routes_each_orders_shard():
    w = _RecordingWriter()
    j = FakeJournal()
    w.get_map[OPEN_ORDERS_PATH] = {"orders": [
        {"ticker": "KXBTC-26SEP1418-B78850", "order_id": "o1", "exchange_index": 2,
         "client_order_id": "v32-a"},
        {"ticker": "KXBTCD-26SEP1418-T78849.99", "order_id": "o2", "exchange_index": 2},
    ]}
    res = cancel_stale_open_orders(w, j, clock=lambda: 0.0)
    assert res["found"] == 2 and res["cancelled"] == 2
    assert set(w.deletes) == {cancel_path("o1", 2), cancel_path("o2", 2)}
    assert all("exchange_index=2" in d for d in w.deletes)


# ===========================================================================
# 6. REPLAY of the incident order-path fixture through the executor + a sharded fake venue
# ===========================================================================
class FakeVenue:
    """A venue that mirrors the incident: it REQUIRES the shard query param on a cancel. A DELETE
    without ``exchange_index`` 404s and leaves the order resting (the exact trap); a sharded DELETE
    removes it. Tracks the max number of OUR orders resting at once."""

    _OID_RE = re.compile(r"/portfolio/events/orders/([^?]+)")

    def __init__(self):
        self.resting: dict[str, dict] = {}
        self.max_concurrent = 0
        self._oid = 0

    def _touch_max(self):
        self.max_concurrent = max(self.max_concurrent, len(self.resting))

    def rest_post(self, path, body):
        self._oid += 1
        oid = f"venue-{self._oid}"
        self.resting[oid] = {"client_order_id": body.get("client_order_id"),
                             "exchange_index": body.get("exchange_index"),
                             "ticker": body.get("ticker")}
        self._touch_max()
        return WriteResponse(201, {"order": {"order_id": oid,
                                             "client_order_id": body.get("client_order_id"),
                                             "fill_count": "0.00", "remaining_count": "1.00"}}, True)

    def rest_delete(self, path):
        m = self._OID_RE.search(path)
        oid = m.group(1) if m else ""
        has_shard = "exchange_index=" in path
        if not has_shard:
            # the incident trap: un-sharded cancel 404s while the order stays LIVE.
            return WriteResponse(404, {"error": {"code": "not_found"}}, False, "http_404")
        if oid in self.resting:
            del self.resting[oid]
            return WriteResponse(200, {"order_id": oid, "reduced_by": "1.00"}, True)
        return WriteResponse(200, {"order_id": oid, "reduced_by": "0.00"}, True)

    def rest_get(self, path, params=None):
        if path == OPEN_ORDERS_PATH:
            return {"orders": [{"order_id": oid, "client_order_id": v["client_order_id"],
                                "ticker": v["ticker"], "exchange_index": v["exchange_index"],
                                "status": "resting"} for oid, v in self.resting.items()]}
        oid = path.rsplit("/", 1)[-1].split("?")[0]
        if oid in self.resting:
            return {"order": {"order_id": oid, "status": "resting", "remaining_count_fp": "1.00"}}
        return {"order": {"order_id": oid, "status": "canceled", "fill_count_fp": "0.00",
                          "remaining_count_fp": "0.00"}}


def _load_incident_steps():
    """Reconstruct the logical (place coid / cancel coid) sequence from the fixture, de-duping the
    driver+executor double-journaling."""
    steps: list[tuple] = []
    last = None
    with gzip.open(FIXTURE, "rt") as f:
        for line in f:
            r = json.loads(line)
            obj = r["obj"]
            coid = obj.get("client_order_id")
            if r["kind"] == "place_rest" and "action" in obj:      # driver-form place only
                step = ("place", coid, obj.get("price"))
            elif r["kind"] == "cancel_rest":
                step = ("cancel", coid, None)
            else:
                continue
            if step[:2] != (last[:2] if last else None) or step[0] == "place":
                # keep places always; collapse the consecutive duplicate cancel journalings
                if not (step[0] == "cancel" and last is not None and last[0] == "cancel"
                        and last[1] == coid):
                    steps.append(step)
                    last = step
    return steps


def test_incident_replay_prefix_would_stack_postfix_bounds_to_one():
    steps = _load_incident_steps()
    assert steps and steps[0][0] == "place"
    n_places = sum(1 for s in steps if s[0] == "place")
    assert n_places >= 5   # the fixture holds several full cycles

    # --- PRE-FIX counterfactual: cancels went un-sharded (404, misread as gone) -> stacking. ---
    pre = FakeVenue()
    for kind, coid, _ in steps:
        if kind == "place":
            pre.rest_post(REST_PREFIX + "/portfolio/events/orders",
                          {"client_order_id": coid, "exchange_index": 2, "ticker": B})
        else:
            # old executor: DELETE with NO shard param, then treat the 404 as "gone" (place next).
            pre.rest_delete("/portfolio/events/orders/venue-x")
    assert pre.max_concurrent > 1                 # the incident: many live rests at once
    assert pre.max_concurrent == n_places         # every place stacked (nothing ever cancelled)

    # --- POST-FIX: drive the real executor; the shard-aware cancel + invariant bound it to one. ---
    venue = FakeVenue()
    ex = _exec(venue, FakeJournal())
    for kind, coid, price in steps:
        if kind == "place":
            ex.on_action(_place(coid=coid, n=str(price or "0.54")), _st(), CTS - 600)
        else:
            ex.on_action(V32Action(kind=ActionKind.CANCEL_REST, client_order_id=coid), _st(),
                         CTS - 590)
    assert venue.max_concurrent <= 1              # never two live rests on the venue
    assert ex.cancel_failed_count == 0            # every cancel landed (shard param present)
    assert ex.rest_invariant_violations == 0      # internal state stayed in step with the venue
    assert len(venue.resting) == 0                # nothing left resting at the end
