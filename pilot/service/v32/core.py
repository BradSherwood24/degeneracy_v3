"""core.py — the PURE decision core for V3.2 (continuous-requote spot-bucket pump-fader).

House law (same discipline as ``signal.py`` / ``box.py``): all strategy logic lives here and in pure
helpers. No clock reads, no network, no disk, no globals. Time comes ONLY from event timestamps, so
``decide_v32`` runs live and in replay bit-identically. Money is Decimal; floats appear only for
timestamps/ages. State is frozen dataclasses transitioned with ``dataclasses.replace``.

WHAT IT REPRODUCES — the forward sim (scratch ``pf_ms_requote.py`` ideal + ``pf_ms_requote2.py``
lagging-executor model), mapped onto a LIVE order feed. Per hourly close T, in the quoting window
T-``quote_start_s`` .. T-``quote_end_s`` (T-15..T-5):

  * Spot bucket B = [Sd, Sd+bucket_width): the range bucket with the highest YES mid among buckets
    with a valid two-sided book, REGARDLESS OF AGE (ruling R-STALE-SPOT). The freshness gate is on the
    SELECTED spot: if its OWN book is older than bucket_freshness_max_age_s (a SEPARATE, larger bound
    than the 1.0 s strike gate; range buckets are thin and tick far less often), the hour stands down
    (reason ``stale_bucket``) — it NEVER falls through to a fresh lower-mid bucket, because the edge is
    spot-bucket-only (a non-spot pump is the wrong trade, not a lesser one). A bucket-connection stall
    (or a partial blackout masked by the liquid co-listed 15M sharing that connection) thus cannot feed
    a quote off a stale spot cap while the strike connection looks alive.
  * Pin wings priced as a taker from the hourly-strike books:
        W = yes_ask(Sd) + fee(yes_ask(Sd)) + no_ask(Su) + fee(no_ask(Su)),   Su = Sd + bucket_width,
    where no_ask(Su) = 1 - yes_bid(Su) (book identity). fee = the audited census fee (IMPORTED). Both
    strike books must be present AND fresh (age <= freshness_max_age_s) else no quote.
  * Rest one bucket-NO bid (post-only; maker fee 0) at the largest whole-cent n with
        n + fee(n) <= 2 - E - W,   capped at no_ask(B) - 0.01 = (1 - yes_bid(B)) - 0.01  (never cross).
    Re-solve n on every strike/bucket book tick; REPLACE per the requote gate (tol / deb_ms).
    Replace = cancel -> confirm -> create; never two live rests (R-OVERLAP ruling 2026-09-13).
  * Fill of the rest (Fill event, or OrderCancelled with filled_count_before_cancel > 0): immediately
    TAKE both wings (buy YES@Sd, buy NO@Su, taker, limit = ask + wing_margin) for the filled count,
    UNCONDITIONALLY (ruling F-2 — the fill happened, so we bound the position to the $2 pin). A
    position of all three legs pays $2 at every settlement. lock = 2 - (n + fee(n)) - W_paid.
    lock_floor gates ONLY the RETRY of a single missing leg after an IOC no-fill (the two held legs
    are then a $1 floor, so a deferred retry is bounded, not naked). One completed set/hour.
  * Shadow (every E in shadow_Es, regardless of mode): re-solve n_shadow(E) each book tick with NO
    lag and NO requote gate; a spot-bucket YES trade strictly above 1 - n_shadow records a shadow
    fill (once per hour per E), but ONLY on prints inside the live quoting window T-15..T-5 (the same
    window gate the live path enforces, on the same eval clock) — a qualifying print outside the
    window emits SHADOW_FILL_OUTSIDE_WINDOW and never fills, since the shadow is the no-lag
    counterfactual of a LIVE fill and live cannot quote outside the window; shadow completion = wing
    asks at the trade tick (both strikes fresh)
    or the next strike book update; shadow lock = 2 - (n_shadow + fee) - W_at_completion. Emits no
    actions — this IS the ideal fill rule running live, so a dry run yields the sim statistic.

Windowing / plumbing: quote only inside the window; CANCEL (no new PLACE) at/after T-quote_end_s;
never two live rests — a replace is STRICTLY SEQUENTIAL (cancel -> wait for OrderCancelled -> create at
the freshly re-solved n on a later tick; a fill during the cancel -> TAKE_WINGS, not PLACE), exactly like
the bucket-change path; hold while a PLACE is pending (awaiting ack); no orders inside
no_orders_after_s_to_settle; a stale/missing strike book cancels the rest; replaces in a trailing
60 s above replace_rate_alarm_per_min cancel the rest and stand the hour down; shakedown -> WOULD_*.

NAMED Phase-1 deviations (see pilot/build/v32_phase1_build_report.md CONFESSIONS): (1) the shadow is
a *no-lag* state machine and cannot reproduce ``pf_ms_requote.py``'s explicit -1 s n-solve offset
(the ideal's 0.43/0.57 became 0.45/0.55 when the wings moved W in the final second); (2) bucket
change waits for OrderCancelled before placing (the safe reading of the spec) whereas the sim swaps
on the forced tick; (3) the fee is the exact Decimal census fee, which differs from the scratch
float lambda by <= $0.0001 at six whole cents (below the cent).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from decimal import ROUND_CEILING, Decimal

from service._simlaw import fee

from service.book import TopOfBook
from service.v32.actions import ActionKind, LegOrder, V32Action, twin_kind
from service.v32.events import (
    BookUpdate,
    ClockTick,
    Fill,
    OrderAck,
    OrderCancelled,
    Trade,
    classify_ticker,
)
from service.v32.params import V32Params

# --- money constants ---
_ZERO = Decimal(0)
_ONE = Decimal(1)
_TWO = Decimal(2)
_CENT = Decimal("0.01")
_LIMIT_CEILING = Decimal("0.99")

# leg sides (Kalshi YES-perspective outcome we BUY on that leg)
BUY_YES = "yes"
BUY_NO = "no"


# ===========================================================================
# Pure money primitives
# ===========================================================================
def _q_cent(x: Decimal) -> Decimal:
    """Quantize to whole cents (2 dp), rounding toward +inf (a safe-high start for solve_n)."""
    return x.quantize(_CENT, rounding=ROUND_CEILING)


def solve_n(budget: Decimal | None, cap: Decimal | None) -> Decimal | None:
    """Largest whole-cent ``n`` with ``n + fee(n) <= budget`` and ``n <= cap`` (else None).

    Reproduces the sim ``solve_n`` (``pf_ms_requote*.py``) in exact Decimal: start at
    min(ceil_to_cent(budget), cap) — guaranteed >= the true maximum since n <= n+fee(n) <= budget —
    then decrement one cent while the fee-inclusive cost exceeds the budget. fee is the imported
    census fee (never retyped). None if no cent >= $0.01 qualifies.
    """
    if budget is None or cap is None:
        return None
    if budget < _CENT or cap < _CENT:
        return None
    n = min(_q_cent(budget), _q_cent(cap))
    while n >= _CENT and n + fee(n) > budget:
        n = n - _CENT
    return n if n >= _CENT else None


def wing_cost(yes_ask_sd: Decimal, no_ask_su: Decimal) -> Decimal:
    """W = yes_ask(Sd) + fee(yes_ask(Sd)) + no_ask(Su) + fee(no_ask(Su)) (both taker legs)."""
    return yes_ask_sd + fee(yes_ask_sd) + no_ask_su + fee(no_ask_su)


def lock_value(n: Decimal, w_paid: Decimal) -> Decimal:
    """lock = 2 - (n + fee(n)) - W_paid per contract.

    NB the ``fee(n)`` here is a TAKER fee charged on the resting (maker) leg. On Kalshi crypto the
    maker fee is 0 (see MEMORY kalshi-fee-exact), so the REALIZED lock is ~fee(n) (~1.7c at n=0.45)
    HIGHER than this value. We keep the fee to stay bit-identical to the pinned sim (pf_ms_requote*)
    whose reference locks (+10.36c / +12.38c) the golden test asserts; it is the conservative
    (pessimistic) direction, so the live edge is understated, never overstated. See the Phase-1
    review's money-math finding."""
    return _TWO - (n + fee(n)) - w_paid


# ===========================================================================
# Records carried in state
# ===========================================================================
@dataclass(frozen=True)
class RestOrder:
    """One resting bucket-NO order. ``live`` = acked & fillable; ``pending`` = placed, awaiting ack
    (not yet fillable). ``price`` is the whole-cent n (dollars)."""

    client_order_id: str
    order_id: str | None
    price: Decimal
    count: int
    placed_ts: float
    live: bool
    pending: bool
    bucket_Sd: int


@dataclass(frozen=True)
class RestFill:
    """A fill of our resting bucket-NO order (n dollars, count contracts)."""

    price: Decimal
    count: int
    server_ts: float


@dataclass(frozen=True)
class WingLeg:
    """One taker completion leg. ``status`` in {"pending","filled","unfilled"}."""

    ticker: str
    side: str
    count: int
    limit: Decimal
    client_order_id: str
    status: str = "pending"
    fill_price: Decimal | None = None
    fill_fee: Decimal | None = None


@dataclass(frozen=True)
class ShadowFill:
    """A shadow fill for one E (the ideal fill rule running live). ``lock`` is None until completed."""

    E: Decimal
    n: Decimal
    offer: Decimal
    print_price: Decimal
    count: Decimal
    server_ts: float
    W_at_completion: Decimal | None = None
    lock: Decimal | None = None


@dataclass(frozen=True)
class ShadowSub:
    """Per-E shadow sub-state. ``n`` is the continuously re-solved (no-lag) desired n."""

    E: Decimal
    n: Decimal | None = None
    filled: bool = False
    fill: ShadowFill | None = None
    awaiting_completion: bool = False


# ===========================================================================
# The window state (immutable; decide_v32 returns a NEW state via replace)
# ===========================================================================
@dataclass(frozen=True)
class V32State:
    close_time: str
    close_epoch: int
    bucket_map: Mapping[str, tuple[float, float]]      # ticker -> (floor, cap), static (discovery)
    shakedown: bool = False

    # live book state
    strike_tops: Mapping[int, TopOfBook] = field(default_factory=dict)
    strike_ts: Mapping[int, float] = field(default_factory=dict)
    strike_tickers: Mapping[int, str] = field(default_factory=dict)
    bucket_tops: Mapping[int, TopOfBook] = field(default_factory=dict)
    bucket_ts: Mapping[int, float] = field(default_factory=dict)
    bucket_tickers: Mapping[int, str] = field(default_factory=dict)

    # current quote context (recomputed each book tick)
    spot_Sd: int | None = None
    spot_Su: int | None = None
    W: Decimal | None = None
    cap: Decimal | None = None
    desired_n: Decimal | None = None
    spot_bucket_stale: bool = False                     # selected spot's own book is stale (R-STALE-SPOT)

    # resting-order lifecycle
    rest_live: RestOrder | None = None
    rest_pending: RestOrder | None = None
    cancel_in_flight: bool = False
    awaiting_replace: bool = False                      # bucket change: cancelled old, place after cxl
    rest_bucket_Sd: int | None = None
    last_replace_ts: float | None = None
    replace_count: int = 0
    replace_times: tuple[float, ...] = ()
    coid_seq: int = 0

    # completion / wings
    rest_fill: RestFill | None = None
    wings_needed: bool = False
    wing_taken: bool = False
    wing_legs: tuple[WingLeg, ...] = ()
    sets_done: int = 0
    one_legged: bool = False

    # shadow (keyed by str(E))
    shadows: Mapping[str, ShadowSub] = field(default_factory=dict)

    # stand-down / dedupe
    stood_down: bool = False
    stand_down_reason: str | None = None
    last_standdown_reason: str | None = None

    @classmethod
    def new(
        cls,
        close_time: str,
        close_epoch: int,
        bucket_map: Mapping[str, tuple[float, float]],
        params: V32Params,
        *,
        shakedown: bool = False,
    ) -> "V32State":
        shadows = {str(E): ShadowSub(E=E) for E in params.shadow_Es}
        return cls(
            close_time=close_time,
            close_epoch=int(close_epoch),
            bucket_map=dict(bucket_map),
            shakedown=shakedown,
            shadows=shadows,
        )


# ===========================================================================
# Small pure helpers
# ===========================================================================
def _valid_two_sided(top: TopOfBook | None) -> bool:
    """A bucket book usable for spot selection: both YES quotes present, 0 < ask <= 1, 0 <= bid <= ask."""
    if top is None or top.suspect:
        return False
    if top.yes_ask is None or top.yes_bid is None:
        return False
    return _ZERO < top.yes_ask <= _ONE and _ZERO <= top.yes_bid <= top.yes_ask


def _fresh(now: float, last_ts: float | None, bound: float) -> bool:
    """A book is fresh iff its age is known and within ``[-bound, bound]``.

    LAW (clock-interleave tolerance, 2026-09-14): the two WS connections (strikes KXBTCD vs buckets
    KXBTC+15M) carry INDEPENDENT server clocks that interleave. A book folded from one connection can
    be stamped with a ts up to a few tens of ms AHEAD of the evaluation clock ``now`` (which is driven
    off the other connection's frame) -- a NEGATIVE age. That is clock skew, NOT staleness, so a book
    up to ``bound`` ahead is fresh. Without this tolerance the age flips negative for a genuine book,
    ``_compute_W``/``_wing_prices`` return None, and ``_requote`` cancels + re-places every ~1 ms
    (the 2026-09-14T17:00:00Z live-dry flap: 61 place/cancel pairs in 5 s -> replace-rate alarm ->
    the hour quoted nothing). The driver ALSO folds these onto a MONOTONE evaluation clock
    (``run_v32.V32Driver.on_book_update``), so negative ages should not arise on the book path at all;
    this bound is the belt-and-braces layer that also covers the private-order paths (Fill/Cancel),
    whose ``now`` is the order channel's own ts and can trail the book clock. A book more than ``bound``
    STALE (positive age > bound) is still stale -- genuine staleness detection is unchanged."""
    if last_ts is None:
        return False
    age = now - last_ts
    return -bound <= age <= bound


def _select_spot(st: V32State) -> int | None:
    """Highest-YES-mid bucket among valid two-sided books, REGARDLESS OF AGE. Ties -> lowest floor.

    Ruling R-STALE-SPOT: spot selection runs over ALL two-sided bucket books, stale or not — the
    highest-YES-mid bucket IS the spot bucket. The freshness gate is applied to the SELECTED spot
    (``_recompute_context`` -> ``spot_bucket_stale``), NEVER by excluding stale buckets from
    selection: the strategy's edge is spot-bucket-only (the 2026-09-01 range scan found the OTM-bucket
    pumps are the informed ones — all-buckets pays far less than spot-only), so resting on a fresh
    lower-mid bucket because the spot book went quiet is the WRONG trade, not a lesser one. A stale
    spot stands the hour down instead."""
    best_floor: int | None = None
    best_mid: Decimal | None = None
    for floor in sorted(st.bucket_tops):
        top = st.bucket_tops[floor]
        if not _valid_two_sided(top):
            continue
        mid = (top.yes_bid + top.yes_ask) / _TWO  # type: ignore[operator]
        if best_mid is None or mid > best_mid:
            best_mid = mid
            best_floor = floor
    return best_floor


def _compute_W(st: V32State, Sd: int, Su: int, now: float, params: V32Params) -> Decimal | None:
    """W from the fresh strike books at Sd/Su, or None if either is stale/missing/invalid."""
    sd_top = st.strike_tops.get(Sd)
    su_top = st.strike_tops.get(Su)
    if sd_top is None or su_top is None:
        return None
    if sd_top.suspect or su_top.suspect:
        return None  # a suspect (malformed-delta / seq-gap) book is untrustworthy: no quote
    if not _fresh(now, st.strike_ts.get(Sd), params.freshness_max_age_s):
        return None
    if not _fresh(now, st.strike_ts.get(Su), params.freshness_max_age_s):
        return None
    ya = sd_top.yes_ask
    na = su_top.no_ask
    if ya is None or na is None:
        return None
    if not (_ZERO < ya < _ONE) or not (_ZERO < na < _ONE):
        return None
    return wing_cost(ya, na)


def _bucket_cap(st: V32State, Sd: int) -> Decimal | None:
    """cap = no_ask(B) - 0.01 = (1 - yes_bid(B)) - 0.01, whole cents; None if bucket invalid.

    Freshness is enforced by the caller: ``_recompute_context`` only computes the cap when the selected
    spot's own book is fresh (``not spot_bucket_stale``, ruling R-STALE-SPOT) — so the cap is never
    read off a stale bucket book."""
    top = st.bucket_tops.get(Sd)
    if not _valid_two_sided(top):
        return None
    return ((_ONE - top.yes_bid) - _CENT).quantize(_CENT)  # type: ignore[union-attr]


def _mk(kind: ActionKind, shakedown: bool, **fields) -> V32Action:
    """Build a V32Action, downgrading order-emitting kinds to their WOULD_* twin in shakedown."""
    return V32Action(kind=twin_kind(kind, shakedown), **fields)


def _mint_coid(st: V32State) -> tuple[str, V32State]:
    seq = st.coid_seq + 1
    coid = f"v32-{st.close_time}-{seq}"
    return coid, replace(st, coid_seq=seq)


# ===========================================================================
# The decision
# ===========================================================================
def decide_v32(
    params: V32Params, state: V32State, event
) -> tuple[V32State, list[V32Action]]:
    """Pure decision. Returns (new_state, actions). See module docstring for the full law."""
    now = event.server_ts
    st = state
    actions: list[V32Action] = []

    if isinstance(event, BookUpdate):
        st = _fold_book(st, event)
        st = _recompute_context(params, st, now)
        st, sc = _shadow_complete(params, st, now)
        st, wa = _wing_step(params, st, now)
        st, qa = _requote(params, st, now)
        actions += wa + qa
        return st, actions

    if isinstance(event, Trade):
        st, ta = _shadow_on_trade(params, st, event)
        st, sc = _shadow_complete(params, st, now)
        actions += ta
        return st, actions

    if isinstance(event, OrderAck):
        st = _apply_ack(st, event)
        return st, actions

    if isinstance(event, OrderCancelled):
        st, ca = _apply_cancelled(params, st, event, now)
        actions += ca
        return st, actions

    if isinstance(event, Fill):
        st, fa = _apply_fill(params, st, event, now)
        actions += fa
        return st, actions

    if isinstance(event, ClockTick):
        # Re-derive the quote context against the tick's clock so a SILENT feed (books stop
        # arriving) is detected: a strike book that has gone stale relative to ``now`` makes W
        # None, which the requote gate turns into CANCEL_REST + stand down. Without this, a
        # stale-then-silent feed would leave a rest live until the next book frame (or expiry).
        st = _recompute_context(params, st, now)
        st, sc = _shadow_complete(params, st, now)
        st, wa = _wing_step(params, st, now)
        st, qa = _requote(params, st, now)
        actions += wa + qa
        return st, actions

    return st, actions


# ---------------------------------------------------------------------------
# Book fold + context
# ---------------------------------------------------------------------------
def _fold_book(st: V32State, event: BookUpdate) -> V32State:
    cls = classify_ticker(event.market_ticker, st.bucket_map)
    if cls is None:
        return st
    kind, floor = cls
    # The RECORDED book age anchor is the FRAME'S OWN ts (``book_ts``), NOT the monotone evaluation
    # clock (``server_ts``): a genuinely stalled feed must still age its book out even while the
    # monotone clock advances off the other connection. Falls back to ``server_ts`` when ``book_ts``
    # is absent (direct-constructed events: unit tests, the golden harness) — the pre-fix behavior.
    book_ts = event.book_ts if event.book_ts is not None else event.server_ts
    if kind == "strike":
        strike_tops = dict(st.strike_tops)
        strike_ts = dict(st.strike_ts)
        strike_tickers = dict(st.strike_tickers)
        strike_tops[floor] = event.top
        strike_ts[floor] = book_ts
        strike_tickers[floor] = event.market_ticker
        return replace(st, strike_tops=strike_tops, strike_ts=strike_ts, strike_tickers=strike_tickers)
    else:
        bucket_tops = dict(st.bucket_tops)
        bucket_ts = dict(st.bucket_ts)
        bucket_tickers = dict(st.bucket_tickers)
        bucket_tops[floor] = event.top
        bucket_ts[floor] = book_ts
        bucket_tickers[floor] = event.market_ticker
        return replace(st, bucket_tops=bucket_tops, bucket_ts=bucket_ts, bucket_tickers=bucket_tickers)


def _recompute_context(params: V32Params, st: V32State, now: float) -> V32State:
    """Re-derive spot bucket, W, cap, desired n, and every shadow n (no-lag) from current books."""
    spot_Sd = _select_spot(st)
    spot_Su = spot_Sd + params.bucket_width if spot_Sd is not None else None
    # R-STALE-SPOT: the selected spot bucket's OWN book must be fresh; a stale spot stands the hour
    # down (never falls through to a lower-mid bucket). W/cap/desired_n and every shadow n are only
    # solved when the spot is present AND fresh, so a stale spot emits no quote and no shadow fill.
    spot_bucket_stale = spot_Sd is not None and not _fresh(
        now, st.bucket_ts.get(spot_Sd), params.bucket_freshness_max_age_s
    )
    W = None
    cap = None
    desired_n = None
    if spot_Sd is not None and spot_Su is not None and not spot_bucket_stale:
        W = _compute_W(st, spot_Sd, spot_Su, now, params)
        cap = _bucket_cap(st, spot_Sd)
        budget = (_TWO - params.E - W) if W is not None else None
        desired_n = solve_n(budget, cap)

    # shadow n per E (no lag / no gate) — same W, cap
    shadows = dict(st.shadows)
    for key, sub in shadows.items():
        if W is not None and cap is not None:
            n_sh = solve_n(_TWO - sub.E - W, cap)
        else:
            n_sh = None
        shadows[key] = replace(sub, n=n_sh)

    return replace(
        st, spot_Sd=spot_Sd, spot_Su=spot_Su, W=W, cap=cap, desired_n=desired_n,
        spot_bucket_stale=spot_bucket_stale, shadows=shadows,
    )


# ---------------------------------------------------------------------------
# Order lifecycle events
# ---------------------------------------------------------------------------
def _apply_ack(st: V32State, event: OrderAck) -> V32State:
    """A pending create is acked -> it becomes the live rest (swapping out any prior live)."""
    p = st.rest_pending
    if p is None or p.client_order_id != event.client_order_id:
        return st
    live = replace(p, order_id=event.order_id, live=True, pending=False)
    return replace(st, rest_live=live, rest_pending=None, cancel_in_flight=False)


def _apply_cancelled(
    params: V32Params, st: V32State, event: OrderCancelled, now: float
) -> tuple[V32State, list[V32Action]]:
    """OrderCancelled confirms a cancel. A partial fill before cancel is treated as a rest fill."""
    actions: list[V32Action] = []
    # clear the matching order slot, remembering the order so a partial fill is booked at ITS
    # price (not the current desired_n, which may have drifted since the order was placed).
    matched = False
    matched_order: RestOrder | None = None
    if st.rest_live is not None and st.rest_live.order_id == event.order_id:
        matched_order = st.rest_live
        st = replace(st, rest_live=None)
        matched = True
    elif st.rest_pending is not None and st.rest_pending.order_id == event.order_id:
        matched_order = st.rest_pending
        st = replace(st, rest_pending=None)
        matched = True
    # A cancel confirm clears the in-flight flag whether or not the slot is still populated (on a
    # bucket change we clear rest_live at cancel-request time, so this OrderCancelled won't match a
    # slot but still confirms the outstanding cancel).
    if matched or st.cancel_in_flight:
        st = replace(st, cancel_in_flight=False)
    if event.filled_count_before_cancel > _ZERO and st.rest_fill is None:
        # a fill slipped in before the cancel landed -> book it at the CANCELLED order's price
        # and take wings. (When the slot was eagerly cleared before the cancel confirm — a bucket
        # change or a stand-down cancel — matched_order is None and we fall back to desired_n;
        # see the Phase-1 review's retained-cancel-context finding.)
        if matched_order is not None:
            n = matched_order.price
        elif st.desired_n is not None:
            n = st.desired_n
        else:
            n = _ZERO
        st = replace(
            st,
            rest_fill=RestFill(price=n, count=int(event.filled_count_before_cancel), server_ts=now),
            wings_needed=True,
            wing_taken=False,
        )
        st, wa = _wing_step(params, st, now)
        actions += wa
    return st, actions


def _apply_fill(
    params: V32Params, st: V32State, event: Fill, now: float
) -> tuple[V32State, list[V32Action]]:
    """A private fill. Wing-leg fills update leg status; a rest fill triggers the completion."""
    actions: list[V32Action] = []
    # wing-leg fill?
    for i, leg in enumerate(st.wing_legs):
        if leg.client_order_id == event.client_order_id:
            legs = list(st.wing_legs)
            if event.count > _ZERO:
                legs[i] = replace(
                    leg, status="filled", fill_price=event.price, fill_fee=fee(event.price)
                )
            else:
                legs[i] = replace(leg, status="unfilled")
            st = replace(st, wing_legs=tuple(legs))
            st = _maybe_close_set(st)
            return st, actions
    # rest fill?
    is_ours = (st.rest_live is not None and st.rest_live.client_order_id == event.client_order_id) or (
        st.rest_pending is not None and st.rest_pending.client_order_id == event.client_order_id
    )
    if is_ours and st.rest_fill is None:
        n = event.price if event.price is not None else (
            st.rest_live.price if st.rest_live is not None else st.desired_n
        )
        st = replace(
            st,
            rest_fill=RestFill(price=n, count=int(event.count), server_ts=now),
            rest_live=None,
            rest_pending=None,
            cancel_in_flight=False,
            wings_needed=True,
            wing_taken=False,
        )
        st, wa = _wing_step(params, st, now)
        actions += wa
    return st, actions


# ---------------------------------------------------------------------------
# Wings (completion / lock floor / per-leg retry)
# ---------------------------------------------------------------------------
def _wing_prices(st: V32State, now: float, params: V32Params) -> tuple[Decimal, Decimal] | None:
    """(yes_ask(Sd), no_ask(Su)) if both strike books are present & fresh & valid, else None."""
    if st.spot_Sd is None or st.spot_Su is None:
        return None
    sd = st.strike_tops.get(st.spot_Sd)
    su = st.strike_tops.get(st.spot_Su)
    if sd is None or su is None:
        return None
    if sd.suspect or su.suspect:
        return None  # never price a taker completion off a suspect (untrustworthy) book
    if not _fresh(now, st.strike_ts.get(st.spot_Sd), params.freshness_max_age_s):
        return None
    if not _fresh(now, st.strike_ts.get(st.spot_Su), params.freshness_max_age_s):
        return None
    if sd.yes_ask is None or su.no_ask is None:
        return None
    if not (_ZERO < sd.yes_ask <= _ONE) or not (_ZERO < su.no_ask <= _ONE):
        return None
    return sd.yes_ask, su.no_ask


def _wing_step(params: V32Params, st: V32State, now: float) -> tuple[V32State, list[V32Action]]:
    """Take (or retry) the wings after a rest fill.

    The INITIAL both-wings take is UNCONDITIONAL (ruling F-2): once the bucket-NO fills we assemble
    the $2 pin regardless of lock_floor. lock_floor gates ONLY the RETRY of a single missing leg
    after an IOC no-fill — at which point the two already-held legs are a $1 floor, so we never
    overpay for the last leg. Both branches honor the settle cutoff (no_orders_after_s_to_settle)."""
    actions: list[V32Action] = []
    if not st.wings_needed:
        return st, actions
    t_to_close = st.close_epoch - now
    if t_to_close < params.no_orders_after_s_to_settle:
        # cutoff: a rest filled but the $2 pin never completed -> a bounded, unhedged set. Flag it
        # one_legged (drives S1_LEGGED). This covers BOTH the wings-taken-but-a-leg-missed case AND
        # the wings-NEVER-taken case (strike feed dead from fill to deadline = a lone bucket-NO), which
        # the earlier ``st.wing_taken and ...`` guard silently missed — leaving the worst unhedged case
        # unflagged and uncounted toward the day latch.
        if st.rest_fill is not None and st.wings_needed:
            incomplete = (not st.wing_legs) or any(l.status != "filled" for l in st.wing_legs)
            if incomplete:
                st = replace(st, one_legged=True)
        return st, actions

    prices = _wing_prices(st, now, params)
    if prices is None:
        return st, actions
    ya, na = prices

    if not st.wing_taken:
        # UNCONDITIONAL initial take (ruling F-2): take BOTH wings at ask + wing_margin now,
        # regardless of lock_floor. The fill already happened; completing the pin bounds the
        # position to the $2 payoff. (lock is still computed for the journal/report.)
        n = st.rest_fill.price if st.rest_fill is not None else _ZERO
        w_paid = ya + fee(ya) + na + fee(na)
        lock = lock_value(n, w_paid)
        count = st.rest_fill.count if st.rest_fill is not None else params.contracts
        coid_y, st = _mint_coid(st)
        coid_n, st = _mint_coid(st)
        yes_limit = min(ya + params.wing_margin, _LIMIT_CEILING)
        no_limit = min(na + params.wing_margin, _LIMIT_CEILING)
        legs = (
            LegOrder(st.strike_tickers.get(st.spot_Sd, ""), BUY_YES, "buy", count, yes_limit),
            LegOrder(st.strike_tickers.get(st.spot_Su, ""), BUY_NO, "buy", count, no_limit),
        )
        wing_legs = (
            WingLeg(legs[0].ticker, BUY_YES, count, yes_limit, coid_y),
            WingLeg(legs[1].ticker, BUY_NO, count, no_limit, coid_n),
        )
        st = replace(st, wing_taken=True, wing_legs=wing_legs)
        actions.append(
            _mk(ActionKind.TAKE_WINGS, st.shakedown, legs=legs, count=count, lock=lock)
        )
        return st, actions

    # already taken: retry any leg reported unfilled, at the fresh ask — but only while the
    # projected set lock (filled legs at their fill price, unfilled legs at the current ask) stays
    # at/above lock_floor (ruling F-2). The already-held leg(s) form the $1 floor, so a deferred
    # retry is bounded, not naked; the retry fires as soon as the ask improves enough.
    n = st.rest_fill.price if st.rest_fill is not None else _ZERO
    projected_cost = _ZERO
    for leg in st.wing_legs:
        if leg.status == "filled" and leg.fill_price is not None:
            projected_cost += leg.fill_price + fee(leg.fill_price)
        else:
            ask = ya if leg.side == BUY_YES else na
            projected_cost += ask + fee(ask)
    projected_lock = _TWO - (n + fee(n)) - projected_cost
    if projected_lock < params.lock_floor:
        return st, actions  # defer the retry; the held leg(s) bound the position

    legs_out = list(st.wing_legs)
    changed = False
    for i, leg in enumerate(st.wing_legs):
        if leg.status != "unfilled":
            continue
        ask = ya if leg.side == BUY_YES else na
        limit = min(ask + params.wing_margin, _LIMIT_CEILING)
        coid_r, st = _mint_coid(st)
        retry = LegOrder(leg.ticker, leg.side, "buy", leg.count, limit)
        legs_out[i] = replace(leg, status="pending", limit=limit, client_order_id=coid_r)
        changed = True
        actions.append(_mk(ActionKind.RETRY_WING, st.shakedown, legs=(retry,), count=leg.count))
    if changed:
        st = replace(st, wing_legs=tuple(legs_out))
    return st, actions


def _maybe_close_set(st: V32State) -> V32State:
    """Both wings filled -> the set is complete: count it and clear the completion flags.

    Gated on ``wings_needed`` so a DUPLICATE fill event for an already-filled leg (same fill
    reported twice by the channel + the status poll) does not increment ``sets_done`` again."""
    if st.wings_needed and st.wing_legs and all(l.status == "filled" for l in st.wing_legs):
        return replace(
            st, sets_done=st.sets_done + 1, wings_needed=False, one_legged=False
        )
    return st


# ---------------------------------------------------------------------------
# Requote gate
# ---------------------------------------------------------------------------
def _cancel_action(st: V32State, order: RestOrder) -> V32Action:
    return _mk(
        ActionKind.CANCEL_REST, st.shakedown,
        order_id=order.order_id, client_order_id=order.client_order_id,
    )


def _place_action(st: V32State, params: V32Params, coid: str, n: Decimal) -> V32Action:
    exp = st.close_epoch - params.quote_end_s
    return _mk(
        ActionKind.PLACE_REST, st.shakedown,
        ticker=st.bucket_tickers.get(st.spot_Sd, ""), side=BUY_NO, action="buy",
        count=params.contracts, price=n, expiration_epoch=exp, client_order_id=coid,
    )


def _emit_place(st: V32State, params: V32Params, now: float) -> tuple[V32State, list[V32Action]]:
    """Mint + emit a PLACE_REST for the current desired n; record it as the pending rest."""
    coid, st = _mint_coid(st)
    n = st.desired_n
    assert n is not None
    order = RestOrder(
        client_order_id=coid, order_id=None, price=n, count=params.contracts,
        placed_ts=now, live=False, pending=True, bucket_Sd=st.spot_Sd,  # type: ignore[arg-type]
    )
    times = tuple(t for t in st.replace_times if now - t <= 60.0) + (now,)
    st = replace(
        st, rest_pending=order, rest_bucket_Sd=st.spot_Sd,
        last_replace_ts=now, replace_count=st.replace_count + 1, replace_times=times,
    )
    return st, [_place_action(st, params, coid, n)]


def _standdown(st: V32State, reason: str) -> tuple[V32State, list[V32Action]]:
    """Emit STAND_DOWN(reason) once per reason-change (dedupe like signal.py)."""
    if reason == st.last_standdown_reason:
        return replace(st, stand_down_reason=reason), []
    st = replace(st, last_standdown_reason=reason, stand_down_reason=reason)
    return st, [V32Action(kind=ActionKind.STAND_DOWN, reason=reason)]


def _cancel_live_if_any(st: V32State) -> tuple[V32State, list[V32Action]]:
    """Cancel a live or pending rest (no new place). Used by every no-quote branch."""
    actions: list[V32Action] = []
    if st.rest_live is not None and not st.cancel_in_flight:
        actions.append(_cancel_action(st, st.rest_live))
        st = replace(st, rest_live=None, cancel_in_flight=True)
    if st.rest_pending is not None:
        actions.append(_cancel_action(st, st.rest_pending))
        st = replace(st, rest_pending=None)
    return st, actions


def _requote(params: V32Params, st: V32State, now: float) -> tuple[V32State, list[V32Action]]:
    """The place/replace/cancel/stand-down decision on the resting bucket-NO order."""
    actions: list[V32Action] = []
    t_to_close = st.close_epoch - now
    in_window = params.quote_start_s >= t_to_close >= params.quote_end_s

    # entered: one rest fill = the one entry for the hour. Stop quoting (wings are handled by
    # _wing_step); cancel any lingering rest. This is the "one completed set per hour" latch at the
    # rest-fill instant (sets_done increments later, when both wings fill).
    if st.rest_fill is not None:
        st, ca = _cancel_live_if_any(st)
        return st, actions + ca

    # replace-rate alarm (trailing 60 s)
    recent = tuple(t for t in st.replace_times if now - t <= 60.0)
    if len(recent) > params.replace_rate_alarm_per_min and not st.stood_down:
        st, ca = _cancel_live_if_any(st)
        st = replace(st, stood_down=True)
        st, sa = _standdown(st, "replace_rate")
        return st, ca + sa

    # any reason we must not hold a quote -> cancel + stand down (dedup reason)
    no_quote_reason = None
    if st.stood_down:
        no_quote_reason = "stood_down"
    elif st.sets_done >= params.max_sets_per_hour:
        no_quote_reason = "set_complete"
    elif t_to_close < params.quote_end_s:
        no_quote_reason = "past_quote_end"
    elif t_to_close > params.quote_start_s:
        no_quote_reason = None  # warmup: seed only, nothing to cancel, no stand-down
        return st, actions
    elif st.spot_Sd is None:
        no_quote_reason = "no_spot_bucket"
    elif st.spot_bucket_stale:
        # R-STALE-SPOT: the selected spot bucket's own book aged past bucket_freshness_max_age_s.
        # Stand down (never quote a fresh lower-mid bucket instead); checked before the wing gate
        # because a stale spot also nulls W.
        no_quote_reason = "stale_bucket"
    elif st.W is None:
        no_quote_reason = "stale_or_missing_wing"
    elif st.desired_n is None or st.desired_n < params.n_min:
        no_quote_reason = "n_below_min"

    if no_quote_reason is not None:
        st, ca = _cancel_live_if_any(st)
        st, sa = _standdown(st, no_quote_reason)
        return st, ca + sa

    # healthy: we may hold a quote. clear any stale stand-down reason.
    if st.last_standdown_reason is not None:
        st = replace(st, last_standdown_reason=None, stand_down_reason=None)

    # bucket change: cancel ANY live and/or pending order on the old bucket, then place on the new
    # bucket after OrderCancelled (safe reading of the spec). rest_bucket_Sd -> None so we don't
    # re-enter this branch; awaiting_replace holds the place until the cancel(s) confirm.
    if st.rest_bucket_Sd is not None and st.rest_bucket_Sd != st.spot_Sd:
        emitted_cancel = False
        if st.rest_live is not None and not st.cancel_in_flight:
            actions.append(_cancel_action(st, st.rest_live))
            emitted_cancel = True
        if st.rest_pending is not None:
            actions.append(_cancel_action(st, st.rest_pending))
            emitted_cancel = True
        st = replace(
            st, rest_live=None, rest_pending=None,
            cancel_in_flight=st.cancel_in_flight or emitted_cancel,
            awaiting_replace=True, rest_bucket_Sd=None,
        )
        return st, actions

    # hold while a create is in flight, or while a bucket-change cancel is unconfirmed.
    if st.rest_pending is not None:
        return st, actions
    if st.awaiting_replace and st.cancel_in_flight:
        return st, actions

    # place / replace
    if st.rest_live is None:
        # first place, or place after a bucket-change cancel confirmed.
        st = replace(st, awaiting_replace=False)
        st, pa = _emit_place(st, params, now)
        return st, actions + pa

    # a live rest exists: replace iff |dn| >= tol AND >= deb_ms since the last replace.
    dn = abs(st.desired_n - st.rest_live.price)  # type: ignore[operator]
    since_ms = (now - (st.last_replace_ts if st.last_replace_ts is not None else -1e18)) * 1000.0
    if dn >= params.tol and since_ms >= params.deb_ms and st.desired_n != st.rest_live.price:
        # SEQUENTIAL replace (R-OVERLAP ruling 2026-09-13): CANCEL the old, WAIT for OrderCancelled,
        # and only THEN PLACE the new at the freshly re-solved n on a later tick — like the bucket-change
        # path. NEVER two live rests, NEVER a fillable old rest beside a new one in flight. A fill during
        # the cancel surfaces as OrderCancelled.filled_count_before_cancel > 0 (or a Fill event) ->
        # TAKE_WINGS, not PLACE. rest_live is kept populated (not eagerly cleared) so such a fill books
        # at the RESTING price, not the drifted desired_n; ``awaiting_replace`` + the cancel-in-flight
        # hold above suppress any PLACE until OrderCancelled clears the slot. The ~200-400 ms of no quote
        # per replace is accepted (~30 s/hour unquoted at ~77 replaces/hour).
        actions.append(_cancel_action(st, st.rest_live))
        st = replace(st, cancel_in_flight=True, awaiting_replace=True)
        return st, actions
    return st, actions


# ---------------------------------------------------------------------------
# Shadow (the ideal fill rule running live; emits no actions)
# ---------------------------------------------------------------------------
def _shadow_on_trade(
    params: V32Params, st: V32State, event: Trade
) -> tuple[V32State, list[V32Action]]:
    """A spot-bucket YES trade strictly above 1 - n_shadow(E) records a shadow fill (once/E/hour)."""
    cls = classify_ticker(event.market_ticker, st.bucket_map)
    if cls is None or cls[0] != "bucket" or cls[1] != st.spot_Sd:
        return st, []
    if event.taker_side != "yes":
        return st, []
    # a shadow fill needs a FRESH spot-bucket book too (ruling: "a shadow fill needs a fresh cap"):
    # if the bucket feed has stalled, do not synthesize an ideal fill off a stale cap. Symmetric to
    # the live-path gate in _select_spot / _recompute_context.
    if not _fresh(event.server_ts, st.bucket_ts.get(st.spot_Sd), params.bucket_freshness_max_age_s):
        return st, []
    # WINDOW GATE (2026-09-15 shadow-window fix): the shadow (the ideal no-lag rule) may only take a
    # print while the LIVE path would have been quoting — inside the same T-15..T-5 window the live
    # path enforces in ``_requote`` (``params.quote_start_s >= t_to_close >= params.quote_end_s``).
    # Bucket books connect ~T-20 and the live path cancels its rest at T-5 by design, so a print
    # outside [quote_end_s, quote_start_s] is one the live path could NEVER have taken. Evaluate
    # against the TRADE's own clock (``event.server_ts`` — the same ``now`` the live requote uses).
    # Outside the window a qualifying print does NOT fill the shadow (the sub is left unfilled so a
    # later in-window print can still fill it) and instead emits an observability-only action so the
    # driver journals + counts the suppressed would-be fill.
    t_to_close = st.close_epoch - event.server_ts
    in_window = params.quote_start_s >= t_to_close >= params.quote_end_s
    shadows = dict(st.shadows)
    actions: list[V32Action] = []
    changed = False
    for key, sub in shadows.items():
        if sub.filled or sub.n is None:
            continue
        offer = _ONE - sub.n
        if event.yes_price > offer:
            if not in_window:
                actions.append(
                    V32Action(
                        kind=ActionKind.SHADOW_FILL_OUTSIDE_WINDOW,
                        shadow_E=sub.E, offer=offer, print_price=event.yes_price,
                        count=int(event.count), t_to_close=Decimal(str(round(t_to_close, 3))),
                    )
                )
                continue
            shadows[key] = replace(
                sub, filled=True, awaiting_completion=True,
                fill=ShadowFill(
                    E=sub.E, n=sub.n, offer=offer, print_price=event.yes_price,
                    count=event.count, server_ts=event.server_ts,
                ),
            )
            changed = True
    if changed:
        st = replace(st, shadows=shadows)
    return st, actions


# ---------------------------------------------------------------------------
# F-1 additive hook: book a fill for an order the core no longer tracks
# ---------------------------------------------------------------------------
def book_late_rest_fill(
    params: V32Params,
    st: V32State,
    *,
    price: Decimal,
    count: int,
    server_ts: float,
    bucket_Sd: int | None = None,
) -> tuple[V32State, list[V32Action]]:
    """ADDITIVE Phase-2 hook (Phase-1 review F-1 — retained cancel context).

    Book a REST fill for one of OUR orders that the pure core no longer tracks — a coid the driver's
    RestBook attributed to us (a just-replaced / eagerly-cancelled order that filled on the exchange
    after the core cleared its slot). Without this, ``decide_v32``'s ``_apply_fill`` drops a fill whose
    coid matches neither ``rest_live`` nor ``rest_pending`` (correct for a truly foreign order, wrong
    for our own replaced one) — leaving an untracked, unhedged bucket-NO and defeating the one-set
    latch. This books the RestFill at the RestBook's RETAINED price (never the drifted ``desired_n``),
    latches the one-set rule, and takes the wings.

    ``bucket_Sd`` (the retained order's bucket floor) forces the spot context to the FILLED bucket so
    the wings price off that bucket's strikes even after the live spot moved on (the exact F-1 failure).
    Idempotent: once any rest fill is booked (``rest_fill`` set), this is a no-op — a fill reported
    twice (fill channel + status poll) never double-books. Emits no order-bearing action in shakedown
    (the wing take downgrades to its WOULD_* twin like every other action)."""
    if st.rest_fill is not None:
        return st, []
    spot_Sd = bucket_Sd if bucket_Sd is not None else st.spot_Sd
    spot_Su = (spot_Sd + params.bucket_width) if spot_Sd is not None else st.spot_Su
    st = replace(
        st,
        rest_fill=RestFill(price=price, count=int(count), server_ts=server_ts),
        rest_live=None,
        rest_pending=None,
        cancel_in_flight=False,
        wings_needed=True,
        wing_taken=False,
        spot_Sd=spot_Sd,
        spot_Su=spot_Su,
    )
    st, wa = _wing_step(params, st, server_ts)
    return st, wa


def _shadow_complete(
    params: V32Params, st: V32State, now: float
) -> tuple[V32State, list[V32Action]]:
    """Complete any awaiting shadow fill once both strike books are fresh (trade tick or next book)."""
    if st.spot_Sd is None or st.spot_Su is None:
        return st, []
    prices = _wing_prices(st, now, params)
    if prices is None:
        return st, []
    ya, na = prices
    w = ya + fee(ya) + na + fee(na)
    shadows = dict(st.shadows)
    changed = False
    for key, sub in shadows.items():
        if not sub.awaiting_completion or sub.fill is None:
            continue
        lock = lock_value(sub.fill.n, w)
        shadows[key] = replace(
            sub, awaiting_completion=False,
            fill=replace(sub.fill, W_at_completion=w, lock=lock),
        )
        changed = True
    if changed:
        st = replace(st, shadows=shadows)
    return st, []
