"""2026-10-02 00:00Z window fixes (first live exercise of the D5 netting path + Brad's "confirm the order
attempted last time actually didn't fill" audit). FAKES ONLY; no network, no proxy, no key, no holdout.

  1. ``_journal_action`` on a WING_NETTED action: the netted legs are ``LegOrder`` (fill price in
     ``limit``); the old line read ``.price`` and raised inside the loop-side ingest.
  2. The async pump: a journaling exception on ONE action is alarmed and the remaining queued events are
     still processed (the 00:00Z fatal dropped the sibling wing-leg Fill -> false one-legged + S1 occurrence).
  3. ``_dispatch_async``: a failure while ingesting ONE result never abandons the dispatch's other futures.
  4. Wing venue-confirm: a wing chunk whose POST response is LOST / non-2xx is checked against the venue's
     fills before the leg is reported unfilled, so a lost response on an executed IOC is never re-bought.
  5. Amend 404 (racing a fill): resolved by status truth with NO DELETE round trip.
  6. The window summary line serialises Decimal money fields (both fill windows exited 1 on this)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace as dr
from decimal import Decimal

from service.proxy_writer import WriteResponse
from service.record_window import _append_summary
from service.v32.actions import ActionKind, V32Action
from service.v32.events import Fill
from service.v32.executor import ORDER_STATUS_PATH_TMPL
from service.v33 import V33State, WingLeg, load_v33_params
from service.v33.actions import LegOrder, V33Action, V33ActionKind
from service.v33.async_executor import FILLS_PATH, V33AsyncExecutor
from service.v33.async_writer import AsyncOrderWriter
from service import run_v33 as RUN

from tests.test_v33_async_fractional import (B, BUCKET_MAP, CLOSE, CTS, EXCH, S_SD, S_SU,
                                             FakeJournal, FracAsyncProxy)


# ---------------------------------------------------------------------------
# 1 + 2: the netted-action journal line and the pump's per-action guard
# ---------------------------------------------------------------------------
class _Exec:
    counts: dict = {}
    stand_down_reason = None

    def on_action(self, a, st, ts):
        return []


def _driver():
    p = load_v33_params()
    st = V33State.new(CLOSE, CTS, BUCKET_MAP, p)
    return RUN.V33Driver(p, st, FakeJournal(), _Exec(), dry_sim=False, clock=lambda: 0.0)


def test_wing_netted_journal_reads_the_leg_limit_as_fill_price():
    drv = _driver()
    legs = (LegOrder(S_SU, "yes", "net", Decimal("3"), Decimal("0.46")),
            LegOrder(S_SU, "no", "net", Decimal("3"), Decimal("0.65")))
    a = V33Action(kind=V33ActionKind.WING_NETTED, ticker=S_SU, side="yes", action="net", count=3,
                  legs=legs, lock=Decimal("-0.33"))
    drv._journal_action(a, 0.0)          # the 00:00Z line: raised AttributeError('price') before the fix
    kinds = [k for k, _ in drv.journal.records]
    assert kinds == ["wing_netted"]
    rec = drv.journal.records[0][1]
    assert [lg["fill_price"] for lg in rec["legs"]] == [Decimal("0.46"), Decimal("0.65")]
    assert rec["realised"] == Decimal("-0.33")


def test_pump_async_survives_a_journal_error_and_keeps_processing(monkeypatch):
    """A journaling failure on ONE action must not abort the pump: the remaining queued events still
    reach decide. decide is faked: event 1 yields an informational action whose legs cannot be journaled
    (the 00:00Z shape: AttributeError inside ``_journal_action``); event 2 marks both wing legs filled.
    Before the fix the exception unwound the pump with event 2 still queued."""
    drv = _driver()
    st = drv.state
    legs = (WingLeg(S_SD, "yes", Decimal("1"), Decimal("0.55"), "v33-wy", batch=0),
            WingLeg(S_SU, "no", Decimal("1"), Decimal("0.90"), "v33-wn", batch=0))
    drv.state = dr(st, wing_legs=legs)
    bad = V33Action(kind=V33ActionKind.WING_NETTED, ticker=S_SU, side="yes", action="net", count=1,
                    legs=(object(),), lock=Decimal("0"))          # journaling this raises AttributeError

    def fake_decide(params, state, ev):
        if ev is ev1:
            return state, [bad]
        filled = tuple(dr(lg, status="filled", fill_price=lg.limit) for lg in state.wing_legs)
        return dr(state, wing_legs=filled), []

    monkeypatch.setattr(RUN, "decide_v33", fake_decide)
    ev1 = Fill(order_id=None, client_order_id="v33-wy", count=Decimal("1"), price=Decimal("0.55"),
               side="yes", server_ts=float(CTS - 500))
    ev2 = Fill(order_id=None, client_order_id="v33-wn", count=Decimal("1"), price=Decimal("0.90"),
               side="no", server_ts=float(CTS - 500))

    async def go():
        drv._pump_async([ev1, ev2])

    asyncio.run(go())
    by_coid = {lg.client_order_id: lg for lg in drv.state.wing_legs}
    assert by_coid["v33-wy"].status == "filled"
    assert by_coid["v33-wn"].status == "filled"          # event 2 was NOT dropped
    alarms = [o for k, o in drv.journal.records if k == "alarm"]
    assert any(o.get("alarm") == "journal_action_error" for o in alarms), drv.journal.records
    assert drv._async_errors >= 1


# ---------------------------------------------------------------------------
# 3: a failing ingest of ONE dispatch result never abandons the others
# ---------------------------------------------------------------------------
class _AsyncExec:
    counts: dict = {}
    stand_down_reason = None

    def __init__(self):
        self.done = []

    async def on_action_async(self, a, st, ts):
        await asyncio.sleep(0)
        self.done.append(a.client_order_id)
        return [("result", a.client_order_id)]


def test_dispatch_async_ingest_error_does_not_abandon_sibling_results(monkeypatch):
    drv = _driver()
    drv.executor = _AsyncExec()
    seen = []

    def ingest(result):
        seen.append(result[0][1])
        if result[0][1] == "a":
            raise RuntimeError("boom on a")

    monkeypatch.setattr(drv, "_ingest_async", ingest)
    acts = [V32Action(kind=ActionKind.CANCEL_REST, order_id="o", ticker=B, side="no", action="buy",
                      count=1, client_order_id=c) for c in ("a", "b", "c")]

    async def go():
        await drv._dispatch_async(acts, 0.0, drv.state)

    asyncio.run(go())
    assert sorted(seen) == ["a", "b", "c"]
    alarms = [o.get("alarm") for k, o in drv.journal.records if k == "alarm"]
    assert "async_ingest_error" in alarms and "async_dispatch_fatal" not in alarms


# ---------------------------------------------------------------------------
# 4: wing venue-confirm on a LOST response
# ---------------------------------------------------------------------------
class _LostResponseProxy(FracAsyncProxy):
    """First wing POST: transport failure (ok=False, status None) AFTER the venue executed. The fills GET
    then shows the executed chunks. Any later POST behaves normally."""

    def __init__(self, venue_fills):
        super().__init__(cap=2)
        self.lost_once = True
        self.venue_fills = venue_fills
        self.gets: list[tuple] = []

    def rest_post(self, path, body, headers=None):
        self.posts.append((path, body))
        if self.lost_once:
            self.lost_once = False
            return WriteResponse(None, {}, False, "post_exception:ReadTimeout")
        return super().rest_post(path, body, headers)

    def rest_get(self, path, params=None):
        self.gets.append((path, params))
        if path == FILLS_PATH:
            return {"fills": self.venue_fills}
        return super().rest_get(path, params)


def _aexec(fake):
    aw = AsyncOrderWriter(fake, wing_workers=6, cancel_workers=12, normal_workers=12)
    ex = V33AsyncExecutor(aw, fake, BUCKET_MAP, EXCH, FakeJournal(), CTS, 300, k_rungs=11,
                          clock=lambda: 1000.0, sleep=lambda _s: None, wing_cap=2, batch_create=True)
    ex.wing_cap = 2
    return ex, aw


def _wing_state(count):
    st = V33State.new(CLOSE, CTS, BUCKET_MAP, load_v33_params())
    legs = (WingLeg(S_SD, "yes", count, Decimal("0.55"), "v33-wy", batch=0),
            WingLeg(S_SU, "no", count, Decimal("0.90"), "v33-wn", batch=0))
    return dr(st, wing_legs=legs)


def _take(ex, aw, st):
    act = V32Action(kind=ActionKind.TAKE_WINGS, ticker=B, side="no", action="buy", count=1,
                    price=Decimal("0.55"), client_order_id="v33-w")

    async def go():
        return await ex.on_action_async(act, st, CTS - 400)

    return asyncio.run(go())


def test_lost_wing_response_is_confirmed_at_the_venue_not_rebought():
    """3-lot wing batch; the POST response is lost but the venue filled all 3 of both legs. The executor
    must report BOTH legs filled (count 3) from the fills lookup -- not 'unfilled' (which would retry and
    buy a second set of wings)."""
    venue = [
        {"ticker": S_SD, "side": "yes", "order_id": "v-1", "count_fp": "2.00",
         "created_time": "2026-09-20T03:53:21.000Z"},
        {"ticker": S_SD, "side": "yes", "order_id": "v-2", "count_fp": "1.00",
         "created_time": "2026-09-20T03:53:21.100Z"},
        {"ticker": S_SU, "side": "no", "order_id": "v-3", "count_fp": "2.00",
         "created_time": "2026-09-20T03:53:21.000Z"},
        {"ticker": S_SU, "side": "no", "order_id": "v-4", "count_fp": "1.00",
         "created_time": "2026-09-20T03:53:21.100Z"},
        # noise: another ticker / an old fill -> ignored
        {"ticker": B, "side": "no", "order_id": "v-9", "count_fp": "1.00",
         "created_time": "2026-09-20T03:53:21.000Z"},
    ]
    fake = _LostResponseProxy(venue)
    ex, aw = _aexec(fake)
    try:
        events = _take(ex, aw, _wing_state(Decimal("3")))
    finally:
        aw.close()
    fills = {e.client_order_id: e for e in events if isinstance(e, Fill)}
    assert fills["v33-wy"].count == Decimal("3") and fills["v33-wn"].count == Decimal("3")
    assert fills["v33-wy"].price == Decimal("0.55") and fills["v33-wn"].price == Decimal("0.90")  # limit
    assert ex._wing_filled[(0, "yes")] == Decimal("3") and ex._wing_filled[(0, "no")] == Decimal("3")
    assert ex.wing_venue_confirms == 2 and ex.wing_venue_confirmed_count == Decimal("6")
    assert len([p for p, _ in fake.posts]) == 1            # ONE send; nothing re-bought
    assert [p for p, _ in fake.gets] == [FILLS_PATH, FILLS_PATH]
    kinds = [k for k, _ in ex.journal.records]
    assert kinds.count("wing_venue_confirm") == 2
    # a retry for these legs now sends NOTHING (remaining 0)
    st2 = _wing_state(Decimal("3"))
    try:
        ex2_events = _take(ex, AsyncOrderWriter(fake), st2)
    finally:
        pass
    assert len([p for p, _ in fake.posts]) == 1


def test_lost_wing_response_with_no_venue_fill_stays_unfilled():
    fake = _LostResponseProxy(venue_fills=[])
    ex, aw = _aexec(fake)
    try:
        events = _take(ex, aw, _wing_state(Decimal("1")))
    finally:
        aw.close()
    fills = {e.client_order_id: e for e in events if isinstance(e, Fill)}
    assert fills["v33-wy"].count == Decimal("0") and fills["v33-wn"].count == Decimal("0")
    assert ex.wing_venue_confirms == 2 and ex.wing_venue_confirmed_count == Decimal("0")


def test_definitive_zero_fill_chunk_does_not_query_the_venue():
    """An IOC the venue ANSWERED with fill_count 0 is a definitive no-fill: no fills GET."""
    class _ZeroFill(FracAsyncProxy):
        def __init__(self):
            super().__init__(cap=2)
            self.gets = []

        def _one(self, o):
            return {"client_order_id": o.get("client_order_id"), "order_id": "oid-z",
                    "fill_count": "0.00", "average_fill_price": None}

        def rest_get(self, path, params=None):
            self.gets.append(path)
            return {}

    fake = _ZeroFill()
    ex, aw = _aexec(fake)
    try:
        events = _take(ex, aw, _wing_state(Decimal("1")))
    finally:
        aw.close()
    assert all(e.count == Decimal("0") for e in events if isinstance(e, Fill))
    assert fake.gets == [] and ex.wing_venue_confirms == 0


# ---------------------------------------------------------------------------
# 5: amend 404 -> status resolve, no DELETE
# ---------------------------------------------------------------------------
def test_amend_404_resolves_by_status_without_a_delete():
    fake = FracAsyncProxy(cap=2)
    deletes = []
    fake.rest_delete = lambda path, headers=None: deletes.append(path) or WriteResponse(404, {}, False)
    ex, aw = _aexec(fake)
    st = V33State.new(CLOSE, CTS, BUCKET_MAP, load_v33_params())
    place = V32Action(kind=ActionKind.PLACE_REST, ticker=B, side="no", action="buy", count=1,
                      price=Decimal("0.45"), client_order_id="v33-r0", expiration_epoch=CTS)

    async def go():
        try:
            await ex.on_action_async(place, st, CTS - 600)
            oid = ex.rest_book["v33-r0"].order_id
            # the amend races the fill: 404 from the venue; the status GET says executed, 1.00 filled
            real_post = fake.rest_post
            fake.rest_post = lambda path, body, headers=None: WriteResponse(
                404, {"error": {"code": "not_found", "message": "not found"}}, False, "http_404")
            fake.get_map[ORDER_STATUS_PATH_TMPL.format(order_id=oid)] = {
                "order": {"order_id": oid, "status": "executed",
                          "fill_count_fp": "1.00", "remaining_count_fp": "0.00"}}
            amend = V32Action(kind=ActionKind.AMEND_REST, order_id=oid, ticker=B, side="no",
                              action="buy", count=1, price=Decimal("0.44"), client_order_id="v33-r0",
                              updated_client_order_id="v33-r0b")
            events = await ex.on_action_async(amend, st, CTS - 590)
            fake.rest_post = real_post
            return events, oid
        finally:
            aw.close()

    events, oid = asyncio.run(go())
    assert deletes == []                                   # no cancel round trip
    kinds = [k for k, _ in ex.journal.records]
    af = [o for k, o in ex.journal.records if k == "amend_failed"]
    assert af and af[-1]["fallback"] == "status_resolve" and af[-1]["status"] == 404
    assert "cancel_rest" not in kinds
    assert ex.counts.get("amend_404_status_resolved", 0) == 1
    # the fill surfaced by status truth reaches the core (a Fill or a cancelled-with-fill event)
    assert any(getattr(e, "count", None) == Decimal("1") or
               getattr(e, "filled_count_before_cancel", None) in (1, Decimal("1")) for e in events), events


# ---------------------------------------------------------------------------
# 6: Decimal-safe summary line
# ---------------------------------------------------------------------------
def test_summary_line_serialises_decimal(tmp_path):
    path = str(tmp_path / "summary.jsonl")
    _append_summary(path, {"close_time": CLOSE, "realized_delta": Decimal("1.2788"),
                           "rungs_filled": Decimal("11")})
    rows = [json.loads(l) for l in open(path, encoding="utf-8")]
    assert rows[0]["realized_delta"] == "1.2788" and rows[0]["rungs_filled"] == "11"


# ---------------------------------------------------------------------------
# 7: wing SUB-BATCHES that fit the venue bucket (the 02:00Z 429 storm: a 12-order burst can never pass)
# ---------------------------------------------------------------------------
from service.v33.executor import (WING_BATCH_MAX_ORDERS, WriteTokenBucket, split_wing_batches,
                                  wing_batch_max_orders)


def test_wing_batch_max_orders_fits_the_bucket():
    assert wing_batch_max_orders(100) == 8          # Basic: 100 tokens, 10/order -> 10, capped at 8 headroom
    assert wing_batch_max_orders(50) == 5
    assert wing_batch_max_orders(5) == 1
    assert wing_batch_max_orders(300) == WING_BATCH_MAX_ORDERS


def test_split_wing_batches_interleaves_legs():
    entries = [{"client_order_id": f"y{i}"} for i in range(6)] + [{"client_order_id": f"n{i}"} for i in range(6)]
    owner = {**{f"y{i}": (0, "yes") for i in range(6)}, **{f"n{i}": (0, "no") for i in range(6)}}
    subs = split_wing_batches(entries, owner, 8)
    assert [len(s) for s in subs] == [8, 4]
    first = [e["client_order_id"] for e in subs[0]]
    assert first == ["y0", "n0", "y1", "n1", "y2", "n2", "y3", "n3"]      # both legs in every sub-batch
    assert sum(len(s) for s in subs) == 12


def test_pacer_fit_waits_for_the_bucket_to_hold_the_cost():
    t = {"now": 0.0}
    slept = []
    b = WriteTokenBucket(100.0, 100.0, lambda: t["now"], lambda s: slept.append(s))
    assert b.acquire(80, "wing_take", priority=True) == 0.0            # first sub-batch: served now
    assert b.acquire(40, "wing_take", priority=True, fit=True) > 0.0   # 20 left: must wait for 40 to fit
    assert slept and abs(slept[0] - 0.2) < 1e-9


def test_eleven_lot_wing_take_goes_out_as_two_fitting_sub_batches():
    """11 lots per leg at the 2-contract cap = 12 chunks. Before: ONE 12-order POST (120 tokens; Kalshi
    rejects it whole on Basic). After: two POSTs of 8 + 4 orders, both legs in each, all filled."""
    fake = FracAsyncProxy(cap=2)
    ex, aw = _aexec(fake)
    try:
        events = _take(ex, aw, _wing_state(Decimal("11")))
    finally:
        aw.close()
    batched = [body for path, body in fake.posts if "orders" in body]
    assert [len(b["orders"]) for b in batched] == [8, 4]
    sides0 = {o["ticker"] for o in batched[0]["orders"]}
    assert sides0 == {S_SD, S_SU}                                     # interleaved, neither leg starved
    fills = {e.client_order_id: e for e in events if isinstance(e, Fill)}
    assert fills["v33-wy"].count == Decimal("11") and fills["v33-wn"].count == Decimal("11")
    assert ex.wing_batches == 2 and ex.wing_chunks == 12
    tw = [o for k, o in ex.journal.records if k == "take_wings" and o.get("chunked")]
    assert [(o["sub_batch"], o["sub_batches"]) for o in tw] == [(1, 2), (2, 2)]


class _Always429(FracAsyncProxy):
    def __init__(self):
        super().__init__(cap=2)
        self.gets = []

    def rest_post(self, path, body, headers=None):
        self.posts.append((path, body))
        return WriteResponse(429, {"error": "too many requests"}, False, "http_429")

    def rest_get(self, path, params=None):
        self.gets.append(path)
        return {}


def test_wing_429_is_definitive_backs_off_and_does_not_query_the_venue():
    fake = _Always429()
    t = {"now": 1000.0}
    aw = AsyncOrderWriter(fake, wing_workers=6, cancel_workers=12, normal_workers=12)
    ex = V33AsyncExecutor(aw, fake, BUCKET_MAP, EXCH, FakeJournal(), CTS, 300, k_rungs=11,
                          clock=lambda: t["now"], sleep=lambda _s: None, wing_cap=2, batch_create=True)
    ex.wing_cap = 2
    act = V32Action(kind=ActionKind.TAKE_WINGS, ticker=B, side="no", action="buy", count=1,
                    price=Decimal("0.55"), client_order_id="v33-w")

    async def go():
        try:
            ev1 = await ex.on_action_async(act, _wing_state(Decimal("1")), CTS - 400)
            n_posts = len(fake.posts)
            assert n_posts >= 1
            # 429 -> definitive: both legs unfilled, NO fills GET, a backoff window is open
            assert all(e.count == Decimal("0") for e in ev1 if isinstance(e, Fill))
            assert fake.gets == [] and ex.wing_venue_confirms == 0
            assert ex.wing_rate_limited_batches >= 1 and ex._wing_backoff_until > t["now"]
            assert "wing_backoff" in [k for k, _ in ex.journal.records]
            # a retry INSIDE the window sends nothing and reports unfilled
            ev2 = await ex.on_action_async(act, _wing_state(Decimal("1")), CTS - 399)
            assert len(fake.posts) == n_posts and ex.wing_backoff_skips == 1
            assert all(e.count == Decimal("0") for e in ev2 if isinstance(e, Fill))
            # once the window has passed the retry sends again (and the streak grows the next window)
            t["now"] = ex._wing_backoff_until + 0.01
            await ex.on_action_async(act, _wing_state(Decimal("1")), CTS - 398)
            assert len(fake.posts) > n_posts and ex._wing_429_streak == 2
        finally:
            aw.close()

    asyncio.run(go())


# ---------------------------------------------------------------------------
# 8 (review 2026-10-02): venue-confirm on a leg with BOTH a READABLE and a LOST sub-batch, and the
# cross-take re-count guard. The venue GET excludes this take's readable chunks (their order_ids are in
# _wing_order_ids), so its count must be booked ADDITIVELY on top of the readable chunks (capped by the
# leg's room) -- NOT subtracted against the readable total, which dropped the real lost-chunk fills, let
# the leg report unfilled and the core RE-BUY the chunk that had executed.
# ---------------------------------------------------------------------------
class _SecondBatchLostProxy(FracAsyncProxy):
    """Sub-batch 1 (the 8-order POST) is SERVED (readable fills with order_ids); sub-batch 2 (the 4-order
    POST) is LOST after the venue executed it. The fills GET then shows the 3 remaining lots per leg."""

    def __init__(self, venue_fills):
        super().__init__(cap=2)
        self.venue_fills = venue_fills
        self.gets: list = []
        self._batch_posts = 0

    def rest_post(self, path, body, headers=None):
        # NB: super().rest_post appends to self.posts, so DON'T pre-append here (would double-count sends).
        if "orders" in body:
            self._batch_posts += 1
            if self._batch_posts == 2:          # the fit-paced second sub-batch: response lost
                self.posts.append((path, body))
                return WriteResponse(None, {}, False, "post_exception:ReadTimeout")
        return super().rest_post(path, body, headers)

    def rest_get(self, path, params=None):
        self.gets.append((path, params))
        if path == FILLS_PATH:
            return {"fills": self.venue_fills}
        return super().rest_get(path, params)


def test_mixed_readable_and_lost_subbatch_books_additively_no_rebuy():
    """11 lots/leg -> sub-batches [8,4]. Sub-batch 1 fills 8 lots/leg (readable); sub-batch 2 (3 lots/leg)
    is LOST but executed at the venue. The leg must complete at 11 (8 readable + 3 venue-confirmed) and
    NOT be re-bought. The pre-fix ``confirmed - agg_count`` compare dropped the 3 (8 >= 3) -> leg unfilled
    -> core re-buys the 3 that already executed -> over-hedge."""
    venue = [
        {"ticker": S_SD, "side": "yes", "order_id": "v-y1", "count_fp": "2.00",
         "created_time": "2026-09-20T03:53:21.000Z"},
        {"ticker": S_SD, "side": "yes", "order_id": "v-y2", "count_fp": "1.00",
         "created_time": "2026-09-20T03:53:21.100Z"},
        {"ticker": S_SU, "side": "no", "order_id": "v-n1", "count_fp": "2.00",
         "created_time": "2026-09-20T03:53:21.000Z"},
        {"ticker": S_SU, "side": "no", "order_id": "v-n2", "count_fp": "1.00",
         "created_time": "2026-09-20T03:53:21.100Z"},
    ]
    fake = _SecondBatchLostProxy(venue)
    ex, aw = _aexec(fake)
    try:
        events = _take(ex, aw, _wing_state(Decimal("11")))
    finally:
        aw.close()
    fills = {e.client_order_id: e for e in events if isinstance(e, Fill)}
    assert fills["v33-wy"].count == Decimal("11") and fills["v33-wn"].count == Decimal("11")
    assert ex._wing_filled[(0, "yes")] == Decimal("11") and ex._wing_filled[(0, "no")] == Decimal("11")
    assert ex.wing_venue_confirmed_count == Decimal("6")       # 3 per leg, booked ADDITIVELY
    # exactly the two original sub-batch sends; the lost one is not re-sent in this take
    assert len([b for _, b in fake.posts if "orders" in b]) == 2
    # a follow-up take for the same legs now sends NOTHING (remaining 0 — no over-buy)
    fake._batch_posts = 0
    try:
        _take(ex, AsyncOrderWriter(fake), _wing_state(Decimal("11")))
    finally:
        pass
    assert len([b for _, b in fake.posts if "orders" in b]) == 2


class _FillsOnlyProxy(FracAsyncProxy):
    def __init__(self, fills):
        super().__init__(cap=2)
        self._fills = fills

    def rest_get(self, path, params=None):
        if path == FILLS_PATH:
            return {"fills": self._fills}
        return super().rest_get(path, params)


def test_venue_fills_excludes_known_ids_and_records_counted_ones():
    """A fill already attributed to a chunk we read (order_id in _wing_order_ids) is excluded; every fill
    the confirm COUNTS has its order_id recorded, so a later confirm inside the 5 s skew window can never
    re-count the same venue fill (the cross-take over-count guard)."""
    fills = [
        {"ticker": S_SD, "side": "yes", "order_id": "known", "count_fp": "2.00",
         "created_time": "2026-09-20T03:53:21.000Z"},
        {"ticker": S_SD, "side": "yes", "order_id": "fresh", "count_fp": "1.00",
         "created_time": "2026-09-20T03:53:21.100Z"},
    ]
    fake = _FillsOnlyProxy(fills)
    ex, aw = _aexec(fake)
    ex._wing_order_ids.add("known")
    try:
        total = asyncio.run(ex._venue_wing_fills_async(S_SD, "yes", 1000.0))
    finally:
        aw.close()
    assert total == Decimal("1")                       # 'known' excluded; only 'fresh' counted
    assert "fresh" in ex._wing_order_ids               # recorded -> a later confirm never re-counts it
    # a second confirm over the SAME venue snapshot now returns 0 (both ids are known)
    try:
        again = asyncio.run(ex._venue_wing_fills_async(S_SD, "yes", 1000.0))
    finally:
        pass
    assert again == Decimal("0")
