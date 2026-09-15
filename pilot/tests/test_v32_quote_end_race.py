"""V3.2 quote-end cancel vs venue-expiry race (2026-09-15 01:00:00Z armed window). FAKES ONLY — no
network, no proxy dialed, no key/holdout read.

The incident: the last unfilled rest (coid ``v32-2026-09-15T01:00:00Z-100``, order
``01a0a28f-...``) carried ``expiration_time`` = close-``quote_end_s`` = the SAME instant as the
executor's own quote-end cancel (T-5). The venue auto-expired it at 00:55:00.000659Z; our quote-end
DELETE at 00:55:00.367Z (correctly sharded) returned 404 (order already terminal). The non-2xx path
then GET'd the order-status, which — an eventually-consistent read — STILL said "resting", so the
executor retried the DELETE 3x AT THE SAME INSTANT (no delay), read "resting" every time, and
declared ``cancel_failed`` + alarm + stand-down on a venue that was actually clean.

The fix (this suite pins it):
  * a code grace so the venue auto-expiry lands EXPIRATION_GRACE_S AFTER the quote end (T-4), and the
    quote-end cancel (T-5) normally wins un-raced;
  * backoff (CANCEL_BACKOFF_S) between the DELETE retries so the eventually-consistent status settles;
  * status-truth: a 404 DELETE + a subsequent TERMINAL status confirms the cancel (via "status", or
    via "expired" when we are at/after the order's own expiration_time), never ``cancel_failed``;
  * ledger counters ``cancels_via_status`` / ``cancels_expired``.
"""

from __future__ import annotations

import gzip
import json
import os
from decimal import Decimal

from service.proxy_writer import WriteResponse
from service.v32 import V32State, load_v32_params
from service.v32.actions import ActionKind, V32Action
from service.v32.events import OrderCancelled
from service.v32.executor import (
    CANCEL_BACKOFF_S,
    CANCEL_RETRY_ATTEMPTS,
    EXPIRATION_GRACE_S,
    LiveExecutor,
    ORDER_STATUS_PATH_TMPL,
)

# The real incident order/window (from the fixture below).
CLOSE_ISO = "2026-09-15T01:00:00Z"
CLOSE_EPOCH = 1789434000          # 2026-09-15T01:00:00Z
QUOTE_END_S = 300                 # T-5
OLD_EXP = CLOSE_EPOCH - QUOTE_END_S           # 1789433700 == 00:55:00Z (the pre-fix race instant)
NEW_EXP = OLD_EXP + EXPIRATION_GRACE_S        # 1789433760 == 00:56:00Z == T-4
B = "KXBTC-26SEP1421-B77950"
BUCKET_MAP = {B: (77900.0, 77999.99)}
EXCH = {B: 2}
FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "v32",
                       "incident_20260915T010000Z_quote_end_race.jsonl.gz")


def _load_incident_place():
    """Pull the real ``place_rest`` (driver form) for the incident order from the fixture."""
    with gzip.open(FIXTURE, "rt") as f:
        for line in f:
            r = json.loads(line)
            obj = r.get("obj", {})
            if r.get("kind") == "place_rest" and "action" in obj:
                return obj
    raise AssertionError("fixture missing a driver-form place_rest")


class FakeJournal:
    def __init__(self):
        self.records: list[tuple] = []

    def append(self, kind, obj, ts):
        self.records.append((kind, dict(obj) if isinstance(obj, dict) else obj))

    def kinds(self):
        return [k for k, _ in self.records]

    def of(self, kind):
        return [o for k, o in self.records if k == kind]


class ScriptWriter:
    """POST always acks a fresh oid; DELETE + GET are scripted per test."""

    def __init__(self, delete_fn, status_seq):
        self.posts: list[tuple] = []
        self.deletes: list[str] = []
        self.gets: list[str] = []
        self._delete_fn = delete_fn
        self._status_seq = list(status_seq)      # each entry is the ``order`` dict for one GET
        self._oid = 0

    def rest_post(self, path, body):
        self.posts.append((path, body))
        self._oid += 1
        oid = f"oid-{self._oid}"
        return WriteResponse(201, {"order": {"order_id": oid,
                                             "client_order_id": body.get("client_order_id"),
                                             "fill_count": "0.00", "remaining_count": "1.00"}}, True)

    def rest_delete(self, path):
        self.deletes.append(path)
        return self._delete_fn(path)

    def rest_get(self, path, params=None):
        self.gets.append(path)
        # the pre-place venue-truth invariant GETs open orders (params={"status":"resting"}); keep it
        # clean so it does NOT consume the order-status script.
        if params is not None:
            return {"orders": []}
        # order-status GETs (no params) walk the scripted sequence (last entry sticks).
        if self._status_seq:
            order = self._status_seq.pop(0) if len(self._status_seq) > 1 else self._status_seq[0]
            return {"order": order}
        return {}


def _sleeps():
    rec: list[float] = []
    return rec, (lambda s: rec.append(s))


def _exec(writer, journal, sleep, close_epoch=CLOSE_EPOCH):
    return LiveExecutor(writer, BUCKET_MAP, EXCH, journal, close_epoch, QUOTE_END_S,
                        clock=lambda: 0.0, sleep=sleep)


def _st(close_epoch=CLOSE_EPOCH):
    return V32State.new(CLOSE_ISO, close_epoch, BUCKET_MAP, load_v32_params())


def _place_from_fixture(coid):
    obj = _load_incident_place()
    return V32Action(kind=ActionKind.PLACE_REST, ticker=obj["ticker"], side="no", action="buy",
                     count=int(Decimal(str(obj["count"]))), price=Decimal(str(obj["price"])),
                     expiration_epoch=int(obj["expiration_epoch"]), client_order_id=coid)


def _always_404(_path):
    return WriteResponse(404, {"error": {"code": "not_found"}}, False, "http_404")


# ===========================================================================
# (a) Incident replay: DELETE 404 -> GET resting -> (after backoff) GET canceled => via status
# ===========================================================================
def test_incident_delete_404_resting_then_canceled_confirms_via_status():
    place = _place_from_fixture("v32-2026-09-15T01:00:00Z-100")
    j = FakeJournal()
    rec_sleeps, sleep = _sleeps()
    # the venue eventually-consistent status: RESTING first (stale), then CANCELED after the backoff.
    w = ScriptWriter(_always_404, status_seq=[
        {"order_id": "oid-1", "status": "resting", "remaining_count_fp": "1.00",
         "fill_count_fp": "0.00"},
        {"order_id": "oid-1", "status": "canceled", "remaining_count_fp": "0.00",
         "fill_count_fp": "0.00"},
    ])
    ex = _exec(w, j, sleep)
    ex.on_action(place, _st(), OLD_EXP - 300)
    # cancel at the quote end (T-5), which with the grace is BEFORE the order's own expiry (T-4).
    events = ex.on_action(V32Action(kind=ActionKind.CANCEL_REST, client_order_id=place.client_order_id),
                          _st(), OLD_EXP)  # now == T-5 < NEW_EXP (T-4)
    # resolved by status-truth: confirmed, NOT failed.
    assert ex.cancel_failed_count == 0
    assert ex.stand_down_reason is None
    assert ex.cancels_confirmed == 1
    assert ex.cancels_via_status == 1 and ex.cancels_expired == 0
    assert "cancel_failed" not in j.kinds()
    assert not [a for a in ex.alarms]                       # NO alarm raised
    conf = j.of("cancel_confirmed")
    assert conf and conf[-1]["via"] == "status" and conf[-1]["delete_status"] == 404
    assert conf[-1]["filled_before_cancel"] == 0
    assert ex.rest_book[place.client_order_id].status == "cancelled"
    assert events[0].filled_count_before_cancel == Decimal(0)
    # at least one backoff sleep happened between the re-reads (the incident's missing delay).
    assert rec_sleeps and rec_sleeps[0] == CANCEL_BACKOFF_S[0]


# ===========================================================================
# (b) DELETE 404 -> terminal status with a fill => filled-before-cancel (wings path fires as today)
# ===========================================================================
def test_delete_404_terminal_with_fill_is_filled_before_cancel():
    j = FakeJournal()
    _, sleep = _sleeps()
    w = ScriptWriter(_always_404, status_seq=[
        {"order_id": "oid-1", "status": "executed", "fill_count_fp": "1.00",
         "remaining_count_fp": "0.00"}])
    ex = _exec(w, j, sleep)
    ex.on_action(_place_from_fixture("c1"), _st(), OLD_EXP - 300)
    events = ex.on_action(V32Action(kind=ActionKind.CANCEL_REST, client_order_id="c1"), _st(),
                          OLD_EXP)
    assert ex.cancels_confirmed == 1 and ex.cancel_failed_count == 0
    # the fill is routed exactly as a fill-before-cancel: booked once, path cancel_race, wings owed.
    assert events[0].filled_count_before_cancel == Decimal(1)
    rest_fills = [f for f in ex.fills if f.get("leg") == "rest"]
    assert len(rest_fills) == 1 and rest_fills[0]["path"] == "cancel_race"
    assert ex.rest_book["c1"].status == "filled"
    conf = j.of("cancel_confirmed")
    assert conf[-1]["filled_before_cancel"] == 1


# ===========================================================================
# (c) DELETE 404 -> resting through the WHOLE backoff => cancel_failed + stand-down (preserved)
# ===========================================================================
def test_delete_404_resting_through_backoff_still_cancel_failed():
    j = FakeJournal()
    rec_sleeps, sleep = _sleeps()
    w = ScriptWriter(_always_404, status_seq=[
        {"order_id": "oid-1", "status": "resting", "remaining_count_fp": "1.00"}])  # always resting
    ex = _exec(w, j, sleep)
    ex.on_action(_place_from_fixture("c1"), _st(), OLD_EXP - 300)
    n_deletes_before = len(w.deletes)
    events = ex.on_action(V32Action(kind=ActionKind.CANCEL_REST, client_order_id="c1"), _st(),
                          OLD_EXP)
    # the existing behaviour is preserved: initial DELETE + CANCEL_RETRY_ATTEMPTS retries.
    assert len(w.deletes) - n_deletes_before == 1 + CANCEL_RETRY_ATTEMPTS
    assert ex.cancel_failed_count == 1
    assert ex.stand_down_reason == "cancel_failed"
    assert ex.rest_book["c1"].status == "cancel_failed"
    assert "cancel_failed" in j.kinds()
    assert any(a["alarm"] == "cancel_failed" for a in ex.alarms)
    assert events[0].filled_count_before_cancel == Decimal(0)
    # (e) the sleep sequence is EXACTLY the backoff, in order.
    assert rec_sleeps == list(CANCEL_BACKOFF_S)


# ===========================================================================
# (point 2) now >= expiration_time + terminal status => confirmed via "expired"
# ===========================================================================
def test_delete_404_at_or_after_expiry_confirms_via_expired():
    j = FakeJournal()
    _, sleep = _sleeps()
    w = ScriptWriter(_always_404, status_seq=[
        {"order_id": "oid-1", "status": "expired", "fill_count_fp": "0.00",
         "remaining_count_fp": "0.00"}])
    ex = _exec(w, j, sleep)
    ex.on_action(_place_from_fixture("c1"), _st(), OLD_EXP - 300)
    # cancel AT/AFTER the order's own expiration (grace already elapsed) -> expiry landing.
    events = ex.on_action(V32Action(kind=ActionKind.CANCEL_REST, client_order_id="c1"), _st(),
                          NEW_EXP + 1)
    assert ex.cancel_failed_count == 0 and ex.stand_down_reason is None
    assert ex.cancels_confirmed == 1
    assert ex.cancels_expired == 1 and ex.cancels_via_status == 0
    conf = j.of("cancel_confirmed")
    assert conf[-1]["via"] == "expired"
    assert events[0].filled_count_before_cancel == Decimal(0)


# ===========================================================================
# (d) create body carries expiration_time == close - quote_end_s + EXPIRATION_GRACE_S (T-4)
# ===========================================================================
def test_create_body_expiration_is_quote_end_plus_grace():
    j = FakeJournal()
    _, sleep = _sleeps()
    w = ScriptWriter(_always_404, status_seq=[])
    ex = _exec(w, j, sleep)
    ex.on_action(_place_from_fixture("c1"), _st(), OLD_EXP - 300)
    body = w.posts[-1][1]
    assert body["expiration_time"] == CLOSE_EPOCH - QUOTE_END_S + EXPIRATION_GRACE_S == NEW_EXP
    # and the RestRecord remembers that expiry for the cancel path's expiry-awareness.
    assert ex.rest_book["c1"].expiration_epoch == NEW_EXP


# ===========================================================================
# (e) the injected sleep is used and its sequence is exactly the backoff (also asserted in (c))
# ===========================================================================
def test_backoff_constant_matches_retry_attempts():
    # one sleep per retry attempt (the loop indexes CANCEL_BACKOFF_S by attempt).
    assert len(CANCEL_BACKOFF_S) == CANCEL_RETRY_ATTEMPTS
    assert list(CANCEL_BACKOFF_S) == [0.25, 0.75, 2.0]
