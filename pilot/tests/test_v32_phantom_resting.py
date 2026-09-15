"""V3.2 read-path-lag phantom fix (2026-09-15 18:00Z live-window incident). FAKES ONLY — no network,
no proxy dialed, no key/holdout read.

The incident: the pre-PLACE venue-truth invariant (PR #46) GETs ``/portfolio/orders?status=resting``
and refuses to PLACE while any of ours rests. Kalshi's LIST read lags the matching engine by up to
~1 s (the same eventual-consistency class as the PR #50 T-5 cancel race). At 18:00Z the executor
confirmed a cancel of order -184 (DELETE 2xx, reduced_by 1.00) at 17:54:58.886, then 0.89 s later the
resting-LIST read STILL showed -184; the invariant misread that phantom as a live stray, cancelled it
(the DELETE came back 404 — already gone), alarmed, and stood the hour down, missing a set. The fix:
a DELETE-confirmed cancel within CANCEL_SETTLE_S makes a still-listed order a phantom (engine truth
beats the lagging read); an unknown survivor is re-read once after INVARIANT_RECHECK_S; and a stray
whose cancel 404s with a terminal/not-found status is a phantom (zero rests), not a violation."""

from __future__ import annotations

import gzip
import json
import os
from collections import deque
from decimal import Decimal

from service.proxy_writer import WriteResponse
from service.v32 import V32State, load_v32_params
from service.v32.actions import ActionKind, V32Action
from service.v32.events import OrderAck, OrderCancelled
from service.v32.executor import (
    CANCEL_SETTLE_S,
    INVARIANT_RECHECK_S,
    OPEN_ORDERS_PATH,
    LiveExecutor,
    cancel_path,
)

CLOSE = "2026-09-15T18:00:00Z"
CTS = 1789495200
B = "KXBTC-26SEP1514-B68250"
BUCKET_MAP = {B: (68200.0, 68299.99)}
EXCH = {B: 2}
FIX_PHANTOM = os.path.join(os.path.dirname(__file__), "fixtures", "v32",
                           "incident_20260915T180000Z_phantom.jsonl.gz")


class FakeJournal:
    def __init__(self):
        self.records: list[tuple] = []

    def append(self, kind, obj, ts):
        self.records.append((kind, obj))

    def kinds(self):
        return [k for k, _ in self.records]

    def of(self, kind):
        return [o for k, o in self.records if k == kind]


class _PhantomWriter:
    """A writer whose resting-orders LIST can be scripted to LAG the engine (still show a just-cancelled
    order). ``list_queue`` gives successive OPEN_ORDERS responses; ``status_map`` gives per-order status
    GETs (default: terminal ``canceled`` so the cancel-confirm poll never sleeps); ``delete_fn`` scripts
    the DELETE (default 200 reduced_by 1.00 = a genuinely-resting order pulled off the book)."""

    def __init__(self):
        self.posts: list[tuple] = []
        self.deletes: list[str] = []
        self.gets: list[tuple] = []
        self.oid_by_coid: dict[str, str] = {}
        self.list_queue: deque = deque()
        self.status_map: dict[str, dict] = {}
        self.delete_fn = None

    def rest_post(self, path, body):
        self.posts.append((path, body))
        coid = body.get("client_order_id") or f"anon-{len(self.posts)}"
        oid = f"oid-{coid}"
        self.oid_by_coid[coid] = oid
        return WriteResponse(201, {"order": {"order_id": oid, "client_order_id": coid,
                                             "fill_count": "0.00", "remaining_count": "1.00"}}, True)

    def rest_delete(self, path):
        self.deletes.append(path)
        if self.delete_fn is not None:
            return self.delete_fn(path)
        return WriteResponse(200, {"reduced_by": "1.00"}, True)

    def rest_get(self, path, params=None):
        self.gets.append((path, params))
        if path == OPEN_ORDERS_PATH:
            return self.list_queue.popleft() if self.list_queue else {"orders": []}
        oid = path.rsplit("/", 1)[-1].split("?")[0]
        st = self.status_map.get(oid)
        if st is not None:
            return st
        return {"order": {"order_id": oid, "status": "canceled", "fill_count_fp": "0.00",
                          "remaining_count_fp": "0.00"}}


def _st():
    return V32State.new(CLOSE, CTS, BUCKET_MAP, load_v32_params())


def _exec(writer, journal, sleeps):
    return LiveExecutor(writer, BUCKET_MAP, EXCH, journal, CTS, 300,
                        clock=lambda: 0.0, sleep=lambda s: sleeps.append(s))


def _place(coid, n="0.48"):
    return V32Action(kind=ActionKind.PLACE_REST, ticker=B, side="no", action="buy", count=1,
                     price=Decimal(n), expiration_epoch=CTS - 300, client_order_id=coid)


def _cancel(coid):
    return V32Action(kind=ActionKind.CANCEL_REST, client_order_id=coid)


def _listed(oid, coid):
    return {"order_id": oid, "client_order_id": coid, "ticker": B, "exchange_index": 2,
            "status": "resting"}


# ===========================================================================
# (a) The incident: a just-confirmed-cancelled order still on the LIST -> phantom -> PLACE proceeds.
# ===========================================================================
def test_incident_phantom_confirmed_cancel_filtered_place_proceeds():
    w = _PhantomWriter()
    j = FakeJournal()
    sleeps: list[float] = []
    ex = _exec(w, j, sleeps)
    # Reproduce the 18:00Z replace chain: place/cancel -184, place/cancel -185 (so -185 is the single
    # `_last_confirmed_gone_oid` the invariant already excludes), then place -186.
    ex.on_action(_place("v32-184", "0.48"), _st(), 100.0)
    oid184 = w.oid_by_coid["v32-184"]
    ex.on_action(_cancel("v32-184"), _st(), 101.0)   # DELETE 2xx -> cancel_confirmed_ts = 101.0
    assert ex.rest_book["v32-184"].cancel_confirmed_ts == 101.0
    ex.on_action(_place("v32-185", "0.49"), _st(), 101.0)
    ex.on_action(_cancel("v32-185"), _st(), 102.0)   # _last_confirmed_gone_oid -> oid-185
    # At the -186 place the LIST STILL shows -184 (read-path lag ~1.9 s), exactly the incident.
    w.list_queue.append({"orders": [_listed(oid184, "v32-184")]})
    events = ex.on_action(_place("v32-186", "0.48"), _st(), 102.9)
    # phantom filtered -> PLACE proceeded (a real create was sent and booked), no violation/alarm/standdown
    assert "v32-186" in ex.rest_book and ex.rest_book["v32-186"].order_id == "oid-v32-186"
    assert ex.rest_invariant_phantoms == 1
    assert ex.rest_invariant_violations == 0
    assert ex.stand_down_reason is None
    assert "rest_invariant_phantom" in j.kinds()
    assert "rest_invariant_violation" not in j.kinds()
    assert not any(a.get("alarm") == "rest_invariant_violation" for a in ex.alarms)
    ph = j.of("rest_invariant_phantom")[0]
    assert ph["order_id"] == oid184 and ph["coid"] == "v32-184"
    assert ph["age_s"] == 1.9 and 0 < ph["age_s"] < CANCEL_SETTLE_S
    # the phantom filter cleared the list on the first pass -> no recheck sleep was needed.
    assert sleeps == []
    assert ex.rest_invariant_rechecks == 0
    assert any(isinstance(e, OrderAck) and e.client_order_id == "v32-186" for e in events)  # -186 acked


def test_confirmed_cancel_older_than_settle_is_not_a_phantom():
    # A stale confirm (older than CANCEL_SETTLE_S) that STILL shows on the list is a real problem, not a
    # read-path phantom -> the invariant must not silently swallow it. (v32-1 must NOT be the single
    # `_last_confirmed_gone_oid`, which is excluded outright; cancel v32-2 after so v32-2 is that oid.)
    w = _PhantomWriter()
    j = FakeJournal()
    sleeps: list[float] = []
    ex = _exec(w, j, sleeps)
    ex.on_action(_place("v32-1", "0.48"), _st(), 100.0)
    oid1 = w.oid_by_coid["v32-1"]
    ex.on_action(_cancel("v32-1"), _st(), 101.0)      # confirmed at 101.0
    ex.on_action(_place("v32-2", "0.49"), _st(), 101.0)
    ex.on_action(_cancel("v32-2"), _st(), 102.0)      # _last_confirmed_gone_oid -> oid-v32-2
    # place far in the future: v32-1 age = 200 - 101 = 99 s >> CANCEL_SETTLE_S, and it is genuinely
    # resting (default DELETE 2xx reduced_by 1.00). List shows it through the re-read.
    w.list_queue.append({"orders": [_listed(oid1, "v32-1")]})
    w.list_queue.append({"orders": [_listed(oid1, "v32-1")]})   # persists through the re-read
    ex.on_action(_place("v32-3", "0.48"), _st(), 200.0)
    assert ex.rest_invariant_phantoms == 0
    assert ex.rest_invariant_rechecks == 1 and sleeps == [INVARIANT_RECHECK_S]
    assert ex.rest_invariant_violations == 1
    assert ex.stand_down_reason == "rest_invariant_violation"


# ===========================================================================
# (b) An UNKNOWN resting order that persists through the re-read and is genuinely resting -> real
#     violation: cancel it, alarm, stand down (PR #46 behaviour preserved).
# ===========================================================================
def test_unknown_stray_rechecked_then_genuine_stands_down():
    w = _PhantomWriter()
    j = FakeJournal()
    sleeps: list[float] = []
    ex = _exec(w, j, sleeps)
    ghost = _listed("ghost-1", "v32-ghost")
    w.list_queue.append({"orders": [ghost]})   # first read
    w.list_queue.append({"orders": [ghost]})   # re-read STILL shows it -> not lag
    # default DELETE = 200 reduced_by 1.00 -> genuinely resting; we just pulled it.
    events = ex.on_action(_place("v32-new", "0.48"), _st(), 100.0)
    assert ex.rest_invariant_rechecks == 1 and INVARIANT_RECHECK_S in sleeps
    assert ex.rest_invariant_violations == 1
    assert ex.rest_invariant_phantoms == 0
    assert ex.stand_down_reason == "rest_invariant_violation"
    assert w.deletes == [cancel_path("ghost-1", 2)]     # the stray was cancelled, shard-aware
    assert len(w.posts) == 0                            # NO create was sent
    assert any(a.get("alarm") == "rest_invariant_violation" for a in ex.alarms)
    assert "rest_invariant_violation" in j.kinds()
    assert isinstance(events[0], OrderCancelled)


def test_unknown_stray_clears_on_reread_place_proceeds():
    # An unknown survivor on the first read that is GONE on the re-read was read-path lag -> proceed.
    w = _PhantomWriter()
    j = FakeJournal()
    sleeps: list[float] = []
    ex = _exec(w, j, sleeps)
    w.list_queue.append({"orders": [_listed("ghost-x", "v32-ghost")]})   # first read: present
    w.list_queue.append({"orders": []})                                  # re-read: cleared
    ex.on_action(_place("v32-new", "0.48"), _st(), 100.0)
    assert ex.rest_invariant_rechecks == 1 and sleeps == [INVARIANT_RECHECK_S]
    assert ex.rest_invariant_violations == 0
    assert ex.stand_down_reason is None
    assert len(w.posts) == 1 and "v32-new" in ex.rest_book   # PLACE proceeded


# ===========================================================================
# (c) An unknown stray that PERSISTS but whose cancel 404s with a terminal/not-found status is a
#     phantom (zero rests) -> PLACE proceeds, no stand-down.
# ===========================================================================
def test_unknown_stray_cancel_404_status_gone_is_phantom_place_proceeds():
    w = _PhantomWriter()
    j = FakeJournal()
    sleeps: list[float] = []
    ex = _exec(w, j, sleeps)
    ghost = _listed("ghost-2", "v32-ghost")
    w.list_queue.append({"orders": [ghost]})
    w.list_queue.append({"orders": [ghost]})   # persists through the re-read
    w.delete_fn = lambda p: WriteResponse(404, {"error": {"code": "not_found"}}, False, "http_404")
    # status GET confirms terminal (PR #50 status-truth) -> the order is off the book.
    w.status_map["ghost-2"] = {"order": {"order_id": "ghost-2", "status": "canceled",
                                         "fill_count_fp": "0.00", "remaining_count_fp": "0.00"}}
    events = ex.on_action(_place("v32-new", "0.48"), _st(), 100.0)
    assert ex.rest_invariant_rechecks == 1 and INVARIANT_RECHECK_S in sleeps
    assert ex.rest_invariant_phantoms == 1
    assert ex.rest_invariant_violations == 0
    assert ex.stand_down_reason is None
    assert w.deletes == [cancel_path("ghost-2", 2)]    # the stray-cancel was attempted (404)
    assert len(w.posts) == 1 and "v32-new" in ex.rest_book   # PLACE proceeded
    ph = j.of("rest_invariant_phantom")[0]
    assert ph["order_id"] == "ghost-2" and ph["via"] == "cancel_confirm" and ph["delete_status"] == 404
    assert not any(a.get("alarm") == "rest_invariant_violation" for a in ex.alarms)


def test_unknown_stray_cancel_404_status_not_found_is_phantom():
    # Same as (c) but the status GET is a bare not-found body (no ``status`` field) -> still gone.
    w = _PhantomWriter()
    j = FakeJournal()
    sleeps: list[float] = []
    ex = _exec(w, j, sleeps)
    ghost = _listed("ghost-3", "v32-ghost")
    w.list_queue.append({"orders": [ghost]})
    w.list_queue.append({"orders": [ghost]})
    w.delete_fn = lambda p: WriteResponse(404, {"error": {"code": "not_found"}}, False, "http_404")
    w.status_map["ghost-3"] = {"error": {"code": "not_found"}}   # available, no status -> gone
    ex.on_action(_place("v32-new", "0.48"), _st(), 100.0)
    assert ex.rest_invariant_phantoms == 1 and ex.rest_invariant_violations == 0
    assert ex.stand_down_reason is None and len(w.posts) == 1


def test_stray_cancel_404_but_status_still_resting_is_a_violation():
    # A 404 DELETE whose status GET says STILL resting is NOT gone -> real violation + stand-down.
    w = _PhantomWriter()
    j = FakeJournal()
    sleeps: list[float] = []
    ex = _exec(w, j, sleeps)
    ghost = _listed("ghost-4", "v32-ghost")
    w.list_queue.append({"orders": [ghost]})
    w.list_queue.append({"orders": [ghost]})
    w.delete_fn = lambda p: WriteResponse(404, {"error": {"code": "not_found"}}, False, "http_404")
    w.status_map["ghost-4"] = {"order": {"order_id": "ghost-4", "status": "resting",
                                         "remaining_count_fp": "1.00"}}
    ex.on_action(_place("v32-new", "0.48"), _st(), 100.0)
    assert ex.rest_invariant_violations == 1 and ex.rest_invariant_phantoms == 0
    assert ex.stand_down_reason == "rest_invariant_violation"
    assert len(w.posts) == 0


# ===========================================================================
# (§1a) A stray that left the book by FILLING is NEVER a silent phantom — the status GET's fill_count is
#       cross-checked (_TERMINAL_STATUSES includes "executed") and the fill is booked or alarmed.
# ===========================================================================
def test_unknown_stray_2xx_reduced_by_0_but_executed_fill_alarms_stands_down():
    # DELETE 2xx pulled nothing (reduced_by 0) BUT the status shows executed fill 1: the order left the
    # book by filling. It is not in our RestBook (unknown coid) -> unbookable -> alarm + stand-down,
    # NOT a silent phantom that drops the fill.
    w = _PhantomWriter()
    j = FakeJournal()
    sleeps: list[float] = []
    ex = _exec(w, j, sleeps)
    ghost = _listed("ghost-f1", "v32-ghost")
    w.list_queue.append({"orders": [ghost]})
    w.list_queue.append({"orders": [ghost]})
    w.delete_fn = lambda p: WriteResponse(200, {"reduced_by": "0.00"}, True)   # pulled nothing
    w.status_map["ghost-f1"] = {"order": {"order_id": "ghost-f1", "status": "executed",
                                          "fill_count_fp": "1.00", "remaining_count_fp": "0.00"}}
    events = ex.on_action(_place("v32-new", "0.48"), _st(), 100.0)
    assert ex.rest_invariant_phantoms == 0            # the fill was NOT swallowed as a phantom
    assert ex.rest_invariant_violations == 0
    assert ex.stand_down_reason == "rest_invariant_unbooked_fill"
    assert any(a.get("alarm") == "rest_invariant_unbooked_fill" for a in ex.alarms)
    assert "rest_invariant_unbooked_fill" in j.kinds()
    assert j.of("rest_invariant_unbooked_fill")[0]["filled"] == 1
    assert len(w.posts) == 0                          # PLACE short-circuited
    assert isinstance(events[0], OrderCancelled)


def test_unknown_stray_404_status_executed_fill_alarms_stands_down():
    # 404 DELETE + status executed fill 1 on an UNKNOWN order -> unbookable fill -> alarm + stand-down.
    w = _PhantomWriter()
    j = FakeJournal()
    sleeps: list[float] = []
    ex = _exec(w, j, sleeps)
    ghost = _listed("ghost-f2", "v32-ghost")
    w.list_queue.append({"orders": [ghost]})
    w.list_queue.append({"orders": [ghost]})
    w.delete_fn = lambda p: WriteResponse(404, {"error": {"code": "not_found"}}, False, "http_404")
    w.status_map["ghost-f2"] = {"order": {"order_id": "ghost-f2", "status": "executed",
                                          "fill_count_fp": "1.00", "remaining_count_fp": "0.00"}}
    ex.on_action(_place("v32-new", "0.48"), _st(), 100.0)
    assert ex.rest_invariant_phantoms == 0 and ex.rest_invariant_violations == 0
    assert ex.stand_down_reason == "rest_invariant_unbooked_fill"
    assert any(a.get("alarm") == "rest_invariant_unbooked_fill" for a in ex.alarms)
    assert len(w.posts) == 0


def test_known_stray_that_filled_is_booked_and_routed_not_dropped():
    # A stray we DO track (in RestBook, price known) that left the book by filling is booked at the
    # order's price (routed to the core like a cancel-race entry), NOT dropped, NOT stood down.
    w = _PhantomWriter()
    j = FakeJournal()
    sleeps: list[float] = []
    ex = _exec(w, j, sleeps)
    ex.on_action(_place("v32-live", "0.48"), _st(), 100.0)   # a live rest, never cancelled
    oid = w.oid_by_coid["v32-live"]
    w.list_queue.append({"orders": [_listed(oid, "v32-live")]})
    w.list_queue.append({"orders": [_listed(oid, "v32-live")]})
    w.delete_fn = lambda p: WriteResponse(200, {"reduced_by": "0.00"}, True)   # nothing to pull: it filled
    w.status_map[oid] = {"order": {"order_id": oid, "status": "executed",
                                   "fill_count_fp": "1.00", "remaining_count_fp": "0.00"}}
    events = ex.on_action(_place("v32-new", "0.49"), _st(), 101.0)
    assert ex.rest_invariant_phantoms == 0 and ex.rest_invariant_violations == 0
    assert ex.stand_down_reason is None
    assert not any(a.get("alarm") == "rest_invariant_unbooked_fill" for a in ex.alarms)
    rest_fills = [f for f in ex.fills if f.get("leg") == "rest"]
    assert len(rest_fills) == 1
    assert rest_fills[0]["price"] == Decimal("0.48") and rest_fills[0]["path"] == "cancel_race"
    assert ex.rest_book["v32-live"].status == "filled"
    # the surprise fill is routed to the core as the entry; the PLACE is short-circuited.
    assert any(isinstance(e, OrderCancelled) and e.filled_count_before_cancel == Decimal(1)
               for e in events)
    assert "v32-new" not in ex.rest_book
    assert len(w.posts) == 1        # only the initial live place; the re-place was short-circuited


def test_report_render_surfaces_phantoms_and_rechecks():
    # The build report claims report.py surfaces phantoms AND rechecks next to violations — pin it.
    from service.v32.report import _render, build_report
    rows = [{"close_time": "2026-09-15T18:00:00Z", "armed": True, "rest_invariant_violations": 0,
             "rest_invariant_phantoms": 3, "rest_invariant_rechecks": 2, "shadow": {}}]
    rep = build_report(rows)
    t = rep["totals"]
    assert t["rest_invariant_violations"] == 0
    assert t["rest_invariant_phantoms"] == 3
    assert t["rest_invariant_rechecks"] == 2
    line = next(l for l in _render(rep).splitlines() if "rest_invariant:" in l)
    assert "violations=0" in line and "phantoms=3" in line and "rechecks=2" in line


# ===========================================================================
# (d) The sleep sequence: a phantom-only pass sleeps 0; an unknown-survivor pass sleeps exactly once.
# ===========================================================================
def test_invariant_sleep_sequence():
    # phantom pass: cleared on the first filter -> zero invariant sleeps.
    w = _PhantomWriter()
    sa: list[float] = []
    ex = _exec(w, FakeJournal(), sa)
    ex.on_action(_place("v32-1", "0.48"), _st(), 100.0)
    oid = w.oid_by_coid["v32-1"]
    ex.on_action(_cancel("v32-1"), _st(), 100.5)
    w.list_queue.append({"orders": [_listed(oid, "v32-1")]})
    ex.on_action(_place("v32-2", "0.48"), _st(), 101.0)
    assert sa == []                       # phantom filter cleared the list with no re-read

    # unknown-survivor pass: exactly one INVARIANT_RECHECK_S sleep before declaring.
    w2 = _PhantomWriter()
    sb: list[float] = []
    ex2 = _exec(w2, FakeJournal(), sb)
    ghost = _listed("ghost-9", "v32-ghost")
    w2.list_queue.append({"orders": [ghost]})
    w2.list_queue.append({"orders": [ghost]})
    ex2.on_action(_place("v32-new", "0.48"), _st(), 100.0)
    assert sb == [INVARIANT_RECHECK_S]


# ===========================================================================
# (e) The fixture slice of the real 18:00Z incident records exists, is small, and carries the
#     phantom signature the fix targets (a confirmed-cancel re-appearing on the resting LIST).
# ===========================================================================
def test_incident_fixture_slice_present_and_shows_phantom_signature():
    assert os.path.exists(FIX_PHANTOM)
    assert os.path.getsize(FIX_PHANTOM) < 200 * 1024
    with gzip.open(FIX_PHANTOM, "rt") as f:
        recs = [json.loads(line) for line in f]
    kinds = {r["kind"] for r in recs}
    assert {"cancel_confirmed", "rest_invariant_violation", "rest_invariant_cancel"} <= kinds
    confirmed_oids = {r["obj"]["order_id"] for r in recs if r["kind"] == "cancel_confirmed"}
    vio = [r for r in recs if r["kind"] == "rest_invariant_violation"]
    assert vio, "fixture must hold the invariant violation"
    listed = {o["order_id"] for o in vio[0]["obj"]["resting"]}
    # THE incident: an order the venue confirmed cancelled still appears in the violation's resting list.
    phantom_oids = listed & confirmed_oids
    assert phantom_oids, "a confirmed-cancelled order should re-appear on the resting LIST"
    # the invariant's stray-cancel came back 404 — the 'stray' was already gone (a phantom).
    inv_cancel = [r for r in recs if r["kind"] == "rest_invariant_cancel"]
    assert inv_cancel and inv_cancel[0]["obj"]["status"] == 404
    # and the phantom's age (confirm -> re-listing) is inside CANCEL_SETTLE_S, so our filter covers it.
    conf_ts = {r["obj"]["order_id"]: r["local_ts"] for r in recs if r["kind"] == "cancel_confirmed"}
    oid = next(iter(phantom_oids))
    age = vio[0]["local_ts"] - conf_ts[oid]
    assert 0 < age < CANCEL_SETTLE_S
