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
    OrderAmended,
    OrderCancelled,
    Trade,
    V32Params,
    V32ParamsInvalid,
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
    assert p.deb_ms == 5000
    assert p.shadow_Es == (Decimal("0.08"), Decimal("0.10"), Decimal("0.12"))
    assert p.bucket_width == 100
    assert p.freshness_max_age_s == 1.0
    assert p.bucket_freshness_max_age_s == 30.0  # separate, larger bound for thin range buckets
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


def test_params_E_not_in_shadow_Es_fails_closed(tmp_path):
    # REGRESSION (ruling L-6): the live E must be one of the shadow_Es, else the shadow does not
    # track the live policy. Fail closed at load.
    raw = json.load(open(DEFAULT_V32_PARAMS_PATH, encoding="utf-8"))
    raw["shadow_Es"] = ["0.08", "0.12"]  # drop 0.10 (== E) -> invalid
    bad = tmp_path / "v32_params.json"
    bad.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(V32ParamsInvalid):
        load_v32_params(str(bad), expected_sha=None)


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


def test_requote_above_tol_and_debounce_amends_in_place():
    # AMEND-FIRST replace (Brad 2026-09-15): a same-bucket requote AMENDS the resting order in place
    # (Kalshi Amend Order V2) — one AMEND_REST, no CANCEL, no PLACE, exactly one order the whole time.
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, coid = _bring_up_live_rest(p, st, now)
    oid = st.rest_live.order_id
    old_price = st.rest_live.price
    # move the wing a lot -> desired n shifts >= tol -> AMEND only, no CANCEL, no PLACE (keep Su fresh).
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 1))
    st, acts = _feed(p, st, BookUpdate(STK_SD, _top("0.60", "0.61"), now + 1))
    kinds = [a.kind for a in acts]
    amends = [a for a in acts if a.kind == ActionKind.AMEND_REST]
    assert amends and ActionKind.CANCEL_REST not in kinds and ActionKind.PLACE_REST not in kinds
    am = amends[-1]
    assert am.order_id == oid and am.client_order_id == coid and am.updated_client_order_id
    assert am.price == st.desired_n and am.price != old_price
    assert st.rest_live is not None            # kept populated so a fill during amend books at n
    assert st.rest_pending is None             # NO new rest in flight
    assert st.amend_in_flight and not st.cancel_in_flight
    # while the amend is in flight, a further big move does NOT emit a second amend/place/cancel.
    st, held = _feed(p, st, BookUpdate(STK_SD, _top("0.50", "0.51"), now + 1.05))
    assert not [a for a in held if a.kind in (ActionKind.AMEND_REST, ActionKind.PLACE_REST,
                                              ActionKind.CANCEL_REST)]
    # confirm the amend: the SAME order_id now rests at the new price + new coid; exactly one order.
    st, _ = _feed(p, st, OrderAmended(oid, am.updated_client_order_id, am.price, now + 1.2))
    assert st.rest_live is not None and not st.amend_in_flight
    assert st.rest_live.order_id == oid                       # order_id persists
    assert st.rest_live.client_order_id == am.updated_client_order_id
    assert st.rest_live.price == am.price
    assert st.rest_pending is None                            # never two live rests
    # replace_count counts order-establishing ops (the initial place = 1) exactly as the cancel+create
    # path did; an amend IS a replace, counted on confirm -> 2 after the first amend.
    assert st.replace_count == 2


def test_requote_debounce_blocks_until_elapsed():
    p = _params(tol=Decimal("0.01"), deb_ms=2000)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_live_rest(p, st, now)   # place at `now`, last_replace_ts=now
    # +0.5s: big move but debounce (2000ms) not elapsed -> no CANCEL, no PLACE. Keep both wings fresh.
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 0.5))
    st, acts = _feed(p, st, BookUpdate(STK_SD, _top("0.60", "0.61"), now + 0.5))
    assert not [a for a in acts if a.kind in (ActionKind.PLACE_REST, ActionKind.CANCEL_REST)]
    # +1.5s: refresh both wings (same prices) so neither goes stale; debounce STILL blocks (< 2.0s).
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 1.5))
    st, acts = _feed(p, st, BookUpdate(STK_SD, _top("0.60", "0.61"), now + 1.5))
    assert not [a for a in acts if a.kind in (ActionKind.PLACE_REST, ActionKind.CANCEL_REST)]
    # +2.1s: debounce elapsed -> the AMEND-FIRST replace fires a single AMEND_REST (no cancel, no place).
    acts = []
    st, a = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 2.1)); acts += a
    st, a = _feed(p, st, BookUpdate(STK_SD, _top("0.60", "0.61"), now + 2.1)); acts += a
    assert [x for x in acts if x.kind == ActionKind.AMEND_REST]
    assert not [x for x in acts if x.kind in (ActionKind.CANCEL_REST, ActionKind.PLACE_REST)]
    assert st.amend_in_flight and not st.cancel_in_flight


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


def test_replace_no_live_fill_on_trade_while_amend_in_flight():
    # R-OVERLAP ruling under amend-first: after AMEND_REST is emitted (amend_in_flight) but before
    # OrderAmended, a public Trade through the rest's price must NOT book a live rest fill (the live fill
    # truth comes ONLY from OrderAmended/OrderCancelled/Fill events; a Trade only ever moves the SHADOW),
    # and NO PLACE_REST/AMEND_REST fires while the amend is in flight.
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, coid = _bring_up_live_rest(p, st, now)   # rest at n=0.45 -> offer 0.55
    st, acts = _feed(p, st, BookUpdate(STK_SD, _top("0.60", "0.61"), now + 1))  # -> AMEND_REST
    assert [a for a in acts if a.kind == ActionKind.AMEND_REST]
    assert st.amend_in_flight and st.rest_live is not None and st.rest_pending is None
    # a YES print on the spot bucket ABOVE the old offer -> shadow only, never a live fill.
    st, acts = _feed(p, st, Trade(B_SD, Decimal("0.62"), "yes", Decimal(5), now + 1.05))
    assert st.rest_fill is None                                   # no live fill from a Trade
    assert not [a for a in acts if a.kind in (ActionKind.PLACE_REST, ActionKind.AMEND_REST)]
    assert st.amend_in_flight                                     # still awaiting the amend confirm


def test_fill_during_amend_takes_wings_not_place():
    # A Fill for the order while an amend is in flight (it filled before/at the amend) -> TAKE_WINGS at
    # the resting price, and NEVER a PLACE_REST (the one-set latch holds); amend_in_flight is cleared.
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, coid = _bring_up_live_rest(p, st, now)   # rest at n=0.45
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 1))     # keep Su fresh
    st, acts = _feed(p, st, BookUpdate(STK_SD, _top("0.60", "0.61"), now + 1))  # -> AMEND_REST
    assert [a for a in acts if a.kind == ActionKind.AMEND_REST]
    assert st.amend_in_flight and st.rest_live is not None
    # the order fills during the amend -> TAKE_WINGS (bounded pin), no PLACE.
    st, acts = _feed(p, st, Fill(st.rest_live.order_id, coid, Decimal(1), Decimal("0.45"), "no",
                                 now + 1.05))
    assert [a for a in acts if a.kind == ActionKind.TAKE_WINGS]
    assert not [a for a in acts if a.kind == ActionKind.PLACE_REST]
    assert st.rest_fill is not None and st.rest_fill.price == Decimal("0.45")
    assert not st.cancel_in_flight and not st.amend_in_flight
    assert st.rest_live is None and st.rest_pending is None


def test_amend_confirm_with_cross_fill_takes_wings_once_no_second_rest():
    # OrderAmended.fill_count > 0 (the amend crossed) -> book the rest fill at the venue's average fill
    # price (NO-space) and TAKE the wings ONCE; amend_in_flight cleared; no second rest ever placed.
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, coid = _bring_up_live_rest(p, st, now)
    oid = st.rest_live.order_id
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 1))     # keep Su fresh
    st, acts = _feed(p, st, BookUpdate(STK_SD, _top("0.60", "0.61"), now + 1))  # -> AMEND_REST
    am = [a for a in acts if a.kind == ActionKind.AMEND_REST][-1]
    assert st.amend_in_flight
    # the amend confirms WITH a cross fill of 1 at a NO-space average fill price of 0.47.
    st, acts = _feed(p, st, OrderAmended(oid, am.updated_client_order_id, am.price, now + 1.1,
                                         remaining_count=Decimal(0), fill_count=Decimal(1),
                                         average_fill_price=Decimal("0.47")))
    assert len([a for a in acts if a.kind == ActionKind.TAKE_WINGS]) == 1
    assert st.rest_fill is not None and st.rest_fill.price == Decimal("0.47")  # booked at avg fill price
    assert not st.amend_in_flight and st.rest_live is None and st.rest_pending is None
    # one set per hour latched at the fill: no PLACE and no AMEND on a later tick.
    st, acts = _feed(p, st, BookUpdate(STK_SD, _top("0.60", "0.61"), now + 1.3))
    assert not [a for a in acts if a.kind in (ActionKind.PLACE_REST, ActionKind.AMEND_REST)]


def test_confirmed_amend_counts_as_a_replace():
    # An amend IS a replace: replace_count + replace_times (the A_REPLACE alarm's input) increment on
    # CONFIRM (OrderAmended) — the same convention as the cancel+create path, which counts at the create.
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, coid = _bring_up_live_rest(p, st, now)   # the initial place counts as 1
    assert st.replace_count == 1 and len(st.replace_times) == 1
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 1))
    st, acts = _feed(p, st, BookUpdate(STK_SD, _top("0.60", "0.61"), now + 1))
    am = [a for a in acts if a.kind == ActionKind.AMEND_REST][-1]
    assert st.replace_count == 1                  # NOT yet counted at the request (counted on confirm)
    st, _ = _feed(p, st, OrderAmended(st.rest_live.order_id, am.updated_client_order_id, am.price,
                                      now + 1.1))
    assert st.replace_count == 2 and len(st.replace_times) == 2


def test_amends_trip_the_replace_rate_alarm():
    # The A_REPLACE (replace_rate) alarm counts confirmed amends: with the alarm at 2/min, three
    # confirmed amends (plus the initial place) in a trailing 60 s stand the hour down with reason
    # `replace_rate` and cancel the rest.
    p = _params(tol=Decimal("0.01"), deb_ms=0, replace_rate_alarm_per_min=2)
    st = _state(p)
    now = T - 600
    st, coid = _bring_up_live_rest(p, st, now)
    all_acts: list = []
    yasks = ["0.60", "0.75", "0.55"]   # each move shifts desired n >= tol from the last resting n
    for i, ya in enumerate(yasks):
        t = now + 1 + i * 0.1
        st, a = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), t)); all_acts += a
        st, a = _feed(p, st, BookUpdate(STK_SD, _top(ya, str(Decimal(ya) + Decimal("0.01"))), t))
        all_acts += a
        am = [x for x in a if x.kind == ActionKind.AMEND_REST]
        if am and st.rest_live is not None:
            st, a2 = _feed(p, st, OrderAmended(st.rest_live.order_id, am[-1].updated_client_order_id,
                                               am[-1].price, t + 0.01))
            all_acts += a2
    # once >2 confirmed replaces (amends) sit in the trailing 60 s the alarm stands the hour down and
    # cancels the rest — the reason may surface on any of the ticks above, so scan them all.
    st, a = _feed(p, st, ClockTick(now + 2)); all_acts += a
    assert st.stood_down
    reasons = [x.reason for x in all_acts if x.kind == ActionKind.STAND_DOWN]
    assert "replace_rate" in reasons
    assert [x for x in all_acts if x.kind == ActionKind.CANCEL_REST]
    assert st.replace_count >= 3


def test_quote_end_cancels_not_amends():
    # The quote-end window close still CANCELS the rest (never AMEND) — amend-first only replaces a
    # LIVE quote inside the window.
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, coid = _bring_up_live_rest(p, st, now)
    st, acts = _feed(p, st, ClockTick(T - 250))   # t_to_close = 250 < quote_end_s (300)
    assert [a for a in acts if a.kind == ActionKind.CANCEL_REST]
    assert not [a for a in acts if a.kind == ActionKind.AMEND_REST]


def test_bucket_change_cancels_not_amends():
    # A bucket change (different ticker) keeps the cancel -> confirm -> place path (an amend cannot change
    # the ticker); never an AMEND_REST.
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, coid = _bring_up_live_rest(p, st, now)
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 0.9))
    st, _ = _feed(p, st, BookUpdate(STK_SU2, _top("0.20", "0.21"), now + 0.9))
    st, acts = _feed(p, st, BookUpdate(B_SU, _top("0.55", "0.57"), now + 0.9))  # higher bucket -> spot
    assert [a for a in acts if a.kind == ActionKind.CANCEL_REST]
    assert not [a for a in acts if a.kind == ActionKind.AMEND_REST]


def test_shakedown_emits_would_amend_twin():
    # In shakedown a same-bucket replace downgrades to the WOULD_AMEND_REST twin (no order-emitting kind).
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p, shakedown=True)
    now = T - 600
    st, coid = _bring_up_live_rest(p, st, now)
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 1))
    st, acts = _feed(p, st, BookUpdate(STK_SD, _top("0.60", "0.61"), now + 1))
    kinds = [a.kind for a in acts]
    assert ActionKind.WOULD_AMEND_REST in kinds
    assert ActionKind.AMEND_REST not in kinds and ActionKind.CANCEL_REST not in kinds
    # a later book tick must NOT place a second rest (one set per hour latched at the fill).
    st, acts = _feed(p, st, BookUpdate(STK_SD, _top("0.60", "0.61"), now + 1.2))
    assert not [a for a in acts if a.kind == ActionKind.PLACE_REST]


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


# ===========================================================================
# Clock-interleave tolerance (2026-09-14 clock-flap fix): a book stamped slightly AHEAD of the
# evaluation clock (the two WS connections' skewed clocks) is FRESH, not stale -> no spurious cancel.
# ===========================================================================
def test_interleave_negative_age_within_bound_keeps_wing_priced():
    # Bring up a live rest, then feed the OTHER strike with a ts 250 ms NEWER, and re-tick the first
    # strike at an OLDER ts -> the evaluation clock (that older tick's ts) is BEHIND the newer strike's
    # stored ts, so its age is -0.25 s. Pre-fix (0.0 <= age) that read "stale" -> W None -> CANCEL +
    # stand down (the ~1 ms flap). Post-fix (-bound <= age <= bound) it is fresh: W stays priced, the
    # live rest is untouched, and NOTHING is cancelled.
    p = _params()
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_live_rest(p, st, now)
    assert st.W == Decimal("1.4290") and st.rest_live is not None
    # Su (79700) stamped 300 ms in the future (the faster connection); prices unchanged.
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 0.30))
    # Sd (79600) re-ticks at now+0.05 -> eval clock now+0.05 < Su's stored now+0.30 -> age(Su) = -0.25 s.
    st, acts = _feed(p, st, BookUpdate(STK_SD, _top("0.75", "0.76"), now + 0.05))
    assert not [a for a in acts if a.kind == ActionKind.CANCEL_REST], "no cancel on a within-bound skew"
    assert not [a for a in acts if a.kind == ActionKind.STAND_DOWN]
    assert st.W == Decimal("1.4290"), "wing stays priced through the clock interleave"
    assert st.rest_live is not None and st.rest_live.price == Decimal("0.45")


def test_interleave_flap_over_many_regressing_ticks_never_cancels():
    # Fifty strike ticks whose ts alternate between the two connections' clocks (a ~40 ms skew each
    # way), prices constant -> pre-fix this is the exact 61-place/61-cancel flap; post-fix ZERO cancels.
    p = _params()
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_live_rest(p, st, now)
    cancels = 0
    for i in range(50):
        # even i: Su newer (+0.30); odd i: Sd re-ticks 40 ms behind -> negative age on Su.
        if i % 2 == 0:
            st, acts = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 0.30 + i * 0.001))
        else:
            st, acts = _feed(p, st, BookUpdate(STK_SD, _top("0.75", "0.76"), now + 0.26 + i * 0.001))
        cancels += len([a for a in acts if a.kind == ActionKind.CANCEL_REST])
    assert cancels == 0, f"clock interleave must not churn the rest (got {cancels} cancels)"
    assert st.rest_live is not None


def test_genuinely_stale_wing_beyond_bound_still_cancels_after_fix():
    # The tolerance is exactly one ``bound`` (1.0 s for strikes): a book more than a full bound STALE
    # (positive age > bound) is still stale and still cancels — genuine staleness detection unchanged.
    p = _params()
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_live_rest(p, st, now)
    # a bucket tick 1.5 s later leaves both strike books 1.5 s old (> 1.0 s strike bound) -> W stale.
    st, acts = _feed(p, st, BookUpdate(B_SD, _top("0.35", "0.36"), now + 1.5))
    assert [a for a in acts if a.kind == ActionKind.CANCEL_REST]
    sd = [a for a in acts if a.kind == ActionKind.STAND_DOWN]
    assert sd and sd[0].reason == "stale_or_missing_wing"


# ===========================================================================
# Spot-bucket freshness gate (R-STALE-SPOT: gate the SELECTED spot, never fall through)
# ===========================================================================
def test_stale_spot_bucket_cancels_with_stale_bucket_reason():
    # the selected spot bucket's own book ages past bucket_freshness_max_age_s = 30s (a bucket-feed
    # stall) while a strike still ticks fresh -> CANCEL + stand down with reason `stale_bucket`, NO
    # PLACE. The spot floor is UNCHANGED (still selected, just stale) — R-STALE-SPOT.
    p = _params()
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_live_rest(p, st, now)
    assert st.spot_Sd == 79600 and st.rest_live is not None
    st, acts = _feed(p, st, BookUpdate(STK_SD, _top("0.75", "0.76"), now + 31.0))
    assert [a for a in acts if a.kind == ActionKind.CANCEL_REST]
    assert not [a for a in acts if a.kind == ActionKind.PLACE_REST]
    sd = [a for a in acts if a.kind == ActionKind.STAND_DOWN]
    assert sd and sd[-1].reason == "stale_bucket"
    assert st.rest_live is None and st.spot_Sd == 79600 and st.spot_bucket_stale


def test_stale_bucket_then_fresh_resumes_place():
    p = _params()
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_live_rest(p, st, now)
    oid = st.rest_live.order_id
    st, acts = _feed(p, st, BookUpdate(STK_SD, _top("0.75", "0.76"), now + 31.0))
    assert [a for a in acts if a.kind == ActionKind.CANCEL_REST]
    st, _ = _feed(p, st, OrderCancelled(oid, now + 31.1, Decimal(0)))
    # buckets + strikes fresh again -> the gate clears and a new rest is placed
    st, acts = _feed_all(p, st, _fresh_books(now + 32.0))
    assert [a for a in acts if a.kind == ActionKind.PLACE_REST]
    assert st.rest_pending is not None and st.spot_Sd == 79600 and not st.spot_bucket_stale


def test_stale_non_spot_bucket_has_no_effect():
    # R-STALE-SPOT (a): a stale NON-spot (lower-mid) bucket changes nothing — selection ignores age, so
    # the higher-mid bucket is still the spot, it is fresh, and quoting continues on it.
    p = _params()
    st = _state(p)
    now = T - 600
    # B_SD (79600) set once at `now` and never refreshed -> the STALE lower-mid non-spot bucket.
    st, _ = _feed(p, st, BookUpdate(B_SD, _top("0.35", "0.36"), now))         # mid 0.355 (lower)
    # spot 79700 + its strikes (Sd=79700, Su=79800), all fresh at now+31; B_SD is now 31s stale.
    st, _ = _feed(p, st, BookUpdate(B_SU, _top("0.40", "0.42"), now + 31.0))  # mid 0.41 -> spot 79700
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.75", "0.76"), now + 31.0))
    st, acts = _feed(p, st, BookUpdate(STK_SU2, _top("0.36", "0.37"), now + 31.0))
    assert st.spot_Sd == 79700 and not st.spot_bucket_stale
    assert [a for a in acts if a.kind == ActionKind.PLACE_REST]  # quoting continues on the fresh spot
    assert not [a for a in acts if a.kind == ActionKind.STAND_DOWN and a.reason == "stale_bucket"]


def test_stale_spot_with_fresh_lower_bucket_stands_down():
    # R-STALE-SPOT (b): the selected spot (higher-mid 79700) goes stale while a FRESH lower-mid bucket
    # (79600) is present -> CANCEL + `stale_bucket`, and NO PLACE on the lower bucket or any other.
    p = _params()
    st = _state(p)
    now = T - 600
    st, _ = _feed(p, st, BookUpdate(B_SD, _top("0.35", "0.36"), now))         # lower bucket 79600
    st, _ = _feed(p, st, BookUpdate(B_SU, _top("0.40", "0.42"), now))         # spot 79700 (higher mid)
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.75", "0.76"), now))       # Sd=79700 yes_ask
    st, acts = _feed(p, st, BookUpdate(STK_SU2, _top("0.36", "0.37"), now))   # Su=79800 no_ask
    place = [a for a in acts if a.kind == ActionKind.PLACE_REST]
    assert place and st.spot_Sd == 79700
    st, _ = _feed(p, st, OrderAck(place[-1].client_order_id, "OID1", now))
    assert st.rest_live is not None
    # refresh ONLY the lower bucket (79600); the spot (79700) book is now 31s stale.
    st, acts = _feed(p, st, BookUpdate(B_SD, _top("0.35", "0.36"), now + 31.0))
    assert st.spot_Sd == 79700 and st.spot_bucket_stale      # still the selected spot, now stale
    assert [a for a in acts if a.kind == ActionKind.CANCEL_REST]
    assert not [a for a in acts if a.kind == ActionKind.PLACE_REST]  # never quotes the fresh lower bucket
    sd = [a for a in acts if a.kind == ActionKind.STAND_DOWN]
    assert sd and sd[-1].reason == "stale_bucket"
    assert st.rest_live is None


def test_clocktick_only_stale_bucket_cancels():
    # a stalled bucket feed with NO further book frames must still cancel the rest on a ClockTick
    # (law 3), symmetric to the strike silent-feed regression. The stale-spot branch is evaluated
    # before the wing branch, so the reason is `stale_bucket` even though the strikes have also aged.
    p = _params()
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_live_rest(p, st, now)
    assert st.rest_live is not None
    st, acts = _feed(p, st, ClockTick(now + 31.0))  # bucket book now 31s old > 30s; no new frame
    assert [a for a in acts if a.kind == ActionKind.CANCEL_REST]
    sd = [a for a in acts if a.kind == ActionKind.STAND_DOWN]
    assert sd and sd[-1].reason == "stale_bucket"
    assert st.rest_live is None and st.spot_Sd == 79600 and st.spot_bucket_stale


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


def test_initial_take_is_unconditional_regardless_of_lock_floor():
    # RULING F-2: the INITIAL both-wings take fires immediately on a fill, even when the lock is
    # far below a (deliberately high) lock_floor. lock_floor does NOT gate the initial take.
    p = _params(lock_floor=Decimal("0.20"))  # lock 0.1036 < 0.20
    st = _state(p)
    now = T - 600
    st, coid = _bring_up_live_rest(p, st, now)
    st, acts = _feed(p, st, Fill(st.rest_live.order_id, coid, Decimal(1), Decimal("0.45"), "no", now + 0.1))
    tw = [a for a in acts if a.kind == ActionKind.TAKE_WINGS]
    assert tw and len(tw) == 1
    assert tw[0].lock == Decimal("0.1036")  # take fired despite lock < floor
    assert st.wing_taken


def test_negative_lock_initial_take_still_fires():
    # RULING F-2: even a negative-lock initial take fires (the fill already happened; assemble the
    # pin to bound the position).
    p = _params(tol=Decimal("0.50"))  # high tol so the expensive-wing book below does NOT requote
    st = _state(p)
    now = T - 600
    st, coid = _bring_up_live_rest(p, st, now)  # rest at n=0.45
    # push both wings very expensive so the completion lock goes negative, without requoting.
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.10", "0.11"), now + 0.1))  # no_ask(Su)=0.90
    st, _ = _feed(p, st, BookUpdate(STK_SD, _top("0.89", "0.90"), now + 0.1))  # yes_ask(Sd)=0.90
    assert st.rest_live is not None and st.rest_live.price == Decimal("0.45")  # not requoted
    st, acts = _feed(p, st, Fill(st.rest_live.order_id, coid, Decimal(1), Decimal("0.45"), "no", now + 0.15))
    tw = [a for a in acts if a.kind == ActionKind.TAKE_WINGS]
    assert tw and tw[0].lock < Decimal(0)  # negative lock
    assert st.wing_taken


def test_retry_wing_gated_by_lock_floor_then_fires_on_improvement():
    # RULING F-2: after the (unconditional) initial take, a single missing leg is RETRIED only while
    # the projected set lock stays at/above lock_floor (the two held legs form the $1 floor).
    p = _params(lock_floor=Decimal("-0.10"))
    st = _state(p)
    now = T - 600
    st, coid = _bring_up_live_rest(p, st, now)
    st, acts = _feed(p, st, Fill(st.rest_live.order_id, coid, Decimal(1), Decimal("0.45"), "no", now + 0.1))
    assert [a for a in acts if a.kind == ActionKind.TAKE_WINGS]  # initial take fired
    yes_coid = st.wing_legs[0].client_order_id
    no_coid = st.wing_legs[1].client_order_id
    # NO leg fills at 0.64 (held); YES leg reports no-fill -> unfilled.
    st, _ = _feed(p, st, Fill("N1", no_coid, Decimal(1), Decimal("0.64"), "no", now + 0.2))
    st, _ = _feed(p, st, Fill(None, yes_coid, Decimal(0), Decimal("0.78"), "yes", now + 0.2))
    assert st.wing_legs[1].status == "filled" and st.wing_legs[0].status == "unfilled"
    # expensive YES ask (0.98) -> projected set lock < -0.10 -> retry DEFERRED.
    st, acts = _feed(p, st, BookUpdate(STK_SD, _top("0.97", "0.98"), now + 0.3))
    assert not [a for a in acts if a.kind == ActionKind.RETRY_WING]
    # YES ask improves to 0.90 -> projected lock >= -0.10 -> retry FIRES.
    st, acts = _feed(p, st, BookUpdate(STK_SD, _top("0.89", "0.90"), now + 0.4))
    retry = [a for a in acts if a.kind == ActionKind.RETRY_WING]
    assert retry and retry[0].legs[0].side == BUY_YES


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


def test_lone_bucket_no_never_hedged_latches_one_legged_at_cutoff():
    # ADVERSARIAL (review): a rest fills but the strike feed is DEAD from the fill to the settle cutoff,
    # so the wings are NEVER taken -> a lone, unhedged bucket-NO. This MUST latch one_legged (drives
    # S1_LEGGED). The earlier cutoff guard required wing_taken=True and silently missed this worst case.
    p = _params()
    st = _state(p)
    now = T - 600
    st, coid = _bring_up_live_rest(p, st, now)
    # the fill arrives 100 s later; the strike books (last ts now) are stale -> no wings taken.
    st, acts = _feed(p, st, Fill(st.rest_live.order_id, coid, Decimal(1), Decimal("0.45"), "no",
                                 now + 100.0))
    assert not [a for a in acts if a.kind == ActionKind.TAKE_WINGS]  # wings could not be taken
    assert st.rest_fill is not None and not st.wing_taken and st.wings_needed
    assert not st.one_legged  # not yet at the cutoff
    # advance to the settle cutoff (t_to_close < no_orders_after_s_to_settle=1): must flag one_legged.
    st, _ = _feed(p, st, ClockTick(server_ts=T - 0.5))
    assert st.one_legged is True


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
    # The alarm counts places (replace_times) in a trailing 60 s; it is checked at the top of _requote
    # and, when exceeded, cancels the rest and latches stood_down. With the SEQUENTIAL replace each
    # replace is a separate cancel->confirm->place cycle, so seed a burst of recent places directly and
    # verify the next in-window book tick trips the alarm and stands the hour down.
    from dataclasses import replace as _dc
    p = _params(tol=Decimal("0.01"), deb_ms=0, replace_rate_alarm_per_min=3)
    st = _state(p)
    now = T - 600
    st, coid = _bring_up_live_rest(p, st, now)  # 1 place, a live rest, replace_times={now}
    # seed 4 recent places (> alarm threshold 3), all within the trailing 60 s.
    st = _dc(st, replace_times=tuple(now + 0.001 * i for i in range(4)), replace_count=4)
    st, acts = _feed(p, st, BookUpdate(STK_SD, _top("0.75", "0.76"), now + 0.02))
    assert [x for x in acts if x.kind == ActionKind.STAND_DOWN and x.reason == "replace_rate"]
    assert [x for x in acts if x.kind == ActionKind.CANCEL_REST]
    assert st.stood_down


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


def test_shadow_still_books_when_stood_down():
    # ITEM B (2026-09-14): the shadow is the IDEAL-rule statistic and must keep running for the whole
    # quoting window regardless of the live stand-down / replace-rate alarm / rest state — it depends
    # ONLY on a fresh spot bucket + fresh wings + the bucket trade tape. This asserts the alarm's
    # ``stood_down`` latch does NOT gate the shadow: n is still re-solved on a book tick and a
    # qualifying spot-bucket YES print still books a shadow fill, while the LIVE path emits nothing.
    p = _params()
    st = _state(p)
    now = T - 600
    st, _ = _feed_all(p, st, _fresh_books(now))
    # simulate the replace-rate alarm having latched the hour down.
    st = replace(st, stood_down=True)
    # a book tick under stood_down must still re-solve every shadow n (W valid -> n priced).
    st, acts = _feed(p, st, BookUpdate(STK_SD, _top("0.75", "0.76"), now + 0.001))
    assert st.stood_down
    assert st.shadows["0.10"].n == Decimal("0.45"), "shadow n must be re-solved under stand-down"
    # the LIVE path is suppressed (stood_down -> no place/quote)
    assert not [a for a in acts if a.kind in (ActionKind.PLACE_REST, ActionKind.WOULD_PLACE_REST)]
    # a qualifying spot-bucket YES print still books the shadow fill despite the stand-down.
    st, _ = _feed(p, st, Trade(B_SD, Decimal("0.56"), "yes", Decimal(80), now + 0.01))
    sub = st.shadows["0.10"]
    assert sub.filled and sub.fill is not None
    assert sub.fill.n == Decimal("0.45") and sub.fill.print_price == Decimal("0.56")
    assert sub.fill.lock == Decimal("0.1036")


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


def test_shadow_does_not_fill_on_stale_bucket():
    # a shadow fill needs a FRESH spot-bucket book too: a qualifying YES print 31s after the last
    # bucket book (age > bucket_freshness_max_age_s) must NOT synthesize an ideal fill off a stale cap.
    p = _params()
    st = _state(p)
    now = T - 600
    st, _ = _feed_all(p, st, _fresh_books(now))
    assert st.shadows["0.10"].n == Decimal("0.45")  # offer 0.55
    st, _ = _feed(p, st, Trade(B_SD, Decimal("0.60"), "yes", Decimal(5), now + 31.0))  # > offer, stale
    assert not st.shadows["0.10"].filled and st.shadows["0.10"].fill is None
    # sanity: the SAME print while the bucket is fresh DOES fill (isolates the freshness gate as cause)
    st2 = _state(p)
    st2, _ = _feed_all(p, st2, _fresh_books(now))
    st2, _ = _feed(p, st2, Trade(B_SD, Decimal("0.60"), "yes", Decimal(5), now + 0.5))
    assert st2.shadows["0.10"].filled

# ---------------------------------------------------------------------------
# Shadow window gate (2026-09-15): the shadow may only take a print while the live path would have
# been quoting (T-15..T-5). A print outside [quote_end_s, quote_start_s] is one the live path could
# NEVER have taken (books connect ~T-20; live cancels its rest at T-5), so it must NOT fill the
# shadow — it emits an observability-only SHADOW_FILL_OUTSIDE_WINDOW action and leaves the sub open.
# ---------------------------------------------------------------------------
def _outside_window_actions(acts):
    return [a for a in acts if a.kind == ActionKind.SHADOW_FILL_OUTSIDE_WINDOW]


def test_shadow_print_after_t5_is_suppressed_not_filled():
    # (a) a qualifying spot-bucket YES print at t_to_close = ~249 s (AFTER T-5, past quote end) does
    # NOT fill the shadow and emits the suppressed record.
    p = _params()
    st = _state(p)
    now = T - 249  # t_to_close 249 < quote_end_s (300): after T-5
    st, _ = _feed_all(p, st, _fresh_books(now))
    assert st.shadows["0.10"].n == Decimal("0.45")  # n still solved (recompute is NOT window-gated)
    st, acts = _feed(p, st, Trade(B_SD, Decimal("0.56"), "yes", Decimal(80), now + 0.01))
    assert not st.shadows["0.10"].filled, "a print after T-5 must not fill the shadow"
    assert st.shadows["0.10"].fill is None
    outside = _outside_window_actions(acts)
    assert outside, "expected a SHADOW_FILL_OUTSIDE_WINDOW record for the suppressed fill"
    a10 = [a for a in outside if a.shadow_E == Decimal("0.10")]
    assert a10, "expected a suppressed record for E=0.10"
    a = a10[0]
    assert a.offer == Decimal("0.55") and a.print_price == Decimal("0.56")
    assert a.t_to_close < Decimal("300"), "suppressed print is past the quote end"


def test_shadow_print_in_window_fills_and_emits_no_suppressed_record():
    # (b) the SAME print at t_to_close = 600 s (inside T-15..T-5) DOES fill and emits no suppressed
    # record — isolates the window gate as the sole cause of (a).
    p = _params()
    st = _state(p)
    now = T - 600  # in window
    st, _ = _feed_all(p, st, _fresh_books(now))
    st, acts = _feed(p, st, Trade(B_SD, Decimal("0.56"), "yes", Decimal(80), now + 0.01))
    assert st.shadows["0.10"].filled and st.shadows["0.10"].fill is not None
    assert st.shadows["0.10"].fill.print_price == Decimal("0.56")
    assert not _outside_window_actions(acts), "an in-window fill emits no suppressed record"


def test_shadow_print_before_t15_is_suppressed_not_filled():
    # (c) a qualifying print at t_to_close = ~1000 s (BEFORE T-15, before the window opens) does NOT
    # fill the shadow and emits the suppressed record.
    p = _params()
    st = _state(p)
    now = T - 1000  # t_to_close 1000 > quote_start_s (900): before T-15
    st, _ = _feed_all(p, st, _fresh_books(now))
    assert st.shadows["0.10"].n == Decimal("0.45")
    st, acts = _feed(p, st, Trade(B_SD, Decimal("0.56"), "yes", Decimal(80), now + 0.01))
    assert not st.shadows["0.10"].filled and st.shadows["0.10"].fill is None
    outside = _outside_window_actions(acts)
    a10 = [a for a in outside if a.shadow_E == Decimal("0.10")]
    assert a10, "expected a suppressed record for E=0.10 before T-15"
    assert a10[0].t_to_close > Decimal("900"), "suppressed print is before the window opens"


def test_shadow_completion_after_t5_for_in_window_fill_still_completes():
    # (d) a shadow fill recorded INSIDE the window whose completion tick lands AFTER T-5 still
    # completes (the window gate touches the FILL, never the completion). The fill defers completion
    # because the wing strikes are stale at the fill instant (age > freshness_max_age_s = 1 s); a
    # fresh strike book after T-5 (t_to_close 295) completes the lock.
    p = _params()
    st = _state(p)
    now0 = T - 400  # in window; solves n, stamps strikes+bucket
    st, _ = _feed_all(p, st, _fresh_books(now0))
    # trade 1.5 s later: in window (t_to_close ~398.5, not suppressed), bucket still fresh (age<30) so
    # the fill records, but the wing strikes are stale (age 1.5 > 1) so completion cannot happen yet.
    st, acts = _feed(p, st, Trade(B_SD, Decimal("0.56"), "yes", Decimal(80), now0 + 1.5))
    assert not _outside_window_actions(acts), "an in-window fill emits no suppressed record"
    sub = st.shadows["0.10"]
    assert sub.filled and sub.fill is not None
    assert sub.awaiting_completion and sub.fill.lock is None, "completion must defer (stale wings)"
    # fresh strike books after T-5 complete the lock (t_to_close 295 < quote_end_s 300).
    st, _ = _feed_all(p, st, _fresh_books(T - 295))
    sub = st.shadows["0.10"]
    assert not sub.awaiting_completion and sub.fill.lock == Decimal("0.1036"), \
        "an in-window shadow fill still completes after T-5"


# ---------------------------------------------------------------------------
# Shadow n_min gate (Registration 3 nit, 2026-09-19): the shadow may only take a print at a solved n at
# or above params.n_min. The live path stands down (n_below_min) below n_min and would never have
# rested there, so a below-n_min print is not a live-reachable counterfactual (same class as the
# T-15..T-5 window gate) -- it emits SHADOW_FILL_BELOW_MIN and leaves the sub open. Evidence: the live
# 2026-09-19 23:00Z window (shadow offer 0.96, n 0.04 < 0.05; a 16-lot print at 0.97 filled the shadow
# while live stood down n_below_min the whole hour).
# ---------------------------------------------------------------------------
def _below_min_actions(acts):
    return [a for a in acts if a.kind == ActionKind.SHADOW_FILL_BELOW_MIN]


def test_shadow_print_below_n_min_is_suppressed_not_filled():
    # force below-min by raising n_min above the golden solved n (0.45): an in-window qualifying print
    # must NOT fill the shadow and must emit the observability record.
    p = _params(n_min=Decimal("0.50"))
    st = _state(p)
    now = T - 600  # in window
    st, _ = _feed_all(p, st, _fresh_books(now))
    assert st.shadows["0.10"].n == Decimal("0.45")  # solved, but below the raised n_min
    st, acts = _feed(p, st, Trade(B_SD, Decimal("0.56"), "yes", Decimal(80), now + 0.01))
    assert not st.shadows["0.10"].filled, "a below-n_min print must not fill the shadow"
    assert st.shadows["0.10"].fill is None
    below = _below_min_actions(acts)
    a10 = [a for a in below if a.shadow_E == Decimal("0.10")]
    assert a10, "expected a SHADOW_FILL_BELOW_MIN record for E=0.10"
    assert a10[0].offer == Decimal("0.55") and a10[0].print_price == Decimal("0.56")
    # no OUTSIDE_WINDOW record (this print is inside the window) -- the below-min gate is the sole cause
    assert not _outside_window_actions(acts)


def test_shadow_print_at_n_min_boundary_fills():
    # n exactly AT n_min is live-reachable (the gate is strict `<`): n_min = 0.45 == the solved n fills.
    p = _params(n_min=Decimal("0.45"))
    st = _state(p)
    now = T - 600
    st, _ = _feed_all(p, st, _fresh_books(now))
    assert st.shadows["0.10"].n == Decimal("0.45")
    st, acts = _feed(p, st, Trade(B_SD, Decimal("0.56"), "yes", Decimal(80), now + 0.01))
    assert st.shadows["0.10"].filled and st.shadows["0.10"].fill is not None
    assert not _below_min_actions(acts), "n exactly at n_min is not below-min -> fills, no record"


def test_shadow_print_above_n_min_fills_no_record():
    # the frozen n_min (0.05) is well below the golden n (0.45) -> normal fill, no below-min record.
    p = _params()
    st = _state(p)
    now = T - 600
    st, _ = _feed_all(p, st, _fresh_books(now))
    st, acts = _feed(p, st, Trade(B_SD, Decimal("0.56"), "yes", Decimal(80), now + 0.01))
    assert st.shadows["0.10"].filled
    assert not _below_min_actions(acts)
