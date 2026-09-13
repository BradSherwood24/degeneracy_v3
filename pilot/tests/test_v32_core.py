"""V3.2 pure-core unit tests: the frozen-policy sha loader, the n solve vs brute force, the cap
never-cross, the freshness cancel, the window cutoffs, the requote gate (tol / debounce / in-flight
hold / bucket change), fill -> TAKE_WINGS with the lock, the lock floor -> defer, one set per hour,
the shakedown WOULD_* downgrade, the replace-rate alarm, the shadow fill + completion, and the exact
census fee's agreement with the scratch KALSHI_FEE_EXACT lambda.

No network, no disk beyond the shipped policy + a tmp mutated copy. 2026-08-20..29 (holdout) and the
2026-08-02..18 seal are never touched.
"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from decimal import Decimal

import pytest

from service._simlaw import fee
from service.book import TopOfBook
from service.box import canonical_sha256 as box_canonical_sha256
from service.v32 import (
    BUY_NO,
    BUY_YES,
    ActionKind,
    BookUpdate,
    ClockTick,
    Fill,
    OrderAck,
    OrderCancelled,
    Trade,
    V32Params,
    V32ParamsShaMismatch,
    V32State,
    canonical_sha256,
    decide_v32,
    load_v32_params,
    parse_bucket_ticker,
    parse_strike_ticker,
    solve_n,
)
from service.v32.params import DEFAULT_V32_PARAMS_PATH

# --- the scratch float lambda (rangelab.KALSHI_FEE_EXACT), replicated for the agreement test ---
_KFE = lambda p, c=1: (math.ceil(0.07 * c * p * (1 - p) * 10000) / 10000) if 0 < p < 1 else 0.0

CLOSE = "2026-09-04T20:00:00Z"
T = 1_000_000  # a synthetic close epoch (seconds)

# synthetic tickers: the core takes bucket floor/cap from the discovery map, never parses bucket
# syntax; strikes are parsed by the round(strike)+0.01 pattern.
BK = {"KXBTC-RANGE-B79600": (79600.0, 79699.99), "KXBTC-RANGE-B79700": (79700.0, 79799.99)}
STK_SD = "KXBTCD-26SEP0416-T79599.99"   # -> 79600
STK_SU = "KXBTCD-26SEP0416-T79699.99"   # -> 79700
STK_SU2 = "KXBTCD-26SEP0416-T79799.99"  # -> 79800
B_SD = "KXBTC-RANGE-B79600"
B_SU = "KXBTC-RANGE-B79700"


def _top(bid: str, ask: str, *, suspect: bool = False) -> TopOfBook:
    yb, ya = Decimal(bid), Decimal(ask)
    return TopOfBook(
        yes_bid=yb, yes_bid_size=Decimal(100), yes_ask=ya, yes_ask_size=Decimal(100),
        no_bid=Decimal(1) - ya, no_bid_size=Decimal(100), no_ask=Decimal(1) - yb,
        no_ask_size=Decimal(100), suspect=suspect,
    )


def _params(**over) -> V32Params:
    p = load_v32_params()
    return replace(p, **over) if over else p


def _state(params: V32Params, *, shakedown: bool = False) -> V32State:
    return V32State.new(CLOSE, T, BK, params, shakedown=shakedown)


def _feed(params, st, event):
    return decide_v32(params, st, event)


def _feed_all(params, st, events):
    acts: list = []
    for e in events:
        st, a = _feed(params, st, e)
        acts += a
    return st, acts


# quotes that yield W=1.4290, cap=0.64, desired_n=0.45 at E=0.10 (the golden-hour numbers).
def _fresh_books(now: float):
    return [
        BookUpdate(B_SD, _top("0.35", "0.36"), now),
        BookUpdate(STK_SU, _top("0.36", "0.37"), now),
        BookUpdate(STK_SD, _top("0.75", "0.76"), now),
    ]


def _bring_up_live_rest(params, st, now):
    """Feed fresh books to emit a PLACE, then ack it -> a live rest at n=0.45. Returns (st, coid)."""
    st, acts = _feed_all(params, st, _fresh_books(now))
    place = [a for a in acts if a.kind in (ActionKind.PLACE_REST, ActionKind.WOULD_PLACE_REST)]
    assert place, "expected a PLACE_REST from fresh in-window books"
    coid = place[-1].client_order_id
    st, _ = _feed(params, st, OrderAck(coid, "OID1", now))
    assert st.rest_live is not None and st.rest_live.price == Decimal("0.45")
    return st, coid


# ===========================================================================
# Policy loader
# ===========================================================================
def test_params_load_and_sha_pin(tmp_path):
    p = load_v32_params()
    assert p.E == Decimal("0.10")
    assert p.tol == Decimal("0.02")
    assert p.deb_ms == 2000
    assert p.shadow_Es == (Decimal("0.08"), Decimal("0.10"), Decimal("0.12"))
    assert p.bucket_width == 100
    # the loaded file self-verifies against the pinned frozen sha
    from service.v32.params import FROZEN_V32_PARAMS_SHA256

    assert p.sha256 == FROZEN_V32_PARAMS_SHA256


def test_params_sha_mismatch_refused(tmp_path):
    raw = json.load(open(DEFAULT_V32_PARAMS_PATH, encoding="utf-8"))
    raw["E"] = "0.11"  # mutate a value -> canonical sha changes
    bad = tmp_path / "v32_params.json"
    bad.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(V32ParamsShaMismatch):
        load_v32_params(str(bad))


def test_params_missing_key_fails_closed(tmp_path):
    raw = json.load(open(DEFAULT_V32_PARAMS_PATH, encoding="utf-8"))
    del raw["E"]
    bad = tmp_path / "v32_params.json"
    bad.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(KeyError):
        load_v32_params(str(bad), expected_sha=None)


def test_canonical_sha_matches_box():
    sample = {"z": 1, "a": "0.10", "m": [1, "x"], "n": 1.0}
    assert canonical_sha256(sample) == box_canonical_sha256(sample)


# ===========================================================================
# Ticker classifiers
# ===========================================================================
def test_parse_strike_ticker():
    assert parse_strike_ticker("KXBTCD-26AUG3006-T77799.99") == 77800
    assert parse_strike_ticker("KXBTCD-26SEP0416-T79599.99") == 79600
    assert parse_strike_ticker("KXBTC-RANGE-B79600") is None  # not a strike
    assert parse_strike_ticker("garbage") is None


def test_parse_bucket_ticker():
    assert parse_bucket_ticker(B_SD, BK) == 79600
    assert parse_bucket_ticker("unknown", BK) is None


# ===========================================================================
# solve_n
# ===========================================================================
def test_solve_n_matches_brute_force():
    def brute(budget, cap):
        best = None
        n = Decimal("0.01")
        while n <= cap:
            if n + fee(n) <= budget:
                best = n
            n += Decimal("0.01")
        return best

    for b_i in range(-20, 200):
        budget = Decimal(b_i) / Decimal(100)
        for c_i in range(1, 100):
            cap = Decimal(c_i) / Decimal(100)
            assert solve_n(budget, cap) == brute(budget, cap), (budget, cap)


def test_solve_n_cap_never_crossed():
    for c_i in range(1, 100):
        cap = Decimal(c_i) / Decimal(100)
        n = solve_n(Decimal("5.0"), cap)  # unbounded budget -> cap binds
        assert n is not None and n <= cap


def test_solve_n_none_below_min_budget():
    assert solve_n(Decimal("0.005"), Decimal("0.64")) is None
    assert solve_n(Decimal("0.5"), Decimal("0.00")) is None


# ===========================================================================
# fee agreement with the scratch KALSHI_FEE_EXACT lambda
# ===========================================================================
def test_fee_agrees_with_scratch_lambda_within_pinned_rounding():
    # exact everywhere except six whole cents where the scratch FLOAT lambda over-charges by exactly
    # one $0.0001 tick (a float-rounding artifact; the Decimal census fee is the audited law).
    known_float_artifacts = {Decimal("0.10"), Decimal("0.20"), Decimal("0.40"),
                             Decimal("0.50"), Decimal("0.60"), Decimal("0.70")}
    for i in range(1, 100):
        p = Decimal(i) / Decimal(100)
        a = fee(p)
        b = Decimal(str(_KFE(float(p))))
        assert abs(a - b) <= Decimal("0.0001")
        if p not in known_float_artifacts:
            assert a == b, p
        else:
            assert b - a == Decimal("0.0001"), p


# ===========================================================================
# Spot selection
# ===========================================================================
def test_spot_bucket_is_highest_yes_mid():
    p = _params()
    st = _state(p)
    now = T - 600
    st, _ = _feed(p, st, BookUpdate(B_SD, _top("0.35", "0.36"), now))     # mid 0.355
    st, _ = _feed(p, st, BookUpdate(B_SU, _top("0.40", "0.42"), now))     # mid 0.41 (higher)
    assert st.spot_Sd == 79700 and st.spot_Su == 79800


def test_spot_ignores_invalid_two_sided_book():
    p = _params()
    st = _state(p)
    now = T - 600
    st, _ = _feed(p, st, BookUpdate(B_SD, _top("0.35", "0.36"), now))
    st, _ = _feed(p, st, BookUpdate(B_SU, _top("0.99", "0.00"), now))  # ask < bid -> invalid
    assert st.spot_Sd == 79600


# ===========================================================================
# Place / ack lifecycle + shakedown WOULD_*
# ===========================================================================
def test_place_then_ack_makes_live_rest():
    p = _params()
    st = _state(p)
    now = T - 600
    st, acts = _feed_all(p, st, _fresh_books(now))
    place = [a for a in acts if a.kind == ActionKind.PLACE_REST]
    assert len(place) == 1
    a = place[0]
    assert a.side == BUY_NO and a.action == "buy" and a.price == Decimal("0.45")
    assert a.ticker == B_SD and a.count == p.contracts
    assert a.expiration_epoch == T - p.quote_end_s
    assert st.rest_pending is not None and st.rest_live is None
    st, _ = _feed(p, st, OrderAck(a.client_order_id, "OID1", now))
    assert st.rest_live is not None and st.rest_live.live and st.rest_live.price == Decimal("0.45")
    assert st.rest_pending is None


def test_shakedown_emits_would_place_only():
    p = _params()
    st = _state(p, shakedown=True)
    now = T - 600
    st, acts = _feed_all(p, st, _fresh_books(now))
    kinds = {a.kind for a in acts}
    assert ActionKind.WOULD_PLACE_REST in kinds
    assert ActionKind.PLACE_REST not in kinds


# ===========================================================================
# Requote gate: tol / debounce / in-flight hold
# ===========================================================================
def test_requote_below_tol_does_not_replace():
    p = _params(tol=Decimal("0.05"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_live_rest(p, st, now)
    # nudge the wing so desired n moves by only ~1c (< tol 5c): no replace.
    st, acts = _feed(p, st, BookUpdate(STK_SD, _top("0.75", "0.77"), now + 1))
    assert not [a for a in acts if a.kind in (ActionKind.PLACE_REST, ActionKind.CANCEL_REST)]


def test_requote_above_tol_and_debounce_replaces():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_live_rest(p, st, now)
    # move the wing a lot -> desired n shifts >= tol -> CANCEL + PLACE (old stays live until ack).
    st, acts = _feed(p, st, BookUpdate(STK_SD, _top("0.60", "0.61"), now + 1))
    kinds = [a.kind for a in acts]
    assert ActionKind.CANCEL_REST in kinds and ActionKind.PLACE_REST in kinds
    assert st.rest_live is not None            # old rest kept fillable until the new acks
    assert st.rest_pending is not None


def test_requote_debounce_blocks_until_elapsed():
    p = _params(tol=Decimal("0.01"), deb_ms=2000)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_live_rest(p, st, now)
    # same big move but only +0.5s since the place -> debounce blocks the replace.
    st, acts = _feed(p, st, BookUpdate(STK_SD, _top("0.60", "0.61"), now + 0.5))
    assert not [a for a in acts if a.kind == ActionKind.PLACE_REST]
    # +2.1s -> debounce elapsed -> replace fires (refresh Su so its wing book stays fresh).
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 2.1))
    st, acts = _feed(p, st, BookUpdate(STK_SD, _top("0.60", "0.61"), now + 2.1))
    assert [a for a in acts if a.kind == ActionKind.PLACE_REST]


def test_requote_holds_while_place_pending():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, acts = _feed_all(p, st, _fresh_books(now))
    assert st.rest_pending is not None and st.rest_live is None  # awaiting first ack
    # a big move while the first create is still pending -> HOLD, no second create.
    st, acts = _feed(p, st, BookUpdate(STK_SD, _top("0.60", "0.61"), now + 1))
    assert not [a for a in acts if a.kind == ActionKind.PLACE_REST]


def test_bucket_change_cancels_then_places_after_confirm():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, coid = _bring_up_live_rest(p, st, now)
    oid = st.rest_live.order_id
    # bring up the NEW bucket's other wing (79800) and refresh 79700 at its SAME price (so the old
    # bucket does not requote), then flip spot to 79700.
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 0.9))
    st, _ = _feed(p, st, BookUpdate(STK_SU2, _top("0.20", "0.21"), now + 0.9))
    st, acts = _feed(p, st, BookUpdate(B_SU, _top("0.55", "0.57"), now + 0.9))
    # the higher bucket becomes spot -> cancel the old rest, do NOT place yet.
    assert [a for a in acts if a.kind == ActionKind.CANCEL_REST]
    assert not [a for a in acts if a.kind == ActionKind.PLACE_REST]
    assert st.rest_live is None and st.awaiting_replace
    # confirm the cancel, then a fresh book on the new bucket's strikes -> place on 79700.
    st, _ = _feed(p, st, OrderCancelled(oid, now + 0.95))
    st, acts = _feed_all(p, st, [
        BookUpdate(STK_SU2, _top("0.20", "0.21"), now + 1.0),
        BookUpdate(STK_SU, _top("0.55", "0.56"), now + 1.0),
    ])
    assert [a for a in acts if a.kind == ActionKind.PLACE_REST]
    assert st.rest_pending is not None and st.rest_pending.bucket_Sd == 79700


# ===========================================================================
# Freshness / window cutoffs / n_min
# ===========================================================================
def test_stale_wing_cancels_rest():
    p = _params()
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_live_rest(p, st, now)
    # a bucket tick 2s later; the strike books are now > freshness (1s) old -> W stale -> cancel.
    st, acts = _feed(p, st, BookUpdate(B_SD, _top("0.35", "0.36"), now + 2.0))
    assert [a for a in acts if a.kind == ActionKind.CANCEL_REST]
    sd = [a for a in acts if a.kind == ActionKind.STAND_DOWN]
    assert sd and sd[0].reason == "stale_or_missing_wing"
    assert st.rest_live is None


def test_warmup_before_window_seeds_only():
    p = _params()
    st = _state(p)
    now = T - 1000  # t_to_close 1000 > quote_start_s 900 -> warmup
    st, acts = _feed_all(p, st, _fresh_books(now))
    assert not acts  # books folded, nothing placed, no stand-down spam
    assert st.strike_tops and st.bucket_tops


def test_past_quote_end_cancels_and_stands_down():
    p = _params()
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_live_rest(p, st, now)
    later = T - 200  # t_to_close 200 < quote_end_s 300 -> past window
    st, acts = _feed(p, st, ClockTick(later))
    assert [a for a in acts if a.kind == ActionKind.CANCEL_REST]
    sd = [a for a in acts if a.kind == ActionKind.STAND_DOWN]
    assert sd and sd[0].reason == "past_quote_end"


def test_n_below_min_stands_down():
    p = _params(n_min=Decimal("0.50"))  # desired n 0.45 < 0.50 -> stand down
    st = _state(p)
    now = T - 600
    st, acts = _feed_all(p, st, _fresh_books(now))
    assert not [a for a in acts if a.kind == ActionKind.PLACE_REST]
    sd = [a for a in acts if a.kind == ActionKind.STAND_DOWN]
    assert sd and sd[-1].reason == "n_below_min"


# ===========================================================================
# Fill -> TAKE_WINGS + lock; lock floor -> defer; one set per hour
# ===========================================================================
def test_fill_takes_wings_with_lock():
    p = _params()
    st = _state(p)
    now = T - 600
    st, coid = _bring_up_live_rest(p, st, now)
    # our resting NO at n=0.45 fills for 1 -> immediately take both wings at the fresh asks.
    st, acts = _feed(p, st, Fill(st.rest_live.order_id, coid, Decimal(1), Decimal("0.45"), "no", now + 0.1))
    tw = [a for a in acts if a.kind == ActionKind.TAKE_WINGS]
    assert len(tw) == 1
    a = tw[0]
    assert a.count == 1 and len(a.legs) == 2
    yes_leg, no_leg = a.legs
    assert yes_leg.ticker == STK_SD and yes_leg.side == BUY_YES
    assert no_leg.ticker == STK_SU and no_leg.side == BUY_NO
    # limits = observed ask + wing_margin (0.02): yes 0.76+0.02, no (1-0.36)+0.02
    assert yes_leg.limit == Decimal("0.78")
    assert no_leg.limit == Decimal("0.66")
    # lock = 2 - (0.45+fee(0.45)) - (0.76+fee(0.76)+0.64+fee(0.64)) = +0.1036
    assert a.lock == Decimal("0.1036")
    assert st.rest_fill is not None and st.wing_taken


def test_lock_floor_defers_wings_then_takes_on_improvement():
    # a very high lock floor makes the initial take uneconomic -> defer; when the asks improve, take.
    p = _params(lock_floor=Decimal("0.20"))
    st = _state(p)
    now = T - 600
    st, coid = _bring_up_live_rest(p, st, now)
    st, acts = _feed(p, st, Fill(st.rest_live.order_id, coid, Decimal(1), Decimal("0.45"), "no", now + 0.1))
    assert not [a for a in acts if a.kind == ActionKind.TAKE_WINGS]  # lock 0.1036 < floor 0.20
    assert st.wings_needed and not st.wing_taken
    # asks improve so lock >= 0.20: cheaper wings (yes 0.70, no-side via su yes_bid 0.42 -> no_ask 0.58)
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.42", "0.43"), now + 0.2))
    st, acts = _feed(p, st, BookUpdate(STK_SD, _top("0.69", "0.70"), now + 0.2))
    tw = [a for a in acts if a.kind == ActionKind.TAKE_WINGS]
    assert tw and tw[0].lock >= Decimal("0.20")


def test_one_set_per_hour_no_requote_after_fill():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, coid = _bring_up_live_rest(p, st, now)
    st, _ = _feed(p, st, Fill(st.rest_live.order_id, coid, Decimal(1), Decimal("0.45"), "no", now + 0.1))
    # further fresh books must NOT place a new rest (we've entered for the hour).
    st, acts = _feed_all(p, st, _fresh_books(now + 0.2))
    assert not [a for a in acts if a.kind in (ActionKind.PLACE_REST, ActionKind.WOULD_PLACE_REST)]


def test_both_wings_filled_increments_sets_done():
    p = _params()
    st = _state(p)
    now = T - 600
    st, coid = _bring_up_live_rest(p, st, now)
    st, acts = _feed(p, st, Fill(st.rest_live.order_id, coid, Decimal(1), Decimal("0.45"), "no", now + 0.1))
    tw = [a for a in acts if a.kind == ActionKind.TAKE_WINGS][0]
    yes_coid = st.wing_legs[0].client_order_id
    no_coid = st.wing_legs[1].client_order_id
    st, _ = _feed(p, st, Fill("Y1", yes_coid, Decimal(1), Decimal("0.76"), "yes", now + 0.2))
    assert st.sets_done == 0  # only one leg filled
    st, _ = _feed(p, st, Fill("N1", no_coid, Decimal(1), Decimal("0.64"), "no", now + 0.3))
    assert st.sets_done == 1 and not st.one_legged


def test_wing_no_fill_triggers_retry():
    p = _params()
    st = _state(p)
    now = T - 600
    st, coid = _bring_up_live_rest(p, st, now)
    st, _ = _feed(p, st, Fill(st.rest_live.order_id, coid, Decimal(1), Decimal("0.45"), "no", now + 0.1))
    yes_coid = st.wing_legs[0].client_order_id
    # the YES leg reports no fill (count 0) -> retry on the next fresh book.
    st, _ = _feed(p, st, Fill(None, yes_coid, Decimal(0), Decimal("0.78"), "yes", now + 0.2))
    assert st.wing_legs[0].status == "unfilled"
    st, acts = _feed(p, st, BookUpdate(STK_SD, _top("0.75", "0.76"), now + 0.3))
    retry = [a for a in acts if a.kind == ActionKind.RETRY_WING]
    assert retry and retry[0].legs[0].side == BUY_YES


def test_partial_fill_before_cancel_books_a_fill():
    p = _params()
    st = _state(p)
    now = T - 600
    st, coid = _bring_up_live_rest(p, st, now)
    oid = st.rest_live.order_id
    st, acts = _feed(p, st, OrderCancelled(oid, now + 0.1, filled_count_before_cancel=Decimal(1)))
    assert st.rest_fill is not None
    assert [a for a in acts if a.kind == ActionKind.TAKE_WINGS]


def test_partial_fill_before_cancel_books_at_resting_price_not_desired_n():
    # REGRESSION (review Fix C): a filled_count_before_cancel must book at the CANCELLED order's
    # resting price, not the (possibly drifted) current desired_n.
    p = _params(tol=Decimal("0.50"))  # high tol -> the desired-n move below does NOT requote
    st = _state(p)
    now = T - 600
    st, coid = _bring_up_live_rest(p, st, now)  # rest at n=0.45
    oid = st.rest_live.order_id
    # move the wing so desired_n drifts to 0.60 while the resting order stays at 0.45.
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 0.1))
    st, _ = _feed(p, st, BookUpdate(STK_SD, _top("0.60", "0.61"), now + 0.1))
    assert st.desired_n == Decimal("0.60") and st.rest_live.price == Decimal("0.45")
    st, _ = _feed(p, st, OrderCancelled(oid, now + 0.2, filled_count_before_cancel=Decimal(1)))
    assert st.rest_fill is not None
    assert st.rest_fill.price == Decimal("0.45")  # the resting price, NOT desired_n 0.60


def test_duplicate_wing_fill_does_not_double_count_sets_done():
    # REGRESSION (review Fix D): a fill event reported twice (fill channel + status poll) for an
    # already-filled leg must not increment sets_done a second time.
    p = _params()
    st = _state(p)
    now = T - 600
    st, coid = _bring_up_live_rest(p, st, now)
    st, _ = _feed(p, st, Fill(st.rest_live.order_id, coid, Decimal(1), Decimal("0.45"), "no", now + 0.1))
    yc = st.wing_legs[0].client_order_id
    nc = st.wing_legs[1].client_order_id
    st, _ = _feed(p, st, Fill("Y1", yc, Decimal(1), Decimal("0.76"), "yes", now + 0.2))
    st, _ = _feed(p, st, Fill("N1", nc, Decimal(1), Decimal("0.64"), "no", now + 0.3))
    assert st.sets_done == 1 and not st.wings_needed
    # duplicate no-leg fill -> sets_done must stay 1.
    st, _ = _feed(p, st, Fill("N1", nc, Decimal(1), Decimal("0.64"), "no", now + 0.4))
    assert st.sets_done == 1


def test_suspect_strike_book_is_not_priced():
    # REGRESSION (review Fix A): a suspect strike book (malformed delta / seq-gap) must not be
    # used to compute W or to place a rest.
    p = _params()
    st = _state(p)
    now = T - 600
    st, acts = _feed_all(p, st, [
        BookUpdate(B_SD, _top("0.35", "0.36"), now),
        BookUpdate(STK_SU, _top("0.36", "0.37"), now),
        BookUpdate(STK_SD, _top("0.75", "0.76", suspect=True), now),  # suspect low wing
    ])
    assert st.W is None
    assert not [a for a in acts if a.kind == ActionKind.PLACE_REST]
    sd = [a for a in acts if a.kind == ActionKind.STAND_DOWN]
    assert sd and sd[-1].reason == "stale_or_missing_wing"


def test_silent_feed_clocktick_cancels_stale_rest():
    # REGRESSION (review Fix B): with a live rest, a ClockTick after the strike books have gone
    # stale (no new book frames) must CANCEL the rest and stand down, not leave it live.
    p = _params()
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_live_rest(p, st, now)
    assert st.rest_live is not None
    st, acts = _feed(p, st, ClockTick(now + 2.0))  # strikes now 2s old > freshness 1s
    assert [a for a in acts if a.kind == ActionKind.CANCEL_REST]
    sd = [a for a in acts if a.kind == ActionKind.STAND_DOWN]
    assert sd and sd[-1].reason == "stale_or_missing_wing"
    assert st.rest_live is None


# ===========================================================================
# Replace-rate alarm
# ===========================================================================
def test_replace_rate_alarm_trips_and_stands_down():
    p = _params(tol=Decimal("0.01"), deb_ms=0, replace_rate_alarm_per_min=3)
    st = _state(p)
    now = T - 600
    st, coid = _bring_up_live_rest(p, st, now)  # 1 place so far
    # drive several replaces in << 60s by moving the wing each tick, acking each so the next fires.
    asks = ["0.70", "0.66", "0.62", "0.58", "0.54"]
    tripped = False
    for i, a in enumerate(asks):
        tt = now + 0.01 * (i + 1)
        st, acts = _feed(p, st, BookUpdate(STK_SD, _top(str(Decimal(a) - Decimal("0.01")), a), tt))
        if [x for x in acts if x.kind == ActionKind.STAND_DOWN and x.reason == "replace_rate"]:
            tripped = True
            assert [x for x in acts if x.kind == ActionKind.CANCEL_REST]
            break
        place = [x for x in acts if x.kind == ActionKind.PLACE_REST]
        if place:  # ack it so the hold-while-pending gate clears for the next replace
            st, _ = _feed(p, st, OrderAck(place[-1].client_order_id, f"OID{i+2}", tt))
    assert tripped and st.stood_down


# ===========================================================================
# Shadow
# ===========================================================================
def test_shadow_fill_and_completion():
    p = _params()
    st = _state(p)
    now = T - 600
    # bring up context (books) so every shadow n is solved; no live rest needed for the shadow.
    st, _ = _feed_all(p, st, _fresh_books(now))
    # n_shadow(0.10) = 0.45 -> offer 0.55; a spot-bucket YES print at 0.56 (> 0.55) fills the shadow.
    st, _ = _feed(p, st, Trade(B_SD, Decimal("0.56"), "yes", Decimal(80), now + 0.01))
    sub = st.shadows["0.10"]
    assert sub.filled and sub.fill is not None
    assert sub.fill.n == Decimal("0.45") and sub.fill.offer == Decimal("0.55")
    assert sub.fill.print_price == Decimal("0.56")
    # completion is at the trade tick (both strikes fresh): lock = +0.1036
    assert sub.fill.lock == Decimal("0.1036")
    # E=0.12 shadow (n=0.43, offer 0.57) does NOT fill on a 0.56 print (0.56 !> 0.57).
    assert not st.shadows["0.12"].filled


def test_shadow_fills_once_per_hour():
    p = _params()
    st = _state(p)
    now = T - 600
    st, _ = _feed_all(p, st, _fresh_books(now))
    st, _ = _feed(p, st, Trade(B_SD, Decimal("0.56"), "yes", Decimal(80), now + 0.01))
    first = st.shadows["0.10"].fill
    st, _ = _feed(p, st, Trade(B_SD, Decimal("0.60"), "yes", Decimal(10), now + 0.02))
    assert st.shadows["0.10"].fill is first  # unchanged: one shadow fill per E per hour


def test_shadow_ignores_no_side_and_non_spot_trades():
    p = _params()
    st = _state(p)
    now = T - 600
    st, _ = _feed_all(p, st, _fresh_books(now))
    st, _ = _feed(p, st, Trade(B_SD, Decimal("0.56"), "no", Decimal(80), now + 0.01))   # no side
    st, _ = _feed(p, st, Trade(B_SU, Decimal("0.99"), "yes", Decimal(80), now + 0.02))  # not spot
    assert not st.shadows["0.10"].filled
