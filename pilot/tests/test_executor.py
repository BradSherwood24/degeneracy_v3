"""Tests for service/executor.py — the single order dispatcher (fake proxy HTTP layer)."""

from __future__ import annotations

import threading
import time
from decimal import Decimal

from service.executor import Executor, ExecutorConfig
from service.journal import Journal
from service.ledger import (
    PURPOSE_ENTRY,
    PURPOSE_REBALANCE_BUY,
    Intent,
    IntentLeg,
)

CT = "2026-06-14T02:00:00Z"
HI, LO = "KXBTCD-HI", "KXBTCD-LO"


class FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


def batch_ok(entries_by_cid):
    """Build a FakeResp that echoes a filled batch slot per submitted client_order_id."""
    def post(path, body):
        orders = body["orders"]
        slots = []
        for e in orders:
            cid = e["client_order_id"]
            fill, price, fee = entries_by_cid[cid]
            slots.append({
                "order_id": "o-" + cid, "client_order_id": cid,
                "fill_count": str(fill), "remaining_count": "0",
                "average_fill_price": str(price), "average_fee_paid": str(fee), "ts_ms": 1,
            })
        return FakeResp(200, {"orders": slots})
    return post


def entry(count=1, hi_cid="h", lo_cid="l", xi=0):
    # xi = exchange_index (shard); routed explicitly (2026-08-27 market_not_found incident) so the
    # dispatch gate (never send an unrouted order) admits the leg.
    legs = (
        IntentLeg(HI, "no", "buy", count, Decimal("0.57"), hi_cid, exchange_index=xi),
        IntentLeg(LO, "yes", "buy", count, Decimal("0.24"), lo_cid, exchange_index=xi),
    )
    return Intent(CT, "sub$1-flip", PURPOSE_ENTRY, legs, t_minus_s=300.0)


def armed_cfg(**kw):
    return ExecutorConfig(armed=True, **kw)


def test_refuses_when_not_armed():
    ex = Executor(Journal(), ExecutorConfig(armed=False), post_fn=lambda p, b: FakeResp(200, {}))
    r = ex.execute(entry())
    assert r.refused == "not_armed" and not r.dispatched
    assert all(resp.no_fill for resp in r.responses)


def test_armed_entry_batches_both_legs_in_one_post():
    posts = []
    def post(path, body):
        posts.append((path, body))
        return batch_ok({"h": (1, "0.57", "0.01"), "l": (1, "0.24", "0.01")})(path, body)
    ex = Executor(Journal(), armed_cfg(), post_fn=post)
    r = ex.execute(entry())
    assert r.dispatched and len(posts) == 1
    assert posts[0][0] == "/trade-api/v2/portfolio/events/orders/batched"
    assert len(posts[0][1]["orders"]) == 2
    assert {resp.client_order_id for resp in r.responses} == {"h", "l"}
    assert all(resp.filled for resp in r.responses)


def test_cap_rejects_oversize_and_bad_prefix():
    ex = Executor(Journal(), armed_cfg(max_contracts=2), post_fn=lambda p, b: FakeResp(200, {}))
    big = Intent(CT, "s", PURPOSE_ENTRY,
                 (IntentLeg(HI, "no", "buy", 3, Decimal("0.5"), "a"),), t_minus_s=300)
    assert ex.execute(big).refused.startswith("cap:count>")
    bad = Intent(CT, "s", PURPOSE_ENTRY,
                 (IntentLeg("XYZ", "no", "buy", 1, Decimal("0.5"), "b"),), t_minus_s=300)
    assert ex.execute(bad).refused == "cap:ticker_prefix:XYZ"


def test_no_orders_after_cutoff_refuses():
    ex = Executor(Journal(), armed_cfg(no_orders_after_s_to_settle=1), post_fn=lambda p, b: FakeResp(200, {}))
    r = ex.execute(entry(), t_minus_s=0.5)
    assert r.refused.startswith("no_orders_after_s_to_settle")


def test_entry_dedup_one_per_window():
    ex = Executor(Journal(), armed_cfg(),
                  post_fn=batch_ok({"h": (1, "0.57", "0.01"), "l": (1, "0.24", "0.01")}))
    assert ex.execute(entry()).dispatched
    # a second entry for the SAME window is refused
    r2 = ex.execute(entry(hi_cid="h2", lo_cid="l2"))
    assert r2.refused == "window_already_entered"


def test_429_on_order_path_is_no_fill_never_retried():
    calls = {"n": 0}
    def post(path, body):
        calls["n"] += 1
        return FakeResp(429, {"error": "rate"})
    ex = Executor(Journal(), armed_cfg(), post_fn=post)
    r = ex.execute(entry())
    assert calls["n"] == 1  # NEVER retried
    assert r.http_status == 429 and all(resp.no_fill for resp in r.responses)
    assert all(resp.error == "http_429" for resp in r.responses)


def test_5xx_on_order_path_is_no_fill():
    ex = Executor(Journal(), armed_cfg(), post_fn=lambda p, b: FakeResp(503, {}))
    r = ex.execute(entry())
    assert r.http_status == 503 and all(resp.no_fill for resp in r.responses)


def test_403_cap_from_proxy_is_no_fill():
    ex = Executor(Journal(), armed_cfg(), post_fn=lambda p, b: FakeResp(403, {"cap": "x"}))
    r = ex.execute(entry())
    assert r.http_status == 403 and all(resp.no_fill for resp in r.responses)


def test_post_exception_is_no_fill_not_raised():
    def post(path, body):
        raise ConnectionError("boom")
    ex = Executor(Journal(), armed_cfg(), post_fn=post)
    r = ex.execute(entry())
    assert all(resp.no_fill for resp in r.responses)
    assert all("post_exception" in resp.error for resp in r.responses)


def test_rate_budget_exhaustion_refuses():
    # budget 20 tokens, entry costs 20 (2 legs x 10); the next order (10) exceeds -> refused.
    ex = Executor(Journal(), armed_cfg(window_token_budget=20),
                  post_fn=batch_ok({"h": (1, "0.57", "0.01"), "l": (1, "0.24", "0.01")}))
    assert ex.execute(entry()).dispatched
    # budget is per-window; a follow-on rebalance in the same window has no tokens left
    reb = Intent(CT, "s", PURPOSE_REBALANCE_BUY,
                 (IntentLeg(HI, "no", "buy", 1, Decimal("0.55"), "r1", exchange_index=0),), t_minus_s=300)
    r = ex.execute(reb)
    assert r.refused == "rate_budget_exhausted"


def test_single_flight_refuses_key_already_in_flight():
    ex = Executor(Journal(), armed_cfg(),
                  post_fn=batch_ok({"h": (1, "0.57", "0.01"), "l": (1, "0.24", "0.01")}))
    # simulate an outstanding order for (window, side='no', purpose=rebalance-buy)
    ex._inflight.add((CT, "no", PURPOSE_REBALANCE_BUY))
    reb = Intent(CT, "s", PURPOSE_REBALANCE_BUY,
                 (IntentLeg(HI, "no", "buy", 1, Decimal("0.55"), "r1", exchange_index=0),), t_minus_s=300)
    assert ex.execute(reb).refused == "single_flight"


def test_mutex_serializes_concurrent_dispatch():
    """Two threads (entry + rebalance) contend; the mutex must serialize -> max concurrency 1."""
    active = {"n": 0, "max": 0}
    lock = threading.Lock()

    def post(path, body):
        with lock:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
        time.sleep(0.02)
        with lock:
            active["n"] -= 1
        # echo fills for whatever cids are present
        if "orders" in body:
            return batch_ok({e["client_order_id"]: (1, "0.5", "0.01") for e in body["orders"]})(path, body)
        return FakeResp(200, {"order": {"order_id": "o", "client_order_id": body["client_order_id"],
                                        "fill_count": "1", "remaining_count": "0",
                                        "average_fill_price": "0.5", "average_fee_paid": "0.01",
                                        "ts_ms": 1}})

    ex = Executor(Journal(), armed_cfg(), post_fn=post)
    reb = Intent("W2", "s", PURPOSE_REBALANCE_BUY,
                 (IntentLeg(HI, "no", "buy", 1, Decimal("0.55"), "r1", exchange_index=0),), t_minus_s=300)
    threads = [
        threading.Thread(target=ex.execute, args=(entry(),)),
        threading.Thread(target=ex.execute, args=(reb,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert active["max"] == 1  # never two POSTs at once (single order authority)


def test_crash_mid_order_rebuild_flags_inflight():
    """Journal the intent, then only the HIGH response arrives (crash). Rebuild + a positions poll:
    the LOW leg is in flight (intent, no response) -> reconcile-first surfaces it."""
    j = Journal()
    def post(path, body):
        # simulate: batch accepted, but we only get to journal HIGH's response before 'crash'
        return FakeResp(200, {"orders": [
            {"order_id": "oh", "client_order_id": "h", "fill_count": "1", "remaining_count": "0",
             "average_fill_price": "0.57", "average_fee_paid": "0.01", "ts_ms": 1},
            {"order_id": "ol", "client_order_id": "l", "fill_count": "1", "remaining_count": "0",
             "average_fill_price": "0.24", "average_fee_paid": "0.01", "ts_ms": 1},
        ]})
    ex = Executor(j, armed_cfg(), post_fn=post)
    ex.execute(entry())
    # Emulate a crash that lost the LOW response record from the flushed journal:
    records = [r for r in j.records()
               if not (r["kind"] == "order_response" and r["obj"].get("client_order_id") == "l")]
    from service.ledger import rebuild_from_journal
    st = rebuild_from_journal(records, CT, "sub$1-flip", HI, LO)
    assert st.net("high") == 1 and st.net("low") == 0
    assert st.inflight_cids() == ("l",)


def test_stop_authorized_flatten_dispatches_while_unarmed():
    posts = []
    def post(path, body):
        posts.append((path, body))
        return FakeResp(200, {"order": {"order_id": "o", "client_order_id": body["client_order_id"],
                                        "fill_count": "1", "remaining_count": "0",
                                        "average_fill_price": "0.55", "average_fee_paid": "0.01",
                                        "ts_ms": 1}})
    ex = Executor(Journal(), ExecutorConfig(armed=False), post_fn=post)  # UNARMED
    flat = Intent(CT, "sub$1-flip", "flatten",
                  (IntentLeg(HI, "no", "sell", 1, Decimal("0.55"), "f1", reduce_only=True,
                             exchange_index=0),))
    r = ex.execute(flat, t_minus_s=300, stop_authorized=True)
    assert r.dispatched and len(posts) == 1
    assert posts[0][0] == "/trade-api/v2/portfolio/events/orders"  # single create (live path)


def test_refuses_leg_without_exchange_index_and_journals(tmp_path=None):
    # Exchange sharding (2026-08-27 market_not_found incident): a leg whose exchange_index is None was
    # never resolved to a shard -> REFUSE (never send an unrouted order). No POST happens.
    posts = []
    j = Journal()
    ex = Executor(j, armed_cfg(), post_fn=lambda p, b: posts.append((p, b)) or FakeResp(200, {}))
    unrouted = entry(xi=None)  # both legs have exchange_index None
    r = ex.execute(unrouted)
    assert r.refused.startswith("no_exchange_index:")
    assert posts == []                                   # nothing was sent to the venue
    assert all(resp.no_fill for resp in r.responses)
    kinds = [rec["kind"] for rec in j.records()]
    assert "order_refused_no_exchange_index" in kinds
    # the refusal record names the offending tickers
    ref = next(rec["obj"] for rec in j.records() if rec["kind"] == "order_refused_no_exchange_index")
    assert set(ref["tickers"]) == {HI, LO}


def test_partial_exchange_index_still_refuses_whole_intent():
    # even one None-routed leg in a batch refuses the whole intent (never send a half-routed pair)
    posts = []
    ex = Executor(Journal(), armed_cfg(), post_fn=lambda p, b: posts.append(1) or FakeResp(200, {}))
    legs = (
        IntentLeg(HI, "no", "buy", 1, Decimal("0.57"), "h", exchange_index=2),
        IntentLeg(LO, "yes", "buy", 1, Decimal("0.24"), "l"),  # exchange_index None
    )
    r = ex.execute(Intent(CT, "sub$1-flip", PURPOSE_ENTRY, legs, t_minus_s=300.0))
    assert r.refused == f"no_exchange_index:{LO}" and posts == []


def test_shard2_entry_wire_body_routes_to_shard2():
    posts = []
    def post(path, body):
        posts.append((path, body))
        return batch_ok({"h": (1, "0.57", "0.01"), "l": (1, "0.24", "0.01")})(path, body)
    ex = Executor(Journal(), armed_cfg(), post_fn=post)
    r = ex.execute(entry(xi=2))
    assert r.dispatched
    assert all(o["exchange_index"] == 2 for o in posts[0][1]["orders"])


def test_stop_authorized_only_for_flatten():
    ex = Executor(Journal(), ExecutorConfig(armed=False), post_fn=lambda p, b: FakeResp(200, {}))
    r = ex.execute(entry(), stop_authorized=True)  # entry is not a flatten
    assert r.refused == "stop_authorized_non_flatten"


# ---------------------------------------------------------------------------
# BUG-1 (units): the executor normalizes the reported price into each leg's side-space at parse, so
# every consumer (ledger/stops/parity) sees a price comparable to the leg's limit. A NO leg's venue
# price comes back in YES-space (1 - true); a YES leg is passed through.
# ---------------------------------------------------------------------------
def _no_yes_entry():
    legs = (
        IntentLeg(HI, "no", "buy", 1, Decimal("0.97"), "h", exchange_index=0),   # NO leg, limit 0.97
        IntentLeg(LO, "yes", "buy", 1, Decimal("0.0250"), "l", exchange_index=0),  # YES leg, limit 0.025
    )
    return Intent(CT, "sub$1-flip", PURPOSE_ENTRY, legs, t_minus_s=300.0)


def test_batch_normalizes_no_leg_to_no_space_yes_leg_passthrough():
    # venue reports the NO leg's fill in YES-space (0.0300 == 1-0.97); YES leg reported as-is.
    post = batch_ok({"h": (1, "0.0300", "0.0021"), "l": (1, "0.0250", "0.0017")})
    ex = Executor(Journal(), armed_cfg(max_contracts=2), post_fn=post)
    r = ex.execute(_no_yes_entry())
    by_cid = {resp.client_order_id: resp for resp in r.responses}
    assert by_cid["h"].average_fill_price == Decimal("0.9700")   # normalized to NO-space
    assert by_cid["h"].raw_reported_price == Decimal("0.0300")   # raw venue value preserved
    assert by_cid["l"].average_fill_price == Decimal("0.0250")   # YES leg unchanged
    assert by_cid["l"].raw_reported_price == Decimal("0.0250")


def test_single_reduce_only_no_sell_normalizes_to_no_space():
    # a NO reduce-only sell @0.95 fills; venue reports 0.0500 (1-0.95) -> normalize to 0.95.
    leg = IntentLeg(HI, "no", "sell", 1, Decimal("0.95"), "s", reduce_only=True, exchange_index=0)
    intent = Intent(CT, "sub$1-flip", "flatten", (leg,), t_minus_s=300.0)
    def post(path, body):
        return FakeResp(200, {"order": {"order_id": "o", "client_order_id": "s",
                                        "fill_count": "1", "remaining_count": "0",
                                        "average_fill_price": "0.0500", "average_fee_paid": "0.0034",
                                        "ts_ms": 1}})
    ex = Executor(Journal(), armed_cfg(), post_fn=post)
    r = ex.execute(intent, stop_authorized=True)
    assert r.responses[0].average_fill_price == Decimal("0.9500")
    assert r.responses[0].raw_reported_price == Decimal("0.0500")
