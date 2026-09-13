"""V3.2 Phase-3 wiring guards: P3-2 (drop off-width buckets) and P3-3 (monotone freshness clock).
FAKES ONLY — no network, no proxy, no holdout read."""

from __future__ import annotations

import os
from decimal import Decimal

from service.book import TopOfBook
from service.record_range import StreamJournal
from service.v32 import V32State, load_v32_params
import service.run_v32 as R

CLOSE = "2026-09-13T20:00:00Z"
CTS = 1789156800
B100_A = "KXBTC-26SEP1316-B68200"
B100_B = "KXBTC-26SEP1316-B68300"
B250 = "KXBTC-26SEP1316-B68000"


# ---------------------------------------------------------------------------
# P3-2: drop every bucket whose width != params.bucket_width
# ---------------------------------------------------------------------------
def test_filter_buckets_keeps_only_target_width():
    bm = {B100_A: (68200.0, 68299.99), B100_B: (68300.0, 68399.99), B250: (68000.0, 68249.99)}
    kept, dropped = R.filter_buckets_to_width(bm, 100)
    assert set(kept) == {B100_A, B100_B}
    assert dropped == [B250]


def test_filter_buckets_all_wrong_width_leaves_empty():
    bm = {B250: (68000.0, 68249.99), "x": (68250.0, 68499.99)}
    kept, dropped = R.filter_buckets_to_width(bm, 100)
    assert kept == {} and len(dropped) == 2  # empty -> main stands down


def test_bucket_width_of():
    assert R.bucket_width_of(68200.0, 68299.99) == 100
    assert R.bucket_width_of(68000.0, 68249.99) == 250
    assert R.bucket_width_of(None, 1) is None


# ---------------------------------------------------------------------------
# P3-3: server_now() is monotone across two interleaved connection clocks
# ---------------------------------------------------------------------------
def _driver(tmp_path):
    p = load_v32_params()
    j = StreamJournal(os.path.join(tmp_path, "w.jsonl"), flush_every=1)
    j.open()
    st = V32State.new(CLOSE, CTS, {B100_A: (68200.0, 68299.99)}, p, shakedown=True)
    ex = R.FrozenExecutor({B100_A: (68200.0, 68299.99)})
    return R.V32Driver(p, st, j, ex, clock=lambda: 0.0), j


def _top(mid):
    m = Decimal(mid)
    return TopOfBook(yes_bid=m, yes_bid_size=None, yes_ask=m, yes_ask_size=None,
                     no_bid=None, no_bid_size=None, no_ask=None, no_ask_size=None, suspect=False)


def test_server_now_does_not_regress_on_out_of_order_frame(tmp_path):
    drv, j = _driver(tmp_path)
    drv.on_book_update(B100_A, _top("0.40"), CTS - 600.0)   # bucket clock at T-600
    assert drv.server_now() == CTS - 600.0
    # a later-arriving frame from the SLOWER connection clock (T-601) must NOT step the tick clock back
    drv.on_book_update(B100_A, _top("0.41"), CTS - 601.0)
    assert drv.server_now() == CTS - 600.0                  # monotone: max held, never regressed
    # a genuinely newer frame advances it
    drv.on_book_update(B100_A, _top("0.42"), CTS - 590.0)
    assert drv.server_now() == CTS - 590.0
    j.close()
