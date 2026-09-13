"""GOLDEN PARITY for the V3.2 core, against ONE real forward hour (close 2026-09-04T20:00:00Z).

The fixture ``fixtures/v32/golden_20260904T200000Z.json`` was extracted read-only from
``historical-data/tob/20260904T200000Z.json.gz`` (hourly-strike ms top-of-book, strikes 79600/79700/
79800) and the ``1-hour-range`` candles + trades for that hour (all $100 buckets' minute candles for
spot selection, and the spot buckets' 1-s prints). 2026-09-04 is NOT in the 2026-08-20..29 holdout
nor the 2026-08-02..18 seal, so it is fair game (Fable's task, explicit). The test opens ONLY the
fixture — no historical-data read at test time.

What is asserted (the reference scripts' logic is PORTED inline — the scratchpad is never imported):

  * pf_ms_requote.py IDEAL (n solved from W at the print -1 s, completion at +1.5 s) reproduces the
    known E=0.10 detail line for this hour: bucket 79600, offer 0.57, print 0.58, lock +12.38c
    (+12c to the cent). This proves the fixture faithfully carries the sim's ideal.
  * pf_ms_requote2.py LAGGING executor (replace when |dn| >= tol and >= deb ms since last; new quote
    live +200 ms; fill = print > 1 - n_resting; completion +1.5 s) with tol=0.01, deb=0 gives
    bucket 79600, n=0.45, offer 0.55, print 0.56, lock +10.36c.
  * The CORE (decide_v32), replayed through the fixture with a requote2-equivalent executor harness
    (200 ms acks; a spot-bucket YES print above 1 - n_live synthesizes the rest fill), produces the
    SAME live fill (n=0.45, lock +10.36c) as the ported lagging reference.
  * The CORE's no-lag shadow at E=0.10 equals the lagging fill here (0.45/0.55/0.56/+10.36c); and,
    as a cross-check of the documented -1 s convention, the CORE's no-lag shadow at E=0.12 equals the
    ideal E=0.10 line (0.43/0.57/0.58/+12.38c) — the wings moved W by ~$0.02 in the final second, so
    the ideal's -1 s n-solve is equivalent to raising E by ~0.02 at no lag. A pure state machine
    cannot look back 1 s, so the core's E=0.10 shadow is 0.45/0.55, not the ideal's 0.43/0.57
    (see the Phase-1 build report CONFESSIONS).
"""

from __future__ import annotations

import heapq
import json
import os
from dataclasses import replace as dreplace
from decimal import ROUND_HALF_EVEN, Decimal

import pytest

from service._simlaw import fee
from service.book import TopOfBook
from service.v32 import (
    ActionKind,
    BookUpdate,
    Fill,
    OrderAck,
    OrderCancelled,
    Trade,
    V32State,
    classify_ticker,
    decide_v32,
    load_v32_params,
    solve_n,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(_HERE, "fixtures", "v32", "golden_20260904T200000Z.json")
_CENT = Decimal("0.01")
_ONE = Decimal(1)
_TWO = Decimal(2)
_LAT_MS = 200


# ---------------------------------------------------------------------------
# Fixture loading + reconstruction (prices in integer cents; ts as delta from close_epoch)
# ---------------------------------------------------------------------------
def _load_fixture():
    if not os.path.exists(FIXTURE):
        pytest.skip("golden fixture absent")
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def _D(cents: int) -> Decimal:
    return Decimal(cents) / Decimal(100)


def _reconstruct(fix):
    cts = fix["close_epoch"]
    # bucket candles: floor -> {ts_s -> (bid, ask)}
    quotes: dict[int, dict[int, tuple[Decimal, Decimal]]] = {}
    for fl, rows in fix["candles"].items():
        quotes[int(fl)] = {cts + dt: (_D(bc), _D(ac)) for dt, bc, ac in rows}
    # spot-bucket prints: floor -> sorted [(ts_s, yes_price, side, count)]
    prints: dict[int, list] = {}
    for fl, rows in fix["prints"].items():
        pl = [(cts + dt, _D(pc), side, Decimal(cnt)) for dt, pc, side, cnt in rows]
        pl.sort(key=lambda x: (x[0], x[1]))
        prints[int(fl)] = pl
    # strike ms books: floor -> (rows[(ts_ms, yes_bid, yes_ask)], times[ts_ms])
    strikes: dict[int, tuple[list, list]] = {}
    for fl, s in fix["strikes"].items():
        rows = [(cts * 1000 + dt, _D(bc), _D(ac)) for dt, bc, ac in s["tob"]]
        strikes[int(fl)] = (rows, [r[0] for r in rows])
    return cts, quotes, prints, strikes


def _at(rows, times, t_ms):
    import bisect

    i = bisect.bisect_right(times, t_ms) - 1
    return rows[i] if i >= 0 else None


def _W_ms(strikes, Sd, t_ms):
    if Sd not in strikes or Sd + 100 not in strikes:
        return None
    a = _at(*strikes[Sd], t_ms)
    b = _at(*strikes[Sd + 100], t_ms)
    if not a or not b:
        return None
    ya = a[2]
    nu = _ONE - b[1]
    if not (Decimal(0) < ya < _ONE) or not (Decimal(0) < nu < _ONE):
        return None
    return ya + fee(ya) + nu + fee(nu)


def _cap(qB):
    return ((_ONE - qB[0]) - _CENT).quantize(_CENT)


# ---------------------------------------------------------------------------
# Ported reference #1: pf_ms_requote.py IDEAL (one fill per hour)
# ---------------------------------------------------------------------------
def _ref_ideal(cts, quotes, prints, strikes, E: Decimal):
    grid = sorted({ts for q in quotes.values() for ts in q if cts - 900 <= ts <= cts - 300})
    for ts in grid:
        s = sm = qB = None
        for lvl in sorted(quotes):
            q = quotes[lvl]
            if ts in q:
                mid = (q[ts][0] + q[ts][1]) / _TWO
                if sm is None or mid > sm:
                    sm, s, qB = mid, lvl, q[ts]
        if s is None or not (Decimal(0) < qB[1] <= _ONE and Decimal(0) <= qB[0] <= qB[1]):
            continue
        cap = _cap(qB)
        for pts, yp, sd, cnt in prints.get(s, []):
            if not (ts < pts <= ts + 60) or sd != "yes":
                continue
            Wl = _W_ms(strikes, s, pts * 1000 - 1000)
            if Wl is None:
                continue
            n = solve_n(_TWO - E - Wl, cap)
            if n is None or yp <= (_ONE - n):
                continue
            W2 = _W_ms(strikes, s, pts * 1000 + 1500)
            if W2 is None:
                continue
            return dict(s=s, n=n, offer=_ONE - n, print=yp, lock=_TWO - (n + fee(n)) - W2)
    return None


# ---------------------------------------------------------------------------
# Ported reference #2: pf_ms_requote2.py LAGGING executor (one fill per hour)
# ---------------------------------------------------------------------------
def _ref_lagging(cts, quotes, prints, strikes, E: Decimal, TOL: Decimal, DEB: int):
    grid = sorted({ts for q in quotes.values() for ts in q if cts - 900 <= ts <= cts - 300})
    minutes = []
    for ts in grid:
        s = sm = qB = None
        for lvl in sorted(quotes):
            q = quotes[lvl]
            if ts in q:
                mid = (q[ts][0] + q[ts][1]) / _TWO
                if sm is None or mid > sm:
                    sm, s, qB = mid, lvl, q[ts]
        if s is None or not (Decimal(0) < qB[1] <= _ONE and Decimal(0) <= qB[0] <= qB[1]):
            continue
        if s not in strikes or s + 100 not in strikes:
            continue
        cap = _cap(qB)
        ticks = sorted(
            {r[0] for r in strikes[s][0] if ts * 1000 <= r[0] < (ts + 60) * 1000}
            | {r[0] for r in strikes[s + 100][0] if ts * 1000 <= r[0] < (ts + 60) * 1000}
            | {ts * 1000}
        )
        pr = [(pts, yp, cnt) for pts, yp, sd, cnt in prints.get(s, []) if ts < pts <= ts + 60 and sd == "yes"]
        minutes.append((ts, s, cap, ticks, pr))

    n_rest = live_at = pending = None
    last_rep = -(10 ** 18)
    prev_s = None
    for ts, s, cap, ticks, pr in minutes:
        forced = s != prev_s
        if forced:
            n_rest = pending = None
            prev_s = s
        events = [(t, "tick", None) for t in ticks] + [(p[0] * 1000, "print", p) for p in pr]
        events.sort(key=lambda e: e[0])
        for t, kind, payload in events:
            if pending is not None and t >= live_at:
                n_rest, pending = pending, None
            if kind == "tick":
                if pending is not None:
                    continue
                W = _W_ms(strikes, s, t)
                nd = solve_n(_TWO - E - W, cap) if W is not None else None
                if nd is None:
                    continue
                if forced or n_rest is None or (abs(nd - n_rest) >= TOL and t - last_rep >= DEB):
                    if n_rest is not None and nd == n_rest:
                        continue
                    pending, live_at, last_rep, forced = nd, t + _LAT_MS, t, False
            else:
                pts, yp, cnt = payload
                if n_rest is None or not (yp > (_ONE - n_rest)):
                    continue
                W2 = _W_ms(strikes, s, t + 1500)
                if W2 is None:
                    continue
                return dict(s=s, n=n_rest, offer=_ONE - n_rest, print=yp, lock=_TWO - (n_rest + fee(n_rest)) - W2)
    return None


# ---------------------------------------------------------------------------
# Core replay harness: requote2-equivalent executor (200 ms acks; synth rest fill on a crossing print)
# ---------------------------------------------------------------------------
def _top(bc: int, ac: int) -> TopOfBook:
    yb, ya = _D(bc), _D(ac)
    return TopOfBook(
        yes_bid=yb, yes_bid_size=None, yes_ask=ya, yes_ask_size=None,
        no_bid=_ONE - ya, no_bid_size=None, no_ask=_ONE - yb, no_ask_size=None, suspect=False,
    )


def _run_core(fix, E: Decimal, tol: Decimal, deb_ms: int):
    cts = fix["close_epoch"]
    bucket_map = {b["ticker"]: (float(b["floor"]), float(b["cap"])) for b in fix["buckets"].values()}
    params = dreplace(load_v32_params(), E=E, tol=tol, deb_ms=deb_ms)
    st = V32State.new(fix["close_time"], cts, bucket_map, params)

    heap: list = []
    seq = 0
    for fl, s in fix["strikes"].items():
        tk = s["ticker"]
        for dt_ms, bc, ac in s["tob"]:
            heap.append((cts + dt_ms / 1000.0, 1, seq, "book", (tk, bc, ac)))
            seq += 1
    for fl, rows in fix["candles"].items():
        tk = fix["buckets"][fl]["ticker"]
        for dt, bc, ac in rows:
            heap.append((cts + dt, 1, seq, "book", (tk, bc, ac)))
            seq += 1
    for fl, rows in fix["prints"].items():
        tk = fix["buckets"][fl]["ticker"]
        for dt, pc, side, cnt in rows:
            heap.append((cts + dt, 2, seq, "trade", (tk, pc, side, cnt)))
            seq += 1
    heapq.heapify(heap)

    dyn = [10 ** 9]
    ack_oid = [0]
    rest_fill = [None]
    take = [None]

    def push(ts, prio, kind, payload):
        dyn[0] += 1
        heapq.heappush(heap, (ts, prio, dyn[0], kind, payload))

    def feed(ev):
        nonlocal st
        st, acts = decide_v32(params, st, ev)
        for a in acts:
            if a.kind == ActionKind.PLACE_REST:
                ack_oid[0] += 1
                push(ev.server_ts + _LAT_MS / 1000.0, 0, "ack", (a.client_order_id, f"OID{ack_oid[0]}"))
            if a.kind == ActionKind.CANCEL_REST:
                # SEQUENTIAL replace (R-OVERLAP): confirm the cancel after one RTT so the core can then
                # place the new quote. filled 0 (no fill during this cancel in the fixture).
                push(ev.server_ts + _LAT_MS / 1000.0, 0, "cancelled", (a.order_id,))
            if a.kind == ActionKind.TAKE_WINGS:
                take[0] = a

    while heap:
        ts, prio, _s, kind, payload = heapq.heappop(heap)
        if kind == "book":
            tk, bc, ac = payload
            feed(BookUpdate(tk, _top(bc, ac), ts))
        elif kind == "ack":
            coid, oid = payload
            feed(OrderAck(coid, oid, ts))
        elif kind == "cancelled":
            (oid,) = payload
            feed(OrderCancelled(oid, ts, Decimal(0)))
        elif kind == "trade":
            tk, pc, side, cnt = payload
            feed(Trade(tk, _D(pc), side, Decimal(cnt), ts))
            rl = st.rest_live
            if rest_fill[0] is None and rl is not None and side == "yes" and _D(pc) > (_ONE - rl.price):
                cl = classify_ticker(tk, bucket_map)
                if cl and cl[0] == "bucket" and cl[1] == st.spot_Sd:
                    rest_fill[0] = dict(s=st.spot_Sd, n=rl.price, offer=_ONE - rl.price, print=_D(pc))
                    feed(Fill(rl.order_id, rl.client_order_id, Decimal(cnt), rl.price, "no", ts))
    return st, rest_fill[0], take[0]


# ===========================================================================
# The golden assertions
# ===========================================================================
def test_fixture_is_not_holdout():
    fix = _load_fixture()
    assert fix["close_time"].startswith("2026-09-04")  # not in 2026-08-20..29 holdout / seal


def test_ideal_reproduces_pf_ms_requote_e10():
    fix = _load_fixture()
    cts, quotes, prints, strikes = _reconstruct(fix)
    r = _ref_ideal(cts, quotes, prints, strikes, Decimal("0.10"))
    assert r is not None
    assert r["s"] == 79600
    assert r["offer"] == Decimal("0.57")
    assert r["print"] == Decimal("0.58")
    assert r["n"] == Decimal("0.43")
    assert r["lock"] == Decimal("0.1238")
    assert r["lock"].quantize(_CENT, rounding=ROUND_HALF_EVEN) == Decimal("0.12")


def test_lagging_reproduces_pf_ms_requote2_tol01_deb0():
    fix = _load_fixture()
    cts, quotes, prints, strikes = _reconstruct(fix)
    r = _ref_lagging(cts, quotes, prints, strikes, Decimal("0.10"), Decimal("0.01"), 0)
    assert r is not None
    assert r["s"] == 79600
    assert r["offer"] == Decimal("0.55")
    assert r["print"] == Decimal("0.56")
    assert r["n"] == Decimal("0.45")
    assert r["lock"] == Decimal("0.1036")


def test_core_live_fill_matches_lagging_reference():
    fix = _load_fixture()
    cts, quotes, prints, strikes = _reconstruct(fix)
    ref = _ref_lagging(cts, quotes, prints, strikes, Decimal("0.10"), Decimal("0.01"), 0)
    st, core_fill, take = _run_core(fix, Decimal("0.10"), Decimal("0.01"), 0)
    assert core_fill is not None, "core produced no rest fill"
    assert core_fill["s"] == ref["s"]
    assert core_fill["n"] == ref["n"] == Decimal("0.45")
    assert core_fill["offer"] == ref["offer"] == Decimal("0.55")
    assert core_fill["print"] == ref["print"] == Decimal("0.56")
    # the core takes the wings; its completion lock equals the lagging reference lock (to the cent).
    assert take is not None and take.kind == ActionKind.TAKE_WINGS
    assert take.lock == ref["lock"] == Decimal("0.1036")


def test_core_shadow_e10_is_nolag_and_e12_reproduces_ideal():
    fix = _load_fixture()
    cts, quotes, prints, strikes = _reconstruct(fix)
    ideal = _ref_ideal(cts, quotes, prints, strikes, Decimal("0.10"))
    lagging = _ref_lagging(cts, quotes, prints, strikes, Decimal("0.10"), Decimal("0.01"), 0)
    st, _fill, _take = _run_core(fix, Decimal("0.10"), Decimal("0.01"), 0)

    # E=0.10 no-lag shadow: matches the lagging fill here (last-book W = 1.4290 -> n=0.45).
    s10 = st.shadows["0.10"].fill
    assert s10 is not None
    assert (s10.n, s10.offer, s10.print_price, s10.lock) == (
        lagging["n"], lagging["offer"], lagging["print"], lagging["lock"]
    )
    assert (s10.n, s10.offer, s10.lock) == (Decimal("0.45"), Decimal("0.55"), Decimal("0.1036"))

    # E=0.12 no-lag shadow reproduces the pf_ms_requote.py E=0.10 IDEAL line (the -1 s ~= +0.02 E
    # equivalence): 0.43 / 0.57 / 0.58 / +12.38c.
    s12 = st.shadows["0.12"].fill
    assert s12 is not None
    assert (s12.n, s12.offer, s12.print_price, s12.lock) == (
        ideal["n"], ideal["offer"], ideal["print"], ideal["lock"]
    )
    assert (s12.n, s12.offer, s12.lock) == (Decimal("0.43"), Decimal("0.57"), Decimal("0.1238"))
