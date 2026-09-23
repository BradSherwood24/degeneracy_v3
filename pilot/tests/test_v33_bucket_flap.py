"""V3.3 BUCKET-FLAP FIX (hotfix 2026-09-23, first live DRY window 07:00Z).

The spot-bucket-switch DEBOUNCE + HYSTERESIS (item 1) and the stale/missing-wing stand-down HOLD (item 2),
plus a FIXTURE replay of the real 07:00Z flap window (t-730..t-715) asserting the debounced core produces
ZERO bucket-change cancel-alls where the old (no-debounce) core flapped.

Shared harness helpers/constants are imported from ``test_v33_core``. No network / proxy / sealed read.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal

from service.v33 import (
    ActionKind,
    BookUpdate,
    ClockTick,
    Fill,
    OrderAck,
    OrderCancelled,
    V33State,
)
from tests.test_v33_core import (
    B_SD,
    B_SU,
    STK_SD,
    STK_SU,
    STK_SU2,
    T,
    _bring_up_ladder,
    _feed,
    _feed_all,
    _params,
    _sd,
    _state,
    _top,
)


# ===========================================================================
# item 1 — spot-bucket switch debounce + hysteresis
# ===========================================================================
def _flip_to(params, st, floor, t, *, tgt=("0.35", "0.39"), oth=("0.10", "0.12")):
    """Feed the two straddling bucket books (79600 / 79700) so ``floor`` is the higher-mid spot, keeping
    both buckets' strikes fresh. ``tgt`` / ``oth`` are (yes_bid, yes_ask) for the target / other bucket:
    the target's mid must exceed the other's (selection) while its yes_bid keeps the cap non-binding so a
    FULL 11-rung ladder rests. Returns (st, acts)."""
    other = 79700 if floor == 79600 else 79600
    tk = B_SD if floor == 79600 else B_SU
    tk_o = B_SD if other == 79600 else B_SU
    # feed the TARGET bucket (higher mid) BEFORE the other: at first placement the target is the only
    # bucket -> the ladder places on it (not the other); after that, max-mid selection is order-free.
    evs = [
        BookUpdate(STK_SD, _sd("0.76"), t),
        BookUpdate(STK_SU, _top("0.36", "0.37"), t),
        BookUpdate(STK_SU2, _top("0.20", "0.21"), t),
        BookUpdate(tk, _top(tgt[0], tgt[1]), t),
        BookUpdate(tk_o, _top(oth[0], oth[1]), t),
    ]
    return _feed_all(params, st, evs)


def _flap_params(**over):
    # relax freshness so a multi-second flap replay does not trip the stale-wing hold; keep the DEFAULT
    # bucket_switch_deb_ms (3000) / hysteresis (15) unless overridden.
    return _params(tol=Decimal("0.01"), deb_ms=0,
                   freshness_max_age_s=3600.0, bucket_freshness_max_age_s=3600.0, **over)


def _ack_all(params, st, t):
    for o in list(st.ladder):
        st, _ = _feed(params, st, OrderAck(o.client_order_id, f"OID-{o.client_order_id}", t))
    return st


def test_flap_five_flips_in_03s_zero_cancel_all():
    # (a) 5 flips within 0.3 s -> ZERO cancel-all; the ladder stays on its bucket.
    p = _flap_params()                                   # deb 3000, hyst 15
    st = _state(p)
    now = T - 600
    st, _ = _flip_to(p, st, 79600, now)                 # ladder resolves on 79600
    st = _ack_all(p, st, now)
    assert st.rest_bucket_Sd == 79600 and len(st.ladder) == 11
    all_acts = []
    for i in range(5):
        t = now + 0.5 + i * 0.05
        st, a = _flip_to(p, st, 79700, t); all_acts += a
        st, a = _flip_to(p, st, 79600, t + 0.02); all_acts += a
    assert not [a for a in all_acts if a.kind in (ActionKind.CANCEL_REST, ActionKind.PLACE_REST)]
    assert st.rest_bucket_Sd == 79600 and len(st.ladder) == 11


def test_flap_clean_move_persist_and_inside_commits_once():
    # (b) a clean move that persists >= 3 s AND >= 15$ inside commits exactly ONE cancel-all/place-all.
    p = _flap_params()
    st = _state(p)
    now = T - 600
    st, _ = _flip_to(p, st, 79600, now)
    st = _ack_all(p, st, now)
    ids = [o.order_id for o in st.ladder]
    all_acts = []
    for t in [now + 0.5, now + 1.5, now + 2.5, now + 3.6]:   # 79700 stays spot, deep inside, > 3 s
        st, a = _flip_to(p, st, 79700, t); all_acts += a
    cancels = [a for a in all_acts if a.kind == ActionKind.CANCEL_REST]
    assert len(cancels) == 11 and st.ladder == () and st.awaiting_replace   # committed exactly once
    for i, oid in enumerate(ids):
        st, acts = _feed(p, st, OrderCancelled(oid, now + 3.7 + i * 0.001))
    places = [a for a in acts if a.kind == ActionKind.PLACE_REST]
    assert len(places) == 11 and all(o.bucket_Sd == 79700 for o in st.ladder)
    assert st.rest_bucket_Sd == 79700


def test_flap_flip_back_at_29s_resets_timer():
    # (c) a flip back at ~2.9 s (< 3 s) RESETS the debounce; a fresh candidate must time out the full
    # window again, so no commit within 2.4 s of the reset.
    p = _flap_params()
    st = _state(p)
    now = T - 600
    st, _ = _flip_to(p, st, 79600, now)
    st = _ack_all(p, st, now)
    all_acts = []
    st, a = _flip_to(p, st, 79700, now + 0.5); all_acts += a     # candidate 79700 pending from +0.5
    st, a = _flip_to(p, st, 79700, now + 3.3); all_acts += a     # +2.8 s continuous (still < 3 s)
    st, a = _flip_to(p, st, 79600, now + 3.4); all_acts += a     # flip back at ~2.9 s -> RESET
    assert st.pending_switch_Sd is None and st.rest_bucket_Sd == 79600
    st, a = _flip_to(p, st, 79700, now + 3.6); all_acts += a     # candidate restarts here
    st, a = _flip_to(p, st, 79700, now + 6.0); all_acts += a     # +2.4 s since reset -> still no commit
    assert not [a for a in all_acts if a.kind == ActionKind.CANCEL_REST]
    assert len(st.ladder) == 11 and st.rest_bucket_Sd == 79600


def test_flap_hysteresis_blocks_boundary_oscillation():
    # a candidate that persists > 3 s but sits < 15$ inside (right at the boundary) does NOT commit.
    p = _flap_params()
    st = _state(p)
    now = T - 600
    st, _ = _flip_to(p, st, 79600, now)
    st = _ack_all(p, st, now)
    all_acts = []
    # 79700 barely wins selection (mid 0.31 vs 0.29) -> implied spot only ~2$ inside -> hysteresis blocks.
    for t in [now + 0.5, now + 2.0, now + 4.0]:
        st, a = _flip_to(p, st, 79700, t, tgt=("0.30", "0.32"), oth=("0.28", "0.30")); all_acts += a
    assert not [a for a in all_acts if a.kind == ActionKind.CANCEL_REST]  # hysteresis blocked the commit
    assert st.rest_bucket_Sd == 79600


# ===========================================================================
# item 2 — stale/missing-wing stand-down HOLD
# ===========================================================================
def test_hold_stale_wing_800ms_then_fresh_resumes():
    # (d) a stale wing for 800 ms then fresh -> no cancel; hold then resume, ladder intact.
    p = _params(tol=Decimal("0.01"), deb_ms=0, stand_down_hold_ms=1500)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    st, a = _feed(p, st, ClockTick(now + 2))            # strikes stale -> W None -> HOLD
    assert st.hold_since is not None and not [x for x in a if x.kind == ActionKind.CANCEL_REST]
    assert [x for x in a if x.kind == ActionKind.STAND_DOWN and x.reason == "stale_or_missing_wing_hold"]
    st, a = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 2.8))   # 800 ms into the hold
    st, a2 = _feed(p, st, BookUpdate(STK_SD, _sd("0.76"), now + 2.8))
    acts = a + a2
    assert st.hold_since is None and len(st.ladder) == 11
    assert [x for x in acts if x.kind == ActionKind.STAND_DOWN
            and x.reason == "stale_or_missing_wing_resume"]
    assert not [x for x in acts if x.kind == ActionKind.CANCEL_REST]


def test_hold_stale_wing_1600ms_cancels_once():
    # (e) a stale wing past the hold (1600 ms > 1500) -> cancel-all ONCE.
    p = _params(tol=Decimal("0.01"), deb_ms=0, stand_down_hold_ms=1500)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    st, a = _feed(p, st, ClockTick(now + 2))            # HOLD begins
    assert not [x for x in a if x.kind == ActionKind.CANCEL_REST]
    st, a = _feed(p, st, ClockTick(now + 3.6))          # 1600 ms later -> cancel-all
    assert len([x for x in a if x.kind == ActionKind.CANCEL_REST]) == 11
    assert [x for x in a if x.kind == ActionKind.STAND_DOWN
            and x.reason == "stale_or_missing_wing_cancel"]
    assert st.ladder == () and st.hold_since is None


def test_hold_rung_fill_during_hold_emits_wing_batch():
    # (f) a rung fill DURING a hold still books + spawns its wing batch (the hold is for the RESTS only);
    # the wings take once the strike book is fresh again.
    p = _params(tol=Decimal("0.01"), deb_ms=0, stand_down_hold_ms=1500)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    st, a = _feed(p, st, ClockTick(now + 2))            # HOLD (W None)
    assert st.hold_since is not None
    top = max(st.ladder, key=lambda o: o.price)
    st, a = _feed(p, st, Fill(top.order_id, top.client_order_id, Decimal(1), top.price, "no", now + 2.1))
    assert st.rungs_filled == 1                          # the fill booked
    assert st.wing_batches or st.coalesce_open is not None   # a wing batch was spawned
    assert st.hold_since is not None and len(st.ladder) == 10   # still holding the remaining RESTS
    st, a = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 2.3))   # fresh -> resume + take
    st, a2 = _feed(p, st, BookUpdate(STK_SD, _sd("0.76"), now + 2.3))
    assert [x for x in a + a2 if x.kind == ActionKind.TAKE_WINGS]


# ===========================================================================
# FIXTURE — the real 07:00Z DRY flap window (t-730..t-715)
# ===========================================================================
def _replay_flap_fixture(params):
    """Replay the real spot_Sd flip sequence (buckets 86400/86500) and return (fixture, cancel-all count)."""
    fx = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "fixtures", "v33", "bucket_flap_20260923T070000Z.json")
    fix = json.load(open(fx, encoding="utf-8"))
    bk = {"KXBTC-RANGE-B86400": (86400.0, 86499.99), "KXBTC-RANGE-B86500": (86500.0, 86599.99)}
    b = {86400: "KXBTC-RANGE-B86400", 86500: "KXBTC-RANGE-B86500"}
    stk = {86400: "KXBTCD-26SEP2000-T86399.99", 86500: "KXBTCD-26SEP2000-T86499.99",
           86600: "KXBTCD-26SEP2000-T86599.99"}
    st = V33State.new(fix["close_time"], T, bk, params)

    def strikes(t):
        return [BookUpdate(stk[86400], _top("0.40", "0.42"), t),
                BookUpdate(stk[86500], _top("0.40", "0.42"), t),
                BookUpdate(stk[86600], _top("0.40", "0.42"), t)]

    def buckets(spot, t):
        other = 86400 if spot == 86500 else 86500
        return [BookUpdate(b[other], _top("0.10", "0.12"), t),
                BookUpdate(b[spot], _top("0.90", "0.92"), t)]

    rows = fix["rows"]
    t0 = T - rows[0][0]
    st, _ = _feed_all(params, st, strikes(t0) + buckets(int(rows[0][1]), t0))
    st = _ack_all(params, st, t0)
    cancels = 0
    for t_minus, spot_Sd, _W, _cap in rows:
        t = T - t_minus
        st, a = _feed_all(params, st, strikes(t) + buckets(int(spot_Sd), t))
        cancels += len([x for x in a if x.kind == ActionKind.CANCEL_REST])
    return fix, cancels


def test_fixture_flap_debounced_zero_cancel_all_vs_old_many():
    # the debounced core produces ZERO bucket-change cancel-alls on the real flap; the old (no debounce,
    # no hysteresis) core produces >= 6 (the finding recorded 11 flips -> would_cancel bursts).
    new = _params(tol=Decimal("0.01"), deb_ms=0,
                  freshness_max_age_s=3600.0, bucket_freshness_max_age_s=3600.0)   # deb 3000, hyst 15
    old = _params(tol=Decimal("0.01"), deb_ms=0, bucket_switch_deb_ms=0, bucket_switch_hysteresis_usd=0,
                  freshness_max_age_s=3600.0, bucket_freshness_max_age_s=3600.0)   # pre-fix behaviour
    fix, new_cancels = _replay_flap_fixture(new)
    _fix2, old_cancels = _replay_flap_fixture(old)
    assert fix["flips"] >= 6
    assert new_cancels == 0, f"debounced core still flapped: {new_cancels} cancel-alls"
    assert old_cancels >= 6, f"control (no debounce) should flap: {old_cancels} cancel-alls"
