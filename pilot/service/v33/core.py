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

  * THE ROLL (``_roll``, replacing V3.2's ``_requote``): when W moves so ``n_top`` changes by one cent,
    the ladder does NOT re-price K orders — it moves ONE order from the end that fell off to the end
    that opened up (Brad's exact mechanism):
      - n_top DOWN 1c (wings dearer) -> amend the current TOP order (shallowest) to ``bottom - 1c``; it
        becomes the new deepest rung. The other K-1 orders are untouched and KEEP QUEUE.
      - n_top UP 1c -> amend the current BOTTOM order (deepest) to ``top + 1c`` (new shallowest).
    A 2c move = two rolls, STRICTLY SEQUENTIAL: the second is issued only after the first amend is
    ACKNOWLEDGED (``roll_pending`` holds the one moving order; further W moves queue). Amend-first via
    ``OrderAmended`` (PR #59 semantics); the executor's cancel -> confirm -> create is the fallback,
    surfacing to the core as an ``OrderCancelled`` for the rolling order -> the core places a fresh rung
    at the roll's target price. The tol/deb_ms gate is applied to the ``n_top`` signal EXACTLY as V3.2
    applies it to its single price. Bucket change -> cancel ALL K, then place ALL K on the new ticker
    (V3.2's cancel-confirm-then-place discipline generalised to K).

  * Fill of rung k -> a rung fill carrying ``rung`` and ``E_rung`` -> a coalesced ``WingBatch`` (Q2):
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
    rung: int
    E_rung: Decimal


@dataclass(frozen=True)
class RungFill:
    """A fill of ONE rung: ``price`` = the resting n, ``count`` lots, tagged with ``rung`` / ``E_rung``
    (captured at fill time) so the falsifier can compute per-rung solved-vs-realised lock."""

    rung: int
    E_rung: Decimal
    price: Decimal
    count: int
    server_ts: float


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
    """The one in-flight roll (amend of ONE rung moving end-to-end). Further W moves queue until this
    resolves via ``OrderAmended`` (success) or ``OrderCancelled`` (the executor's cancel->create
    fallback -> the core then places a fresh rung at ``target_price``)."""

    order_id: str | None
    old_coid: str
    new_coid: str
    target_price: Decimal
    target_rung: int
    target_E_rung: Decimal
    direction: int            # the anchor step this roll effects: -1 (n_top down) or +1 (n_top up)
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
    anchor_n_top: Decimal | None = None                 # the n_top the ladder currently represents;
                                                        # the roll trigger is n_top vs THIS (not the top
                                                        # survivor's price, which fills would move) so a
                                                        # partial sweep never spuriously triggers a roll.
    roll_pending: RollPending | None = None             # the one in-flight roll
    converging_dir: int = 0                              # 0 = not converging; +/-1 = an active multi-cent
                                                        # convergence in that direction. deb_ms debounces
                                                        # only the START; a same-sign continuation rolls
                                                        # each acked cent without re-debounce; a sign flip
                                                        # re-debounces (Round 2 pacing).
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
        return cls(
            close_time=close_time,
            close_epoch=int(close_epoch),
            bucket_map=dict(bucket_map),
            shakedown=shakedown,
            shadows=shadows,
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
          asserted pre-fill); * at most one roll in flight, and its moving order is a ladder member unless
          it has already filled (golden f); * a taken wing batch has exactly two legs sized to its total;
          * ``rungs_filled`` equals the number of booked rung fills."""
        lad = self.ladder
        assert len(lad) <= params.rungs, f"ladder has {len(lad)} > K={params.rungs} rests"
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
        # roll / bucket-change mutual exclusion + the roll's moving order is a ladder member (unless the
        # moving rung has already filled and been dropped -- golden f, then the late ack just clears it).
        assert not (self.roll_pending is not None and self.awaiting_replace), (
            "roll and bucket-change cannot be in flight together"
        )
        if self.roll_pending is not None:
            n_moving = sum(1 for o in lad if o.client_order_id == self.roll_pending.old_coid)
            assert n_moving <= 1, f"roll_pending matches {n_moving} ladder orders (must be <= 1)"
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
        # consecutiveness only before any fill (a filled rung is not refilled -> legitimate gaps)
        if self.rungs_filled == 0 and len(prices) >= 2:
            s = sorted(prices, reverse=True)
            for a, b in zip(s, s[1:]):
                assert a - b == _CENT, f"ladder prices not consecutive cents: {s}"


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


def _sync_wing_mirrors(st: V33State) -> V33State:
    """Re-derive the ``one_legged`` mirror from the batch state (kept for L2/ledger compatibility)."""
    one_legged = any(b.one_legged for b in st.wing_batches)
    return replace(st, one_legged=one_legged)


def _top_order(st: V33State) -> RestOrder | None:
    """The shallowest (highest-price) rung, or None if the ladder is empty."""
    return max(st.ladder, key=lambda o: o.price) if st.ladder else None


def _bottom_order(st: V33State) -> RestOrder | None:
    """The deepest (lowest-price) rung, or None if the ladder is empty."""
    return min(st.ladder, key=lambda o: o.price) if st.ladder else None


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
        st, qa = _roll(params, st, now)
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
        st, qa = _roll(params, st, now)
        return st, wa + qa

    return st, actions


# ---------------------------------------------------------------------------
# Context (spot / W / cap / n_top / shadows / E_rung refresh)
# ---------------------------------------------------------------------------
def _recompute_context(params: V33Params, st: V33State, now: float) -> V33State:
    """Re-derive spot bucket, W, cap, the ladder-top ``n_top``, every shadow n, and refresh each live
    rung's LIVE labels (``rung`` AND ``E_rung``) from the current n_top. Uses the UNCHANGED V3.2
    spot/W/cap law (imported)."""
    spot_Sd = _select_spot(st)
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


def _apply_amended(
    params: V33Params, st: V33State, event: OrderAmended, now: float
) -> tuple[V33State, list[V33Action]]:
    """A roll amend confirm. The order_id PERSISTS; the moved rung updates price + coid + rung + E_rung
    IN PLACE. ``rest_booked_by_coid`` carries forward (order_id persists, coid rotates). Counts one
    confirmed roll (replace_count / replace_times / roll_count / roll_single_order_count). A cross fill
    (fill_count > 0) books its own rung fill at the venue average price; then continue converging."""
    actions: list[V33Action] = []
    rp = st.roll_pending
    matched = rp is not None and (
        (event.order_id is not None and rp.order_id == event.order_id)
        or rp.old_coid == event.client_order_id or rp.new_coid == event.client_order_id
    )
    if not matched:
        # amend confirm for an order the core no longer rolls (e.g. it filled first) -> release the hold.
        return replace(st, roll_pending=None), actions

    # the rolling order may have FILLED (removed from ladder) before the amend landed -> just clear.
    order = next((o for o in st.ladder if o.client_order_id == rp.old_coid), None)
    if order is None:
        return replace(st, roll_pending=None), actions

    booked = dict(st.rest_booked_by_coid)
    if rp.new_coid != rp.old_coid and rp.old_coid in booked:
        booked[rp.new_coid] = booked.get(rp.new_coid, 0) + booked.pop(rp.old_coid)

    new_price = event.price if event.price is not None else rp.target_price
    # BLOCKING #2 (reviewer 2026-09-22): derive rung/E_rung from the CURRENT n_top at ACK time, not the
    # emit-time values baked into RollPending — n_top may have moved (W reverted, or the cap bound) while
    # the amend was in flight, and the invariant checks against the current n_top. Fall back to the stored
    # target only when n_top is momentarily unknown (no fresh wing).
    if st.n_top is not None:
        new_rung = _rung_of(st.n_top, new_price)
        new_E = _e_rung(params, st.n_top, new_price)
    else:
        new_rung, new_E = rp.target_rung, rp.target_E_rung
    ladder = _replace_order(
        st.ladder, rp.old_coid,
        price=new_price, client_order_id=rp.new_coid,
        rung=new_rung, E_rung=new_E,
    )
    times = tuple(t for t in st.replace_times if now - t <= 60.0) + (now,)
    anchor = st.anchor_n_top
    if anchor is not None:
        anchor = anchor + rp.direction * _CENT           # the ladder has now shifted one cent
    st = replace(
        st, ladder=ladder, roll_pending=None, rest_booked_by_coid=booked, anchor_n_top=anchor,
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

    # continue converging toward n_top (a 2c move: the next cent, now that this one acked).
    st, ra = _roll(params, st, now)
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

    rp = st.roll_pending
    if rp is not None and event.order_id is not None and rp.order_id == event.order_id:
        # FALLBACK: the amend failed and the executor cancelled -> drop the old rung, place a fresh one
        # at the roll's target price (same end state as a successful amend, fresh queue). Skip the
        # re-place if the order fully filled before the cancel (nothing left to move).
        ladder = _drop_order(st.ladder, rp.old_coid)
        anchor = st.anchor_n_top
        if anchor is not None:
            anchor = anchor + rp.direction * _CENT       # the roll's shift still takes effect
        st = replace(st, ladder=ladder, roll_pending=None, anchor_n_top=anchor)
        still_resting = st.rest_booked_by_coid.get(rp.new_coid, st.rest_booked_by_coid.get(
            rp.old_coid, 0)) < params.lots_per_rung
        # NIT #9 (reviewer 2026-09-22): re-check the cap on the fallback re-place — for an up-roll the cap
        # was only checked at emit; if no_ask dropped meanwhile the target could now sit above the cap.
        # Clamp to the cap and drop the rung if that pushes it below n_min. Derive rung/E_rung from the
        # CURRENT n_top (BLOCKING #2) so the fresh order's labels match the invariant, not emit-time.
        target = rp.target_price
        if st.cap is not None and target > st.cap:
            target = st.cap
        if (not st.rest_allotment_done and still_resting and _in_window(params, st, now)
                and target >= params.n_min):
            if st.n_top is not None:
                r_rung, r_E = _rung_of(st.n_top, target), _e_rung(params, st.n_top, target)
            else:
                r_rung, r_E = rp.target_rung, rp.target_E_rung
            st, pa = _place_one(params, st, target, r_rung, r_E, now)
            actions += pa
        return st, actions

    # bucket-change / stand-down cancel: drop the slot (if still present) and decrement the count.
    if live_order is not None:
        st = replace(st, ladder=_drop_order(st.ladder, live_order.client_order_id))
    if st.outstanding_cancels > 0:
        st = replace(st, outstanding_cancels=st.outstanding_cancels - 1)
    # a bucket change waiting to re-place: once all cancels confirmed and the ladder is clear, place all.
    st, pa = _roll(params, st, now)
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
    from the ladder (Q3 no refill), and COALESCE the fill into the open wing group (Q2)."""
    rf = RungFill(rung=rung, E_rung=E_rung, price=price, count=int(delta), server_ts=now)
    booked = dict(st.rest_booked_by_coid)
    booked[coid] = booked.get(coid, 0) + int(delta)
    # remove the (now filled) rung from the live ladder; a filled rung is not refilled inside the window.
    ladder = st.ladder
    order = next((o for o in ladder if o.client_order_id == coid), None)
    if order is not None:
        remaining = order.count - booked[coid]
        if remaining <= 0:
            ladder = _drop_order(ladder, coid)
        else:
            # a partial fill of a rung (only possible if lots_per_rung > 1): keep the remainder resting.
            ladder = _replace_order(ladder, coid, count=remaining)
    st = replace(
        st, ladder=ladder, rest_fills=st.rest_fills + (rf,),
        rungs_filled=st.rungs_filled + 1, rest_booked_by_coid=booked,
    )
    st = _coalesce_add(params, st, rf, now)
    # latch the allotment when every rung has filled (ladder empty via fills) or max_sets reached.
    if (st.rungs_filled >= params.max_sets_per_hour
            or (not st.ladder and not st.awaiting_replace and st.roll_pending is None
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
    st = replace(st, ladder=(), roll_pending=None, anchor_n_top=None, converging_dir=0)
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


def _desired_rungs(params: V33Params, n_top: Decimal) -> list[tuple[Decimal, int, Decimal]]:
    """The full desired ladder: (price, rung k, E_rung) for k = 0..K-1 with price >= n_min (truncate
    the bottom). The top honours the cap already (n_top came from solve_n with the cap)."""
    out: list[tuple[Decimal, int, Decimal]] = []
    for k in range(params.rungs):
        price = n_top - k * _CENT
        if price < params.n_min:
            break
        out.append((price, k, params.E_min + k * _CENT))
    return out


def _place_all(params: V33Params, st: V33State, now: float) -> tuple[V33State, list[V33Action]]:
    """Place the whole desired ladder (K_eff rungs). Counts as ONE ladder placement (the debounce anchor
    for the first roll); the per-rung PLACE_RESTs are emitted together."""
    assert st.n_top is not None
    actions: list[V33Action] = []
    for price, rung, E_rung in _desired_rungs(params, st.n_top):
        st, a = _place_one(params, st, price, rung, E_rung, now)
        actions += a
    if actions:
        times = tuple(t for t in st.replace_times if now - t <= 60.0) + (now,)
        st = replace(
            st, rest_bucket_Sd=st.spot_Sd, anchor_n_top=st.n_top, converging_dir=0,
            last_replace_ts=now, replace_count=st.replace_count + 1, replace_times=times,
        )
    return st, actions


def _emit_roll(
    params: V33Params, st: V33State, order: RestOrder, target_price: Decimal, direction: int,
    now: float
) -> tuple[V33State, list[V33Action]]:
    """Amend ONE rung end-to-end (Brad's roll). Mints a new coid (a price change forfeits queue), sets
    ``roll_pending`` (blocks any further roll until confirm), and the moved rung's target rung/E_rung
    are derived from the current n_top. Counted on CONFIRM (``_apply_amended``), like V3.2."""
    assert st.n_top is not None
    new_coid, st = _mint_coid(st)
    # emit-time fallback labels only; _apply_amended re-derives from the CURRENT n_top at ack (BLOCKING #2).
    target_rung = _rung_of(st.n_top, target_price)
    target_E = _e_rung(params, st.n_top, target_price)
    rp = RollPending(
        order_id=order.order_id, old_coid=order.client_order_id, new_coid=new_coid,
        target_price=target_price, target_rung=target_rung, target_E_rung=target_E,
        direction=direction, started_ts=now,
    )
    st = replace(st, roll_pending=rp)
    exp = st.close_epoch - params.quote_end_s
    action = _mk(
        ActionKind.AMEND_REST, st.shakedown,
        order_id=order.order_id, ticker=st.bucket_tickers.get(st.spot_Sd, ""), side=BUY_NO,
        action="buy", count=params.lots_per_rung, price=target_price, expiration_epoch=exp,
        client_order_id=order.client_order_id, updated_client_order_id=new_coid,
    )
    return st, [action]


def _roll(params: V33Params, st: V33State, now: float) -> tuple[V33State, list[V33Action]]:
    """The place / roll / cancel / stand-down decision on the ladder (replaces V3.2 ``_requote``)."""
    actions: list[V33Action] = []
    t_to_close = st.close_epoch - now

    # allotment complete: every rung filled (or max_sets). Stop quoting; cancel any lingering rung.
    if st.rest_allotment_done:
        st, ca = _cancel_all(st)
        return st, ca

    # replace-rate alarm (trailing 60 s) — counts rolls + the ladder placement.
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
        st, ca = _cancel_all(st)
        st, sa = _standdown(st, no_quote_reason)
        return st, ca + sa

    # healthy: clear any stale stand-down reason.
    if st.last_standdown_reason is not None:
        st = replace(st, last_standdown_reason=None, stand_down_reason=None)

    # bucket change: cancel ALL rungs on the old bucket, place ALL on the new one after confirms.
    if (st.rest_bucket_Sd is not None and st.rest_bucket_Sd != st.spot_Sd
            and st.roll_pending is None and st.ladder):
        st, ca = _cancel_all(st, track_outstanding=True)
        st = replace(st, awaiting_replace=True, rest_bucket_Sd=None)
        return st, actions + ca

    # hold while a roll is in flight, or bucket-change cancels are unconfirmed, or any rung is pending.
    if st.roll_pending is not None:
        return st, actions
    if st.awaiting_replace and st.outstanding_cancels > 0:
        return st, actions
    if any(o.pending for o in st.ladder):
        return st, actions

    # place: first placement, or place-all after a bucket-change cancel confirmed, or an empty ladder.
    if not st.ladder:
        if st.awaiting_replace:
            st = replace(st, awaiting_replace=False)
            st, pa = _place_all(params, st, now)
            return st, actions + pa
        if st.rungs_filled > 0:
            # every rung filled and none refilled (Q3) -> the allotment is done.
            st = replace(st, rest_allotment_done=True)
            return st, actions
        st, pa = _place_all(params, st, now)
        return st, actions + pa

    # a live ladder on the SAME bucket: roll toward n_top. The trigger is n_top vs the ANCHOR (the n_top
    # the ladder represents), NOT the top survivor's price — so a partial sweep (which removes the top
    # rungs) never spuriously triggers a roll while W is unchanged.
    #
    # PACING (Round 2, reviewer Q4/Q5 + Q-ROLL-DEB): ``deb_ms`` debounces only the START of a convergence.
    # Once committed (``converging_dir`` == the move's sign), each subsequent cent rolls as soon as the
    # prior amend acks (this ``_roll`` is re-entered from ``_apply_amended``) with NO re-debounce, while
    # the sign is unchanged; a SIGN FLIP re-debounces. And a LARGE jump (|dn| >= fast_shift_min_cents,
    # e.g. a cap crash stranding most of the ladder above a bound cap) shifts the WHOLE ladder in ONE
    # step (cancel-all/place-all, like a bucket change) instead of crawling K one-cent rolls.
    top = _top_order(st)
    bottom = _bottom_order(st)
    assert top is not None and bottom is not None and st.n_top is not None
    if st.anchor_n_top is None:
        st = replace(st, anchor_n_top=st.n_top)          # defensive: adopt the current top as anchor
        return st, actions
    dn = st.n_top - st.anchor_n_top
    if abs(dn) < params.tol or dn == _ZERO:
        # converged (or within tolerance) -> the convergence is over.
        if st.converging_dir != 0:
            st = replace(st, converging_dir=0)
        return st, actions
    direction = -1 if dn < _ZERO else +1
    mid = st.converging_dir == direction                 # a same-sign continuation is not re-debounced
    since_ms = (now - (st.last_replace_ts if st.last_replace_ts is not None else -1e18)) * 1000.0
    if not mid and since_ms < params.deb_ms:
        return st, actions                               # still waiting out the START debounce

    # committed to converging in ``direction``.
    cents = int((abs(dn) / _CENT).to_integral_value())
    if cents >= params.fast_shift_min_cents:
        # FAST SHIFT: cancel every rung and re-place the whole ladder at the new n_top in one step (the
        # cap-crash / large-jump path). Place-all runs once the cancels confirm (via _apply_cancelled).
        st, ca = _cancel_all(st, track_outstanding=True)
        st = replace(st, awaiting_replace=True, converging_dir=0)
        return st, actions + ca

    st = replace(st, converging_dir=direction)
    if direction < 0:
        # n_top DOWN: shift the ladder down 1c -> move the TOP order to bottom-1c (new deepest).
        target = bottom.price - _CENT
        if target >= params.n_min:
            st, ra = _emit_roll(params, st, top, target, -1, now)
            return st, actions + ra
        # the ladder cannot go below n_min -> shrink from the top (cancel the shallowest rung).
        st = _remember_cancel_ctx(st, top)
        action = _cancel_action(st, top)
        st = replace(st, ladder=_drop_order(st.ladder, top.client_order_id),
                     anchor_n_top=st.anchor_n_top - _CENT,
                     outstanding_cancels=st.outstanding_cancels + (1 if top.order_id else 0),
                     last_replace_ts=now,
                     replace_count=st.replace_count + 1,
                     replace_times=tuple(t for t in st.replace_times if now - t <= 60.0) + (now,),
                     roll_count=st.roll_count + 1,
                     roll_single_order_count=st.roll_single_order_count + 1)
        return st, actions + [action]
    # n_top UP: shift the ladder up 1c -> move the BOTTOM order to top+1c (new shallowest), provided the
    # new top honours the post-only cap.
    target = top.price + _CENT
    if st.cap is None or target <= st.cap:
        st, ra = _emit_roll(params, st, bottom, target, +1, now)
        return st, actions + ra
    # cannot exceed the cap -> hold (the top already sits at the cap).
    return st, actions
