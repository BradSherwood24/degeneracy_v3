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

L5 AMENDMENT (2026-09-29) — PER-RUNG LOT WEIGHTS (``rung_lots``; Brad's "scale the edge, not 100% at
10c"). Each rung may rest a DIFFERENT number of lots. ``params.rung_lots[k]`` is the configured weight of
rung k (k=0 top .. K-1 deepest); absent in JSON -> ``(lots_per_rung,)*K`` (uniform -> byte-identical).
A 0-weight rung is state "no order" (never placed/targeted). The hour's exposure cap is the CONTRACT
allotment ``sum(rung_lots)`` (``max_sets_per_hour`` stays the coarse rung/fill-event gate).

  THE ROLL / RE-SIZE DECISION. The open margin slots are ANONYMOUS and the convergence pairs a live
  order to a vacant target by minimal price movement (Brad's one-order roll keeps queue). With unequal
  weights an order's slot changes as n_top drifts, so per-slot exactness and the cheap one-order roll are
  in tension. We choose the cheap roll: an order carries the lots it was PLACED with (a slot's weight)
  and KEEPS that count through every roll (the AMEND re-prices, never re-sizes; the amend->cancel->create
  fallback re-places the SAME remaining count, so both roll paths share one end state). Re-sizing to a
  slot's weight happens ONLY when a FRESH order is created for that slot: the initial placement, a
  bucket-change re-place, or a released-slot vacant-create. Consequence: after n_top drifts the middle
  orders keep their placed counts while their slot labels shift ("weight smearing"); the INITIAL
  allocation (the full ladder laid down with per-rung weights) dominates, and n_top moves only a few
  cents per window. The strict "count == weight(current slot)" cannot be a step invariant under any
  cheap-roll scheme (a 1c shift would re-size every order); the asserted invariants are instead
  ``1 <= count <= max(rung_lots)`` per order and ``filled + resting contracts <= sum(rung_lots)``.

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
from service.v33.actions import ActionKind, LegOrder, V33Action, V33ActionKind, twin_kind
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
# tiny epsilon for the print-through trigger boundary (a print exactly at the tick counts), matching
# ``service.v33.shadow.ideal_rung_crosses``.
_EPS = Decimal("0.000000001")
# D4 (2026-09-30 incident): a missing wing leg is retried NO MORE OFTEN than this. The pre-fix retry
# re-fired on EVERY book/trade/clock tick (967 IOC creates in ~96 s, blocking the loop ~60 s and burning
# the daily create budget). The floor caps re-fires at 1/250 ms per missing leg while the lock-floor gate
# and the T-``no_orders_after_s_to_settle`` cutoff still apply, and only one retry is ever in flight per
# leg (a leg is skipped while ``status != "unfilled"``).
WING_RETRY_MIN_INTERVAL_MS = 250
# D3 count precision: Kalshi crypto fills are FRACTIONAL (``count_fp``); lots are carried as Decimal at
# 2dp end-to-end (event -> book -> wing take -> ledger). A whole-lot count keeps 0 decimals so a dry
# (1-lot) window is byte-identical; a fractional fill (e.g. 0.44) is preserved exactly.
_COUNT_Q = Decimal("0.01")


def _q_count(c: Decimal) -> Decimal:
    """Normalise a lot count to 2dp Decimal, stripping a trailing-zero fraction so a whole count is the
    bare integer Decimal (``Decimal('2')`` not ``Decimal('2.00')``) — this keeps whole-lot (dry) journals
    byte-identical while a fractional count (``0.44``) survives quantisation."""
    c = Decimal(c).quantize(_COUNT_Q)
    return c.to_integral_value() if c == c.to_integral_value() else c


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
    count: Decimal        # lots resting (D3: Decimal — a partial fractional fill leaves a fractional rest)
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
    count: Decimal        # lots filled in THIS event (D3: Decimal — Kalshi crypto fills are fractional)
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
    # PRINT-THROUGH (2026-09-26): a bucket-NO leg bought as an IOC TAKER by the `complete` stall branch
    # (not a maker rung fill). The ledger applies the taker fee to a taker leg (a maker rung fill is fee 0).
    taker: bool = False
    # L5 (2026-09-29): the CONFIGURED lot weight of this fill's rung (rung_lots[rung]) at fill time, for
    # the ledger/report allocation table. ``count`` is the lots filled in THIS event (may be < weight on a
    # partial); ``weight`` is the rung's full configured size. None when the fill's margin is out of range.
    weight: int | None = None
    # GATE A (2026-10-03 02:00Z naked fill): a fill on an EXECUTOR-OWNED order the core no longer had on its
    # ladder (evicted, cancelled, or never known), booked from the fill event itself and HEDGED. True only
    # when the price is not a legitimate rung of the current ladder geometry (the margin array is then left
    # untouched); a legitimate-rung orphan is booked as an ordinary rung fill with ``orphan`` False.
    orphan: bool = False


@dataclass(frozen=True)
class WingLeg:
    """One taker completion leg. ``status`` in {"pending","filled","unfilled"}; ``batch`` is the index
    of the ``WingBatch`` this leg belongs to. ``wing_legs`` is a FLAT tuple across all batches."""

    ticker: str
    side: str
    count: Decimal        # lots (D3: Decimal — sized to the batch's filled amount)
    limit: Decimal
    client_order_id: str
    status: str = "pending"
    fill_price: Decimal | None = None
    fill_fee: Decimal | None = None
    batch: int = 0
    # D4 (2026-09-30): when this leg last had a RETRY_WING emitted for it. The retry is gated to no more
    # often than ``WING_RETRY_MIN_INTERVAL_MS``; ``None`` = never retried (the initial take is unbounded).
    last_retry_ts: float | None = None
    # D5 (2026-09-30, Brad): lots of this leg NETTED against an opposite-side filled leg on the SAME
    # market (adjacent-bucket overlap: A's NO@Su-strike and B's YES@Sd-strike are one ticker). The venue
    # nets +YES/+NO to flat and credits $1/contract immediately; ``netted`` is closed (realised now, NOT
    # held to settlement). ``count - netted`` is the portion still held.
    netted: Decimal = _ZERO


@dataclass(frozen=True)
class WingBatch:
    """One taker-completion batch = the rung fills COALESCED within ``wing_coalesce_ms`` (Q2). Its two
    legs (sized to ``total_count``) live in ``V33State.wing_legs`` tagged with this batch's ``index``;
    ``fills`` is the per-rung breakdown so per-rung locks stay computable.

    ``taken`` — the both-wings take has been emitted. ``completed`` — both wings filled (one counted
    SET, or K sets when it coalesced K rungs). ``one_legged`` -- reached the cutoff with a wing missing.

    PRINT-THROUGH (2026-09-26): a batch created by the early-hedge trigger sets ``print_through=True`` and
    ``taken_count`` to the lots it PRE-HEDGED before any rung fill (its legs are sized to ``taken_count``,
    not to ``total_count`` which is 0 until the rungs fill and their RungFills are appended). ``resolved``
    marks a print-through batch whose stall policy has run (complete / unwind / partial) -- it is exempt
    from the leg-sizing invariant while its legs are being unwound."""

    index: int
    server_ts: float
    fills: tuple[RungFill, ...]
    taken: bool = False
    completed: bool = False
    one_legged: bool = False
    print_through: bool = False
    taken_count: Decimal = _ZERO
    resolved: bool = False
    # D2 (2026-09-30 incident): the spot bucket the batch's rungs FILLED on. Its wing strikes are solved
    # from THIS bucket (never the current ``st.spot_Sd``, which may have moved on between fill and take).
    # For a coalesced batch it is the fills' shared ``bucket_Sd``; for a print-through batch it is stamped
    # at trigger time (the pre-hedged rungs' bucket). ``None`` only before any fill is attached.
    bucket_Sd: int | None = None
    # D4: RETRY_WING emissions issued for this batch (journalled per batch for the falsifier/report).
    retries: int = 0

    @property
    def total_count(self) -> Decimal:
        return sum((f.count for f in self.fills), _ZERO)

    @property
    def leg_count(self) -> Decimal:
        """The count the wing legs were sized to: ``taken_count`` for a print-through batch (pre-hedged
        before fills exist), else ``total_count`` (the sum of the rung fills it coalesced)."""
        return self.taken_count if self.print_through else self.total_count


@dataclass(frozen=True)
class NettedPair:
    """D5 (2026-09-30, Brad): a YES leg and a NO leg on the SAME market that the venue netted to flat. It
    arises across ADJACENT buckets — set A (on ``[Sd_A, Su_A)``) holds NO on the ``Su_A`` strike; set B
    (one bucket up, ``[Su_A, Su_A+w)``) takes YES on B's ``Sd`` strike, which IS the ``Su_A`` strike. The
    pair always pays exactly $1/contract, so the venue credits $1 and zeroes the position immediately.
    We book the pair CLOSED (realised now, no settlement lookup) and keep each set's other legs held.
    ``realised`` = ``count * (1 - yes_cost - no_cost)`` where the costs include fees."""

    ticker: str
    count: Decimal
    yes_cost: Decimal            # per-contract YES fill price + fee
    no_cost: Decimal             # per-contract NO fill price + fee
    realised: Decimal            # total realised = count * (1 - yes_cost - no_cost)
    yes_batch: int
    no_batch: int
    server_ts: float


@dataclass(frozen=True)
class PrintThroughTrigger:
    """One print-through early-hedge trigger (Brad, 2026-09-26). When a bucket YES print lands within
    ``print_through_ticks`` of a resting rung's offer (1-n) and moving toward it, the wing IOC batch is
    fired EARLY (before our own rung fill confirms) for the pre-hedged rungs, at the ask the sweep started
    from. ``batch_index`` is the WingBatch it created; ``rung_coids``/``rung_prices`` are the pre-hedged
    resting rungs; ``count`` the total lots. As each pre-hedged rung fills, its coid joins ``filled_coids``
    and its RungFill is appended to the batch. On a STALL (no rung fill within ``print_through_stall_ms``)
    or a partial wing fill, the trigger resolves via the stall policy (``resolution`` in
    {"filled","complete","unwind","partial"}) and the numbers below let the reporter score it."""

    batch_index: int
    rung_coids: tuple[str, ...]
    rung_prices: tuple[Decimal, ...]
    count: Decimal        # D3: Decimal lots (pre-hedged rung lots; fractional-safe)
    trigger_ts: float
    yes_print: Decimal
    W_at_trigger: Decimal | None
    n_top_at_trigger: Decimal | None
    lock_at_trigger: Decimal | None
    yes_ask_at_trigger: Decimal | None = None
    no_ask_at_trigger: Decimal | None = None
    filled_coids: tuple[str, ...] = ()
    resolved: bool = False
    resolution: str | None = None            # filled | complete | unwind | partial
    shortfall: Decimal = _ZERO                # lots resolved by the stall policy (count - filled)
    complete_price: Decimal | None = None     # bucket-NO ask paid on a `complete`
    lock_at_completion: Decimal | None = None
    roundtrip_cost: Decimal | None = None     # per-window unwind round-trip cost ($ total)
    resolved_ts: float | None = None
    # F2 (2026-09-26 R2): a stall cancels the unfilled rests FIRST and finalises (complete/unwind) only
    # once those cancels confirm, so a fill that races the cancel is attributed to THIS (still-active)
    # batch and the complete size is the TRUE remaining shortfall -- never a second wing batch.
    stall_pending: bool = False               # cancels emitted, awaiting confirm before finalise
    pending_cancels: tuple[str, ...] = ()     # order_ids of the stall cancels we are waiting on
    # F3 (2026-09-26 R2): ARMED completes book the taker bucket-NO from the IOC response (like the wings),
    # not optimistically; this is the coid the core is waiting on. DRY books optimistically (no venue).
    complete_coid: str | None = None


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
    # STALE-WING LIVENESS (2026-10-03, gate D rewrite): the latest ``book_ts`` folded from ANY strike
    # (KXBTCD) book. The strike connection carries ~188 markets, so any strike frame proves the feed is
    # alive; a single quiet deep-wing book is NOT staleness (it is the same book). The wing gates read
    # feed liveness (``strike_feed_dead_s``) plus a LOOSE per-strike bound (``wing_book_max_age_s``)
    # instead of V3.2's 1.0 s per-strike age. (The bucket connection needs no analogue: the spot bucket's
    # own book is already gated at ``bucket_freshness_max_age_s`` = 30 s, R-STALE-SPOT.)
    strike_feed_ts: float | None = None

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
    # D1 (2026-09-30 incident): the retained attribution record for an order removed from the ladder
    # (eager-clear / cancel-all) whose cancel has not yet confirmed. Keyed by order_id ->
    # (coid, price, rung, E_rung, bucket_Sd). ``bucket_Sd`` is the bucket the rung ACTUALLY rested on, so
    # a late fill surfaced via the cancel-confirm path is attributed to the rung's OWN bucket, NEVER the
    # current spot bucket (the incident mis-hedged because the fallback read ``spot_Sd`` after the bucket
    # had moved on).
    cancel_ctx: Mapping[str, tuple[str, Decimal, int, Decimal, int | None]] = field(default_factory=dict)
    # D1: the hour is stood down fail-closed when a fill cannot be attributed to a named bucket.
    bucket_unknown: bool = False
    amend_cross_pending: Mapping[str, int] = field(default_factory=dict)
    # GATE A: the sum of per-trade INCREMENT fills (ws / dry-sim / executor POST response; NOT the poll's
    # cumulative-derived delta) reported per rung coid, whether or not the order was on the ladder. An
    # off-ladder (orphan) increment books only ``seen - booked`` (clipped to the event's own count), so a
    # late ws echo of lots a cancel-confirm or poll already booked is never hedged twice, while a fill the
    # cancel-confirm MISSED (unreadable status -> 0) is still hedged.
    fill_seen_by_coid: Mapping[str, Decimal] = field(default_factory=dict)
    rest_allotment_done: bool = False                   # every rung filled (or max_sets) -> stop quoting

    coalesce_open: CoalesceGroup | None = None
    wing_batches: tuple[WingBatch, ...] = ()
    wing_legs: tuple[WingLeg, ...] = ()
    next_batch_index: int = 0
    sets_done: int = 0
    one_legged: bool = False                            # mirror: any batch flagged one_legged
    # D5 (2026-09-30, Brad): YES/NO wing legs the venue netted to flat across adjacent buckets (each pays
    # $1/contract, booked CLOSED now). The ledger books their realised $1 and NEVER lists them in the
    # settlement backfill (a netted market shows position 0).
    netted_pairs: tuple[NettedPair, ...] = ()

    # PRINT-THROUGH WINGS (2026-09-26): the active/resolved early-hedge triggers. A pre-hedged rung's coid
    # is carried here so its fill attaches to the pre-emptive batch (no double take) and a stall / partial
    # can resolve it via the stall policy.
    print_through: tuple[PrintThroughTrigger, ...] = ()
    pt_stood_down: bool = False                         # a print-through partial fail-closed latched down
    pt_one_legged: bool = False                         # latched one-legged from a fail-closed/short unwind
                                                        # (the batch is dropped, so the mirror needs a latch)

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
    # STALE-WING SUB-CAUSE (2026-10-03 labeling fix): the DIAGNOSTIC label for WHY the REST W gate
    # (``_v33_compute_W``) returned None. LABEL ONLY -- no gate, dedup, falsifier or behaviour reads
    # these; the stand-down REASON strings are unchanged.
    #  * ``wing_sub_cause`` is the CURRENT classification, refreshed every ``_recompute_context`` when the
    #    W gate RAN and returned None (else None). The ``v33_eval`` record and the PLAIN stale stand_down
    #    read it.
    #  * ``hold_sub_cause`` is the label captured at the instant a stale/missing-wing HOLD began; it is
    #    carried through to the ``stand_down_hold`` / ``stand_down_cancel`` / ``stand_down_resume`` that
    #    end the hold (never cleared on the healthy path -- the next hold overwrites it, and only a
    #    resume/cancel immediately following a hold ever reads it).
    wing_sub_cause: str | None = None
    hold_sub_cause: str | None = None

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
        # L5: a rung with weight 0 is state "no order" (0), never "open order wanted" (1), so the
        # convergence never places, targets, or stalls on it (the open span is naturally non-contiguous).
        margin_state = tuple(
            1 if (e_min_c <= m <= e_min_c + params.rungs - 1
                  and _weight_of_rung(params, m - e_min_c) > 0) else 0
            for m in range(e_min_c + params.rungs)
        )
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
        # L5: the true exposure cap is in CONTRACTS (sum of the per-rung weights), not rung units, because
        # a weighted ladder rests unequal lots per rung and a rung may partially fill. At weight 1 this is
        # exactly the old rung-unit assert (filled + resting <= K). ``rungs_filled`` (fill-EVENT count) is
        # kept as the coarse ``max_sets_per_hour`` gate, not the exposure invariant.
        allot = _allotment(params)
        assert _filled_contracts(self) + _resting_contracts(self) <= allot, (
            f"window exposure {_filled_contracts(self)} filled + {_resting_contracts(self)} resting "
            f"contracts > allotment sum(rung_lots)={allot}"
        )
        prices = [o.price for o in lad]
        assert len(set(prices)) == len(prices), f"two rests on one price: {sorted(prices)}"
        max_w = max(params.rung_lots)
        for o in lad:
            # L5: an order carries the lots it was PLACED with (a slot's weight), less any partial fill,
            # so it is a rung weight at most and > 0 while resting (it drops at 0). It is NOT re-derived
            # from the current slot (rolls keep count -> smearing; see the module "L5" note). D3: a
            # FRACTIONAL partial (count_fp) can leave a fractional remainder resting (e.g. 0.56 of a 1-lot
            # rung), so the floor is ``> 0``, not ``>= 1``.
            assert _ZERO < o.count <= max_w, f"rung count {o.count} not in (0, max(rung_lots)={max_w}]"
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
        # wing-batch / leg consistency: a TAKEN batch has exactly two legs, each sized to the batch's leg
        # count (``total_count`` normally; ``taken_count`` for a print-through batch pre-hedged before its
        # rungs fill). A print-through batch whose stall policy has RESOLVED (legs being unwound) is exempt.
        for b in self.wing_batches:
            legs = [l for l in self.wing_legs if l.batch == b.index]
            if b.taken and not (b.print_through and b.resolved):
                assert len(legs) == 2, f"taken batch {b.index} has {len(legs)} legs (must be 2)"
                assert all(l.count == b.leg_count for l in legs), (
                    f"batch {b.index} leg counts != leg_count {b.leg_count}"
                )
        # print-through: a pre-hedged rung's coid appears in at most one active trigger; filled_coids is a
        # subset of rung_coids; a resolved trigger books no further.
        seen_pt: set[str] = set()
        for t in self.print_through:
            assert set(t.filled_coids) <= set(t.rung_coids), (
                f"pt trigger {t.batch_index}: filled_coids not a subset of rung_coids"
            )
            if not t.resolved:
                for c in t.rung_coids:
                    assert c not in seen_pt, f"coid {c} pre-hedged by two active print-through triggers"
                    seen_pt.add(c)
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


# --- L5 (2026-09-29): per-rung lot weights ---------------------------------------------------------
# ``params.rung_lots`` is the RESOLVED weight vector (index k = rung k, k=0 top .. K-1 deepest; a 0 means
# that rung is never placed). Absent in JSON -> ``(lots_per_rung,)*K`` (uniform, byte-identical). The
# CONTRACT allotment ``sum(rung_lots)`` is the true exposure cap; ``max_sets_per_hour`` stays in RUNG
# (fill-event) units. See the module docstring "L5" note for the roll/re-size decision.
def _weight_of_rung(params: V33Params, k: int) -> int:
    """The configured lot weight for rung index ``k`` (k=0 top .. K-1 deepest); 0 out of range."""
    return params.rung_lots[k] if 0 <= k < len(params.rung_lots) else 0


def _weight_of_margin(params: V33Params, m: int) -> int:
    """The configured lot weight for margin slot ``m`` (m = E_min_c is the top rung)."""
    return _weight_of_rung(params, m - _emin_cents(params))


def _allotment(params: V33Params) -> int:
    """The hour's CONTRACT allotment = sum of the per-rung weights (the true exposure cap)."""
    return sum(params.rung_lots)


def _resting_contracts(st: V33State) -> Decimal:
    """Contracts currently live/pending on the resting rungs (sum of the ladder order counts). D3: the
    sum is Decimal (a fractional partial leaves a fractional resting remainder)."""
    return sum((o.count for o in st.ladder), _ZERO)


def _filled_contracts(st: V33State) -> Decimal:
    """Contracts filled this hour (sum of every booked rung-fill count, incl. taker completes). D3:
    Decimal (fractional fills)."""
    return sum((f.count for f in st.rest_fills), _ZERO)


def _sync_wing_mirrors(st: V33State) -> V33State:
    """Re-derive the ``one_legged`` mirror from the batch state (kept for L2/ledger compatibility). A
    print-through fail-closed / complete-short drops the batch, so ``pt_one_legged`` LATCHES that event
    (it can never be un-set by a later sync)."""
    one_legged = any(b.one_legged for b in st.wing_batches) or st.pt_one_legged
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
        st = _stamp_strike_feed(st, event)
        st = _recompute_context(params, st, now)
        st, _ = _shadow_complete(params, st, now)
        st, wa = _wing_step(params, st, now)
        st, pa = _pt_stall_step(params, st, now)   # resolve any print-through trigger past its stall
        st, qa = _converge(params, st, now)
        return st, wa + pa + qa

    if isinstance(event, Trade):
        st, ta = _shadow_on_trade(params, st, event)
        st, _ = _shadow_complete(params, st, now)
        # take/close any coalesced wings whose window elapsed as the clock advanced with this print
        st, wa = _wing_step(params, st, now)
        # PRINT-THROUGH: fire the wing take EARLY off a bucket print approaching a resting rung (Brad),
        # then resolve any earlier trigger that has now stalled.
        st, pta = _print_through_step(params, st, event, now)
        st, psa = _pt_stall_step(params, st, now)
        return st, ta + wa + pta + psa

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
        st, pa = _pt_stall_step(params, st, now)
        st, qa = _converge(params, st, now)
        return st, wa + pa + qa

    return st, actions


# ---------------------------------------------------------------------------
# Strike-feed liveness + the V3.3 wing gate (gate D rewrite, 2026-10-03)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _WingLawView:
    """The ONE field the reused V3.2 wing law (``_compute_W`` / ``_wing_prices``) reads off its params:
    the per-strike age bound. V3.3 hands the V3.2 law this view carrying ``wing_book_max_age_s`` (the
    LOOSE bound) instead of the V3.2 1.0 s ``freshness_max_age_s``, so the V3.2 law is reused UNCHANGED
    (book present, not suspect, valid prices) and the V3.2 core keeps its own 1.0 s semantics
    byte-identically."""

    freshness_max_age_s: float


def _stamp_strike_feed(st: V33State, event: BookUpdate) -> V33State:
    """Record the strike connection's liveness: the latest ``book_ts`` (the frame's OWN server ts, NOT the
    monotone eval clock, so a lagging/stalled connection ages out) of any STRIKE book fold."""
    cls = classify_ticker(event.market_ticker, st.bucket_map)
    if cls is None or cls[0] != "strike":
        return st
    book_ts = event.book_ts if event.book_ts is not None else event.server_ts
    if st.strike_feed_ts is not None and book_ts <= st.strike_feed_ts:
        return st
    return replace(st, strike_feed_ts=book_ts)


def _strike_feed_alive(params: V33Params, st: V33State, now: float) -> bool:
    """The strike connection is ALIVE iff some strike frame's own ts is within ``strike_feed_dead_s`` of
    the eval clock (same clock-interleave tolerance as ``_fresh``). Never seen -> dead."""
    return _fresh(now, st.strike_feed_ts, params.strike_feed_dead_s)


def _v33_compute_W(st: V33State, Sd: int, Su: int, now: float, params: V33Params) -> Decimal | None:
    """W for the REST decision: the strike feed must be alive, then the UNCHANGED V3.2 ``_compute_W``
    (books present, not suspect, valid asks) with the LOOSE per-strike bound ``wing_book_max_age_s``.
    The bound is the SAME one the wing TAKE gate uses, so the ladder never rests a rung whose fill the take
    gate would refuse to hedge."""
    if not _strike_feed_alive(params, st, now):
        return None
    return _compute_W(st, Sd, Su, now, _WingLawView(params.wing_book_max_age_s))  # type: ignore[arg-type]


def wing_unavailable_cause(params: V33Params, st: V33State, now: float) -> str | None:
    """DIAGNOSTIC label for WHY the REST W gate (``_v33_compute_W``) returned None. Evaluated ONLY when
    that gate returned None (the caller holds that contract); returns the FIRST failing check in the EXACT
    order ``_strike_feed_alive`` then the V3.2 ``_compute_W`` apply, so the label is always the check that
    actually failed, never a later one that merely happens to also be true:

        feed_dead
        -> wing_missing_sd / wing_missing_su      (no book ever for that strike)
        -> wing_suspect_sd / wing_suspect_su      (malformed-delta / seq-gap book)
        -> wing_too_old_sd / wing_too_old_su      (older than the LOOSE ``wing_book_max_age_s``)
        -> wing_no_ask_sd  (yes_ask None: no NO bid on Sd)  / wing_no_ask_su (no_ask None: no YES bid on Su)
        -> wing_price_invalid_sd / wing_price_invalid_su    (ask outside the gate's (0, 1) range)
        -> unknown

    PURE, LABEL ONLY: no state change, never raises (any surprise -> ``unknown``). The two wing strikes
    are the CURRENT spot pair (``st.spot_Sd`` / ``st.spot_Su``); with no spot pair there is no wing to
    blame (that stand-down is ``no_spot_bucket``, not ``stale_or_missing_wing``) -> ``unknown``. The price
    bound mirrors ``_compute_W``'s OPEN interval ``_ZERO < ask < _ONE`` (an ask at exactly 1 fails the rest
    gate, so it is labelled invalid here too), NOT the take gate's half-open ``<= _ONE``: this label
    describes the REST gate that drives the ``stale_or_missing_wing`` stand-down."""
    try:
        Sd = st.spot_Sd
        Su = st.spot_Su
        if Sd is None or Su is None:
            return "unknown"
        # 1. strike CONNECTION liveness -- the gate's first check.
        if not _strike_feed_alive(params, st, now):
            return "feed_dead"
        # 2..N mirror ``_compute_W``'s own order (missing, then suspect, then age, then ask-present, then
        # price validity), Sd before Su within each class, using the LOOSE per-strike bound the V3.3 gate
        # hands the V3.2 law.
        sd_top = st.strike_tops.get(Sd)
        su_top = st.strike_tops.get(Su)
        if sd_top is None:
            return "wing_missing_sd"
        if su_top is None:
            return "wing_missing_su"
        if sd_top.suspect:
            return "wing_suspect_sd"
        if su_top.suspect:
            return "wing_suspect_su"
        bound = params.wing_book_max_age_s
        if not _fresh(now, st.strike_ts.get(Sd), bound):
            return "wing_too_old_sd"
        if not _fresh(now, st.strike_ts.get(Su), bound):
            return "wing_too_old_su"
        ya = sd_top.yes_ask
        na = su_top.no_ask
        if ya is None:
            return "wing_no_ask_sd"
        if na is None:
            return "wing_no_ask_su"
        if not (_ZERO < ya < _ONE):
            return "wing_price_invalid_sd"
        if not (_ZERO < na < _ONE):
            return "wing_price_invalid_su"
        return "unknown"
    except Exception:  # noqa: BLE001 -- a diagnostic label must never break the pump.
        return "unknown"


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
    # the W gate RUNS only when a fresh spot pair exists; otherwise W is None for a DIFFERENT stand-down
    # reason (no_spot_bucket / stale_bucket) and there is no wing to blame.
    gate_ran = spot_Sd is not None and spot_Su is not None and not spot_bucket_stale
    if gate_ran:
        W = _v33_compute_W(st, spot_Sd, spot_Su, now, params)
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
        spot_bucket_stale=spot_bucket_stale, shadows=shadows, wing_sub_cause=None,
        pending_switch_Sd=pend_Sd, pending_switch_since=pend_since, pending_switch_hyst_met=pend_hyst,
    )
    # LABELING (2026-10-03): when the W gate RAN and returned None, classify the sub-cause for the
    # stale/missing-wing diagnostics. The classifier reads the NOW-updated spot pair off ``st``. LABEL
    # ONLY -- no gate reads ``wing_sub_cause``; it never changes the quote/stand-down decision.
    if gate_ran and st.W is None:
        st = replace(st, wing_sub_cause=wing_unavailable_cause(params, st, now))
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

    delta = _q_count(event.fill_count) if event.fill_count is not None else _ZERO
    if delta > 0:
        moved = next((o for o in st.ladder if o.client_order_id == rp.new_coid), None)
        n = event.average_fill_price if event.average_fill_price is not None else new_price
        if moved is not None:
            # the moved order is still in the ladder -> its own bucket_Sd attributes the cross fill.
            st, wa = _book_rung_fill(params, st, moved.client_order_id, n, delta, moved.rung,
                                     moved.E_rung, now, retained_Sd=moved.bucket_Sd)
            actions += wa
            if event.order_id is not None and any(
                o.client_order_id == rp.new_coid for o in st.ladder
            ):
                acp = dict(st.amend_cross_pending)
                acp[event.order_id] = acp.get(event.order_id, _ZERO) + delta
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
    the cancel -> book the delta via the still-live order or ``cancel_ctx`` (cumulative -> delta).

    GATE B (2026-10-03 02:00Z naked fill) -- IDENTITY. The event is attributed by ``order_id`` when it has
    one, else by ``client_order_id``, and NEVER by ``None == None``: the pre-fix lookup matched a venue-id-less
    rejection to the FIRST pending order, so nine pre-flight rejections evicted nine innocent pending rungs
    (two of which then rested live, owned by nobody, and filled naked). An event that names no order at all
    matches nothing and raises ``cancel_unattributed`` instead of touching the ladder. The bucket-change
    ``outstanding_cancels`` count (which counts only orders that HAD a venue id) is decremented only by an
    event that carries a venue id.

    GATE A -- a cancel-confirm that surfaces a fill on an order the core has neither on its ladder nor in
    ``cancel_ctx`` (an executor-owned order the core lost) is still BOOKED and HEDGED from the event's own
    coid / price / market ticker (``orphan_rung_fill_hedged``)."""
    actions: list[V33Action] = []
    filled = _q_count(event.filled_count_before_cancel)
    ev_oid = event.order_id
    ev_coid = getattr(event, "client_order_id", None)

    # book a partial fill first (attribute by live order, else cancel_ctx), before we drop slots.
    coid: str | None = None
    price: Decimal | None = None
    rung: int | None = None
    E_rung: Decimal | None = None
    retained_Sd: int | None = None
    if ev_oid is not None:
        live_order = next((o for o in st.ladder if o.order_id == ev_oid), None)
    elif ev_coid is not None:
        live_order = next((o for o in st.ladder if o.client_order_id == ev_coid), None)
    else:
        live_order = None   # GATE B: no identity -> matches NOTHING (never the first pending order)
    if live_order is not None:
        coid, price, rung, E_rung, retained_Sd = (
            live_order.client_order_id, live_order.price, live_order.rung, live_order.E_rung,
            live_order.bucket_Sd,
        )
    else:
        ctx = st.cancel_ctx.get(ev_oid) if ev_oid is not None else None
        if ctx is None and ev_coid is not None:
            ctx = next((c for c in st.cancel_ctx.values() if c[0] == ev_coid), None)
        if ctx is not None:
            # D1: the 5th element is the retained bucket_Sd (the rung's OWN bucket) — this is the incident
            # path (fill surfaced by the cancel confirm after the ladder was torn down on a bucket change).
            coid, price, rung, E_rung, retained_Sd = ctx
    if ev_oid is None and ev_coid is None:
        actions.append(V33Action(kind=V33ActionKind.ALARM, reason="cancel_unattributed",
                                 count=filled))
    if filled > 0 and coid is not None and price is not None:
        already = _q_count(st.rest_booked_by_coid.get(coid, _ZERO))
        delta = filled - already
        if delta > 0:
            st, wa = _book_rung_fill(params, st, coid, price, delta, rung or 0,
                                     E_rung if E_rung is not None else params.E_min, now,
                                     retained_Sd=retained_Sd)
            actions += wa
    elif filled > 0 and coid is None:
        # GATE A: a fill surfaced by the cancel-confirm on an order the core does not know. Book + hedge it
        # from the event itself (the executor's RestRecord supplies its coid / price / market ticker).
        o_coid = ev_coid if ev_coid is not None else (f"orphan-oid-{ev_oid}" if ev_oid else None)
        o_price = getattr(event, "price", None)
        if o_coid is not None and o_price is not None:
            already = _q_count(st.rest_booked_by_coid.get(o_coid, _ZERO))
            delta = filled - already
            if delta > 0:
                st, oa = _book_orphan_fill(params, st, o_coid, ev_oid, Decimal(o_price), delta,
                                           getattr(event, "market_ticker", None), now)
                actions += oa
        else:
            actions.append(V33Action(kind=V33ActionKind.ALARM, reason="orphan_rung_fill_unpriced",
                                     order_id=ev_oid, client_order_id=ev_coid, count=filled))

    rp = None
    if ev_oid is not None:
        rp = next((r for r in st.rolls_in_flight if r.order_id == ev_oid), None)
    elif ev_coid is not None:
        rp = next((r for r in st.rolls_in_flight if r.old_coid == ev_coid), None)
    if rp is not None:
        # FALLBACK: the amend failed and the executor cancelled -> drop the old order and place a fresh
        # one at the roll's target (same end state as a successful amend, fresh queue). L5: re-place the
        # moving order's REMAINING resting lots (``mover.count``, already reduced by any partial booked
        # above) — the roll KEEPS count, so the fallback matches the amend end-state (not the slot weight).
        # Skip the re-place if the order fully filled before the cancel (nothing left to move).
        mover = next((o for o in st.ladder if o.client_order_id == rp.old_coid), None)
        replace_count = int(mover.count) if mover is not None else 0
        st = _drop_roll(st, rp.old_coid)
        st = replace(st, ladder=_drop_order(st.ladder, rp.old_coid))
        still_resting = replace_count > 0
        # NIT #9: re-check the cap on the fallback re-place (no_ask may have dropped since emit). Clamp to
        # the cap and drop if that pushes below n_min. Derive rung/E_rung from the CURRENT n_top.
        target = rp.target_price
        if st.cap is not None and target > st.cap:
            target = st.cap
        # GATE C(iii): a STOOD-DOWN core never quotes -- the fallback re-place is skipped (the executor
        # refuses a new rest when stood down; an amend it refuses is routed back here as a plain cancel).
        if (not st.rest_allotment_done and not st.stood_down and still_resting
                and _in_window(params, st, now)
                and target >= params.n_min and st.n_top is not None
                and _filled_contracts(st) + _resting_contracts(st) + replace_count <= _allotment(params)):
            r_rung, r_E = _rung_of(st.n_top, target), _e_rung(params, st.n_top, target)
            st, pa = _place_one(params, st, target, r_rung, r_E, now, count=replace_count)
            actions += pa
        st, ra = _converge(params, st, now)
        return st, actions + ra

    # bucket-change / stand-down / shrink cancel: drop the slot (if still present) and decrement the count.
    if live_order is not None:
        st = replace(st, ladder=_drop_order(st.ladder, live_order.client_order_id))
    # GATE B: only an event for an order the core COUNTED can resolve a counted bucket-change cancel --
    # ``_cancel_all(track_outstanding=True)`` counts live orders only and remembers each in ``cancel_ctx``.
    # A pending order's rejection / no-op cancel, or the ack-path cancel of a create that was still in
    # flight when the core cancelled it (never counted), never decrements it.
    if (ev_oid is not None and (live_order is not None or ev_oid in st.cancel_ctx)
            and st.outstanding_cancels > 0):
        st = replace(st, outstanding_cancels=st.outstanding_cancels - 1)
    # PRINT-THROUGH F2: if this was a stall cancel, drop it from the trigger's pending list and finalise
    # once they have all confirmed (any racing fill was booked above, so the shortfall is now the TRUTH).
    st, pt_a = _pt_on_cancel_confirmed(params, st, event.order_id, now)
    actions += pt_a
    # a bucket change waiting to re-place: once all cancels confirmed and the ladder is clear, place all.
    st, pa = _converge(params, st, now)
    return st, actions + pa


def _apply_fill(
    params: V33Params, st: V33State, event: Fill, now: float
) -> tuple[V33State, list[V33Action]]:
    """A private fill. A wing-leg fill updates leg status; a rung fill books the rung (at its PRE-roll
    resting price if a roll is in flight for it) and spawns/joins the coalesced wing batch."""
    actions: list[V33Action] = []
    # PRINT-THROUGH complete taker fill (armed F3): book the bucket-NO from the IOC response, not optimistically.
    res = _pt_apply_complete_fill(params, st, event, now)
    if res is not None:
        return res
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
            b = next((x for x in st.wing_batches if x.index == leg.batch), None)
            # PRINT-THROUGH fail-closed: a pre-emptive wing leg that did NOT fully fill (the ask moved past
            # our limit) means one_legged risk on a batch we already committed. Do not RETRY (as a normal
            # batch would): cancel the pre-hedged rests, unwind whatever wing filled, stand the hour down.
            if event.count <= _ZERO and b is not None and b.print_through and not b.resolved:
                st, fa = _pt_fail_closed(params, st, b.index, now)
                return st, actions + fa
            # D5: a newly-filled wing leg may net against an opposite-side filled leg on the SAME market
            # (adjacent-bucket overlap). Book the netted $1 pair(s) before closing the set.
            if event.count > _ZERO:
                st, na = _net_wings(st, event.client_order_id, now)
                actions += na
            st = _maybe_close_set(st, leg.batch)
            return st, actions

    # rung fill? match a ladder order by coid.
    order = next((o for o in st.ladder if o.client_order_id == event.client_order_id), None)
    if order is None:
        # GATE A (2026-10-03 02:00Z naked fill): the pre-fix ``return`` here DISCARDED a fill on an order the
        # executor owned but the core had lost (two NO lots expired naked, -$0.40). Every owned fill is now
        # booked and hedged from the event itself -- including when the core is stood down.
        st, oa = _apply_orphan_fill(params, st, event, now)
        return st, actions + oa
    delta = _q_count(event.count)
    if delta > 0 and event.client_order_id is not None and getattr(event, "source", None) != "poll":
        seen = dict(st.fill_seen_by_coid)
        seen[event.client_order_id] = _q_count(seen.get(event.client_order_id, _ZERO) + delta)
        st = replace(st, fill_seen_by_coid=seen)
    # N1: skip lots an amend CROSS already booked for this order_id (the venue echoes the crossed taker
    # fill on the WS ``fill`` channel with a fresh trade_id the driver's dedup cannot catch).
    oid = order.order_id if order.order_id is not None else event.order_id
    if delta > 0 and oid is not None and st.amend_cross_pending.get(oid, _ZERO) > 0:
        pending = _q_count(st.amend_cross_pending.get(oid, _ZERO))
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
        # at ``order.price`` on the venue until the amend acks) — golden (f). The order is live in the
        # ladder here, so its own ``bucket_Sd`` attributes the fill (D1).
        n = order.price
        st, wa = _book_rung_fill(params, st, order.client_order_id, n, delta, order.rung,
                                 order.E_rung, now,
                                 retained_Sd=order.bucket_Sd,
                                 fill_ticker=getattr(event, "market_ticker", None))
        actions += wa
    return st, actions


def _apply_orphan_fill(
    params: V33Params, st: V33State, event: Fill, now: float
) -> tuple[V33State, list[V33Action]]:
    """GATE A: a private Fill whose coid matches NO ladder order and NO wing leg. The executor attributed it
    to an order it OWNS (the driver only pumps fills its RestBook recognises), so it is a real bucket-NO
    lot that must be hedged whatever the core remembers. Attribution, in order: the ``cancel_ctx`` record
    for this coid (the rung's own bucket / rung), else the fill's OWN market ticker. A ticker that names a
    market which is NOT one of the window's buckets (e.g. a strike) is not a rung fill: alarmed, not booked.

    DEDUPE (cancel-confirm vs late ws echo): ``fill_seen_by_coid`` accumulates every reported lot; only the
    excess over what is already BOOKED for the coid (by any path: ws, poll, cancel-confirm cumulative) is
    booked, clipped to this event's own count. So a ws echo of lots a cancel-confirm already booked hedges
    nothing twice, while a fill the cancel-confirm missed (unreadable status -> 0) is still hedged."""
    count = _q_count(event.count) if event.count is not None else _ZERO
    coid = event.client_order_id
    if count <= 0 or coid is None:
        return st, []
    ticker = getattr(event, "market_ticker", None)
    # REVIEW (gate A hardening): an owned fill with no price cannot be booked (``_book_orphan_fill`` derives
    # the rung from ``_rung_of(n_top, price)``). The sibling cancel-confirm orphan branch already guards this
    # (``orphan_rung_fill_unpriced``); without the symmetric guard here an unpriced fill raised on the decide
    # ingest path (brief lesson 3: an ingest exception drops events elsewhere). Owned fills are always priced
    # live (ws = rec.price, poll = rec.price, POST = action.price), so this is fail-closed defence, not a
    # normal path. Guarded BEFORE ``fill_seen_by_coid`` is touched so a dropped fill never skews the dedupe.
    if event.price is None:
        return st, [V33Action(kind=V33ActionKind.ALARM, reason="orphan_rung_fill_unpriced",
                              client_order_id=coid, order_id=event.order_id, ticker=ticker, count=count)]
    ctx = next(((oid, c) for oid, c in st.cancel_ctx.items() if c[0] == coid), None)
    if ctx is None and ticker and _sd_for_bucket_ticker(st, ticker) is None:
        return st, [V33Action(kind=V33ActionKind.ALARM, reason="orphan_fill_not_bucket",
                              client_order_id=coid, order_id=event.order_id, ticker=ticker,
                              count=count, price=event.price)]
    booked = _q_count(st.rest_booked_by_coid.get(coid, _ZERO))
    if getattr(event, "source", None) == "poll":
        # the poll's count is ALREADY (venue cumulative - booked): book it as is, never add it to the
        # increment sum (a later ws echo of the same lots then nets to zero against ``booked``).
        delta = count
    else:
        seen = dict(st.fill_seen_by_coid)
        seen_new = _q_count(seen.get(coid, _ZERO) + count)
        seen[coid] = seen_new
        st = replace(st, fill_seen_by_coid=seen)
        delta = min(count, seen_new - booked)
    if delta <= 0:
        return st, []     # already booked (e.g. by the cancel-confirm cumulative) -- nothing new to hedge
    retained_Sd = ctx[1][4] if ctx is not None else None
    order_id = event.order_id if event.order_id is not None else (ctx[0] if ctx is not None else None)
    return _book_orphan_fill(params, st, coid, order_id, event.price, delta, ticker, now,
                             retained_Sd=retained_Sd)


def _book_orphan_fill(
    params: V33Params, st: V33State, coid: str, order_id: str | None, price: Decimal, delta: Decimal,
    ticker: str | None, now: float, *, retained_Sd: int | None = None,
) -> tuple[V33State, list[V33Action]]:
    """GATE A: book ``delta`` lots of an executor-owned bucket-NO order the core lost, at ``price``, and let
    the coalesced wing batch hedge it (D2: the batch hedges the strikes of the bucket the fill landed in).
    The rung / E_rung are DERIVED from the price vs the current ladder geometry when that is a legitimate
    rung (0 <= rung < K at the current n_top); otherwise the fill is labelled ``orphan`` and the margin
    array is left untouched. Works stood down and with an empty ladder: a stood-down core must still hedge
    (``_wing_step`` never reads ``stood_down``); it must not quote (``_converge`` does). Always raises
    ``orphan_rung_fill_hedged``."""
    if st.n_top is not None:
        rung = _rung_of(st.n_top, price)
        legit = 0 <= rung < params.rungs
        E_rung = _e_rung(params, st.n_top, price)
    else:
        rung, legit, E_rung = 0, False, params.E_min
    alarm = V33Action(kind=V33ActionKind.ALARM, reason="orphan_rung_fill_hedged",
                      client_order_id=coid, order_id=order_id, ticker=ticker, count=delta, price=price)
    st, wa = _book_rung_fill(params, st, coid, price, delta, rung, E_rung, now,
                             retained_Sd=retained_Sd, fill_ticker=ticker, orphan=not legit,
                             order_id=order_id)
    return st, [alarm] + wa


# ---------------------------------------------------------------------------
# Rung fill booking + coalesced wings
# ---------------------------------------------------------------------------
def _sd_for_bucket_ticker(st: V33State, ticker: str | None) -> int | None:
    """D1 last-resort attribution: invert ``st.bucket_tickers`` (Sd -> ticker) to recover the bucket Sd
    a fill's OWN market ticker names. ``None`` if the ticker is unknown/absent."""
    if not ticker:
        return None
    for sd, tk in st.bucket_tickers.items():
        if tk == ticker:
            return sd
    return None


def _book_rung_fill(
    params: V33Params, st: V33State, coid: str, price: Decimal, delta: Decimal, rung: int,
    E_rung: Decimal, now: float, *, retained_Sd: int | None = None, fill_ticker: str | None = None,
    orphan: bool = False, order_id: str | None = None,
) -> tuple[V33State, list[V33Action]]:
    """Book ``delta`` filled lots of rung ``coid`` at ``price``: record the RungFill, remove the rung
    from the ladder (Q3 no refill), and COALESCE the fill into the open wing group (Q2). The fill's margin
    is DERIVED from ``price`` vs the current n_top (Brad R4: the index is never a stored identity); the
    2-slot is tied to this fill via ``filled_at``.

    D1 (2026-09-30 incident) — FILL-TO-BUCKET ATTRIBUTION NEVER READS THE CURRENT SPOT BUCKET. The bucket
    the fill hedges is resolved, in order: (1) the order's OWN ``bucket_Sd`` while it is still live in the
    ladder; (2) ``retained_Sd`` — the bucket retained by the caller through cancel-all (the live order, or
    ``cancel_ctx`` for a fill surfaced only by the cancel confirm); (3) the fill's OWN market ticker
    inverted through ``st.bucket_tickers``. If NONE of those names a bucket, the fill is booked
    ``bucket_unknown`` (no wings), the hour is stood down FAIL-CLOSED, and a ``fill_bucket_unknown``
    stand-down is emitted — we never hedge a leg whose bucket we cannot name."""
    delta = _q_count(delta)
    booked = dict(st.rest_booked_by_coid)
    booked[coid] = _q_count(booked.get(coid, _ZERO) + delta)
    # remove the (now filled) rung from the live ladder; a filled rung is not refilled inside the window.
    ladder = st.ladder
    order = next((o for o in ladder if o.client_order_id == coid), None)
    if order is not None:
        order_id = order.order_id
    # D1 attribution: order's own bucket (live) -> retained bucket (cancel_ctx / caller) -> fill's own
    # market ticker inverted -> None (fail-closed). The pre-fix ``rest_bucket_Sd or spot_Sd`` fallback is
    # GONE: after a bucket change ``rest_bucket_Sd`` is None and ``spot_Sd`` is the NEW bucket, which is
    # exactly the mis-attribution that hedged the wrong strikes in the incident.
    fill_Sd = None
    if order is not None and order.bucket_Sd is not None:
        fill_Sd = order.bucket_Sd
    elif retained_Sd is not None:
        fill_Sd = retained_Sd
    else:
        fill_Sd = _sd_for_bucket_ticker(st, fill_ticker)
    fill_bucket_ticker = st.bucket_tickers.get(fill_Sd) if fill_Sd is not None else None
    fill_Su = (fill_Sd + params.bucket_width) if fill_Sd is not None else None
    if order is not None:
        # L5: subtract THIS event's ``delta`` from the order's CURRENT resting count (not the cumulative
        # ``booked``, which double-counts across successive partials — a latent bug harmless at weight 1
        # where a rung fills in exactly one event). A weight-w rung filling c < w keeps w-c resting; the
        # exposure invariant (filled + resting contracts) is conserved through each partial. D3: Decimal.
        remaining = _q_count(order.count - delta)
        if remaining <= 0:
            ladder = _drop_order(ladder, coid)
        else:
            # a partial fill of a rung: keep the (possibly fractional) remainder resting.
            ladder = _replace_order(ladder, coid, count=remaining)
    # DERIVE the fill's margin from the price vs the current n_top (the passed rung/E_rung are the caller's
    # fallback label when n_top is momentarily unknown). This is the literal "index derived from price".
    if st.n_top is not None:
        m = _margin_of(params, st.n_top, price)
        rung = m - _emin_cents(params)
        E_rung = params.E_min + rung * _CENT
    else:
        m = _emin_cents(params) + rung
    rf = RungFill(rung=rung, E_rung=E_rung, price=price, count=_q_count(delta), server_ts=now,
                  coid=coid, order_id=order_id, W=st.W, n_top=st.n_top,
                  bucket_ticker=fill_bucket_ticker, bucket_Sd=fill_Sd, bucket_Su=fill_Su,
                  weight=(_weight_of_rung(params, rung)
                          if (not orphan and 0 <= rung < len(params.rung_lots)) else None),
                  orphan=orphan)
    # MARGIN ARRAY (Brad R4): mark this fill's margin as consumed (state 2) and tie the 2-slot to THIS
    # fill (``filled_at``). A rare fill on a STRANDED order (margin outside the nominal array) is still
    # booked (rungs_filled ++, the exposure cap holds) but leaves the array untouched — the exposure
    # guard, not the array, is the true K-lot cap.
    margin_state = st.margin_state
    filled_at = st.filled_at
    # GATE A: an ``orphan`` fill (price not a legitimate rung of the current geometry) never touches the
    # margin array -- it is booked, capped by the exposure guard, and hedged, but it consumes no slot.
    if not orphan and 0 <= m < len(margin_state):
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
    # D1 FAIL-CLOSED: the fill's bucket could not be named from any retained record or its own ticker. The
    # RungFill is booked (so the held NO leg is accounted) but NO wings are taken and the hour stands down
    # — a hedge on a guessed bucket is what the incident did. The ledger/report price the held leg on the
    # fill's (now None) bucket and flag it.
    if fill_Sd is None:
        st = replace(st, bucket_unknown=True, stood_down=True, rest_allotment_done=True)
        ca: list[V33Action] = []
        if st.ladder:
            st, ca = _cancel_all(st)
        st, sa = _standdown(st, "fill_bucket_unknown")
        st = _sync_wing_mirrors(st)
        return st, ca + sa
    # PRINT-THROUGH: if this rung was pre-hedged by an active trigger, its wings are ALREADY in hand.
    # Attach the fill to that batch (no coalesce, no second take) and try to close the set.
    pt_idx = _pt_trigger_index_for_coid(st, coid)
    if pt_idx is not None:
        t = st.print_through[pt_idx]
        st = replace(
            st,
            wing_batches=_replace_batch(st.wing_batches, t.batch_index, fills=(
                next(b for b in st.wing_batches if b.index == t.batch_index).fills + (rf,))),
            print_through=_replace_pt(st.print_through, pt_idx,
                                      filled_coids=t.filled_coids + (coid,)),
        )
        st = _maybe_close_set(st, t.batch_index)
    else:
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
        # D2: stamp the batch's bucket from its fills (all coalesced rungs share one bucket). The wing
        # strikes are solved from THIS bucket in ``_wing_step``, never the current spot.
        bsd = next((f.bucket_Sd for f in g.fills if f.bucket_Sd is not None), None)
        batch = WingBatch(index=g.index, server_ts=g.first_ts, fills=g.fills, bucket_Sd=bsd)
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


def _batch_bucket(params: V33Params, b: WingBatch) -> tuple[int | None, int | None]:
    """D2: the (Sd, Su) the batch's rungs FILLED on — the fills' shared ``bucket_Sd`` (stamped at
    coalesce), else the batch's own ``bucket_Sd`` (print-through, stamped at trigger). ``(None, None)`` if
    no fill has named a bucket yet."""
    sd = next((f.bucket_Sd for f in b.fills if f.bucket_Sd is not None), None)
    if sd is None:
        sd = b.bucket_Sd
    if sd is None:
        return None, None
    return sd, sd + params.bucket_width


def _wing_prices_for_bucket(
    st: V33State, sd: int | None, su: int | None, now: float, params: V33Params
) -> tuple[Decimal, Decimal] | None:
    """The V3.2 ``_wing_prices`` gate (present, valid, non-suspect strike books) evaluated for a SPECIFIC
    bucket rather than the current spot — by temporarily viewing ``st`` with ``spot_Sd/Su`` = the fill's
    bucket. The pure V3.2 law is reused UNCHANGED (never reimplemented); only the bucket it reads moves.

    STALE-WING LIVENESS (2026-10-03): the strike FEED must be alive (``strike_feed_dead_s``), and the
    per-strike age bound is the LOOSE ``wing_book_max_age_s`` (belt-and-braces), not V3.2's 1.0 s: a deep
    wing strike is a quiet book, and a quiet book on a live feed is the same, truthful book."""
    if sd is None or su is None:
        return None
    if not _strike_feed_alive(params, st, now):
        return None
    return _wing_prices(replace(st, spot_Sd=sd, spot_Su=su), now,
                        _WingLawView(params.wing_book_max_age_s))  # type: ignore[arg-type]


def _wing_step(params: V33Params, st: V33State, now: float) -> tuple[V33State, list[V33Action]]:
    """Close any expired coalescing group, then take (or retry) the wings for every CLOSED batch.

    D2: each batch's wing strikes and asks are solved from the batch's OWN filled bucket (``_batch_bucket``
    / ``_wing_prices_for_bucket``), never the current ``st.spot_Sd`` — a fill hedges the strikes of the
    bucket it landed in, even if spot has moved on by take time. A batch whose bucket has no fresh strike
    book is skipped (that batch waits) without blocking other batches."""
    st = _coalesce_flush(params, st, now)
    actions: list[V33Action] = []
    if not st.wing_batches:
        return _sync_wing_mirrors(st), actions
    t_to_close = st.close_epoch - now
    if t_to_close < params.no_orders_after_s_to_settle:
        for b in list(st.wing_batches):
            if b.completed or b.one_legged:
                continue
            # a print-through batch that never completed (a wing missing, OR wings held but a pre-hedged
            # rung never filled) reaches the cutoff one-legged -- exactly the risk the falsifier counts.
            if b.print_through:
                if not b.completed:
                    st = replace(st, wing_batches=_replace_batch(st.wing_batches, b.index,
                                                                 one_legged=True, resolved=True))
                    st = _pt_mark_resolution(st, b.index, "partial", now)
                continue
            legs = _batch_legs(st, b.index)
            incomplete = (not legs) or any(l.status != "filled" for l in legs)
            if incomplete:
                st = replace(st, wing_batches=_replace_batch(st.wing_batches, b.index, one_legged=True))
        return _sync_wing_mirrors(st), actions

    for index in [b.index for b in st.wing_batches]:
        b = next((x for x in st.wing_batches if x.index == index), None)
        if b is None or b.completed:
            continue
        # print-through batches were taken at trigger time (not here) and never RETRY (a missing wing goes
        # to the fail-closed unwind path in _apply_fill); _wing_step leaves them alone.
        if b.print_through:
            continue
        sd, su = _batch_bucket(params, b)
        prices = _wing_prices_for_bucket(st, sd, su, now, params)
        if prices is None:
            continue                       # this batch's bucket has no fresh strike book yet — it waits.
        ya, na = prices
        if not b.taken:
            st, a = _take_batch(params, st, b, sd, su, ya, na, now)
        else:
            st, a = _retry_batch(params, st, b, sd, su, ya, na, now)
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
    params: V33Params, st: V33State, b: WingBatch, sd: int | None, su: int | None,
    ya: Decimal, na: Decimal, now: float
) -> tuple[V33State, list[V33Action]]:
    """UNCONDITIONAL initial take (ruling F-2) of ONE coalesced batch's two wings at ask + wing_margin,
    sized to the batch's TOTAL filled count. ``lock`` (batch total) is computed for the journal/report.
    D2: the wing strikes are the FILL's bucket strikes (``sd``/``su``), never the current spot."""
    w_paid = ya + fee(ya) + na + fee(na)
    lock = _batch_lock(b, w_paid)
    count = _q_count(b.total_count)
    coid_y, st = _mint_coid(st)
    coid_n, st = _mint_coid(st)
    yes_limit = min(ya + params.wing_margin, _LIMIT_CEILING)
    no_limit = min(na + params.wing_margin, _LIMIT_CEILING)
    legs = (
        LegOrder(st.strike_tickers.get(sd, ""), BUY_YES, "buy", count, yes_limit),
        LegOrder(st.strike_tickers.get(su, ""), BUY_NO, "buy", count, no_limit),
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
    params: V33Params, st: V33State, b: WingBatch, sd: int | None, su: int | None,
    ya: Decimal, na: Decimal, now: float
) -> tuple[V33State, list[V33Action]]:
    """Retry any unfilled leg of ONE batch at the fresh ask, gated by the projected batch lock staying
    at/above lock_floor (ruling F-2). The already-held leg forms the floor, so a deferred retry is
    bounded, not naked.

    D4 (2026-09-30 incident): a leg is retried NO MORE OFTEN than ``WING_RETRY_MIN_INTERVAL_MS`` (a leg
    whose last retry was < that ago is skipped this tick) and only ONE retry is ever in flight per leg
    (a leg is eligible only while ``status == "unfilled"``). The pre-fix path re-fired on every book/trade
    /clock tick (967 IOC creates in ~96 s). ``bucket`` strikes (``sd``/``su``) key the retry tickers."""
    actions: list[V33Action] = []
    legs = _batch_legs(st, b.index)
    count = _q_count(b.total_count)
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
    retries_added = 0
    for i, leg in enumerate(st.wing_legs):
        if leg.batch != b.index or leg.status != "unfilled":
            continue
        # D4 cadence floor: skip a leg retried within WING_RETRY_MIN_INTERVAL_MS (do NOT touch its state,
        # so it stays "unfilled" and is reconsidered once the floor elapses).
        if (leg.last_retry_ts is not None
                and (now - leg.last_retry_ts) * 1000.0 < WING_RETRY_MIN_INTERVAL_MS):
            continue
        ask = ya if leg.side == BUY_YES else na
        limit = min(ask + params.wing_margin, _LIMIT_CEILING)
        coid_r, st = _mint_coid(st)
        retry = LegOrder(leg.ticker, leg.side, "buy", leg.count, limit)
        legs_out[i] = replace(leg, status="pending", limit=limit, client_order_id=coid_r,
                              last_retry_ts=now)
        changed = True
        retries_added += 1
        actions.append(_mk(ActionKind.RETRY_WING, st.shakedown, legs=(retry,), count=leg.count))
    if changed:
        st = replace(st, wing_legs=tuple(legs_out))
        st = replace(st, wing_batches=_replace_batch(st.wing_batches, b.index, retries=b.retries + retries_added))
    return st, actions


def _net_wings(st: V33State, coid: str, now: float) -> tuple[V33State, list[V33Action]]:
    """D5 (2026-09-30, Brad): net the just-filled wing leg ``coid`` against any OPPOSITE-side FILLED wing
    leg on the SAME market. This happens across ADJACENT buckets: set A's NO@Su-strike and set B's
    YES@Sd-strike are ONE ticker. The venue nets +YES/+NO to flat and credits $1/contract immediately, so
    we book the overlapping lots CLOSED (realised now, never held to settlement) — the ledger/report/
    backfill then price only the un-netted held legs and add the netted $1. Handles PARTIAL overlap (a
    1.44 NO vs a 2.00 YES nets 1.44, the 0.56 YES stays held). Dormant in single-bucket operation (no
    opposite-side leg ever shares a strike), so every existing test is unaffected."""
    actions: list[V33Action] = []
    guard = 0
    while True:
        guard += 1
        if guard > 64:
            break
        L = next((l for l in st.wing_legs if l.client_order_id == coid), None)
        if L is None or L.status != "filled" or L.fill_price is None:
            break
        avail_L = L.count - L.netted
        if avail_L <= 0:
            break
        M = next((l for l in st.wing_legs
                  if l.ticker == L.ticker and l.side != L.side and l.status == "filled"
                  and l.fill_price is not None and (l.count - l.netted) > 0), None)
        if M is None:
            break
        q = _q_count(min(avail_L, M.count - M.netted))
        if q <= 0:
            break
        yes_leg, no_leg = (L, M) if L.side == BUY_YES else (M, L)
        yes_cost = yes_leg.fill_price + fee(yes_leg.fill_price)
        no_cost = no_leg.fill_price + fee(no_leg.fill_price)
        realised = q * (_ONE - yes_cost - no_cost)
        pair = NettedPair(ticker=L.ticker, count=q, yes_cost=yes_cost, no_cost=no_cost,
                          realised=realised, yes_batch=yes_leg.batch, no_batch=no_leg.batch,
                          server_ts=now)
        both = {L.client_order_id, M.client_order_id}
        new_legs = tuple(replace(l, netted=_q_count(l.netted + q)) if l.client_order_id in both else l
                         for l in st.wing_legs)
        st = replace(st, wing_legs=new_legs, netted_pairs=st.netted_pairs + (pair,))
        legs = (LegOrder(yes_leg.ticker, BUY_YES, "net", q, yes_leg.fill_price),
                LegOrder(no_leg.ticker, BUY_NO, "net", q, no_leg.fill_price))
        actions.append(_mk(V33ActionKind.WING_NETTED, st.shakedown, legs=legs, ticker=L.ticker,
                           count=q, lock=realised))
    return st, actions


def _maybe_close_set(st: V33State, index: int) -> V33State:
    """Both wings of BATCH ``index`` filled -> count it as ``leg_count`` completed sets and mark done.

    For a PRINT-THROUGH batch, completion needs BOTH wings filled AND every pre-hedged rung filled
    (``total_count`` of appended RungFills == ``taken_count``): the wings alone are a naked position until
    the rungs they hedge actually fill (else the stall policy resolves the shortfall)."""
    b = next((x for x in st.wing_batches if x.index == index), None)
    if b is None or b.completed:
        return _sync_wing_mirrors(st)
    legs = _batch_legs(st, index)
    wings_ok = bool(legs) and all(l.status == "filled" for l in legs)
    if not wings_ok:
        return _sync_wing_mirrors(st)
    if b.print_through:
        if b.total_count < b.taken_count:
            return _sync_wing_mirrors(st)   # wings in hand, waiting on the pre-hedged rung(s) to fill
        st = replace(
            st, sets_done=st.sets_done + b.taken_count,
            wing_batches=_replace_batch(st.wing_batches, index, completed=True, one_legged=False,
                                        resolved=True),
        )
        st = _pt_mark_resolution(st, index, "filled", None)
        return _sync_wing_mirrors(st)
    st = replace(
        st, sets_done=st.sets_done + b.total_count,
        wing_batches=_replace_batch(st.wing_batches, index, completed=True, one_legged=False),
    )
    return _sync_wing_mirrors(st)


# ---------------------------------------------------------------------------
# Print-through wings (Brad, 2026-09-26): fire the wing take EARLY off a bucket print
# ---------------------------------------------------------------------------
def _pt_covered_coids(st: V33State) -> set[str]:
    """Rung coids pre-hedged by an ACTIVE (unresolved) print-through trigger. Convergence never rolls
    these, and a new trigger never double-hedges them. F7 (2026-09-26 R2): covers EVERY rung_coid of an
    unresolved trigger (not only the not-yet-filled ones), so a partially-filled rung (lots_per_rung > 1)
    can never be re-hedged by a second trigger while its remainder still rests."""
    out: set[str] = set()
    for t in st.print_through:
        if not t.resolved:
            out.update(t.rung_coids)
    return out


def _pt_trigger_index_for_coid(st: V33State, coid: str) -> int | None:
    """The index into ``st.print_through`` of the active trigger that pre-hedged ``coid``, or None. F7: a
    fill on a pre-hedged coid attributes to its unresolved trigger's batch even on a later partial
    (lots_per_rung > 1) -- so repeated partials of one rung all attach to the same pre-emptive batch and
    never coalesce a fresh one."""
    for i, t in enumerate(st.print_through):
        if not t.resolved and coid in t.rung_coids:
            return i
    return None


def _replace_pt(triggers: tuple[PrintThroughTrigger, ...], idx: int, **fields
                ) -> tuple[PrintThroughTrigger, ...]:
    out = list(triggers)
    out[idx] = replace(out[idx], **fields)
    return tuple(out)


def _pt_mark_resolution(st: V33State, batch_index: int, resolution: str, now: float | None
                        ) -> V33State:
    """Mark the trigger for ``batch_index`` resolved with ``resolution`` (idempotent: an already-resolved
    trigger keeps its first resolution)."""
    out = list(st.print_through)
    for i, t in enumerate(out):
        if t.batch_index == batch_index and not t.resolved:
            out[i] = replace(t, resolved=True, resolution=resolution, resolved_ts=now)
            return replace(st, print_through=tuple(out))
    return st


def _pt_wing_bids(st: V33State) -> tuple[Decimal | None, Decimal | None]:
    """The current wing BIDS to unwind into (sell YES@Sd at yes_bid, sell NO@Su at no_bid)."""
    yb = nb = None
    top_sd = st.strike_tops.get(st.spot_Sd) if st.spot_Sd is not None else None
    top_su = st.strike_tops.get(st.spot_Su) if st.spot_Su is not None else None
    if top_sd is not None and top_sd.yes_bid is not None:
        yb = top_sd.yes_bid
    if top_su is not None and top_su.no_bid is not None:
        nb = top_su.no_bid
    return yb, nb


def _pt_bucket_no_ask(st: V33State) -> Decimal | None:
    """The current bucket-NO ask on the ladder's bucket (for the ``complete`` stall taker leg)."""
    rest_sd = st.rest_bucket_Sd if st.rest_bucket_Sd is not None else st.spot_Sd
    if rest_sd is None:
        return None
    top = st.bucket_tops.get(rest_sd)
    if top is None or top.no_ask is None:
        return None
    return top.no_ask


def _batch_wpaid_held(st: V33State, batch_index: int) -> Decimal:
    """Per-contract wing cost actually IN HAND for a batch: sum over FILLED legs only of
    ``fill_price + fee`` (or the limit if a filled leg somehow lacks a price). F1 (2026-09-26 R2): an
    UNFILLED leg contributes 0 -- we never paid for a wing that did not fill, so it is not part of the
    held cost (nor of the round-trip). Used by ``complete`` (both wings filled by the stall gate) and
    ``unwind`` (per filled leg)."""
    w = _ZERO
    for lg in _batch_legs(st, batch_index):
        if lg.status == "filled":
            price = lg.fill_price if lg.fill_price is not None else lg.limit
            w += price + fee(price)
    return w


def _print_through_step(
    params: V33Params, st: V33State, event: Trade, now: float
) -> tuple[V33State, list[V33Action]]:
    """Fire the wing take EARLY when a bucket YES print lands within ``print_through_ticks`` of a resting
    rung's offer (1-n) and moving toward it. Pre-hedges every such live rung not already covered, in ONE
    batch sized to the total, at the wing asks we currently see (+ ``print_through_slack_c``). Records a
    ``PrintThroughTrigger``; the fills, stall and fail-closed paths carry it from here."""
    if not params.print_through:
        return st, []
    if getattr(event, "taker_side", None) != "yes":
        return st, []
    rest_sd = st.rest_bucket_Sd if st.rest_bucket_Sd is not None else st.spot_Sd
    if rest_sd is None:
        return st, []
    bucket_ticker = st.bucket_tickers.get(rest_sd)
    if bucket_ticker is None or event.market_ticker != bucket_ticker:
        return st, []
    if st.rest_allotment_done or st.stood_down or st.pt_stood_down:
        return st, []
    # F6 (2026-09-26 R2): fire the PRE-emptive take only inside the live quoting window (T-quote_start ..
    # T-quote_end). A print after quote-end (or in the settle grace) would pre-hedge a rung that can no
    # longer fill before settle -> a forced unwind. (A wing take on a REAL fill is legitimately post-window;
    # a pre-emptive take on an unfilled rung is not.)
    if not _in_window(params, st, now):
        return st, []
    # D2: price the pre-emptive wings on the LADDER's bucket (rest_sd), the bucket the pre-hedged rungs
    # rest on — not the current spot (identical in the gated path where spot == rest, correct if it drifts).
    rest_su = rest_sd + params.bucket_width
    asks = _wing_prices_for_bucket(st, rest_sd, rest_su, now, params)
    if asks is None:
        return st, []                     # cannot hedge without fresh wing books -> do not fire
    ya, na = asks
    yes_print = event.yes_price
    threshold = params.print_through_ticks * _CENT
    covered = _pt_covered_coids(st)
    inflight_old = {r.old_coid for r in st.rolls_in_flight}
    # F5 (2026-09-26 R2): a print pre-hedges a rung only when it is APPROACHING that rung's offer from
    # below within `print_through_ticks` -- i.e. the print sits in [offer - ticks*1c, offer - 1c]. This is
    # the leading-edge signal (fire one-to-ticks cents EARLY, before the cross); the cross itself fills the
    # rung and attributes to the pre-emptive batch. A lone deep print (far above a cheap rung's offer) no
    # longer pre-hedges the whole cheaper ladder.
    cands = []
    for o in st.ladder:
        if not (o.live and not o.pending and o.order_id is not None):
            continue                       # F6: only a live, acked, resting rung (never a pending create)
        if o.client_order_id in covered or o.client_order_id in inflight_old:
            continue
        offer = _ONE - o.price
        if (offer - threshold) - _EPS <= yes_print <= (offer - _CENT) + _EPS:
            cands.append(o)
    if not cands:
        return st, []
    slack = params.print_through_slack_c * _CENT
    yes_limit = min(ya + slack, _LIMIT_CEILING)
    no_limit = min(na + slack, _LIMIT_CEILING)
    count = _q_count(sum((o.count for o in cands), _ZERO))
    w_paid = yes_limit + fee(yes_limit) + no_limit + fee(no_limit)
    lock_at_trigger = sum((o.count * lock_value(o.price, w_paid) for o in cands), _ZERO)
    idx = st.next_batch_index
    coid_y, st = _mint_coid(st)
    coid_n, st = _mint_coid(st)
    # D2: the pre-emptive wing strikes are the LADDER bucket's strikes (rest_sd / rest_su), and the batch
    # carries that bucket so a later re-price/retry keys off it.
    legs = (
        LegOrder(st.strike_tickers.get(rest_sd, ""), BUY_YES, "buy", count, yes_limit),
        LegOrder(st.strike_tickers.get(rest_su, ""), BUY_NO, "buy", count, no_limit),
    )
    new_legs = (
        WingLeg(legs[0].ticker, BUY_YES, count, yes_limit, coid_y, batch=idx),
        WingLeg(legs[1].ticker, BUY_NO, count, no_limit, coid_n, batch=idx),
    )
    batch = WingBatch(index=idx, server_ts=now, fills=(), taken=True, print_through=True,
                      taken_count=count, bucket_Sd=rest_sd)
    trig = PrintThroughTrigger(
        batch_index=idx, rung_coids=tuple(o.client_order_id for o in cands),
        rung_prices=tuple(o.price for o in cands), count=count, trigger_ts=now, yes_print=yes_print,
        W_at_trigger=st.W, n_top_at_trigger=st.n_top, lock_at_trigger=lock_at_trigger,
        yes_ask_at_trigger=ya, no_ask_at_trigger=na,
    )
    st = replace(
        st, wing_batches=st.wing_batches + (batch,), wing_legs=st.wing_legs + new_legs,
        next_batch_index=idx + 1, print_through=st.print_through + (trig,),
    )
    st = _sync_wing_mirrors(st)
    return st, [_mk(ActionKind.TAKE_WINGS, st.shakedown, legs=legs, count=count, lock=lock_at_trigger)]


def _pt_latch_if_done(params: V33Params, st: V33State) -> V33State:
    """Latch ``rest_allotment_done`` when the allotment is fully spent (mirrors _book_rung_fill's latch)."""
    if (st.rungs_filled >= params.max_sets_per_hour
            or (not st.ladder and not st.awaiting_replace and not st.rolls_in_flight
                and st.outstanding_cancels == 0)):
        return replace(st, rest_allotment_done=True)
    return st


def _pt_book_taker_complete(
    params: V33Params, st: V33State, idx: int, price: Decimal, count: Decimal, now: float
) -> V33State:
    """Book ``count`` bucket-NO lots bought as an IOC TAKER (the ``complete`` stall leg) into the trigger's
    batch at ``price`` -- a fee-bearing taker leg (RungFill.taker=True). Shared by the DRY optimistic book
    and the ARMED book-from-IOC-response path (F3). D2: the completing rung's bucket is the LADDER bucket
    (rest_sd), captured on the RungFill; it is never the raw spot."""
    count = _q_count(count)
    if count <= 0:
        return st
    trig = st.print_through[idx]
    batch = next(b for b in st.wing_batches if b.index == trig.batch_index)
    rest_sd = batch.bucket_Sd if batch.bucket_Sd is not None else (
        st.rest_bucket_Sd if st.rest_bucket_Sd is not None else st.spot_Sd)
    bt = st.bucket_tickers.get(rest_sd) if rest_sd is not None else None
    rung_lbl = _rung_of(st.n_top, price) if st.n_top is not None else 0
    e_lbl = _e_rung(params, st.n_top, price) if st.n_top is not None else params.E_min
    rf = RungFill(rung=rung_lbl, E_rung=e_lbl, price=price, count=count, server_ts=now,
                  coid=None, order_id=None, W=st.W, n_top=st.n_top, bucket_ticker=bt,
                  bucket_Sd=rest_sd, bucket_Su=(rest_sd + params.bucket_width) if rest_sd is not None
                  else None, taker=True,
                  weight=(_weight_of_rung(params, rung_lbl)
                          if 0 <= rung_lbl < len(params.rung_lots) else None))
    st = replace(st, rest_fills=st.rest_fills + (rf,), rungs_filled=st.rungs_filled + 1,
                 wing_batches=_replace_batch(st.wing_batches, batch.index, fills=batch.fills + (rf,)))
    return st


def _pt_cancel_unfilled_rungs(
    params: V33Params, st: V33State, trig: PrintThroughTrigger, now: float
) -> tuple[V33State, list[V33Action], list[str]]:
    """Cancel-first: pull every pre-hedged rung of ``trig`` still RESTING in the ladder (F7: by ladder
    membership, so a partially-filled rung's remainder is pulled too), so the stall/fail-closed resolution
    can never end up double-filled. Marks each cancelled rung's current margin consumed (state 2) so
    convergence never re-places it. Returns (st, actions, cancelled_order_ids) -- the order_ids we now
    await a cancel confirm for (F2)."""
    actions: list[V33Action] = []
    ms = list(st.margin_state)
    cancelled: list[str] = []
    for coid in trig.rung_coids:
        o = next((x for x in st.ladder if x.client_order_id == coid), None)
        if o is None:
            continue                            # already filled+dropped, or never placed
        actions.append(_cancel_action(st, o))
        st = _remember_cancel_ctx(st, o)
        if st.n_top is not None:
            m = _margin_of(params, st.n_top, o.price)
            if 0 <= m < len(ms):
                ms[m] = 2                       # consumed by the stall -> convergence never re-places it
        if o.order_id is not None:
            cancelled.append(o.order_id)
        st = replace(st, ladder=_drop_order(st.ladder, coid),
                     outstanding_cancels=st.outstanding_cancels + (1 if o.order_id else 0))
    st = replace(st, margin_state=tuple(ms))
    return st, actions, cancelled


def _pt_begin_stall(
    params: V33Params, st: V33State, idx: int, now: float
) -> tuple[V33State, list[V33Action]]:
    """F2 phase 1: on a stall, cancel the unfilled rests FIRST and mark the trigger ``stall_pending``,
    then WAIT for those cancels to confirm before finalising. A fill that races the cancel arrives as an
    OrderCancelled(filled>0) and attributes to THIS still-active batch, so ``_pt_finalize_stall`` completes
    the TRUE remaining shortfall -- never over-buying a taker set and never coalescing a second wing batch.
    If nothing is awaiting a confirm (no order_ids to cancel), finalise immediately."""
    st, ca, oids = _pt_cancel_unfilled_rungs(params, st, st.print_through[idx], now)
    st = replace(st, print_through=_replace_pt(st.print_through, idx, stall_pending=True,
                                               pending_cancels=tuple(oids)))
    if not oids:
        st, fa = _pt_finalize_stall(params, st, idx, now)
        return st, ca + fa
    return st, ca


def _pt_on_cancel_confirmed(
    params: V33Params, st: V33State, order_id: str | None, now: float
) -> tuple[V33State, list[V33Action]]:
    """A stall cancel confirmed (F2): drop ``order_id`` from any stall_pending trigger's
    ``pending_cancels``; when a trigger's list empties, finalise it. Called from ``_apply_cancelled``
    AFTER any racing fill has been booked (and thus attributed to the batch)."""
    actions: list[V33Action] = []
    if order_id is None or not st.print_through:
        return st, actions
    for idx in range(len(st.print_through)):
        t = st.print_through[idx]
        if not t.stall_pending or t.resolved or order_id not in t.pending_cancels:
            continue
        remaining = tuple(o for o in t.pending_cancels if o != order_id)
        st = replace(st, print_through=_replace_pt(st.print_through, idx, pending_cancels=remaining))
        if not remaining:
            st, fa = _pt_finalize_stall(params, st, idx, now)
            actions += fa
    return st, actions


def _pt_finalize_stall(
    params: V33Params, st: V33State, idx: int, now: float
) -> tuple[V33State, list[V33Action]]:
    """F2 phase 2: the stall cancels have confirmed and any racing fill has attributed to the batch. Now
    complete the TRUE remaining shortfall (buy the bucket-NO taker) or unwind the pre-taken wings."""
    actions: list[V33Action] = []
    trig = st.print_through[idx]
    if trig.resolved:
        return st, actions
    st = replace(st, print_through=_replace_pt(st.print_through, idx, stall_pending=False))
    batch = next((b for b in st.wing_batches if b.index == trig.batch_index), None)
    if batch is None:
        return _pt_mark_resolution(st, trig.batch_index, "filled", now), actions
    shortfall = trig.count - batch.total_count
    if shortfall <= 0:
        # the race filled every pre-hedged rung -> no taker needed; the wing/rung fills close the set.
        st = _maybe_close_set(st, batch.index)
        st = _pt_mark_resolution(st, batch.index, "filled", now)
        return _sync_wing_mirrors(st), actions
    no_ask = _pt_bucket_no_ask(st)
    w_paid = _batch_wpaid_held(st, batch.index)          # both wings in hand (stall gate)
    lock_complete_per = lock_value(no_ask, w_paid) if no_ask is not None else None
    floor = params.print_through_min_lock_c * _CENT
    policy = params.print_through_policy
    do_complete = (
        policy != "unwind" and no_ask is not None and lock_complete_per is not None
        and (batch.total_count > 0 or lock_complete_per >= floor)
    )
    if do_complete:
        st, a = _pt_complete(params, st, idx, _q_count(shortfall), no_ask, lock_complete_per, now)
    else:
        st, a = _pt_unwind(params, st, idx, now, resolution="unwind")
    return _sync_wing_mirrors(st), actions + a


def _pt_complete(
    params: V33Params, st: V33State, idx: int, shortfall: Decimal, no_ask: Decimal,
    lock_complete_per: Decimal, now: float
) -> tuple[V33State, list[V33Action]]:
    """The ``complete`` branch: buy ``shortfall`` bucket-NO as an IOC taker. DRY books it optimistically
    (no venue). ARMED (F3) sends the IOC with ``complete_coid`` and books from its Fill in ``_apply_fill``;
    a fill shortfall there unwinds the un-hedged wings + stands down. D3: ``shortfall`` is Decimal lots."""
    shortfall = _q_count(shortfall)
    trig = st.print_through[idx]
    batch = next(b for b in st.wing_batches if b.index == trig.batch_index)
    rest_sd = batch.bucket_Sd if batch.bucket_Sd is not None else (
        st.rest_bucket_Sd if st.rest_bucket_Sd is not None else st.spot_Sd)
    bt = st.bucket_tickers.get(rest_sd) if rest_sd is not None else None
    if st.shakedown:
        st = _pt_book_taker_complete(params, st, idx, no_ask, shortfall, now)
        st = replace(st, print_through=_replace_pt(
            st.print_through, idx, resolved=True, resolution="complete", shortfall=shortfall,
            complete_price=no_ask, lock_at_completion=lock_complete_per, resolved_ts=now))
        st = _maybe_close_set(st, batch.index)
        st = _pt_latch_if_done(params, st)
    else:
        cc, st = _mint_coid(st)
        st = replace(st, print_through=_replace_pt(
            st.print_through, idx, resolved=True, resolution="complete", shortfall=shortfall,
            complete_price=no_ask, lock_at_completion=lock_complete_per, resolved_ts=now,
            complete_coid=cc))
    legs = (LegOrder(bt or "", BUY_NO, "buy", shortfall, no_ask),)
    action = _mk(V33ActionKind.TAKE_BUCKET_NO, st.shakedown, legs=legs, ticker=bt or "", side=BUY_NO,
                 action="buy", count=shortfall, price=no_ask,
                 client_order_id=(None if st.shakedown else st.print_through[idx].complete_coid))
    return st, [action]


def _pt_unwind(
    params: V33Params, st: V33State, idx: int, now: float, *, resolution: str
) -> tuple[V33State, list[V33Action]]:
    """Sell the pre-taken wings back IOC at the current bids. F1 (2026-09-26 R2): sell ONLY legs that
    FILLED, each sized to what we actually HOLD -- never the never-filled wing (that would open a naked
    short, the opposite of going flat). Cost/round-trip from filled legs only.

    Two shapes: (a) both wings filled (a clean stall-unwind) -> sell the un-hedged remainder
    (``leg.count - filled_lots``) of each; if some rungs filled, keep those as a completed set. (b) only
    one wing filled (fail-closed) -> sell that one leg's full held count and drop the batch."""
    actions: list[V33Action] = []
    trig = st.print_through[idx]
    batch = next(b for b in st.wing_batches if b.index == trig.batch_index)
    legs = _batch_legs(st, batch.index)
    filled_legs = [lg for lg in legs if lg.status == "filled"]
    both_filled = len(filled_legs) == 2
    filled_lots = batch.total_count
    yes_bid, no_bid = _pt_wing_bids(st)

    def _bid(side: str) -> Decimal | None:
        return yes_bid if side == BUY_YES else no_bid

    legs_sell: list[LegOrder] = []
    roundtrip = _ZERO
    keep_hedged = both_filled and filled_lots > 0
    sell_each = (filled_legs[0].count - filled_lots) if both_filled else None  # per-leg for the pair case
    for lg in filled_legs:
        n = sell_each if sell_each is not None else lg.count
        if n <= 0:
            continue
        paid = lg.fill_price if lg.fill_price is not None else lg.limit
        bid = _bid(lg.side)
        if bid is not None:
            legs_sell.append(LegOrder(lg.ticker, lg.side, "sell", _q_count(n), bid))
            roundtrip += ((paid + fee(paid)) - (bid - fee(bid))) * _q_count(n)
        else:
            roundtrip += (paid + fee(paid)) * _q_count(n)           # no bid -> conservative full loss

    if keep_hedged:
        # keep the ``filled_lots`` sets: shrink both wing legs + taken_count to filled_lots, complete them.
        new_legs = tuple(replace(lg, count=filled_lots) if (lg.batch == batch.index
                                                            and lg.status == "filled") else lg
                         for lg in st.wing_legs)
        st = replace(st, wing_legs=new_legs,
                     wing_batches=_replace_batch(st.wing_batches, batch.index, taken_count=filled_lots),
                     print_through=_replace_pt(st.print_through, idx,
                                               shortfall=_q_count(sell_each or _ZERO),
                                               roundtrip_cost=roundtrip,
                                               resolved=True, resolution=resolution, resolved_ts=now))
        st = _maybe_close_set(st, batch.index)
    else:
        # drop the (now-flat, or one-legged) batch + legs so the ledger never counts unwound wings as held.
        st = replace(
            st,
            wing_batches=tuple(b for b in st.wing_batches if b.index != batch.index),
            wing_legs=tuple(lg for lg in st.wing_legs if lg.batch != batch.index),
            print_through=_replace_pt(st.print_through, idx,
                                      shortfall=_q_count(trig.count - filled_lots),
                                      roundtrip_cost=roundtrip, resolved=True, resolution=resolution,
                                      resolved_ts=now))
    if legs_sell:
        actions.append(_mk(V33ActionKind.UNWIND_WINGS, st.shakedown, legs=tuple(legs_sell),
                           count=max(l.count for l in legs_sell)))
    return st, actions


def _pt_fail_closed(
    params: V33Params, st: V33State, batch_index: int, now: float
) -> tuple[V33State, list[V33Action]]:
    """A pre-emptive wing leg did NOT fully fill (the ask moved past our tight limit) -> FAIL CLOSED
    (the one-legged risk the falsifier counts). Cancel the pre-hedged rests, unwind ONLY the wing that
    actually filled (F1 -- never sell the leg we never bought), and stand the hour down. Never retry,
    never place again this window."""
    actions: list[V33Action] = []
    idx = next((i for i, t in enumerate(st.print_through) if t.batch_index == batch_index
                and not t.resolved), None)
    if idx is None:
        return st, actions
    trig = st.print_through[idx]
    st, ca, _oids = _pt_cancel_unfilled_rungs(params, st, trig, now)
    actions += ca
    st, ua = _pt_unwind(params, st, idx, now, resolution="partial")   # F1: filled legs only
    actions += ua
    st = replace(st, pt_stood_down=True, stood_down=True, pt_one_legged=True)
    st, sd = _standdown(st, "print_through_partial")
    st = _sync_wing_mirrors(st)
    return st, actions + sd


def _pt_apply_complete_fill(
    params: V33Params, st: V33State, event: Fill, now: float
) -> tuple[V33State, list[V33Action]] | None:
    """F3: book an ARMED ``complete`` taker fill from the IOC response. Returns (st, actions) if ``event``
    is a complete taker fill for a trigger's ``complete_coid``, else None. A fill SHORTFALL (the marketable
    IOC did not fully fill) leaves ``shortfall - got`` un-hedged wings -> sell them back (F1) + stand down
    (journalled ``print_through_complete_short``)."""
    for i, t in enumerate(st.print_through):
        if t.complete_coid is None or t.complete_coid != event.client_order_id:
            continue
        got = _q_count(event.count)
        price = event.price if event.price is not None else (t.complete_price or _ZERO)
        st = _pt_book_taker_complete(params, st, i, price, got, now)
        st = replace(st, print_through=_replace_pt(st.print_through, i, complete_coid=None))
        batch = next((b for b in st.wing_batches if b.index == t.batch_index), None)
        short = _q_count(t.shortfall) - got
        actions: list[V33Action] = []
        if short > 0 and batch is not None:
            # the un-hedged wings for the missed lots: shrink the batch to what is hedged and sell them.
            hedged = batch.total_count                         # filled_lots + got
            yes_bid, no_bid = _pt_wing_bids(st)
            legs_sell: list[LegOrder] = []
            roundtrip = t.roundtrip_cost or _ZERO
            for lg in _batch_legs(st, batch.index):
                if lg.status != "filled":
                    continue
                bid = yes_bid if lg.side == BUY_YES else no_bid
                paid = lg.fill_price if lg.fill_price is not None else lg.limit
                if bid is not None:
                    legs_sell.append(LegOrder(lg.ticker, lg.side, "sell", _q_count(short), bid))
                    roundtrip += ((paid + fee(paid)) - (bid - fee(bid))) * short
                else:
                    roundtrip += (paid + fee(paid)) * short
            new_legs = tuple(replace(lg, count=hedged) if (lg.batch == batch.index
                                                           and lg.status == "filled") else lg
                             for lg in st.wing_legs)
            st = replace(st, wing_legs=new_legs,
                         wing_batches=_replace_batch(st.wing_batches, batch.index, taken_count=hedged),
                         print_through=_replace_pt(st.print_through, i, roundtrip_cost=roundtrip),
                         pt_stood_down=True, stood_down=True, pt_one_legged=True)
            if legs_sell:
                actions.append(_mk(V33ActionKind.UNWIND_WINGS, st.shakedown, legs=tuple(legs_sell),
                                   count=int(short)))
            st, csd = _standdown(st, "print_through_complete_short")
            actions += csd
        st = _maybe_close_set(st, t.batch_index)
        st = _pt_latch_if_done(params, st)
        return _sync_wing_mirrors(st), actions
    return None


def _pt_stall_step(
    params: V33Params, st: V33State, now: float
) -> tuple[V33State, list[V33Action]]:
    """Begin the stall policy for every active print-through trigger whose pre-hedged rung(s) have not
    filled within ``print_through_stall_ms``, and fail-closed any whose wings did not both fill. A trigger
    already in the cancel-confirm wait (``stall_pending``) is left alone -- it finalises when its cancels
    confirm (``_pt_on_cancel_confirmed``)."""
    if not params.print_through or not st.print_through:
        return st, []
    actions: list[V33Action] = []
    stall_s = params.print_through_stall_ms / 1000.0
    for idx in range(len(st.print_through)):
        t = st.print_through[idx]
        if t.resolved or t.stall_pending:
            continue
        batch = next((b for b in st.wing_batches if b.index == t.batch_index), None)
        if batch is None or batch.completed:
            continue
        if batch.total_count >= t.count:
            continue                                  # every pre-hedged rung filled -> close handles it
        if now - t.trigger_ts < stall_s:
            continue                                  # not stalled yet
        legs = _batch_legs(st, batch.index)
        wings_filled = bool(legs) and all(l.status == "filled" for l in legs)
        if not wings_filled:
            st, fa = _pt_fail_closed(params, st, batch.index, now)
            actions += fa
            continue
        st, sa = _pt_begin_stall(params, st, idx, now)
        actions += sa
    return st, actions


def print_through_summary(st: V33State) -> list[dict]:
    """A per-trigger summary for the ledger row / report (numbers, not orders)."""
    out: list[dict] = []
    for t in st.print_through:
        out.append({
            "batch_index": t.batch_index,
            "count": t.count,
            "filled": len(t.filled_coids),
            "trigger_ts": t.trigger_ts,
            "yes_print": str(t.yes_print),
            "yes_ask_at_trigger": (str(t.yes_ask_at_trigger) if t.yes_ask_at_trigger is not None else None),
            "no_ask_at_trigger": (str(t.no_ask_at_trigger) if t.no_ask_at_trigger is not None else None),
            "W_at_trigger": (str(t.W_at_trigger) if t.W_at_trigger is not None else None),
            "n_top_at_trigger": (str(t.n_top_at_trigger) if t.n_top_at_trigger is not None else None),
            "lock_at_trigger": (str(t.lock_at_trigger) if t.lock_at_trigger is not None else None),
            "resolution": t.resolution,
            "resolved": t.resolved,
            "shortfall": t.shortfall,
            "complete_price": (str(t.complete_price) if t.complete_price is not None else None),
            "lock_at_completion": (str(t.lock_at_completion)
                                   if t.lock_at_completion is not None else None),
            "roundtrip_cost": (str(t.roundtrip_cost) if t.roundtrip_cost is not None else None),
        })
    return out


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
    # D1: retain the order's OWN bucket_Sd so a late fill surfaced only by the cancel confirm is
    # attributed to the bucket the rung rested on, not the (possibly moved-on) current spot bucket.
    ctx[order.order_id] = (order.client_order_id, order.price, order.rung, order.E_rung, order.bucket_Sd)
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


def _cancel_all_then_replace(st: V33State) -> tuple[V33State, list[V33Action]]:
    """Gate D (2026-10-03, the 02:00Z naked-fill incident): EVERY cancel-all that can be followed by a
    re-place TRACKS its cancels and arms ``awaiting_replace``, exactly like the bucket-change path. The
    place path then holds while ``outstanding_cancels > 0`` and re-places only once the ladder is clear, so
    a cancel-all is never raced by a re-place of the same slots while the venue still holds the old rests.
    (Pre-fix the stale-wing hold-expiry cancelled untracked, the feed read fresh 230 ms later, and
    ``_place_all`` re-placed 11 over 11 unconfirmed cancels.) ``awaiting_replace`` is only ever SET here
    (never cleared): a no-op cancel-all over an empty ladder leaves an earlier pending re-place intact."""
    had_orders = bool(st.ladder)
    st, ca = _cancel_all(st, track_outstanding=True)
    if had_orders:
        st = replace(st, awaiting_replace=True)
    return st, ca


def _standdown(st: V33State, reason: str) -> tuple[V33State, list[V33Action]]:
    if reason == st.last_standdown_reason:
        return replace(st, stand_down_reason=reason), []
    st = replace(st, last_standdown_reason=reason, stand_down_reason=reason)
    return st, [V33Action(kind=ActionKind.STAND_DOWN, reason=reason)]


def _place_action(st: V33State, params: V33Params, coid: str, n: Decimal, count: int) -> V33Action:
    exp = st.close_epoch - params.quote_end_s
    return _mk(
        ActionKind.PLACE_REST, st.shakedown,
        ticker=st.bucket_tickers.get(st.spot_Sd, ""), side=BUY_NO, action="buy",
        count=count, price=n, expiration_epoch=exp, client_order_id=coid,
    )


def _place_one(
    params: V33Params, st: V33State, price: Decimal, rung: int, E_rung: Decimal, now: float,
    *, count: Decimal | None = None,
) -> tuple[V33State, list[V33Action]]:
    """Emit ONE PLACE_REST rung at ``price`` and add it (pending) to the ladder. ``count`` is the lots to
    rest: L5 -> the rung's configured weight ``rung_lots[rung]`` for a FRESH slot placement (default), or
    an explicit remaining count for the roll's cancel->create fallback (which re-places what was still
    resting, matching the amend end-state). Used by the bucket / first placement (each rung), the
    vacant-create, and the roll fallback."""
    c = _q_count(count) if count is not None else _q_count(Decimal(_weight_of_rung(params, rung)))
    coid, st = _mint_coid(st)
    order = RestOrder(
        client_order_id=coid, order_id=None, price=price, count=c,
        placed_ts=now, live=False, pending=True, bucket_Sd=st.spot_Sd, rung=rung, E_rung=E_rung,
    )
    st = replace(st, ladder=st.ladder + (order,))
    return st, [_place_action(st, params, coid, price, c)]


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
    """Place one order at EACH placeable OPEN slot in the margin array (Brad's model, R4), each sized to
    its rung's configured WEIGHT (L5). After a partial sweep of margins 5..k, this places the REMAINING
    open margins on the new/first ticker; the CONTRACT allotment ``sum(rung_lots)`` bounds the total
    resting lots (a slot whose weight would overrun the remaining allotment is skipped). If the allotment
    is already spent (no open placeable slot and filled >= max_sets), latch ``rest_allotment_done``.
    Counts as ONE ladder placement (the debounce anchor)."""
    assert st.n_top is not None
    actions: list[V33Action] = []
    slots = _placeable_open_slots(params, st)
    # never exceed the remaining CONTRACT allotment (L5). ``_place_all`` is only called with an empty
    # ladder, so ``resting`` is 0 here; the belt-and-braces subtracts it anyway. The coarse rung-unit
    # ``max_sets_per_hour`` latch is kept as a separate gate (unchanged from the pre-L5 behaviour).
    budget = _allotment(params) - _filled_contracts(st) - _resting_contracts(st)
    if budget <= 0 or st.rungs_filled >= params.max_sets_per_hour:
        return replace(st, rest_allotment_done=True), actions
    used = 0
    for m, price in slots:
        w = _weight_of_margin(params, m)
        if w <= 0 or used + w > budget:
            continue  # weight-0 slot (never state 1, defensive) or would overrun the allotment
        rung = m - _emin_cents(params)
        st, a = _place_one(params, st, price, rung, params.E_min + rung * _CENT, now, count=w)
        actions += a
        used += w
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
    CONFIRM (``_apply_amended``). The caller registers the roll + emits. L5: the amend re-prices but KEEPS
    the order's count (``order.count``) — a roll never re-sizes; re-sizing to a slot weight happens only
    on a FRESH order (place-all / vacant-create / roll fallback re-place)."""
    new_coid, st = _mint_coid(st)
    rp = RollPending(
        order_id=order.order_id, old_coid=order.client_order_id, new_coid=new_coid,
        target_price=target_price, target_margin=target_margin, started_ts=now,
    )
    exp = st.close_epoch - params.quote_end_s
    action = _mk(
        ActionKind.AMEND_REST, st.shakedown,
        order_id=order.order_id, ticker=st.bucket_tickers.get(st.spot_Sd, ""), side=BUY_NO,
        action="buy", count=int(order.count), price=target_price, expiration_epoch=exp,
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
                # capture the sub-cause at the instant the hold begins; it rides the hold through to the
                # resume/cancel that ends it (label only).
                st = replace(st, hold_reason=no_quote_reason, hold_since=now,
                             hold_sub_cause=st.wing_sub_cause)
                return st, [V33Action(kind=ActionKind.STAND_DOWN,
                                      reason="stale_or_missing_wing_hold")]  # journalled as stand_down_hold
            if (now - st.hold_since) * 1000.0 < params.stand_down_hold_ms:
                return st, actions   # still holding: keep the rests, emit nothing new
            # hold elapsed -> cancel-all + the real stand-down (journalled as stand_down_cancel). Set the
            # dedup reason to the PLAIN form so subsequent stale ticks (ladder now empty) dedup silently.
            st = replace(st, hold_reason=None, hold_since=None,
                         last_standdown_reason="stale_or_missing_wing",
                         stand_down_reason="stale_or_missing_wing")
            # gate D: TRACKED cancels + awaiting_replace -> the resume path waits for every confirm.
            st, ca = _cancel_all_then_replace(st)
            return st, ca + [V33Action(kind=ActionKind.STAND_DOWN,
                                       reason="stale_or_missing_wing_cancel")]
        # any other no-quote reason (or hold disabled / nothing to protect): cancel + stand down as before.
        # gate D audit: no_spot_bucket / stale_bucket / n_below_min (and stale_or_missing_wing with the hold
        # disabled) can all RESUME on the next healthy tick, so their cancels are tracked too; past_quote_end
        # and stood_down never resume (tracking them is harmless and keeps the count exact).
        if st.hold_since is not None:
            st = replace(st, hold_reason=None, hold_since=None)
        st, ca = _cancel_all_then_replace(st)
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
        st, ca = _cancel_all_then_replace(st)
        st = replace(st, rest_bucket_Sd=None)
        return st, actions + ca

    # hold while ANY tracked cancel-all (bucket change, stale-wing hold expiry, or any resumable no-quote
    # stand-down -- gate D) is unconfirmed: never re-place over rests the venue may still hold.
    if st.awaiting_replace and st.outstanding_cancels > 0:
        return st, actions

    # place: first placement, or place the open slots once every tracked cancel confirmed.
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
    # OUT = LIVE orders (acked, not already being amended) whose price is not a desired target. A rung
    # PRE-HEDGED by an active print-through trigger is NEVER rolled (its wings are committed at its offer;
    # rolling would rotate its coid and break the pre-hedge attribution) -- it simply rests until it fills.
    pt_covered = _pt_covered_coids(st)
    # L5: a PARTIALLY-FILLED rung (booked > 0) still resting is NEVER rolled — its slot is consumed
    # (state 2) and its remainder must stay put to complete that rung; rolling it would disturb a fill in
    # progress. (No-op at weight 1, where a resting order never has a booked partial.)
    OUT = [o for o in st.ladder if o.live and o.order_id is not None
           and o.client_order_id not in inflight_old and o.price not in target
           and o.client_order_id not in pt_covered
           and st.rest_booked_by_coid.get(o.client_order_id, 0) == 0]
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
        # extra VACANT with no OUT order (a suppressed slot became placeable) -> CREATE, sized to the
        # slot's WEIGHT, but never past the CONTRACT allotment (L5). Each target is a distinct open slot
        # (<= K), so price-slot uniqueness is guaranteed; the allotment is the binding cap.
        allot = _allotment(params)
        for tprice in VACANT[pairs:pairs + remaining]:
            m = target[tprice]
            w = _weight_of_margin(params, m)
            if w <= 0:
                continue
            if _filled_contracts(st) + _resting_contracts(st) + w > allot:
                break
            rung = m - _emin_cents(params)
            st, pa = _place_one(params, st, tprice, rung, params.E_min + rung * _CENT, now, count=w)
            actions += pa
            extra_times.append(now)

    if actions:
        new_times = tuple(t for t in st.replace_times if now - t <= 60.0) + tuple(extra_times)
        st = replace(st, last_replace_ts=now, replace_times=new_times)
    return st, actions
