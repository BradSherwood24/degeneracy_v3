"""reference_impl_review.py — INDEPENDENT reimplementation of the ceremony decision core, written
from pilot/ceremony/commission.md + pilot/ceremony/falsifier.md + PLAN's frozen-policy description
ALONE (NOT from signal.py / reconciler.py), and driven against the PRODUCTION code with the same
seeded event/state streams. This is the arming-gate cross-check required by PLAN Phase 3.

WHAT IS REIMPLEMENTED (decision logic only):
  A) Entry decision (RefEntryEngine):
       * sub-$1 flip: first moment flip-C < $1.00 STRICT, ALL quintiles.
       * Q1-strangle: EV = fair_strangle_q - strangle-C >= 5c, ONLY quintile 0, only if the
         strangle was not stood down.
       * freshness/staleness fail-closed: both legs known, not suspect, age <= 1.0s, no future ts.
       * first-qualifying-wins per window (window-level mutual exclusion), sub-$1 priority on a
         same-event tie.
       * no orders with < 1s to settle (StandDown, emitted once); warmup outside the final 900s.
  B) Imbalance protocol (ref_propose): retry-buy the deficient leg bounded by the pair-cost ceiling
     computed with ACTUAL fees, <= 5 retries/side, no rebalance-buy inside 3s (sell-down only),
     no order at all inside 1s (ride to settlement), sell-down target rounds DOWN, GiveUp -> S2.

WHAT IS IMPORTED (frozen law the commission says to import, NEVER reimplement): the audited census
``fee`` and the census EV curve (``fair_strangle_q``), and ``WINDOW_S``. Only the DECISION RULES
(comparisons, gates, priority, bounds, rounding) are re-derived here — those are what the differential
tests exercise. A divergence means production disagrees with a plain reading of the frozen text.

No network, no clock, no sealed-day read. Seeded RNG => deterministic scenario counts.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal

from service._simlaw import WINDOW_S, fee, load_ev_curve
from service.book import TopOfBook
from service.ledger import (
    PURPOSE_REBALANCE_BUY,
    PURPOSE_REBALANCE_SELL,
    Intent,
    IntentLeg,
    new_ledger,
    record_intent,
    record_response,
)
from service.orders.envelope import OrderResponse, no_fill_response
from service.policy import Q1_STRANGLE, SUB_DOLLAR_FLIP, load_policy
from service.reconciler import (
    Balanced,
    GiveUp,
    RebalanceQuotes,
    RetryBuy,
    RideToSettlement,
    SellDown,
    propose_rebalance,
)
from service.signal import (
    FIRE,
    STAND_DOWN,
    WOULD_FIRE,
    BookUpdate,
    ClockTick,
    WindowState,
    decide,
)

P = load_policy()
CURVE = load_ev_curve()
FAIR_Q0 = CURVE.fair_for("strangle", 0)

HI, LO = "KXBTCD-HI", "KXBTCD-LO"

# ceremony constants (read from the FROZEN policy, not hardcoded)
C_MAX = P.sub_dollar_C_max                      # 1.00
EV_MIN = P.q1_strangle_ev_min                   # 0.05
FRESH = Decimal(str(P.freshness_max_leg_age_s)) # 1.0 s
NO_ORDERS_S = P.no_orders_after_s_to_settle     # 1 s
NO_REBAL_S = P.imbalance.no_rebalance_after_s_to_settle  # 3 s
MAX_RETRY = P.imbalance.max_retries_per_side    # 5
CEIL = P.imbalance.pair_cost_ceiling_sub1       # 1.0320


def _top(yes_ask=None, no_ask=None, suspect=False) -> TopOfBook:
    d = lambda v: None if v is None else Decimal(str(v))  # noqa: E731
    return TopOfBook(
        yes_bid=None, yes_bid_size=None, yes_ask=d(yes_ask), yes_ask_size=None,
        no_bid=None, no_bid_size=None, no_ask=d(no_ask), no_ask_size=None, suspect=suspect,
    )


# ===========================================================================
# (A) INDEPENDENT ENTRY REFERENCE — derived from the ceremony text.
# ===========================================================================
class RefEntryEngine:
    """A parallel state machine for the entry decision, written from the frozen text."""

    def __init__(self, close_time, high_ticker, low_ticker, quintile, fair_strangle_q, T,
                 *, strangle_disabled=False, shakedown=False):
        self.T = int(T)
        self.high_ticker = high_ticker
        self.low_ticker = low_ticker
        self.quintile = quintile
        self.fair = fair_strangle_q
        self.strangle_disabled = strangle_disabled
        self.shakedown = shakedown
        self.high_top = None
        self.low_top = None
        self.high_ts = None
        self.low_ts = None
        self.entered = False
        self.standdown_emitted = False

    # --- pure cost helpers (ceremony: C from ASKS + audited fee) ---
    @staticmethod
    def _leg(price):
        return None if price is None else price + fee(price)

    def _flip_C(self):
        hi = self._leg(self.high_top.no_ask)
        lo = self._leg(self.low_top.yes_ask)
        return None if hi is None or lo is None else hi + lo

    def _strangle_C(self):
        hi = self._leg(self.high_top.yes_ask)
        lo = self._leg(self.low_top.no_ask)
        return None if hi is None or lo is None else hi + lo

    def _age(self, now, last):
        if last is None:
            return None
        a = now - last
        return None if a < 0 else a  # future ts -> unknown -> stale (fail closed)

    def _both_fresh(self, now):
        if self.high_top is None or self.low_top is None:
            return False
        if self.high_top.suspect or self.low_top.suspect:
            return False
        ha = self._age(now, self.high_ts)
        la = self._age(now, self.low_ts)
        if ha is None or la is None:
            return False
        bound = float(FRESH)
        return ha <= bound and la <= bound

    def on_event(self, event):
        now = event.server_ts
        if isinstance(event, BookUpdate):
            if event.market == self.high_ticker:
                self.high_top, self.high_ts = event.top, now
            elif event.market == self.low_ticker:
                self.low_top, self.low_ts = event.top, now
            else:
                return []  # unrelated leg never moves a decision

        if self.entered:
            return []

        t_minus = self.T - now

        # no orders inside the settle cutoff (falsifier I3: past 1s -> no orders); emit once.
        if t_minus < NO_ORDERS_S:
            if not self.standdown_emitted:
                self.standdown_emitted = True
                return [("STAND_DOWN", None, None, None, t_minus)]
            return []

        # only the final WINDOW_S is an entry window (warmup otherwise).
        if t_minus > WINDOW_S:
            return []

        if not self._both_fresh(now):
            return []

        active = P.sources_for_quintile(self.quintile)
        # priority order: sub-$1 flip FIRST (arithmetic floor wins a same-event tie), then strangle.
        cand = []
        if SUB_DOLLAR_FLIP in active:
            c = self._flip_C()
            if c is not None and c < C_MAX:  # STRICT <
                legs = ((self.high_ticker, "no", 1, self.high_top.no_ask),
                        (self.low_ticker, "yes", 1, self.low_top.yes_ask))
                cand.append((SUB_DOLLAR_FLIP, c, None, legs))
        if Q1_STRANGLE in active and self.quintile == 0 and not self.strangle_disabled:
            c = self._strangle_C()
            if c is not None:
                ev = self.fair - c
                if ev >= EV_MIN:  # inclusive >=
                    legs = ((self.high_ticker, "yes", 1, self.high_top.yes_ask),
                            (self.low_ticker, "no", 1, self.low_top.no_ask))
                    cand.append((Q1_STRANGLE, c, ev, legs))

        if not cand:
            return []
        source, C, ev, legs = cand[0]
        self.entered = True
        kind = "WOULD_FIRE" if self.shakedown else "FIRE"
        return [(kind, source, C, ev, t_minus, legs)]


# --- normalizers so production Actions and ref tuples compare apples-to-apples ---
def _norm_prod(a):
    legs = tuple((lg.ticker, lg.side, lg.count, str(lg.limit_price)) for lg in a.legs)
    return (a.kind, a.source, None if a.C is None else str(a.C),
            None if a.ev is None else str(a.ev), a.t_minus_s, legs)


def _norm_ref(r):
    if r[0] == "STAND_DOWN":
        return (STAND_DOWN, None, None, None, r[4], ())
    kind = FIRE if r[0] == "FIRE" else WOULD_FIRE
    _, source, C, ev, t_minus, legs = r
    nlegs = tuple((t, s, c, str(p)) for (t, s, c, p) in legs)
    return (kind, source, None if C is None else str(C),
            None if ev is None else str(ev), t_minus, nlegs)


# ===========================================================================
# (B) INDEPENDENT IMBALANCE REFERENCE — derived from falsifier I1-I4.
# ===========================================================================
def ref_propose(hi_net, lo_net, cost_so_far, buy_retries, sell_retries, quotes, ceiling, t_minus):
    """Return a normalized proposal tuple from a plain reading of the imbalance bounds."""
    if hi_net == lo_net:
        return ("Balanced",)
    if hi_net < lo_net:
        deficient, overfilled = "high", "low"
    else:
        deficient, overfilled = "low", "high"
    def_ticker = HI if deficient == "high" else LO
    def_side = "no" if deficient == "high" else "yes"   # flip held sides: high=no, low=yes
    over_ticker = HI if overfilled == "high" else LO
    over_side = "no" if overfilled == "high" else "yes"
    hi, lo = hi_net, lo_net
    deficit = abs(hi - lo)
    matched = min(hi, lo)
    over_net = hi if overfilled == "high" else lo

    # I3: inside the no-orders cutoff -> ride to settlement (no order at all).
    if t_minus < NO_ORDERS_S:
        return ("Ride",)

    sell_only = t_minus < NO_REBAL_S  # I3: past 3s, sell-down only

    if not sell_only:
        buy_price = quotes.buy_price(deficient)
        if buy_retries < MAX_RETRY and buy_price is not None:
            projected = deficit * (buy_price + fee(buy_price))
            ceiling_total = ceiling * over_net  # target pairs = the overfilled count
            if cost_so_far + projected <= ceiling_total:  # never buy above the ceiling
                return ("RetryBuy", deficient, def_ticker, def_side, int(deficit), str(buy_price))

    sell_price = quotes.sell_price(overfilled)
    if sell_retries < MAX_RETRY and sell_price is not None:
        sell_count = int(over_net) - int(matched)   # round DOWN to min
        if sell_count > 0:
            return ("SellDown", overfilled, over_ticker, over_side, sell_count, str(sell_price))

    return ("GiveUp", "S2")


def _norm_prop(p):
    if isinstance(p, Balanced):
        return ("Balanced",)
    if isinstance(p, RideToSettlement):
        return ("Ride",)
    if isinstance(p, GiveUp):
        return ("GiveUp", p.stop)
    if isinstance(p, RetryBuy):
        return ("RetryBuy", p.which, p.ticker, p.side, p.count, str(p.limit_price))
    if isinstance(p, SellDown):
        return ("SellDown", p.which, p.ticker, p.side, p.count, str(p.limit_price))
    raise AssertionError(f"unknown proposal {p!r}")


# ===========================================================================
# Boundary-exact book construction (guarantees C==1.00 / EV==0.05 scenarios exist).
# ===========================================================================
def _grid(step=Decimal("0.001")):
    v = Decimal("0.001")
    out = []
    while v <= Decimal("0.999"):
        out.append(v)
        v += step
    return out


_GRID = _grid()


def find_flip_asks(target_C):
    """A (no_ask, yes_ask) pair whose flip-C equals target_C exactly, or None."""
    for na in _GRID:
        rest = target_C - (na + fee(na))
        for ya in _GRID:
            if ya + fee(ya) == rest:
                return na, ya
    return None


def find_strangle_asks(target_C):
    for ya in _GRID:
        rest = target_C - (ya + fee(ya))
        for na in _GRID:
            if na + fee(na) == rest:
                return ya, na
    return None


# ===========================================================================
# DIFFERENTIAL TESTS
# ===========================================================================
def _run_entry_stream(cfg, events):
    """Drive both engines over the same event list; return (prod_actions, ref_actions)."""
    st = WindowState.new(
        cfg["close_time"], HI, LO, cfg["quintile"], cfg["fair"],
        strangle_disabled=cfg["strangle_disabled"], shakedown=cfg["shakedown"], T=cfg["T"],
    )
    ref = RefEntryEngine(
        cfg["close_time"], HI, LO, cfg["quintile"], cfg["fair"], cfg["T"],
        strangle_disabled=cfg["strangle_disabled"], shakedown=cfg["shakedown"],
    )
    prod_out, ref_out = [], []
    for ev in events:
        st, actions = decide(P, st, ev)
        prod_out.extend(_norm_prod(a) for a in actions)
        ref_out.extend(_norm_ref(r) for r in ref.on_event(ev))
    return prod_out, ref_out


def _rand_top(rng, allow_missing=True):
    suspect = rng.random() < 0.12
    ya = None if (allow_missing and rng.random() < 0.15) else rng.choice(_GRID)
    na = None if (allow_missing and rng.random() < 0.15) else rng.choice(_GRID)
    return _top(yes_ask=ya, no_ask=na, suspect=suspect)


def test_entry_differential_1000_plus_randomized():
    rng = random.Random(20260821)
    T = 1_000_000
    n = 0
    for _ in range(1100):
        cfg = {
            "close_time": "2026-06-14T02:00:00Z", "T": T,
            "quintile": rng.randint(0, 4),
            "fair": FAIR_Q0,
            "strangle_disabled": rng.random() < 0.3,
            "shakedown": rng.random() < 0.5,
        }
        events = []
        # seed some warmup + firing-window + cutoff events, ages chosen to straddle 1.0s
        n_ev = rng.randint(2, 8)
        for _i in range(n_ev):
            # t_minus spanning warmup(>900), window, cutoff(<1) and their exact edges
            t_minus = rng.choice([
                rng.uniform(0.0, 2.0), rng.uniform(1.0, 900.0), rng.uniform(899.0, 902.0),
                1.0, float(NO_ORDERS_S), float(WINDOW_S), 900.0, 0.5, 3.0,
            ])
            now = T - t_minus
            leg = rng.choice([HI, LO, "UNRELATED"])
            if rng.random() < 0.25:
                events.append(ClockTick(server_ts=now))
            else:
                events.append(BookUpdate(market=leg, top=_rand_top(rng), server_ts=now))
        prod, ref = _run_entry_stream(cfg, events)
        assert prod == ref, f"ENTRY DIVERGENCE\ncfg={cfg}\nprod={prod}\nref={ref}"
        n += 1
    assert n >= 1000


def test_entry_boundary_C_exactly_one_does_not_fire():
    # STRICT < : C == 1.00 must NOT fire on BOTH sides.
    pair = find_flip_asks(C_MAX)
    assert pair is not None, "no exact C==1.00 grid pair found"
    na, ya = pair
    T = 1_000_000
    now_seed = T - 300.0
    cfg = {"close_time": "x", "T": T, "quintile": 1, "fair": FAIR_Q0,
           "strangle_disabled": True, "shakedown": False}
    ev = [
        BookUpdate(HI, _top(no_ask=na), now_seed),
        BookUpdate(LO, _top(yes_ask=ya), now_seed),
        ClockTick(now_seed),
    ]
    prod, ref = _run_entry_stream(cfg, ev)
    assert prod == ref
    assert all(a[0] != FIRE for a in prod)  # C == max -> no fire


def test_entry_boundary_C_just_below_fires():
    pair = find_flip_asks(C_MAX - Decimal("0.001"))
    assert pair is not None
    na, ya = pair
    T = 1_000_000
    now_seed = T - 300.0
    cfg = {"close_time": "x", "T": T, "quintile": 1, "fair": FAIR_Q0,
           "strangle_disabled": True, "shakedown": False}
    ev = [BookUpdate(HI, _top(no_ask=na), now_seed),
          BookUpdate(LO, _top(yes_ask=ya), now_seed),
          ClockTick(now_seed)]
    prod, ref = _run_entry_stream(cfg, ev)
    assert prod == ref
    assert any(a[0] == FIRE and a[1] == SUB_DOLLAR_FLIP for a in prod)


def test_entry_boundary_ev_exactly_5c_fires_and_just_under_does_not():
    # inclusive >= : EV == 0.05 fires; EV == 0.049 does not. The census FAIR is not grid-aligned,
    # so we hold the asks fixed and set a SYNTHETIC fair to place EV EXACTLY on/under the threshold
    # (fair is an input to both engines; this isolates the EV comparison operator itself).
    ya, na = Decimal("0.40"), Decimal("0.35")
    strangle_C = (ya + fee(ya)) + (na + fee(na))
    T = 1_000_000
    now_seed = T - 300.0
    for fair, should_fire in [(strangle_C + EV_MIN, True),                    # EV == 0.05
                              (strangle_C + EV_MIN - Decimal("0.001"), False)]:  # EV == 0.049
        cfg = {"close_time": "x", "T": T, "quintile": 0, "fair": fair,
               "strangle_disabled": False, "shakedown": False}
        # only the strangle can fire (no flip asks present)
        ev = [BookUpdate(HI, _top(yes_ask=ya), now_seed),
              BookUpdate(LO, _top(no_ask=na), now_seed)]
        prod, ref = _run_entry_stream(cfg, ev)
        assert prod == ref
        fired = any(a[0] == FIRE and a[1] == Q1_STRANGLE for a in prod)
        assert fired is should_fire, f"EV boundary fair={fair}: fired={fired}, want {should_fire}"


def test_entry_boundary_freshness_exactly_1s_and_just_over():
    # age == 1.0s is fresh (fires); age == 1.001s is stale (no fire). Both sides agree.
    # Seed both legs in WARMUP (t_minus 901 > 900 -> no fire at seed), then a ClockTick `age` later
    # lands in-window with BOTH legs aged exactly `age` — isolating the freshness bound.
    pair = find_flip_asks(C_MAX - Decimal("0.002"))
    na, ya = pair
    T = 1_000_000
    cfg = {"close_time": "x", "T": T, "quintile": 1, "fair": FAIR_Q0,
           "strangle_disabled": True, "shakedown": False}
    for age, should_fire in [(1.0, True), (1.001, False)]:
        seed_ts = T - 901.0                  # warmup: no fire at seed
        tick_ts = seed_ts + age              # both legs age == `age`; t_minus == 901 - age (in window)
        ev = [BookUpdate(HI, _top(no_ask=na), seed_ts),
              BookUpdate(LO, _top(yes_ask=ya), seed_ts),
              ClockTick(tick_ts)]
        prod, ref = _run_entry_stream(cfg, ev)
        assert prod == ref
        assert any(a[0] == FIRE for a in prod) is should_fire


def test_entry_boundary_t_minus_exactly_1s_fires_and_just_under_standsdown():
    pair = find_flip_asks(C_MAX - Decimal("0.002"))
    na, ya = pair
    T = 1_000_000
    cfg = {"close_time": "x", "T": T, "quintile": 1, "fair": FAIR_Q0,
           "strangle_disabled": True, "shakedown": False}
    for t_minus, expect_fire in [(1.0, True), (0.999, False)]:
        now = T - t_minus
        # seed BOTH legs at exactly the boundary instant (no earlier in-window event to fire first):
        # HI update alone -> not both-fresh -> no action; LO update -> both fresh at t_minus.
        ev = [BookUpdate(HI, _top(no_ask=na), now),
              BookUpdate(LO, _top(yes_ask=ya), now)]
        prod, ref = _run_entry_stream(cfg, ev)
        assert prod == ref
        assert any(a[0] == FIRE for a in prod) is expect_fire


# --------------------------------------------------------------------------- #
# Imbalance differential                                                       #
# --------------------------------------------------------------------------- #
def _resp(cid, fill, price, fee_amt):
    return OrderResponse(cid, "o", Decimal(str(fill)), Decimal(0),
                         Decimal(str(price)), Decimal(str(fee_amt)), 1)


def _build_state(hi_fill, lo_fill, buy_retries, sell_retries, hi_price="0.57", lo_price="0.24"):
    cnt = max(hi_fill, lo_fill, 1)
    legs = (IntentLeg(HI, "no", "buy", cnt, Decimal(hi_price), "h"),
            IntentLeg(LO, "yes", "buy", cnt, Decimal(lo_price), "l"))
    st = record_intent(new_ledger("W", SUB_DOLLAR_FLIP, HI, LO),
                       Intent("W", SUB_DOLLAR_FLIP, "entry", legs))
    st = record_response(st, _resp("h", hi_fill, hi_price, "0.01") if hi_fill > 0
                         else no_fill_response("h", "x"))
    st = record_response(st, _resp("l", lo_fill, lo_price, "0.01") if lo_fill > 0
                         else no_fill_response("l", "x"))
    # deficient leg gets the recorded rebalance-buy intents; overfilled gets sell intents
    if hi_fill < lo_fill:
        def_t, over_t, def_s, over_s = HI, LO, "no", "yes"
    else:
        def_t, over_t, def_s, over_s = LO, HI, "yes", "no"
    for i in range(buy_retries):
        st = record_intent(st, Intent("W", SUB_DOLLAR_FLIP, PURPOSE_REBALANCE_BUY,
                           (IntentLeg(def_t, def_s, "buy", 1, Decimal("0.20"), f"rb{i}"),)))
    for i in range(sell_retries):
        st = record_intent(st, Intent("W", SUB_DOLLAR_FLIP, PURPOSE_REBALANCE_SELL,
                           (IntentLeg(over_t, over_s, "sell", 1, Decimal("0.50"), f"rs{i}"),)))
    return st


def test_imbalance_differential_1000_plus_randomized():
    rng = random.Random(994001)
    n = 0
    for _ in range(1200):
        hi_fill = rng.randint(0, 3)
        lo_fill = rng.randint(0, 3)
        buy_r = rng.randint(0, 6)
        sell_r = rng.randint(0, 6)
        # quotes: sometimes absent, prices straddle the ceiling
        bq = None if rng.random() < 0.2 else rng.choice([Decimal("0.05"), Decimal("0.20"),
                                                         Decimal("0.24"), Decimal("0.45"),
                                                         Decimal("0.60"), Decimal("0.90")])
        sq = None if rng.random() < 0.2 else rng.choice([Decimal("0.10"), Decimal("0.50"),
                                                         Decimal("0.55")])
        # which leg is deficient determines which quote slot is the buy/sell one
        if hi_fill < lo_fill:
            quotes = RebalanceQuotes(high_buy=bq, low_sell=sq)
        elif lo_fill < hi_fill:
            quotes = RebalanceQuotes(low_buy=bq, high_sell=sq)
        else:
            quotes = RebalanceQuotes(high_buy=bq, low_buy=bq, high_sell=sq, low_sell=sq)
        t_minus = rng.choice([rng.uniform(0.0, 2.0), rng.uniform(2.5, 400.0),
                              0.999, 1.0, 3.0, 2.999, float(NO_REBAL_S)])
        ceiling = rng.choice([CEIL, Decimal("1.00"), Decimal("0.80"), Decimal("1.20")])

        st = _build_state(hi_fill, lo_fill, buy_r, sell_r)
        prod = _norm_prop(propose_rebalance(st, P, t_minus, quotes, ceiling))
        ref = ref_propose(
            st.net("high"), st.net("low"), st.pair_net_cash_out(),
            buy_r, sell_r, quotes, ceiling, t_minus,
        )
        assert prod == ref, (
            f"IMBALANCE DIVERGENCE\nhi={hi_fill} lo={lo_fill} buy_r={buy_r} sell_r={sell_r} "
            f"bq={bq} sq={sq} t={t_minus} ceil={ceiling}\nprod={prod}\nref={ref}"
        )
        n += 1
    assert n >= 1000


def test_imbalance_boundary_ceiling_exact_buys_and_just_over_sells():
    # cost_so_far + projected == ceiling_total -> buys (<=); a hair over -> sell-down.
    # The ceiling is a free Decimal parameter, so we place the boundary EXACTLY by choosing the
    # buy price then setting ceiling_per_pair = (cost + projected)/over_net (isolates the operator).
    st = _build_state(1, 0, 0, 0)  # low deficient, high overfilled net=1
    cost = st.pair_net_cash_out()  # 0.57 + 0.01 = 0.58
    over_net = st.net("high")      # 1
    bp = Decimal("0.24")
    projected = Decimal(1) * (bp + fee(bp))
    exact_ceiling = (cost + projected) / over_net
    q = RebalanceQuotes(low_buy=bp, high_sell=Decimal("0.55"))

    # exactly at the ceiling -> buys
    prod = _norm_prop(propose_rebalance(st, P, 300, q, exact_ceiling))
    ref = ref_propose(st.net("high"), st.net("low"), cost, 0, 0, q, exact_ceiling, 300)
    assert prod == ref and prod[0] == "RetryBuy"

    # a hair under the ceiling -> cost+projected now EXCEEDS it -> sell-down (never buys above)
    tight = exact_ceiling - Decimal("0.0001")
    prod2 = _norm_prop(propose_rebalance(st, P, 300, q, tight))
    ref2 = ref_propose(st.net("high"), st.net("low"), cost, 0, 0, q, tight, 300)
    assert prod2 == ref2 and prod2[0] == "SellDown"


def test_imbalance_boundary_retry_count_5_blocks_buy():
    st = _build_state(0, 1, MAX_RETRY, 0)  # high deficient, 5 buy retries already
    q = RebalanceQuotes(high_buy=Decimal("0.20"), low_sell=Decimal("0.50"))
    prod = _norm_prop(propose_rebalance(st, P, 300, q, CEIL))
    ref = ref_propose(st.net("high"), st.net("low"), st.pair_net_cash_out(),
                      MAX_RETRY, 0, q, CEIL, 300)
    assert prod == ref
    assert prod[0] == "SellDown"  # buys exhausted at 5 -> sell-down


def test_imbalance_boundary_t_minus_edges():
    st = _build_state(1, 0, 0, 0)
    q = RebalanceQuotes(low_buy=Decimal("0.24"), high_sell=Decimal("0.55"))
    for t_minus, want in [(0.999, "Ride"), (1.0, "SellDown"), (2.999, "SellDown"),
                          (3.0, "RetryBuy")]:
        prod = _norm_prop(propose_rebalance(st, P, t_minus, q, CEIL))
        ref = ref_propose(st.net("high"), st.net("low"), st.pair_net_cash_out(),
                          0, 0, q, CEIL, t_minus)
        assert prod == ref
        assert prod[0] == want, f"t-{t_minus}: {prod[0]} != {want}"


def test_imbalance_selldown_rounds_down():
    st = _build_state(3, 1, MAX_RETRY, 0)  # high 3, low 1; buys exhausted -> sell high down to 1
    q = RebalanceQuotes(high_sell=Decimal("0.55"))
    prod = _norm_prop(propose_rebalance(st, P, 300, q, CEIL))
    ref = ref_propose(st.net("high"), st.net("low"), st.pair_net_cash_out(),
                      MAX_RETRY, 0, q, CEIL, 300)
    assert prod == ref
    assert prod[0] == "SellDown" and prod[4] == 2  # 3 -> 1 (round down to min)


def test_imbalance_giveup_is_s2():
    st = _build_state(1, 0, MAX_RETRY, 0)  # buys exhausted, no sell quote -> GiveUp S2
    q = RebalanceQuotes(low_buy=Decimal("0.24"))  # no high_sell
    prod = _norm_prop(propose_rebalance(st, P, 300, q, CEIL))
    ref = ref_propose(st.net("high"), st.net("low"), st.pair_net_cash_out(),
                      MAX_RETRY, 0, q, CEIL, 300)
    assert prod == ref
    assert prod == ("GiveUp", "S2")
