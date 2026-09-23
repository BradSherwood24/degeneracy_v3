"""core.py — the PURE decision core for V3.3 (rolling-ladder spot-bucket pump-fader).

Forked from ``service.v32.core`` (Phase L1, 2026-09-22, Brad's go). SAME house law: no clock reads, no
network, no disk, no globals; time comes ONLY from event timestamps so ``decide_v33`` runs live and in
replay bit-identically; money is Decimal; state is a frozen dataclass transitioned with
``dataclasses.replace``. The V3.2 core is FROZEN — this is a fork, not a refactor; the pure primitives
that are UNCHANGED (``solve_n``, the W solver ``_compute_W``/``wing_cost``, ``lock_value``, the
freshness gate ``_fresh``, spot discovery ``_select_spot``, the cap ``_bucket_cap``, the book fold
``_fold_book`` and the whole in-process SHADOW) are IMPORTED read-only from ``service.v32.core`` so the
two cores cannot drift on shared law. See pilot/build/v33_l1_core_build_report.md for every deliberate
divergence.

WHAT V3.3 CHANGES (Brad, PLAN_V33 sec 1) — the ROLLING LADDER and THE ROLL:

  * Instead of ONE resting NO bid of ``contracts`` lots at the E=10c level, rest K = ``rungs`` one-lot
    NO bids on K consecutive cents, anchored at the top rung ``n_top = n(E_min, W)`` (solved EXACTLY as
    V3.2 solves n: largest whole cent with n + fee(n) <= 2 - E_min - W, floor ``n_min``, cap
    ``no_ask(B) - 0.01``). Rung k sits at ``n_top - k`` cents (k = 0..K-1); its margin is ``E_min + k``
    cents. ``n_min`` truncates the ladder FROM THE BOTTOM (a rung below n_min is simply not placed;
    K_effective < K). The top rung honours the post-only cap: because rungs are anchored RELATIVE to
    ``n_top`` and ``n_top`` is already capped by ``solve_n``'s cap arg, a capped top shifts the WHOLE
    ladder down by the same amount (the simplest rule, and the one that keeps every rung post-only).

  * THE MARGIN-ARRAY CONVERGENCE (``_converge``, replacing V3.2's ``_requote``; Brad's R4 model). The
    desired state is a MARGIN ARRAY ``margin_state`` in profit space (index m = cents of profit at the
    current n_top; m = E_min_c is n_top): 0 = no order (below E_min), 1 = OPEN order wanted, 2 = filled
    (consumed; never re-opened, Q3). The OPEN 1-slots are ANONYMOUS — no order owns a slot; a live order's
    index is ALWAYS derived from its current price vs n_top, and any live order may be paired with any
    vacant 1-slot. A 2-slot is tied to the actual fill (``filled_at`` -> the RungFill with order_id/coid/W
    /n_top) so the falsifier's per-margin accounting anchors to the fill.
      Each tick (after a START-only debounce, ``deb_ms``, with sign-flip re-debounce) the core computes
    OUT = live orders whose derived margin is not an open slot (fell off an end, or landed on a 2/0), and
    VACANT = placeable open slots with no order, and pairs them greedily (minimal movement), emitting up
    to ``max_amends_in_flight`` AMENDs at once; as each acks (or falls back via cancel->create per order)
    the next pairs issue until converged. So a 1c W move = one order moves (Brad's one-order roll); a Nc
    move = N orders move, N-at-a-time, the rest KEEP QUEUE (this replaces R2's single-roll + fast-shift
    cancel-all). Extra OUT with no placeable slot -> CANCEL (shrink at n_min/cap); extra VACANT with no
    OUT (a suppressed slot released) -> CREATE, never past K. Amend-first via ``OrderAmended`` (PR #59);
    the executor's cancel -> confirm -> create fallback surfaces as an ``OrderCancelled`` for the rolling
    order -> the core places a fresh order at that roll's target. Bucket change -> cancel ALL live, then
    place the PLACEABLE OPEN slots on the new ticker (after a partial sweep, the remaining 1-margins).
    Window exposure ``rungs_filled + live rests <= K`` holds at every step (BLOCKING #R2-1).

  * Fill of a rung -> a rung fill (its margin DERIVED from price vs n_top) -> a coalesced ``WingBatch``:
    rung fills arriving within ``wing_coalesce_ms`` (150 ms) of the FIRST are coalesced into ONE wing
    pair sized to the total filled; a fill after the window closes starts a new batch. A filled rung is
    NOT refilled inside the window (Q3 ``refill_in_window`` False) — the ladder simply has K-filled
    live rests. ``max_sets_per_hour`` = K.

  * The T-``quote_end_s`` cancel-all, freshness stand-down (W missing -> stand down), and the S-stop
    hooks are V3.2's, generalised from one order to K.

The FALSIFIER quantities (PLAN_V33 sec 6) are all derivable from state/events: per-rung SOLVED lock
``2 - n - fee(n) - W`` (via ``lock_value`` at the rung's n and the W at fill), the rung fill's
``E_rung``, ``rungs_filled`` / ``roll_count``, and the single-order-roll ratio
(``roll_single_order_count`` / ``roll_count`` — 1.0 by construction here; L2 journal-counts the venue
truth).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal

from service._simlaw import fee

from service.book import TopOfBook
from service.v33.actions import ActionKind, LegOrder, V33Action, twin_kind
from service.v33.events import (
    BookUpdate,
    ClockTick,
    Fill,
    OrderAck,
    OrderAmended,
    OrderCancelled,
    Trade,
    classify_ticker,
)
from service.v33.params import V33Params

# --- reused, UNCHANGED V3.2 pure law (imported read-only; NEVER reimplemented here) ---
from service.v32.core import (
    ShadowFill,          # noqa: F401  (re-exported for L2/tests)
    ShadowSub,
    _bucket_cap,
    _compute_W,
    _fold_book,
    _fresh,
    _shadow_complete,
    _shadow_on_trade,
    _select_spot,
    _valid_two_sided,
    _wing_prices,
    lock_value,
    solve_n,
    wing_cost,           # noqa: F401  (re-exported for L2/tests)
)

# --- money constants ---
_ZERO = Decimal(0)
_ONE = Decimal(1)
_TWO = Decimal(2)
_CENT = Decimal("0.01")
_LIMIT_CEILING = Decimal("0.99")

BUY_YES = "yes"
BUY_NO = "no"


# ===========================================================================
# Records carried in state
# ===========================================================================
@dataclass(frozen=True)
class RestOrder:
    """One resting bucket-NO rung. ``live`` = acked & fillable; ``pending`` = placed, awaiting ack.
    ``price`` is the whole-cent n (dollars); ``rung`` is the ladder index k; ``E_rung`` is the rung's
    margin ``E_min + k`` cents (recomputed from ``price`` vs the current ``n_top`` after every roll)."""

    client_order_id: str
    order_id: str | None
    price: Decimal
    count: int
    placed_ts: float
    live: bool
    pending: bool
    bucket_Sd: int
    # ``rung`` / ``E_rung`` are a DERIVED LABEL (refreshed every context tick from price vs n_top), NOT a
    # stored identity: the open margin slots are anonymous (Brad R4) and any order may be paired to any
    # vacant slot by the convergence. Used only for RungFill/report; never to decide an order's slot.
    rung: int
    E_rung: Decimal


@dataclass(frozen=True)
class RungFill:
    """A fill of ONE rung, and the reference a filled (state-2) margin slot carries (Brad's R4
    clarification: "each E index with a value of 2 should be tied to a specific order that has been
    filled"). ``rung`` is the DERIVED margin index at fill time (m - E_min_c; a label, not an identity —
    the open 1-slots are anonymous), ``price`` = the resting n, ``count`` lots. ``coid`` / ``order_id``
    tie the 2-slot to the actual order; ``W`` / ``n_top`` are the wing cost and top anchor at fill, so the
    falsifier's per-margin accounting (solved lock = ``lock_value(price, W)``) is anchored to the fill."""

    rung: int
    E_rung: Decimal
    price: Decimal
    count: int
    server_ts: float
    coid: str | None = None
    order_id: str | None = None
    W: Decimal | None = None
    n_top: Decimal | None = None
    # The bucket the rung ACTUALLY rested on, captured at FILL time (L2 R2, reviewer MUST-FIX-3): a
    # rest-and-fill across a bucket change within one window would otherwise mislabel the held bucket-NO
    # leg and mis-settle the backfill. ``bucket_ticker`` is what the ledger/backfill price against;
    # ``bucket_Sd``/``bucket_Su`` are the fill-time spot bounds for the report.
    bucket_ticker: str | None = None
    bucket_Sd: int | None = None
    bucket_Su: int | None = None


@dataclass(frozen=True)
class WingLeg:
    """One taker completion leg. ``status`` in {"pending","filled","unfilled"}; ``batch`` is the index
    of the ``WingBatch`` this leg belongs to. ``wing_legs`` is a FLAT tuple across all batches."""

    ticker: str
    side: str
    count: int
    limit: Decimal
    client_order_id: str
    status: str = "pending"
    fill_price: Decimal | None = None
    fill_fee: Decimal | None = None
    batch: int = 0


@dataclass(frozen=True)
class WingBatch:
    """One taker-completion batch = the rung fills COALESCED within ``wing_coalesce_ms`` (Q2). Its two
    legs (sized to ``total_count``) live in ``V33State.wing_legs`` tagged with this batch's ``index``;
    ``fills`` is the per-rung breakdown so per-rung locks stay computable.

    ``taken`` — the both-wings take has been emitted. ``completed`` — both wings filled (one counted
    SET, or K sets when it coalesced K rungs). ``one_legged`` — reached the cutoff with a wing missing."""

    index: int
    server_ts: float
    fills: tuple[RungFill, ...]
    taken: bool = False
    completed: bool = False
    one_legged: bool = False

    @property
    def total_count(self) -> int:
        return sum(f.count for f in self.fills)


@dataclass(frozen=True)
class CoalesceGroup:
    """The OPEN coalescing group: rung fills accumulating since ``first_ts``. Closes into a
    ``WingBatch`` once the clock passes ``first_ts + wing_coalesce_ms`` (then the wings are taken)."""

    index: int
    first_ts: float
    fills: tuple[RungFill, ...]


@dataclass(frozen=True)
class RollPending:
    """One in-flight convergence amend: order ``old_coid`` (venue ``order_id``) is being moved to
    ``target_price`` (margin ``target_margin`` at issue time) under the rotated ``new_coid``. Up to
    ``max_amends_in_flight`` of these run concurrently. Resolves via ``OrderAmended`` (success) or
    ``OrderCancelled`` (the executor's cancel->create fallback -> the core places a fresh order at
    ``target_price``). rung/E_rung of the moved order are re-derived from the CURRENT n_top at ack, so
    only ``target_price`` (and ``target_margin`` for reference) is carried."""

    order_id: str | None
    old_coid: str
    new_coid: str
    target_price: Decimal
    target_margin: int
    started_ts: float


# ===========================================================================
# The window state (immutable; decide_v33 returns a NEW state via replace)
# ===========================================================================
@dataclass(frozen=True)
class V33State:
    close_time: str
    close_epoch: int
    bucket_map: Mapping[str, tuple[float, float]]
    shakedown: bool = False

    # live book state (SAME field names as V32State so the reused V3.2 helpers duck-type over it)
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
    n_top: Decimal | None = None                        # the solved ladder-top anchor
    spot_bucket_stale: bool = False

    # ladder lifecycle
    ladder: tuple[RestOrder, ...] = ()                  # K rungs (live and/or pending)
    # MARGIN-ARRAY desired state (Brad's model, R4): index m = cents of profit at the current n_top
    # (m = E_min_c is n_top). 0 = no order (below E_min), 1 = open order wanted, 2 = filled (consumed,
    # never re-opened this window, Q3). The OPEN SPAN S = {m : state == 1}. A fill at current margin m
    # sets state[m] = 2. Convergence keeps one live order per PLACEABLE open slot. The 1-slots are
    # ANONYMOUS (no order home); a 2-slot is tied to the actual fill via ``filled_at`` (Brad R4).
    margin_state: tuple[int, ...] = ()
    filled_at: Mapping[int, RungFill] = field(default_factory=dict)   # margin -> the fill that consumed it
    rolls_in_flight: tuple[RollPending, ...] = ()       # up to max_amends_in_flight concurrent amends
    converging_dir: int = 0                              # 0 = idle; +/-1 = an active convergence in that
                                                        # direction. deb_ms debounces only the START; a
                                                        # same-sign continuation issues on each ack with no
                                                        # re-debounce; a sign flip re-debounces (R2 pacing).
    awaiting_replace: bool = False                       # bucket change: cancelled all, place after confirms
    outstanding_cancels: int = 0                          # bucket-change cancels awaiting OrderCancelled
    rest_bucket_Sd: int | None = None                    # the bucket the ladder is on
    last_replace_ts: float | None = None
    replace_count: int = 0                               # ladder placements + rolls (feeds the alarm)
    replace_times: tuple[float, ...] = ()
    roll_count: int = 0                                  # confirmed rolls (falsifier)
    roll_single_order_count: int = 0                     # rolls that moved exactly one order (falsifier)
    coid_seq: int = 0

    # fills / wings
    rest_fills: tuple[RungFill, ...] = ()               # every rung fill booked this hour
    rungs_filled: int = 0                                # count of rungs filled (a set contributes 1)
    rest_booked_by_coid: Mapping[str, int] = field(default_factory=dict)
    cancel_ctx: Mapping[str, tuple[str, Decimal, int, Decimal]] = field(default_factory=dict)
    amend_cross_pending: Mapping[str, int] = field(default_factory=dict)
    rest_allotment_done: bool = False                    # every rung filled (or max_sets) -> stop quoting

    coalesce_open: CoalesceGroup | None = None
    wing_batches: tuple[WingBatch, ...] = ()
    wing_legs: tuple[WingLeg, ...] = ()
    next_batch_index: int = 0
    sets_done: int = 0
    one_legged: bool = False                            # mirror: any batch flagged one_legged

    # shadow (keyed by str(E)) — the V3.2 shadow, forked faithfully
    shadows: Mapping[str, ShadowSub] = field(default_factory=dict)

    # stand-down / dedupe
    stood_down: bool = False
    stand_down_reason: str | None = None
    last_standdown_reason: str | None = None

    # BUCKET-FLAP FIX (2026-09-23): the pending spot-bucket switch being timed (the ladder stays on
    # ``rest_bucket_Sd`` until it commits), and the stale/missing-wing stand-down HOLD.
    pending_switch_Sd: int | None = None                 # the candidate new bucket under the debounce timer
    pending_switch_since: float | None = None            # when it first became the continuous resolved spot
    pending_switch_hyst_met: bool = False                # the hysteresis was satisfied at least once pending
    hold_reason: str | None = None                       # the stand-down reason currently being HELD
    hold_since: float | None = None                      # when the hold began

    @classmethod
    def new(
        cls,
        close_time: str,
        close_epoch: int,
        bucket_map: Mapping[str, tuple[float, float]],
        params: V33Params,
        *,
        shakedown: bool = False,
    ) -> "V33State":
        shadows = {str(E): ShadowSub(E=E) for E in params.shadow_Es}
        # margin array: indices 0..(E_min_c + K - 1); 0 below E_min, 1 (open order wanted) for the K
        # slots at margins [E_min_c, E_min_c + K - 1].
        e_min_c = _emin_cents(params)
        margin_state = tuple(1 if e_min_c <= m <= e_min_c + params.rungs - 1 else 0
                             for m in range(e_min_c + params.rungs))
        return cls(
            close_time=close_time,
            close_epoch=int(close_epoch),
            bucket_map=dict(bucket_map),
            shakedown=shakedown,
            shadows=shadows,
            margin_state=margin_state,
        )

    # ------------------------------------------------------------------
    def check_invariants(self, params: V33Params) -> None:
        """Cheap structural invariants, asserted each step by the tests (and safe to run live).

        * never more than K live rests; * never two rests on one price; * prices are whole cents >= n_min;
        * every live rest's ``rung`` AND ``E_rung`` are CONSISTENT with the current ``n_top``
          (``rung == round((n_top - price)/1c)`` and ``E_rung == E_min + (n_top - price)``) — both are
          refreshed together on every context recompute and reconciled on every roll ack, so they never
          disagree (BLOCKING #1/#2, reviewer 2026-09-22); * a rung may legitimately sit ABOVE ``n_top``
          transiently (a fast up-move or a cap crash), i.e. rung < 0 = "stranded above the top" — that is
          allowed, not an error (BLOCKING #2); * ladder prices are consecutive cents from the top down
          while NO rung has filled (fills open gaps that are not refilled, so consecutiveness is only
          asserted pre-fill); * at most ``max_amends_in_flight`` concurrent rolls, each on a DISTINCT
          order that is a ladder member unless it has already filled (golden f) — no order ever has two
          amends in flight; * a taken wing batch has exactly two legs sized to its total;
          * ``rungs_filled`` equals the number of booked rung fills; * the WINDOW EXPOSURE
          ``rungs_filled + live rests <= K`` at every step (BLOCKING #R2-1: a re-placement after a partial
          sweep must never regrow the ladder past K - filled)."""
        lad = self.ladder
        assert len(lad) <= params.rungs, f"ladder has {len(lad)} > K={params.rungs} rests"
        assert self.rungs_filled + len(lad) <= params.rungs, (
            f"window exposure {self.rungs_filled} filled + {len(lad)} resting > K={params.rungs}"
        )
        prices = [o.price for o in lad]
        assert len(set(prices)) == len(prices), f"two rests on one price: {sorted(prices)}"
        for o in lad:
            assert o.count == params.lots_per_rung, f"rung count {o.count} != {params.lots_per_rung}"
            assert isinstance(o.rung, int), f"bad rung index {o.rung!r}"
            assert o.price == o.price.quantize(_CENT), f"rung price {o.price} not a whole cent"
            assert o.price >= params.n_min, f"rung price {o.price} < n_min {params.n_min}"
            if self.n_top is not None:
                # rung/E_rung are the LIVE position vs n_top (negative rung = stranded above the top).
                assert o.rung == _rung_of(self.n_top, o.price), (
                    f"rung {o.rung} != round((n_top-price)/1c) for price {o.price}, n_top {self.n_top}"
                )
                assert o.E_rung == params.E_min + (self.n_top - o.price), (
                    f"E_rung {o.E_rung} != E_min+(n_top-price) for price {o.price}, n_top {self.n_top}"
                )
        # rolls: at most max_amends_in_flight, on DISTINCT orders (no order two amends), mutually
        # exclusive with a bucket change; each moving order matches <= 1 ladder member (0 if it filled).
        assert len(self.rolls_in_flight) <= params.max_amends_in_flight, (
            f"{len(self.rolls_in_flight)} rolls in flight > max_amends_in_flight "
            f"{params.max_amends_in_flight}"
        )
        assert not (self.rolls_in_flight and self.awaiting_replace), (
            "rolls and bucket-change cannot be in flight together"
        )
        moving_coids = [r.old_coid for r in self.rolls_in_flight]
        assert len(set(moving_coids)) == len(moving_coids), (
            f"an order has two amends in flight: {moving_coids}"
        )
        for r in self.rolls_in_flight:
            n_moving = sum(1 for o in lad if o.client_order_id == r.old_coid)
            assert n_moving <= 1, f"roll {r.old_coid} matches {n_moving} ladder orders (must be <= 1)"
        # wing-batch / leg consistency: a TAKEN batch has exactly two legs, each sized to the batch total.
        for b in self.wing_batches:
            legs = [l for l in self.wing_legs if l.batch == b.index]
            if b.taken:
                assert len(legs) == 2, f"taken batch {b.index} has {len(legs)} legs (must be 2)"
                assert all(l.count == b.total_count for l in legs), (
                    f"batch {b.index} leg counts != total {b.total_count}"
                )
        assert self.rungs_filled == len(self.rest_fills), (
            f"rungs_filled {self.rungs_filled} != booked fills {len(self.rest_fills)}"
        )
        # NB (R4): consecutiveness is no longer a step invariant — a convergence moves up to
        # max_amends_in_flight orders at once, so the ladder is transiently non-contiguous even before any
        # fill. "Every live order's margin in the open span" is a REST property (nothing in flight, past
        # the debounce), asserted by tests after convergence completes, not on every event.


# ===========================================================================
# Small pure helpers
# ===========================================================================
def _mk(kind: ActionKind, shakedown: bool, **fields) -> V33Action:
    """Build a V33Action, downgrading order-emitting kinds to their WOULD_* twin in shakedown."""
    return V33Action(kind=twin_kind(kind, shakedown), **fields)


def _mint_coid(st: V33State) -> tuple[str, V33State]:
    seq = st.coid_seq + 1
    coid = f"v33-{st.close_time}-{seq}"
    return coid, replace(st, coid_seq=seq)


def _e_rung(params: V33Params, n_top: Decimal, price: Decimal) -> Decimal:
    """The rung's margin label from its price vs the current top: E_min + (n_top - price)."""
    return params.E_min + (n_top - price)


def _rung_of(n_top: Decimal, price: Decimal) -> int:
    """The rung's live index = (n_top - price) in cents. Negative when the order rests ABOVE n_top (a
    transient during a fast up-move or a cap crash: "stranded above the top")."""
    return int(((n_top - price) / _CENT).to_integral_value())


def _emin_cents(params: V33Params) -> int:
    """E_min in whole cents (the margin index of the top rung, n_top)."""
    return int((params.E_min / _CENT).to_integral_value())


def _margin_of(params: V33Params, n_top: Decimal, price: Decimal) -> int:
    """The order's CURRENT margin index (cents of profit) at ``n_top``: E_min_c + (n_top - price)/1c."""
    return _emin_cents(params) + _rung_of(n_top, price)


def _price_of_margin(params: V33Params, n_top: Decimal, m: int) -> Decimal:
    """The desired price for margin slot ``m`` at ``n_top``: n_top - (m - E_min_c) cents."""
    return n_top - (m - _emin_cents(params)) * _CENT


def _open_slots(st: V33State) -> list[int]:
    """The OPEN SPAN S = margin indices whose desired state is 1 (order wanted, not filled/below-E_min)."""
    return [m for m, s in enumerate(st.margin_state) if s == 1]


def _sync_wing_mirrors(st: V33State) -> V33State:
    """Re-derive the ``one_legged`` mirror from the batch state (kept for L2/ledger compatibility)."""
    one_legged = any(b.one_legged for b in st.wing_batches)
    return replace(st, one_legged=one_legged)


def _replace_order(ladder: tuple[RestOrder, ...], coid: str, **fields) -> tuple[RestOrder, ...]:
    """Return ``ladder`` with the order whose client_order_id == coid replaced (by dataclasses.replace)."""
    out = list(ladder)
    for i, o in enumerate(out):
        if o.client_order_id == coid:
            out[i] = replace(o, **fields)
            break
    return tuple(out)


def _drop_order(ladder: tuple[RestOrder, ...], coid: str) -> tuple[RestOrder, ...]:
    return tuple(o for o in ladder if o.client_order_id != coid)


# ===========================================================================
# The decision
# ===========================================================================
def decide_v33(
    params: V33Params, state: V33State, event
) -> tuple[V33State, list[V33Action]]:
    """Pure decision. Returns (new_state, actions). See module docstring for the full law."""
    now = event.server_ts
    st = state
    actions: list[V33Action] = []

    if isinstance(event, BookUpdate):
        st = _fold_book(st, event)
        st = _recompute_context(params, st, now)
        st, _ = _shadow_complete(params, st, now)
        st, wa = _wing_step(params, st, now)
        st, qa = _converge(params, st, now)
        return st, wa + qa

    if isinstance(event, Trade):
        st, ta = _shadow_on_trade(params, st, event)
        st, _ = _shadow_complete(params, st, now)
        # take/close any coalesced wings whose window elapsed as the clock advanced with this print
        st, wa = _wing_step(params, st, now)
        return st, ta + wa

    if isinstance(event, OrderAck):
        st = _apply_ack(st, event)
        return st, actions

    if isinstance(event, OrderAmended):
        st, aa = _apply_amended(params, st, event, now)
        return st, aa

    if isinstance(event, OrderCancelled):
        st, ca = _apply_cancelled(params, st, event, now)
        return st, ca

    if isinstance(event, Fill):
        st, fa = _apply_fill(params, st, event, now)
        return st, fa

    if isinstance(event, ClockTick):
        st = _recompute_context(params, st, now)
        st, _ = _shadow_complete(params, st, now)
        st, wa = _wing_step(params, st, now)
        st, qa = _converge(params, st, now)
        return st, wa + qa

    return st, actions


# ---------------------------------------------------------------------------
# Context (spot / W / cap / n_top / shadows / E_rung refresh)
# ---------------------------------------------------------------------------
def _implied_spot(params: V33Params, st: V33State) -> Decimal | None:
    """A cheap implied BTC spot from the range-bucket ladder: the yes-mid-weighted centroid of bucket
    CENTRES over the valid two-sided buckets (E[settlement] ~ current spot for the short horizon). None
    if no valid bucket carries weight. Used only by the bucket-switch hysteresis (Brad's "$X inside")."""
    num = _ZERO
    den = _ZERO
    half = Decimal(params.bucket_width) / _TWO
    for floor, top in st.bucket_tops.items():
        if not _valid_two_sided(top):
            continue
        mid = (top.yes_bid + top.yes_ask) / _TWO  # type: ignore[operator]
        if mid <= _ZERO:
            continue
        num += mid * (Decimal(floor) + half)
        den += mid
    return (num / den) if den > _ZERO else None


def _hysteresis_ok(params: V33Params, st: V33State, candidate_Sd: int) -> bool:
    """The bucket-switch hysteresis: the implied spot must sit >= ``bucket_switch_hysteresis_usd`` inside
    the candidate bucket (away from either boundary). If the implied spot cannot be measured (thin
    ladder), fall back to debounce-only (return True). ``hysteresis_usd`` == 0 also always passes."""
    hyst = params.bucket_switch_hysteresis_usd
    if hyst <= 0:
        return True
    imp = _implied_spot(params, st)
    if imp is None:
        return True
    lo = Decimal(candidate_Sd) + hyst
    hi = Decimal(candidate_Sd) + params.bucket_width - hyst
    return lo <= imp < hi


def _resolve_effective_bucket(
    params: V33Params, st: V33State, raw_Sd: int | None, now: float
) -> tuple[int | None, int | None, float | None, bool]:
    """BUCKET-FLAP FIX (2026-09-23): map the instantaneous resolved spot ``raw_Sd`` to the EFFECTIVE bucket
    the ladder uses, debouncing a switch. Returns (effective_Sd, pending_Sd, pending_since, pending_hyst).

    While a switch is pending the ladder stays on ``rest_bucket_Sd`` (rolls continue there, fills book
    there); the switch commits only once the new bucket has been the resolved spot continuously for
    >= ``bucket_switch_deb_ms`` AND the implied spot sat >= ``bucket_switch_hysteresis_usd`` inside it at
    least once. A flip back to the ladder's bucket (or to a different candidate) resets the timer. With no
    ladder yet (first placement) the effective bucket follows ``raw_Sd`` immediately (nothing to protect)."""
    rest = st.rest_bucket_Sd
    if raw_Sd is None:
        return None, None, None, False
    if rest is None or raw_Sd == rest:
        # first placement, or spot is back on the ladder's bucket -> no pending switch.
        return raw_Sd, None, None, False
    # raw_Sd != rest: a candidate switch under the debounce.
    pend_Sd, pend_since, pend_hyst = (
        st.pending_switch_Sd, st.pending_switch_since, st.pending_switch_hyst_met)
    if pend_Sd != raw_Sd or pend_since is None:
        pend_Sd, pend_since, pend_hyst = raw_Sd, now, False   # (re)start the timer for a new candidate
    if not pend_hyst:
        pend_hyst = _hysteresis_ok(params, st, raw_Sd)         # latch the hysteresis once satisfied
    elapsed_ms = (now - pend_since) * 1000.0
    # COMMIT when the debounce has elapsed AND either the hysteresis held OR the ANTI-STRAND cap has been
    # reached (R2 NIT-1: the highest-yes-mid candidate and the centroid hysteresis can disagree, so a spot
    # parked a few $ inside the new bucket would otherwise be stranded on the old bucket the whole window).
    if elapsed_ms >= params.bucket_switch_deb_ms and (
            pend_hyst or elapsed_ms >= params.bucket_switch_max_pending_ms):
        return raw_Sd, None, None, False                       # COMMIT: effective flips to the new bucket
    return rest, pend_Sd, pend_since, pend_hyst                 # still pending: stay on the old bucket


def _recompute_context(params: V33Params, st: V33State, now: float) -> V33State:
    """Re-derive spot bucket, W, cap, the ladder-top ``n_top``, every shadow n, and refresh each live
    rung's LIVE labels (``rung`` AND ``E_rung``) from the current n_top. Uses the UNCHANGED V3.2
    spot/W/cap law (imported). The EFFECTIVE spot bucket is debounced (bucket-flap fix): a switch commits
    only after ``bucket_switch_deb_ms`` + hysteresis (or the ``bucket_switch_max_pending_ms`` anti-strand
    cap); while pending the ladder stays on its bucket."""
    raw_Sd = _select_spot(st)
    spot_Sd, pend_Sd, pend_since, pend_hyst = _resolve_effective_bucket(params, st, raw_Sd, now)
    spot_Su = spot_Sd + params.bucket_width if spot_Sd is not None else None
    spot_bucket_stale = spot_Sd is not None and not _fresh(
        now, st.bucket_ts.get(spot_Sd), params.bucket_freshness_max_age_s
    )
    W = None
    cap = None
    n_top = None
    if spot_Sd is not None and spot_Su is not None and not spot_bucket_stale:
        W = _compute_W(st, spot_Sd, spot_Su, now, params)
        cap = _bucket_cap(st, spot_Sd)
        budget = (_TWO - params.E_min - W) if W is not None else None
        n_top = solve_n(budget, cap)

    shadows = dict(st.shadows)
    for key, sub in shadows.items():
        if W is not None and cap is not None:
            n_sh = solve_n(_TWO - sub.E - W, cap)
        else:
            n_sh = None
        shadows[key] = replace(sub, n=n_sh)

    st = replace(
        st, spot_Sd=spot_Sd, spot_Su=spot_Su, W=W, cap=cap, n_top=n_top,
        spot_bucket_stale=spot_bucket_stale, shadows=shadows,
        pending_switch_Sd=pend_Sd, pending_switch_since=pend_since, pending_switch_hyst_met=pend_hyst,
    )
    # refresh each rung's LIVE labels (BOTH rung AND E_rung) from the current n_top — BLOCKING #1
    # (reviewer 2026-09-22): rung was previously left stale while E_rung tracked n_top, corrupting the
    # §6 per-rung falsifier key after any roll. They must move together and stay the live position.
    if n_top is not None and st.ladder:
        st = replace(
            st,
            ladder=tuple(
                replace(o, rung=_rung_of(n_top, o.price), E_rung=_e_rung(params, n_top, o.price))
                for o in st.ladder
            ),
        )
    return st


# ---------------------------------------------------------------------------
# Order lifecycle events
# ---------------------------------------------------------------------------
def _apply_ack(st: V33State, event: OrderAck) -> V33State:
    """A pending rung create is acked -> it becomes live."""
    for o in st.ladder:
        if o.client_order_id == event.client_order_id and o.pending:
            ladder = _replace_order(
                st.ladder, event.client_order_id,
                order_id=event.order_id, live=True, pending=False,
            )
            return replace(st, ladder=ladder)
    return st


def _drop_roll(st: V33State, old_coid: str) -> V33State:
    """Remove the in-flight roll for ``old_coid`` from ``rolls_in_flight``."""
    return replace(st, rolls_in_flight=tuple(r for r in st.rolls_in_flight if r.old_coid != old_coid))


def _apply_amended(
    params: V33Params, st: V33State, event: OrderAmended, now: float
) -> tuple[V33State, list[V33Action]]:
    """A convergence amend confirm. The order_id PERSISTS; the moved order updates price + coid + rung +
    E_rung IN PLACE (labels from the CURRENT n_top, BLOCKING #2). ``rest_booked_by_coid`` carries forward
    (order_id persists, coid rotates). Counts one confirmed roll. A cross fill (fill_count > 0) books its
    own rung fill at the venue average price. Then RE-CONVERGES to issue the next pair(s) toward S."""
    actions: list[V33Action] = []
    rp = next(
        (r for r in st.rolls_in_flight if
         (event.order_id is not None and r.order_id == event.order_id)
         or r.old_coid == event.client_order_id or r.new_coid == event.client_order_id),
        None,
    )
    if rp is None:
        # amend confirm for an order the core no longer rolls -> nothing to do.
        return st, actions
    st = _drop_roll(st, rp.old_coid)

    # the moved order may have FILLED (removed from ladder) before the amend landed -> just drop the roll.
    order = next((o for o in st.ladder if o.client_order_id == rp.old_coid), None)
    if order is None:
        st, ra = _converge(params, st, now)
        return st, actions + ra

    booked = dict(st.rest_booked_by_coid)
    if rp.new_coid != rp.old_coid and rp.old_coid in booked:
        booked[rp.new_coid] = booked.get(rp.new_coid, 0) + booked.pop(rp.old_coid)

    new_price = event.price if event.price is not None else rp.target_price
    if st.n_top is not None:
        new_rung = _rung_of(st.n_top, new_price)
        new_E = _e_rung(params, st.n_top, new_price)
    else:
        new_rung = rp.target_margin - _emin_cents(params)
        new_E = params.E_min + new_rung * _CENT
    ladder = _replace_order(
        st.ladder, rp.old_coid,
        price=new_price, client_order_id=rp.new_coid, rung=new_rung, E_rung=new_E,
    )
    times = tuple(t for t in st.replace_times if now - t <= 60.0) + (now,)
    st = replace(
        st, ladder=ladder, rest_booked_by_coid=booked,
        last_replace_ts=now, replace_count=st.replace_count + 1, replace_times=times,
        roll_count=st.roll_count + 1, roll_single_order_count=st.roll_single_order_count + 1,
    )

    delta = int(event.fill_count) if event.fill_count is not None else 0
    if delta > 0:
        moved = next((o for o in st.ladder if o.client_order_id == rp.new_coid), None)
        n = event.average_fill_price if event.average_fill_price is not None else new_price
        if moved is not None:
            st, wa = _book_rung_fill(params, st, moved.client_order_id, n, delta, moved.rung,
                                     moved.E_rung, now)
            actions += wa
            if event.order_id is not None and any(
                o.client_order_id == rp.new_coid for o in st.ladder
            ):
                acp = dict(st.amend_cross_pending)
                acp[event.order_id] = acp.get(event.order_id, 0) + delta
                st = replace(st, amend_cross_pending=acp)

    # re-converge: issue the next pair(s) now this one acked (continuation, no re-debounce).
    st, ra = _converge(params, st, now)
    return st, actions + ra


def _apply_cancelled(
    params: V33Params, st: V33State, event: OrderCancelled, now: float
) -> tuple[V33State, list[V33Action]]:
    """An OrderCancelled. Three cases: (1) the executor's amend->cancel->create FALLBACK for the rolling
    order -> drop it and PLACE a fresh rung at the roll's target; (2) a bucket-change / stand-down /
    quote-end cancel -> decrement the outstanding count; (3) any of the above with a partial fill before
    the cancel -> book the delta via the still-live order or ``cancel_ctx`` (cumulative -> delta)."""
    actions: list[V33Action] = []
    filled = int(event.filled_count_before_cancel)

    # book a partial fill first (attribute by live order, else cancel_ctx), before we drop slots.
    coid: str | None = None
    price: Decimal | None = None
    rung: int | None = None
    E_rung: Decimal | None = None
    live_order = next((o for o in st.ladder if o.order_id == event.order_id), None)
    if live_order is not None:
        coid, price, rung, E_rung = (
            live_order.client_order_id, live_order.price, live_order.rung, live_order.E_rung
        )
    else:
        ctx = st.cancel_ctx.get(event.order_id)
        if ctx is not None:
            coid, price, rung, E_rung = ctx
    if filled > 0 and coid is not None and price is not None:
        already = st.rest_booked_by_coid.get(coid, 0)
        delta = filled - already
        if delta > 0:
            st, wa = _book_rung_fill(params, st, coid, price, delta, rung or 0,
                                     E_rung if E_rung is not None else params.E_min, now)
            actions += wa

    rp = next((r for r in st.rolls_in_flight
               if event.order_id is not None and r.order_id == event.order_id), None)
    if rp is not None:
        # FALLBACK: the amend failed and the executor cancelled -> drop the old order and place a fresh
        # one at the roll's target (same end state as a successful amend, fresh queue). Skip the re-place
        # if the order fully filled before the cancel (nothing left to move).
        st = _drop_roll(st, rp.old_coid)
        st = replace(st, ladder=_drop_order(st.ladder, rp.old_coid))
        still_resting = st.rest_booked_by_coid.get(rp.new_coid, st.rest_booked_by_coid.get(
            rp.old_coid, 0)) < params.lots_per_rung
        # NIT #9: re-check the cap on the fallback re-place (no_ask may have dropped since emit). Clamp to
        # the cap and drop if that pushes below n_min. Derive rung/E_rung from the CURRENT n_top.
        target = rp.target_price
        if st.cap is not None and target > st.cap:
            target = st.cap
        if (not st.rest_allotment_done and still_resting and _in_window(params, st, now)
                and target >= params.n_min and st.n_top is not None
                and len(st.ladder) + st.rungs_filled < params.rungs):
            r_rung, r_E = _rung_of(st.n_top, target), _e_rung(params, st.n_top, target)
            st, pa = _place_one(params, st, target, r_rung, r_E, now)
            actions += pa
        st, ra = _converge(params, st, now)
        return st, actions + ra

    # bucket-change / stand-down / shrink cancel: drop the slot (if still present) and decrement the count.
    if live_order is not None:
        st = replace(st, ladder=_drop_order(st.ladder, live_order.client_order_id))
    if st.outstanding_cancels > 0:
        st = replace(st, outstanding_cancels=st.outstanding_cancels - 1)
    # a bucket change waiting to re-place: once all cancels confirmed and the ladder is clear, place all.
    st, pa = _converge(params, st, now)
    return st, actions + pa


def _apply_fill(
    params: V33Params, st: V33State, event: Fill, now: float
) -> tuple[V33State, list[V33Action]]:
    """A private fill. A wing-leg fill updates leg status; a rung fill books the rung (at its PRE-roll
    resting price if a roll is in flight for it) and spawns/joins the coalesced wing batch."""
    actions: list[V33Action] = []
    # wing-leg fill?
    for i, leg in enumerate(st.wing_legs):
        if leg.client_order_id == event.client_order_id:
            legs = list(st.wing_legs)
            if event.count > _ZERO:
                legs[i] = replace(leg, status="filled", fill_price=event.price,
                                  fill_fee=fee(event.price))
            else:
                legs[i] = replace(leg, status="unfilled")
            st = replace(st, wing_legs=tuple(legs))
            st = _maybe_close_set(st, leg.batch)
            return st, actions

    # rung fill? match a ladder order by coid.
    order = next((o for o in st.ladder if o.client_order_id == event.client_order_id), None)
    if order is None:
        return st, actions
    delta = int(event.count)
    # N1: skip lots an amend CROSS already booked for this order_id (the venue echoes the crossed taker
    # fill on the WS ``fill`` channel with a fresh trade_id the driver's dedup cannot catch).
    oid = order.order_id if order.order_id is not None else event.order_id
    if delta > 0 and oid is not None and st.amend_cross_pending.get(oid, 0) > 0:
        pending = st.amend_cross_pending.get(oid, 0)
        skip = min(pending, delta)
        acp = dict(st.amend_cross_pending)
        if pending - skip > 0:
            acp[oid] = pending - skip
        else:
            acp.pop(oid, None)
        st = replace(st, amend_cross_pending=acp)
        delta -= skip
    if delta > 0:
        # book at the order's RESTING price (pre-roll: if a roll is in flight the order is still resting
        # at ``order.price`` on the venue until the amend acks) — golden (f).
        n = order.price
        st, wa = _book_rung_fill(params, st, order.client_order_id, n, delta, order.rung,
                                 order.E_rung, now)
        actions += wa
    return st, actions


# ---------------------------------------------------------------------------
# Rung fill booking + coalesced wings
# ---------------------------------------------------------------------------
def _book_rung_fill(
    params: V33Params, st: V33State, coid: str, price: Decimal, delta: int, rung: int,
    E_rung: Decimal, now: float
) -> tuple[V33State, list[V33Action]]:
    """Book ``delta`` filled lots of rung ``coid`` at ``price``: record the RungFill, remove the rung
    from the ladder (Q3 no refill), and COALESCE the fill into the open wing group (Q2). The fill's margin
    is DERIVED from ``price`` vs the current n_top (Brad R4: the index is never a stored identity); the
    2-slot is tied to this fill via ``filled_at``."""
    booked = dict(st.rest_booked_by_coid)
    booked[coid] = booked.get(coid, 0) + int(delta)
    # remove the (now filled) rung from the live ladder; a filled rung is not refilled inside the window.
    ladder = st.ladder
    order = next((o for o in ladder if o.client_order_id == coid), None)
    order_id = order.order_id if order is not None else None
    # capture the bucket the rung ACTUALLY rested on, at FILL time (MUST-FIX-3): from the order's own
    # bucket_Sd (the rung was placed on it), falling back to the ladder's / current spot bucket.
    fill_Sd = order.bucket_Sd if order is not None and order.bucket_Sd is not None else (
        st.rest_bucket_Sd if st.rest_bucket_Sd is not None else st.spot_Sd)
    fill_bucket_ticker = st.bucket_tickers.get(fill_Sd) if fill_Sd is not None else None
    fill_Su = (fill_Sd + params.bucket_width) if fill_Sd is not None else None
    if order is not None:
        remaining = order.count - booked[coid]
        if remaining <= 0:
            ladder = _drop_order(ladder, coid)
        else:
            # a partial fill of a rung (only possible if lots_per_rung > 1): keep the remainder resting.
            ladder = _replace_order(ladder, coid, count=remaining)
    # DERIVE the fill's margin from the price vs the current n_top (the passed rung/E_rung are the caller's
    # fallback label when n_top is momentarily unknown). This is the literal "index derived from price".
    if st.n_top is not None:
        m = _margin_of(params, st.n_top, price)
        rung = m - _emin_cents(params)
        E_rung = params.E_min + rung * _CENT
    else:
        m = _emin_cents(params) + rung
    rf = RungFill(rung=rung, E_rung=E_rung, price=price, count=int(delta), server_ts=now,
                  coid=coid, order_id=order_id, W=st.W, n_top=st.n_top,
                  bucket_ticker=fill_bucket_ticker, bucket_Sd=fill_Sd, bucket_Su=fill_Su)
    # MARGIN ARRAY (Brad R4): mark this fill's margin as consumed (state 2) and tie the 2-slot to THIS
    # fill (``filled_at``). A rare fill on a STRANDED order (margin outside the nominal array) is still
    # booked (rungs_filled ++, the exposure cap holds) but leaves the array untouched — the exposure
    # guard, not the array, is the true K-lot cap.
    margin_state = st.margin_state
    filled_at = st.filled_at
    if 0 <= m < len(margin_state):
        if margin_state[m] != 2:
            ms = list(margin_state)
            ms[m] = 2
            margin_state = tuple(ms)
        fa = dict(filled_at)
        fa[m] = rf
        filled_at = fa
    st = replace(
        st, ladder=ladder, rest_fills=st.rest_fills + (rf,),
        rungs_filled=st.rungs_filled + 1, rest_booked_by_coid=booked,
        margin_state=margin_state, filled_at=filled_at,
    )
    st = _coalesce_add(params, st, rf, now)
    # latch the allotment when every rung has filled (ladder empty via fills) or max_sets reached.
    if (st.rungs_filled >= params.max_sets_per_hour
            or (not st.ladder and not st.awaiting_replace and not st.rolls_in_flight
                and st.outstanding_cancels == 0)):
        st = replace(st, rest_allotment_done=True)
    st, wa = _wing_step(params, st, now)
    st = _sync_wing_mirrors(st)
    return st, wa


def _coalesce_flush(params: V33Params, st: V33State, now: float) -> V33State:
    """Close the open coalescing group into a WingBatch once the clock passes first_ts + window."""
    g = st.coalesce_open
    if g is None:
        return st
    if now - g.first_ts > params.wing_coalesce_ms / 1000.0:
        batch = WingBatch(index=g.index, server_ts=g.first_ts, fills=g.fills)
        return replace(st, wing_batches=st.wing_batches + (batch,), coalesce_open=None)
    return st


def _coalesce_add(params: V33Params, st: V33State, rf: RungFill, now: float) -> V33State:
    """Add a rung fill to the coalescing group: flush an EXPIRED group first (starting a new one),
    then either open a new group or extend the still-open one (Q2: within wing_coalesce_ms of first)."""
    g = st.coalesce_open
    if g is not None and now - g.first_ts > params.wing_coalesce_ms / 1000.0:
        st = _coalesce_flush(params, st, now)
        g = None
    if g is None:
        idx = st.next_batch_index
        st = replace(
            st, coalesce_open=CoalesceGroup(index=idx, first_ts=now, fills=(rf,)),
            next_batch_index=idx + 1,
        )
    else:
        st = replace(st, coalesce_open=replace(g, fills=g.fills + (rf,)))
    return st


# ---------------------------------------------------------------------------
# Wings (take / retry / complete) — sized to the coalesced batch total
# ---------------------------------------------------------------------------
def _batch_legs(st: V33State, index: int) -> list[WingLeg]:
    return [l for l in st.wing_legs if l.batch == index]


def _wing_step(params: V33Params, st: V33State, now: float) -> tuple[V33State, list[V33Action]]:
    """Close any expired coalescing group, then take (or retry) the wings for every CLOSED batch."""
    st = _coalesce_flush(params, st, now)
    actions: list[V33Action] = []
    if not st.wing_batches:
        return _sync_wing_mirrors(st), actions
    t_to_close = st.close_epoch - now
    if t_to_close < params.no_orders_after_s_to_settle:
        for b in list(st.wing_batches):
            if b.completed or b.one_legged:
                continue
            legs = _batch_legs(st, b.index)
            incomplete = (not legs) or any(l.status != "filled" for l in legs)
            if incomplete:
                st = replace(st, wing_batches=_replace_batch(st.wing_batches, b.index, one_legged=True))
        return _sync_wing_mirrors(st), actions

    prices = _wing_prices(st, now, params)
    if prices is None:
        return _sync_wing_mirrors(st), actions
    ya, na = prices
    for index in [b.index for b in st.wing_batches]:
        b = next((x for x in st.wing_batches if x.index == index), None)
        if b is None or b.completed:
            continue
        if not b.taken:
            st, a = _take_batch(params, st, b, ya, na, now)
        else:
            st, a = _retry_batch(params, st, b, ya, na, now)
        actions += a
    return _sync_wing_mirrors(st), actions


def _replace_batch(batches: tuple[WingBatch, ...], index: int, **fields) -> tuple[WingBatch, ...]:
    out = list(batches)
    for i, b in enumerate(out):
        if b.index == index:
            out[i] = replace(b, **fields)
            break
    return tuple(out)


def _batch_lock(b: WingBatch, w_paid: Decimal) -> Decimal:
    """The batch's TOTAL lock across its coalesced rungs: sum_f count_f * (2 - (n_f + fee(n_f)) - W)."""
    total = _ZERO
    for f in b.fills:
        total += f.count * lock_value(f.price, w_paid)
    return total


def _take_batch(
    params: V33Params, st: V33State, b: WingBatch, ya: Decimal, na: Decimal, now: float
) -> tuple[V33State, list[V33Action]]:
    """UNCONDITIONAL initial take (ruling F-2) of ONE coalesced batch's two wings at ask + wing_margin,
    sized to the batch's TOTAL filled count. ``lock`` (batch total) is computed for the journal/report."""
    w_paid = ya + fee(ya) + na + fee(na)
    lock = _batch_lock(b, w_paid)
    count = b.total_count
    coid_y, st = _mint_coid(st)
    coid_n, st = _mint_coid(st)
    yes_limit = min(ya + params.wing_margin, _LIMIT_CEILING)
    no_limit = min(na + params.wing_margin, _LIMIT_CEILING)
    legs = (
        LegOrder(st.strike_tickers.get(st.spot_Sd, ""), BUY_YES, "buy", count, yes_limit),
        LegOrder(st.strike_tickers.get(st.spot_Su, ""), BUY_NO, "buy", count, no_limit),
    )
    new_legs = (
        WingLeg(legs[0].ticker, BUY_YES, count, yes_limit, coid_y, batch=b.index),
        WingLeg(legs[1].ticker, BUY_NO, count, no_limit, coid_n, batch=b.index),
    )
    st = replace(
        st, wing_legs=st.wing_legs + new_legs,
        wing_batches=_replace_batch(st.wing_batches, b.index, taken=True),
    )
    return st, [_mk(ActionKind.TAKE_WINGS, st.shakedown, legs=legs, count=count, lock=lock)]


def _retry_batch(
    params: V33Params, st: V33State, b: WingBatch, ya: Decimal, na: Decimal, now: float
) -> tuple[V33State, list[V33Action]]:
    """Retry any unfilled leg of ONE batch at the fresh ask, gated by the projected batch lock staying
    at/above lock_floor (ruling F-2). The already-held leg forms the floor, so a deferred retry is
    bounded, not naked."""
    actions: list[V33Action] = []
    legs = _batch_legs(st, b.index)
    count = b.total_count
    n_cost = sum((f.price + fee(f.price)) * f.count for f in b.fills)  # per-lot rest cost, weighted
    projected_wing = _ZERO
    for leg in legs:
        if leg.status == "filled" and leg.fill_price is not None:
            projected_wing += (leg.fill_price + fee(leg.fill_price)) * leg.count
        else:
            ask = ya if leg.side == BUY_YES else na
            projected_wing += (ask + fee(ask)) * leg.count
    projected_lock = _TWO * count - n_cost - projected_wing
    if projected_lock < params.lock_floor * count:
        return st, actions

    legs_out = list(st.wing_legs)
    changed = False
    for i, leg in enumerate(st.wing_legs):
        if leg.batch != b.index or leg.status != "unfilled":
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


def _maybe_close_set(st: V33State, index: int) -> V33State:
    """Both wings of BATCH ``index`` filled -> count it as ``total_count`` completed sets and mark done."""
    b = next((x for x in st.wing_batches if x.index == index), None)
    if b is None or b.completed:
        return _sync_wing_mirrors(st)
    legs = _batch_legs(st, index)
    if legs and all(l.status == "filled" for l in legs):
        st = replace(
            st, sets_done=st.sets_done + b.total_count,
            wing_batches=_replace_batch(st.wing_batches, index, completed=True, one_legged=False),
        )
    return _sync_wing_mirrors(st)


# ---------------------------------------------------------------------------
# The roll (place / roll / cancel / stand-down) — replaces V3.2's _requote
# ---------------------------------------------------------------------------
def _in_window(params: V33Params, st: V33State, now: float) -> bool:
    t_to_close = st.close_epoch - now
    return params.quote_start_s >= t_to_close >= params.quote_end_s


def _cancel_action(st: V33State, order: RestOrder) -> V33Action:
    return _mk(
        ActionKind.CANCEL_REST, st.shakedown,
        order_id=order.order_id, client_order_id=order.client_order_id,
    )


def _remember_cancel_ctx(st: V33State, order: RestOrder) -> V33State:
    """Record an eagerly-cleared populated order so a later OrderCancelled(filled) stays attributable."""
    if order.order_id is None:
        return st
    ctx = dict(st.cancel_ctx)
    ctx[order.order_id] = (order.client_order_id, order.price, order.rung, order.E_rung)
    return replace(st, cancel_ctx=ctx)


def _cancel_all(st: V33State, *, track_outstanding: bool = False) -> tuple[V33State, list[V33Action]]:
    """Cancel EVERY live/pending rung (no new place). Removes them from the ladder and remembers each
    live order's cancel context so a partial fill caught only by the cancel is still booked + hedged."""
    actions: list[V33Action] = []
    n_live = 0
    for o in st.ladder:
        actions.append(_cancel_action(st, o))
        if o.order_id is not None:
            st = _remember_cancel_ctx(st, o)
            n_live += 1
    st = replace(st, ladder=(), rolls_in_flight=(), converging_dir=0)
    if track_outstanding:
        st = replace(st, outstanding_cancels=st.outstanding_cancels + n_live)
    return st, actions


def _standdown(st: V33State, reason: str) -> tuple[V33State, list[V33Action]]:
    if reason == st.last_standdown_reason:
        return replace(st, stand_down_reason=reason), []
    st = replace(st, last_standdown_reason=reason, stand_down_reason=reason)
    return st, [V33Action(kind=ActionKind.STAND_DOWN, reason=reason)]


def _place_action(st: V33State, params: V33Params, coid: str, n: Decimal) -> V33Action:
    exp = st.close_epoch - params.quote_end_s
    return _mk(
        ActionKind.PLACE_REST, st.shakedown,
        ticker=st.bucket_tickers.get(st.spot_Sd, ""), side=BUY_NO, action="buy",
        count=params.lots_per_rung, price=n, expiration_epoch=exp, client_order_id=coid,
    )


def _place_one(
    params: V33Params, st: V33State, price: Decimal, rung: int, E_rung: Decimal, now: float
) -> tuple[V33State, list[V33Action]]:
    """Emit ONE PLACE_REST rung at ``price`` and add it (pending) to the ladder. Used by the bucket /
    first placement (each rung) and by the roll's cancel->create fallback."""
    coid, st = _mint_coid(st)
    order = RestOrder(
        client_order_id=coid, order_id=None, price=price, count=params.lots_per_rung,
        placed_ts=now, live=False, pending=True, bucket_Sd=st.spot_Sd, rung=rung, E_rung=E_rung,
    )
    st = replace(st, ladder=st.ladder + (order,))
    return st, [_place_action(st, params, coid, price)]


def _placeable_open_slots(params: V33Params, st: V33State) -> list[tuple[int, Decimal]]:
    """The open span slots (margin_state == 1) that are placeable at the current n_top (n_min <= price <=
    cap), as (margin, price) sorted top-first (shallowest margin). This is Brad's "the 1s in the array"."""
    assert st.n_top is not None
    out: list[tuple[int, Decimal]] = []
    for m in _open_slots(st):
        price = _price_of_margin(params, st.n_top, m)
        if price >= params.n_min and (st.cap is None or price <= st.cap):
            out.append((m, price))
    return out


def _place_all(params: V33Params, st: V33State, now: float) -> tuple[V33State, list[V33Action]]:
    """Place one order at EACH placeable OPEN slot in the margin array (Brad's model, R4). After a partial
    sweep of margins 5..k, this places the REMAINING open margins (k+1..15) on the new/first ticker — NOT
    the nominal top K−filled — and the open-span size is exactly K − filled_in_range, so exposure
    (rungs_filled + placed) <= K. If the allotment is already spent (no open placeable slot and filled >=
    max_sets), latch ``rest_allotment_done``. Counts as ONE ladder placement (the debounce anchor)."""
    assert st.n_top is not None
    actions: list[V33Action] = []
    slots = _placeable_open_slots(params, st)
    # never exceed the remaining allotment (belt-and-braces to the open-span sizing).
    budget = params.max_sets_per_hour - st.rungs_filled
    if budget <= 0 or (not slots and st.rungs_filled >= params.max_sets_per_hour):
        return replace(st, rest_allotment_done=True), actions
    for m, price in slots[:budget]:
        rung = m - _emin_cents(params)
        st, a = _place_one(params, st, price, rung, params.E_min + rung * _CENT, now)
        actions += a
    if actions:
        times = tuple(t for t in st.replace_times if now - t <= 60.0) + (now,)
        st = replace(
            st, rest_bucket_Sd=st.spot_Sd, converging_dir=0,
            last_replace_ts=now, replace_count=st.replace_count + 1, replace_times=times,
        )
    return st, actions


def _emit_roll(
    params: V33Params, st: V33State, order: RestOrder, target_price: Decimal, target_margin: int,
    now: float
) -> tuple[V33State, RollPending, V33Action]:
    """Build ONE convergence amend moving ``order`` to ``target_price``. Mints a new coid (a price change
    forfeits queue). Returns the new state, the RollPending to register, and the AMEND action. Counted on
    CONFIRM (``_apply_amended``). The caller registers the roll + emits."""
    new_coid, st = _mint_coid(st)
    rp = RollPending(
        order_id=order.order_id, old_coid=order.client_order_id, new_coid=new_coid,
        target_price=target_price, target_margin=target_margin, started_ts=now,
    )
    exp = st.close_epoch - params.quote_end_s
    action = _mk(
        ActionKind.AMEND_REST, st.shakedown,
        order_id=order.order_id, ticker=st.bucket_tickers.get(st.spot_Sd, ""), side=BUY_NO,
        action="buy", count=params.lots_per_rung, price=target_price, expiration_epoch=exp,
        client_order_id=order.client_order_id, updated_client_order_id=new_coid,
    )
    return st, rp, action


def _converge(params: V33Params, st: V33State, now: float) -> tuple[V33State, list[V33Action]]:
    """The place / converge / cancel / stand-down decision on the ladder (Brad's margin-array model, R4;
    replaces V3.2 ``_requote`` and the R2 single-roll+fast-shift). Drives the live orders toward one order
    per placeable OPEN slot of ``margin_state``, moving up to ``max_amends_in_flight`` at a time."""
    actions: list[V33Action] = []
    t_to_close = st.close_epoch - now

    # allotment complete: every rung filled (or max_sets). Stop quoting; cancel any lingering rung.
    if st.rest_allotment_done:
        st, ca = _cancel_all(st)
        return st, ca

    # replace-rate alarm (trailing 60 s) — counts placements + amends.
    recent = tuple(t for t in st.replace_times if now - t <= 60.0)
    if len(recent) > params.replace_rate_alarm_per_min and not st.stood_down:
        st, ca = _cancel_all(st)
        st = replace(st, stood_down=True)
        st, sa = _standdown(st, "replace_rate")
        return st, ca + sa

    # no-quote reasons (SAME ladder as V3.2's requote gate)
    no_quote_reason = None
    if st.stood_down:
        no_quote_reason = "stood_down"
    elif t_to_close < params.quote_end_s:
        no_quote_reason = "past_quote_end"
    elif t_to_close > params.quote_start_s:
        return st, actions  # warmup: nothing to cancel, no stand-down
    elif st.spot_Sd is None:
        no_quote_reason = "no_spot_bucket"
    elif st.spot_bucket_stale:
        no_quote_reason = "stale_bucket"
    elif st.W is None:
        no_quote_reason = "stale_or_missing_wing"
    elif st.n_top is None or st.n_top < params.n_min:
        no_quote_reason = "n_below_min"

    if no_quote_reason is not None:
        # BUCKET-FLAP FIX item 2 (2026-09-23): a STALE/MISSING-WING stand-down with a LIVE ladder does NOT
        # cancel immediately. HOLD the rests (no new places/rolls, no cancel) for up to ``stand_down_hold_ms``;
        # if freshness returns (below), resume; if the hold elapses, cancel-all as before. The hold is for
        # the RESTS only: a rung fill during the hold still books + spawns its wing batch (that path is
        # independent of _converge, and wings are taker orders priced from the live book at fill time).
        if (no_quote_reason == "stale_or_missing_wing" and params.stand_down_hold_ms > 0
                and (st.ladder or st.rolls_in_flight)):
            if st.hold_since is None:
                st = replace(st, hold_reason=no_quote_reason, hold_since=now)
                return st, [V33Action(kind=ActionKind.STAND_DOWN,
                                      reason="stale_or_missing_wing_hold")]  # journalled as stand_down_hold
            if (now - st.hold_since) * 1000.0 < params.stand_down_hold_ms:
                return st, actions   # still holding: keep the rests, emit nothing new
            # hold elapsed -> cancel-all + the real stand-down (journalled as stand_down_cancel). Set the
            # dedup reason to the PLAIN form so subsequent stale ticks (ladder now empty) dedup silently.
            st = replace(st, hold_reason=None, hold_since=None,
                         last_standdown_reason="stale_or_missing_wing",
                         stand_down_reason="stale_or_missing_wing")
            st, ca = _cancel_all(st)
            return st, ca + [V33Action(kind=ActionKind.STAND_DOWN,
                                       reason="stale_or_missing_wing_cancel")]
        # any other no-quote reason (or hold disabled / nothing to protect): cancel + stand down as before.
        if st.hold_since is not None:
            st = replace(st, hold_reason=None, hold_since=None)
        st, ca = _cancel_all(st)
        st, sa = _standdown(st, no_quote_reason)
        return st, ca + sa

    # healthy: freshness returned. If we were HOLDING a stale/missing-wing stand-down, RESUME (the rests
    # were kept; the convergence below picks up where it left off).
    resume_actions: list[V33Action] = []
    if st.hold_since is not None:
        st = replace(st, hold_reason=None, hold_since=None)
        resume_actions.append(V33Action(kind=ActionKind.STAND_DOWN,
                                        reason="stale_or_missing_wing_resume"))  # -> stand_down_resume
    actions += resume_actions
    # clear any stale stand-down reason.
    if st.last_standdown_reason is not None:
        st = replace(st, last_standdown_reason=None, stand_down_reason=None)

    # bucket change: cancel ALL rungs on the old bucket, place the open slots on the new one after confirms.
    if (st.rest_bucket_Sd is not None and st.rest_bucket_Sd != st.spot_Sd
            and not st.rolls_in_flight and st.ladder):
        st, ca = _cancel_all(st, track_outstanding=True)
        st = replace(st, awaiting_replace=True, rest_bucket_Sd=None)
        return st, actions + ca

    # hold while bucket-change cancels are unconfirmed.
    if st.awaiting_replace and st.outstanding_cancels > 0:
        return st, actions

    # place: first placement, or place the open slots after a bucket-change cancel confirmed.
    if not st.ladder and not st.rolls_in_flight:
        if st.awaiting_replace:
            st = replace(st, awaiting_replace=False)
            st, pa = _place_all(params, st, now)
            return st, actions + pa
        if st.rungs_filled > 0 and not _placeable_open_slots(params, st):
            # every open slot filled and none refilled (Q3) -> the allotment is done.
            st = replace(st, rest_allotment_done=True)
            return st, actions
        st, pa = _place_all(params, st, now)
        return st, actions + pa

    # hold the convergence while any order is still PENDING an ack (an initial placement or a just-issued
    # create) — it has no order_id to amend, and its price is committed; converge once it is live.
    if any(o.pending for o in st.ladder):
        return st, actions

    # ------------------------------------------------------------------
    # CONVERGENCE: drive live orders toward one order per placeable open slot.
    # ------------------------------------------------------------------
    assert st.n_top is not None
    # target price -> margin for each placeable open slot.
    target = {price: m for (m, price) in _placeable_open_slots(params, st)}
    inflight_old = {r.old_coid for r in st.rolls_in_flight}
    inflight_targets = {r.target_price for r in st.rolls_in_flight}
    occupied = {o.price for o in st.ladder}
    # OUT = LIVE orders (acked, not already being amended) whose price is not a desired target.
    OUT = [o for o in st.ladder if o.live and o.order_id is not None
           and o.client_order_id not in inflight_old and o.price not in target]
    # VACANT = target prices with no committed order (live/pending price or in-flight target).
    VACANT = sorted(p for p in target if p not in occupied and p not in inflight_targets)

    if not OUT and not VACANT:
        # converged (nothing to move/place); clear the convergence flag once nothing is in flight.
        if not st.rolls_in_flight and st.converging_dir != 0:
            st = replace(st, converging_dir=0)
        return st, actions

    # direction of the need (for the start-debounce sign-flip): OUT below the open span => n_top dropped
    # (-1); OUT above => n_top rose (+1). VACANT-only => a released/new slot; treat as a fresh placement.
    open_span = _open_slots(st)
    min_open = open_span[0] if open_span else _emin_cents(params)
    max_open = open_span[-1] if open_span else _emin_cents(params) + params.rungs - 1
    if OUT:
        below = sum(1 for o in OUT if _margin_of(params, st.n_top, o.price) < min_open)
        above = sum(1 for o in OUT if _margin_of(params, st.n_top, o.price) > max_open)
        direction = -1 if below >= above else +1
    else:
        direction = st.converging_dir if st.converging_dir != 0 else +1

    mid = st.converging_dir == direction and (st.converging_dir != 0 or st.rolls_in_flight)
    since_ms = (now - (st.last_replace_ts if st.last_replace_ts is not None else -1e18)) * 1000.0
    if not mid and since_ms < params.deb_ms:
        return st, actions  # still waiting out the START debounce for a fresh convergence / sign flip

    st = replace(st, converging_dir=direction)
    budget = params.max_amends_in_flight - len(st.rolls_in_flight)
    if budget <= 0:
        return st, actions  # already at the concurrency cap; wait for acks

    # pair OUT and VACANT greedily by price (minimal movement): move OUT[i] -> VACANT[i] via AMEND.
    # AMENDs are counted toward the replace-rate alarm on CONFIRM (``_apply_amended``), not here, so they
    # are NOT added to ``replace_times`` at emit (that would double-count). Shrink-cancels and vacant-
    # creates have no confirm-count path, so they ARE counted here on emit.
    OUT_sorted = sorted(OUT, key=lambda o: o.price)
    pairs = min(len(OUT_sorted), len(VACANT), budget)
    rolls = list(st.rolls_in_flight)
    for i in range(pairs):
        o = OUT_sorted[i]
        tprice = VACANT[i]
        st, rp, action = _emit_roll(params, st, o, tprice, target[tprice], now)
        rolls.append(rp)
        actions.append(action)
    st = replace(st, rolls_in_flight=tuple(rolls))
    remaining = budget - pairs
    extra_times: list[float] = []      # replace_times entries for the NON-amend ops (cancel/create)
    if remaining > 0 and len(OUT_sorted) > pairs:
        # extra OUT with no vacant slot (n_min/cap suppressed the deep end) -> CANCEL (shrink).
        for o in OUT_sorted[pairs:pairs + remaining]:
            st = _remember_cancel_ctx(st, o)
            actions.append(_cancel_action(st, o))
            st = replace(st, ladder=_drop_order(st.ladder, o.client_order_id),
                         outstanding_cancels=st.outstanding_cancels + (1 if o.order_id else 0))
            extra_times.append(now)
    elif remaining > 0 and len(VACANT) > pairs:
        # extra VACANT with no OUT order (a suppressed slot became placeable) -> CREATE, but never past K.
        for tprice in VACANT[pairs:pairs + remaining]:
            if len(st.ladder) + st.rungs_filled >= params.rungs:
                break
            m = target[tprice]
            rung = m - _emin_cents(params)
            st, pa = _place_one(params, st, tprice, rung, params.E_min + rung * _CENT, now)
            actions += pa
            extra_times.append(now)

    if actions:
        new_times = tuple(t for t in st.replace_times if now - t <= 60.0) + tuple(extra_times)
        st = replace(st, last_replace_ts=now, replace_times=new_times)
    return st, actions
