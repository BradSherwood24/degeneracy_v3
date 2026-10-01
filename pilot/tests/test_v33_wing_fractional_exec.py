"""V3.3 executor D3 (review, 2026-09-30 incident): the coalesced wing TAKE must hedge a FRACTIONAL fill.

The core fix (commits 9135e27/73f5efa) carries ``count_fp`` as Decimal up to the wing legs, but the
armed executor's ``_take_wings`` still truncated the hedge with ``int(lg.count)`` (and the wire body via
``to_v2_order``'s ``f"{int(count):.2f}"``). A 1.44-lot fill would then send only 1 wing lot and leave
0.44 NAKED the moment V3.3 re-arms. These tests pin the review fix: the wire ``count`` is the 2dp
fractional string, the chunks sum EXACTLY to the fill, and the aggregated hedge equals the fractional
fill — never the int-truncated lot. FAKES ONLY; no network, no proxy, no key/holdout.
"""

from __future__ import annotations

from dataclasses import replace as dr
from decimal import Decimal

from service.proxy_writer import WriteResponse
from service.v32.events import Fill
from service.v33 import V33State, WingLeg, load_v33_params
from service.v33.executor import V33LiveExecutor

CLOSE = "2026-09-20T04:00:00Z"
CTS = 1789876800
B = "KXBTC-26SEP2000-B80450"
S_SD = "KXBTCD-26SEP2000-T80399.99"
S_SU = "KXBTCD-26SEP2000-T80499.99"
BUCKET_MAP = {B: (80400.0, 80499.99)}
EXCH = {B: 2, S_SD: 2, S_SU: 2}


class FakeJournal:
    def __init__(self):
        self.records: list[tuple] = []

    def append(self, kind, obj, ts):
        self.records.append((kind, obj))


class FractionalCapWriter:
    """A proxy fake that keeps the FRACTIONAL wire ``count`` (unlike the whole-lot ``CapWriter`` in
    test_v33_executor.py, which ``int(...)``-truncates). It rejects a create whose count > cap
    (numerically, as the real proxy's ``max_contracts_per_order`` does) and echoes the requested count as
    the fill_count, so a test can assert both the chunk wire bodies and the aggregated hedge."""

    def __init__(self, cap):
        self.cap = Decimal(str(cap))
        self.posts: list[tuple] = []
        self.chunk_counts: list[Decimal] = []     # numeric count of every chunk create seen

    def _slot(self, o):
        coid = o.get("client_order_id")
        cnt = Decimal(str(o.get("count", "1")))   # NO int() truncation — keep the fraction
        self.chunk_counts.append(cnt)
        if cnt > self.cap:                        # the real proxy's numeric cap comparison
            return {"client_order_id": coid, "order_id": None, "fill_count": "0.00",
                    "error": "too_large"}
        return {"client_order_id": coid, "order_id": f"oid-{coid}",
                "fill_count": f"{cnt}", "average_fill_price": o.get("price", "0.5000")}

    def rest_post(self, path, body):
        self.posts.append((path, body))
        if "orders" in body:
            return WriteResponse(200, {"orders": [self._slot(o) for o in body["orders"]]}, True)
        return WriteResponse(200, {"order": self._slot(body)}, True)

    def rest_delete(self, path):
        return WriteResponse(200, {"reduced_by": "0.00"}, True)

    def rest_get(self, path, params=None):
        return {}


def _exec(writer, k=11):
    return V33LiveExecutor(writer, BUCKET_MAP, EXCH, FakeJournal(), CTS, 300, k_rungs=k,
                           clock=lambda: 0.0, sleep=lambda _s: None, batch_create=True)


def _wing_state(count):
    st = V33State.new(CLOSE, CTS, BUCKET_MAP, load_v33_params())
    legs = (WingLeg(S_SD, "yes", count, Decimal("0.55"), "v33-wy", batch=0),
            WingLeg(S_SU, "no", count, Decimal("0.90"), "v33-wn", batch=0))
    return dr(st, wing_legs=legs)


def test_wire_body_count_is_fractional_not_int_truncated():
    """The wing chunk wire body sends ``count`` as the 2dp fractional string ('1.44'), not the pre-fix
    ``int(1.44)`` = '1.00'."""
    w = FractionalCapWriter(cap=2)
    ex = _exec(w)
    ex.wing_cap = 2
    st = _wing_state(Decimal("1.44"))
    ex._take_wings(st, CTS - 400)
    # ceil(1.44/2) = 1 chunk per wing, each carrying the full fractional 1.44.
    counts = sorted(str(c) for c in w.chunk_counts)
    assert counts == ["1.44", "1.44"], counts
    # and the actual wire bodies carry the fractional string (the bug was to_v2_order's int() -> '1.00').
    for _path, body in w.posts:
        bodies = body["orders"] if "orders" in body else [body]
        assert all(o["count"] == "1.44" for o in bodies), bodies


def test_fractional_fill_is_fully_hedged_not_under_hedged():
    """A 1.44 fill hedges 1.44 lots on each wing (the aggregated Fill), never the int-truncated 1."""
    w = FractionalCapWriter(cap=2)
    ex = _exec(w)
    ex.wing_cap = 2
    st = _wing_state(Decimal("1.44"))
    events = ex._take_wings(st, CTS - 400)
    fills = [e for e in events if isinstance(e, Fill)]
    assert len(fills) == 2
    assert all(f.count == Decimal("1.44") for f in fills), [str(f.count) for f in fills]
    assert Decimal("1") not in {f.count for f in fills}          # the mirage the fix removes


def test_multi_chunk_last_chunk_is_the_fractional_remainder():
    """A 3.44 fill at cap 2 chunks to [2.00, 1.44] per wing (Σ = 3.44), the LAST chunk fractional, and
    aggregates back to the full 3.44 hedge."""
    w = FractionalCapWriter(cap=2)
    ex = _exec(w)
    ex.wing_cap = 2
    st = _wing_state(Decimal("3.44"))
    events = ex._take_wings(st, CTS - 400)
    # 2 wings x ceil(3.44/2)=2 chunks = 4 chunk creates; each wing's chunks sum to 3.44.
    assert len(w.chunk_counts) == 4
    assert sum(w.chunk_counts) == Decimal("2") * Decimal("3.44")   # both wings, full
    per_wing = sorted(str(c) for c in w.chunk_counts)
    assert per_wing == ["1.44", "1.44", "2.00", "2.00"], per_wing
    assert all(c <= Decimal("2") for c in w.chunk_counts)          # never over the cap
    fills = [e for e in events if isinstance(e, Fill)]
    assert all(f.count == Decimal("3.44") for f in fills), [str(f.count) for f in fills]
