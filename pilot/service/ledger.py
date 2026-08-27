"""ledger.py — the local position/intent ledger (pure data + pure transition functions).

Records every order INTENT (client_order_id, window, leg, source, count, limit, purpose), every
RESPONSE, and every derived position delta. Money is Decimal throughout. The transition functions are
pure: ``record_intent`` / ``record_response`` return a NEW ``LedgerState`` (input untouched), so the
same rebuild runs deterministically live and from a journal.

House law wiring (PLAN F13-as-modified): the Executor journals an ``order_intent`` record BEFORE the
POST and an ``order_response`` record BEFORE acting on the reply. ``rebuild_from_journal`` replays
exactly those record kinds, so a crash mid-window can be reconstructed from the (flushed) journal +
an exchange positions poll — the reconcile-first startup flow flags any intent that has no matching
response as IN-FLIGHT.

Position accounting:
  * A leg is keyed by (ticker, outcome_side) where outcome_side in {"yes","no"} is the outcome we
    HOLD (buy YES -> we hold "yes"; buy NO -> we hold "no"). A sell of that outcome reduces it.
  * ``bought_cost`` / ``sold_proceeds`` use ACTUAL fills: fill_count * average_fill_price, with the
    ACTUAL ``average_fee_paid`` from the response (buys add the fee to cost; sells subtract it from
    proceeds). See FEE_IS_TOTAL below.
  * The pair's two legs are ``high_ticker`` (higher strike) and ``low_ticker``; their held sides
    depend on the source (flip: high=no, low=yes; strangle: high=yes, low=no) and are captured from
    the ENTRY intent's legs.

FEE_IS_TOTAL (live-verification; DEFAULT CHANGED IN PHASE-3 REVIEW 2026-08-21):
Kalshi's per-entry field is named ``average_fee_paid`` — the word "average" makes per-contract
plausible, and its true semantics are unconfirmed until the first live fill. The fail-closed choice
under that uncertainty is to treat it as a PER-CONTRACT fee and multiply by ``fill_count``
(``FEE_IS_TOTAL = False``): if the truth is per-contract we are exact, and if the truth is TOTAL we
OVER-count (cost too high -> S1 realized_min too low -> MORE likely to halt), which is the safe
direction the commission demands ("under-counting fees is the unsafe direction"). The prior build
defaulted to True (add once), which UNDER-counts for any ``fill_count >= 2`` if the field is
per-contract — the unsafe, less-halting direction, contradicting its own confessed rationale. At
``fill_count == 1`` the two agree, so nothing changes at single-contract size; the difference only
appears at 2 pairs (rung 2), exactly where it must fail safe. The paired-replay's actual-fee
reconciliation confirms the true semantics on the first live fill; if TOTAL is confirmed, flip back.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from service.orders.envelope import OrderResponse, normalize_fill_to_side
from service.policy import Q1_STRANGLE, SUB_DOLLAR_FLIP

# --- intent purposes ---
PURPOSE_ENTRY = "entry"
PURPOSE_REBALANCE_BUY = "rebalance-buy"
PURPOSE_REBALANCE_SELL = "rebalance-sell"
PURPOSE_FLATTEN = "flatten"

FEE_IS_TOTAL = False  # see module docstring (Phase-3 review: fail-closed = multiply by fill_count)
MIN_PAIR_PAYOUT = Decimal("1.00")  # sub-$1 flip: worst-case settlement payout per matched pair

# The wide-box source id. Kept as a local constant (not imported from service.box) so this
# safety-critical, early-imported module never risks an import cycle; test_box_wiring asserts it
# equals ``service.box.WIDE_BOX``. A matched box pair has a GUARANTEED $1 floor (exactly one deep-ITM
# leg pays outside the pin region, both inside -> $2 pinned), so it is floor-booked at close exactly
# like a sub-$1 flip's matched portion; the +$1 pinned bonus rides in via the settlement backfill.
WIDE_BOX = "wide-box"

# Sources whose matched pairs carry a guaranteed >= $1/pair settlement floor (booked conservatively
# at close in ``realized_at_close``). A Q1-strangle has NO floor and is deliberately excluded.
_FLOOR_SOURCES = (SUB_DOLLAR_FLIP, WIDE_BOX)


@dataclass(frozen=True)
class IntentLeg:
    """One leg of an order intent. ``side`` is the outcome ('yes'/'no'); ``action`` is buy/sell.
    ``limit_price`` is the OBSERVED ask (buy) or bid (sell) in dollars. ``client_order_id`` is the
    per-leg idempotency key."""

    ticker: str
    side: str
    action: str
    count: int
    limit_price: Decimal
    client_order_id: str
    reduce_only: bool | None = None
    # Exchange-sharding route (Kalshi changelog 2026-08-24; 2026-08-27 ``market_not_found`` incident).
    # The shard the ticker lives on, captured from the wake market record. None = unknown -> the
    # Executor REFUSES to dispatch (never sends an unrouted order). Defaults to None so pre-fix
    # journals (records without the field) rebuild unchanged; ``rebuild_from_journal`` never dispatches.
    exchange_index: int | None = None


@dataclass(frozen=True)
class Intent:
    """A dispatch intent: 1 leg (rebalance/flatten) or 2 legs (entry batch)."""

    window: str
    source: str
    purpose: str
    legs: tuple[IntentLeg, ...]
    t_minus_s: float | None = None


@dataclass(frozen=True)
class LegPosition:
    """Per-(ticker, held-side) running position from ACTUAL fills.

    Notional (price*count, fee-free) is tracked separately from fees so the average FILL price is
    recoverable exactly (for the parity price-delta comparison) while cash accounting keeps fees."""

    ticker: str
    side: str
    bought: Decimal = Decimal(0)
    bought_notional: Decimal = Decimal(0)  # sum(fill * avg_fill_price), fee-free
    buy_fees: Decimal = Decimal(0)
    sold: Decimal = Decimal(0)
    sold_notional: Decimal = Decimal(0)  # sum(fill * avg_fill_price), fee-free
    sell_fees: Decimal = Decimal(0)

    @property
    def net(self) -> Decimal:
        return self.bought - self.sold

    @property
    def fees(self) -> Decimal:
        return self.buy_fees + self.sell_fees

    @property
    def bought_cost(self) -> Decimal:
        return self.bought_notional + self.buy_fees

    @property
    def sold_proceeds(self) -> Decimal:
        return self.sold_notional - self.sell_fees

    @property
    def net_cash_out(self) -> Decimal:
        return self.bought_cost - self.sold_proceeds

    @property
    def avg_buy_price(self) -> Decimal | None:
        return (self.bought_notional / self.bought) if self.bought > 0 else None


@dataclass(frozen=True)
class LedgerState:
    """Immutable window ledger. Transitions return a new instance."""

    window: str
    source: str
    high_ticker: str
    low_ticker: str
    high_side: str | None = None  # held outcome on the high leg (from the entry intent)
    low_side: str | None = None
    positions: dict[tuple[str, str], LegPosition] = field(default_factory=dict)
    intents: tuple[Intent, ...] = ()
    responses: tuple[OrderResponse, ...] = ()
    # client_order_id -> the leg it was submitted for (matching responses to legs)
    cid_to_leg: dict[str, IntentLeg] = field(default_factory=dict)
    # client_order_ids that have received a response (fill or no-fill)
    responded_cids: frozenset[str] = frozenset()

    # --- pure reads ---
    def leg_key(self, which: str) -> tuple[str, str] | None:
        if which == "high" and self.high_side is not None:
            return (self.high_ticker, self.high_side)
        if which == "low" and self.low_side is not None:
            return (self.low_ticker, self.low_side)
        return None

    def position(self, which: str) -> LegPosition | None:
        key = self.leg_key(which)
        return self.positions.get(key) if key is not None else None

    def net(self, which: str) -> Decimal:
        pos = self.position(which)
        return pos.net if pos is not None else Decimal(0)

    def matched_pairs(self) -> Decimal:
        return min(self.net("high"), self.net("low"))

    def is_balanced(self) -> bool:
        """End-state OK: 1:1 (nets equal and >=0) or 0:0."""
        return self.net("high") == self.net("low")

    def pair_net_cash_out(self) -> Decimal:
        total = Decimal(0)
        for which in ("high", "low"):
            pos = self.position(which)
            if pos is not None:
                total += pos.net_cash_out
        return total

    def realized_min(self) -> Decimal:
        """Worst-case realized on the matched portion of a sub-$1 flip pair: matched * MIN_PAIR_PAYOUT
        minus the net cash out (both legs, actual fees). Sub-$1 flip has a guaranteed >= $1/pair
        settlement, so this is the arithmetic-floor check for S1."""
        return self.matched_pairs() * MIN_PAIR_PAYOUT - self.pair_net_cash_out()

    def realized_cashflow(self) -> Decimal:
        """Settlement-INDEPENDENT realized cash from ACTUAL fills on both legs: proceeds - costs
        (all fees included) == -pair_net_cash_out(). For a fully-closed position (every leg net 0 —
        the flatten / round-trip case) this is the COMPLETE realized P&L, known immediately. For a
        position still holding net, it is the cash OUTLAY only; the held legs' settlement payoff is
        added later by the settlement backfill (see ``unsettled_legs``)."""
        return -self.pair_net_cash_out()

    def realized_at_close(self) -> Decimal:
        """The realized P&L booked at window close (BUG-2 repair): the settlement-independent cash
        flow, PLUS the sub-$1 flip's guaranteed >= $1/pair floor on the matched portion (a genuine
        settlement floor, so booking it is conservative and correct). Naked/overhang legs and any
        non-flip held legs contribute only their cash OUTLAY here (a conservative, safe-direction
        loss); their settlement payoff arrives via the backfill. Zero when nothing filled."""
        base = self.realized_cashflow()
        if self.source in _FLOOR_SOURCES:
            base = base + self.matched_pairs() * MIN_PAIR_PAYOUT
        return base

    def unsettled_legs(self) -> tuple[tuple[str, str, int], ...]:
        """(ticker, side, count) for each held leg whose settlement payoff is NOT yet booked at
        close — the input to the settlement backfill. For a sub-$1 flip the matched portion is
        floor-booked in ``realized_at_close`` and excluded here; only the naked OVERHANG (net beyond
        the matched count) remains pending. For any other source (no guaranteed floor) EVERY held
        leg is pending. Empty when the window ended flat/closed."""
        matched = self.matched_pairs()
        out: list[tuple[str, str, int]] = []
        for which in ("high", "low"):
            pos = self.position(which)
            if pos is None or pos.net <= 0:
                continue
            if self.source == SUB_DOLLAR_FLIP:
                held = pos.net - min(pos.net, matched)  # overhang only (matched is floor-booked)
            else:
                held = pos.net  # no floor -> the whole held net is pending settlement
            if held > 0:
                out.append((pos.ticker, pos.side, int(held)))
        return tuple(out)

    def has_any_fill(self) -> bool:
        """True iff any leg saw a real fill (a buy or a sell) this window."""
        return any(p.bought > 0 or p.sold > 0 for p in self.positions.values())

    def inflight_cids(self) -> tuple[str, ...]:
        """client_order_ids that were journaled as intents but have no matching response yet."""
        return tuple(cid for cid in self.cid_to_leg if cid not in self.responded_cids)


def new_ledger(window: str, source: str, high_ticker: str, low_ticker: str) -> LedgerState:
    return LedgerState(window=window, source=source, high_ticker=high_ticker, low_ticker=low_ticker)


def _held_sides_from_entry(intent: Intent, state: LedgerState) -> tuple[str | None, str | None]:
    """Capture the held outcome on each leg from an ENTRY intent's legs (buy legs)."""
    high_side, low_side = state.high_side, state.low_side
    for leg in intent.legs:
        if leg.ticker == state.high_ticker:
            high_side = leg.side
        elif leg.ticker == state.low_ticker:
            low_side = leg.side
    return high_side, low_side


def record_intent(state: LedgerState, intent: Intent) -> LedgerState:
    """Record a dispatch intent (pure). Captures held sides from the entry intent; registers each
    leg's client_order_id for later response matching."""
    high_side, low_side = state.high_side, state.low_side
    if intent.purpose == PURPOSE_ENTRY:
        high_side, low_side = _held_sides_from_entry(intent, state)
    new_cid = dict(state.cid_to_leg)
    for leg in intent.legs:
        new_cid[leg.client_order_id] = leg
    return replace(
        state,
        high_side=high_side,
        low_side=low_side,
        intents=state.intents + (intent,),
        cid_to_leg=new_cid,
    )


def _fee_of(resp: OrderResponse) -> Decimal:
    if resp.average_fee_paid is None:
        return Decimal(0)
    fee = resp.average_fee_paid
    return fee if FEE_IS_TOTAL else fee * resp.fill_count


def record_response(state: LedgerState, resp: OrderResponse) -> LedgerState:
    """Fold a per-entry response into positions (pure).

    A no-fill / zero-fill response updates only the responded-cid set (no position change). A real
    fill updates the matched leg's LegPosition using ACTUAL fill price + fee. Responses whose
    client_order_id is unknown to the ledger are recorded but move no position (fail-closed:
    an unknown fill is the Reconciler's S3 concern, not a silent position edit)."""
    responded = set(state.responded_cids)
    if resp.client_order_id:
        responded.add(resp.client_order_id)
    new_state = replace(
        state,
        responses=state.responses + (resp,),
        responded_cids=frozenset(responded),
    )

    leg = state.cid_to_leg.get(resp.client_order_id) if resp.client_order_id else None
    if leg is None or resp.fill_count <= 0 or resp.average_fill_price is None:
        return new_state

    key = (leg.ticker, leg.side)
    pos = new_state.positions.get(key) or LegPosition(ticker=leg.ticker, side=leg.side)
    fill = resp.fill_count
    notional = fill * resp.average_fill_price
    fee = _fee_of(resp)
    if leg.action == "buy":
        pos = replace(
            pos,
            bought=pos.bought + fill,
            bought_notional=pos.bought_notional + notional,
            buy_fees=pos.buy_fees + fee,
        )
    elif leg.action == "sell":
        pos = replace(
            pos,
            sold=pos.sold + fill,
            sold_notional=pos.sold_notional + notional,
            sell_fees=pos.sell_fees + fee,
        )
    else:  # fail closed: never guess a direction
        return new_state
    new_positions = dict(new_state.positions)
    new_positions[key] = pos
    return replace(new_state, positions=new_positions)


def settlement_payoff(
    unsettled_legs: list[dict[str, Any]] | tuple[Any, ...],
    results: dict[str, str],
) -> Decimal:
    """The settlement-backfill delta (BUG-2 repair, part c): given each held ticker's market result
    ('yes'/'no'), the payoff of the unsettled legs. A Kalshi contract pays MIN_PAIR_PAYOUT ($1) per
    contract iff its held outcome ``side`` matches the market ``result``, else $0. This payoff is
    ADDED to the window's realized — the legs' cash OUTLAY was already booked conservatively at close,
    so: a leg that loses adds $0 (close's conservative loss stands); a leg that wins adds count*$1
    (correcting the close's worst-case assumption up to the true realized).

    Fail-closed: a missing result for a held ticker, or a result that is not exactly 'yes'/'no',
    raises — a backfill is never guessed."""
    total = Decimal(0)
    for leg in unsettled_legs:
        ticker = leg["ticker"] if isinstance(leg, dict) else leg[0]
        side = leg["side"] if isinstance(leg, dict) else leg[1]
        count = leg["count"] if isinstance(leg, dict) else leg[2]
        res = results.get(ticker)
        if res is None:
            raise KeyError(f"settlement_payoff: no settlement result for held ticker {ticker}")
        if res not in ("yes", "no"):
            raise ValueError(
                f"settlement_payoff: result for {ticker} must be 'yes' or 'no', got {res!r}"
            )
        if res == side:
            total += Decimal(str(count)) * MIN_PAIR_PAYOUT
    return total


def retries_for_side(state: LedgerState, ticker: str, purpose: str) -> int:
    """Count recorded rebalance intents of ``purpose`` that targeted ``ticker`` (the per-side
    retry budget counter, derived from the intent record — no separate mutable counter)."""
    n = 0
    for intent in state.intents:
        if intent.purpose != purpose:
            continue
        if any(leg.ticker == ticker for leg in intent.legs):
            n += 1
    return n


# ---------------------------------------------------------------------------
# Fills record for the Phase-2 parity harness (five-bin paired report).
#
# F3 (Phase-2 review, handed to Phase 3): parity.assign_bin scores BIN_BOTH_MATCH ("the sim tells
# the truth") when an entry filled but NO fills leg matched the paired tickers -> a zero-comparison
# false honesty receipt. The fix has two halves:
#   (a) MINE (here): the emitted fills record ALWAYS carries per-leg entries keyed by the ACTUAL
#       paired high/low tickers with the average FILL price (fee-free) and the ACTUAL fee, so
#       parity._price_deltas always finds >=1 comparable leg when the pair filled. `fills_record`
#       is the documented contract; `to_window_fills` maps it onto parity's WindowFills/LegFill.
#   (b) THE INTEGRATOR'S one-line parity change (I do NOT edit parity.py — file ownership): in
#       assign_bin's bin-5 branch, require a comparable leg before certifying a match, e.g.
#           deltas = _price_deltas(sim, fills, high_ticker, low_ticker)
#           if not deltas:                       # <-- ADD: no comparable leg -> not a match
#               return WindowParity(close_time, BIN_BOTH_NO_FILL, sim, live, neutrality_ok=True,
#                                   source_match=source_match,
#                                   detail={"cause": "filled but no leg comparable to sim tickers"})
#       (BIN_BOTH_NO_FILL is the least-wrong existing bin for "filled but uncertifiable"; the
#       integrator may prefer a new UNCOMPARABLE label.) My record makes (a) hold so (b) is a
#       belt-and-suspenders guard, never the primary path.
#
# Per-leg fee: LegFill has no fee slot today. `fills_record` (the dict contract) carries `avg_fee`
# per leg; if the report wants it, the integrator adds an `avg_fee: Decimal = Decimal(0)` field to
# LegFill and passes it through `to_window_fills`. Fees flow to S1 via the ledger regardless.
# ---------------------------------------------------------------------------
def fills_record(state: LedgerState) -> dict[str, Any]:
    """The documented Phase-3 -> parity fills contract (plain data; Decimals kept).

    Shape:
      {
        "filled": bool,          # both legs of the pair filled at least the matched count
        "imbalance": bool,       # end-state NOT 1:1/0:0
        "realized_payoff": Decimal | None,   # matched * MIN_PAIR_PAYOUT - net cash out (sub-$1 floor)
        "matched_pairs": Decimal,
        "legs": [ {"ticker","side","count","avg_price","avg_fee"} ]   # one per filled paired leg
      }
    `avg_price` is the fee-free average fill price (comparable to the sim tape leg price);
    `avg_fee` is the ACTUAL total fee attributed to that leg's buys.
    """
    matched = state.matched_pairs()
    legs: list[dict[str, Any]] = []
    for which in ("high", "low"):
        pos = state.position(which)
        if pos is None or pos.bought <= 0:
            continue
        legs.append(
            {
                "ticker": pos.ticker,
                "side": pos.side,
                "count": int(pos.net) if pos.net == int(pos.net) else pos.net,
                "avg_price": pos.avg_buy_price,
                "avg_fee": pos.buy_fees,
            }
        )
    both_filled = state.net("high") > 0 and state.net("low") > 0
    realized = state.realized_min() if matched > 0 else None
    return {
        "filled": both_filled,
        "imbalance": not state.is_balanced(),
        "realized_payoff": realized,
        "matched_pairs": matched,
        "legs": legs,
        # BUG-2 repair: a realized number for EVERY window with any fill.
        # ``realized_cashflow`` is settlement-independent (complete for a closed/flattened position);
        # ``realized_at_close`` adds the sub-$1 flip guaranteed floor on matched pairs; ``unsettled_legs``
        # are the held legs still awaiting the settlement backfill.
        "any_fill": state.has_any_fill(),
        "realized_cashflow": state.realized_cashflow(),
        "realized_at_close": state.realized_at_close(),
        "unsettled_legs": [
            {"ticker": t, "side": s, "count": c} for (t, s, c) in state.unsettled_legs()
        ],
    }


def to_window_fills(state: LedgerState) -> Any:
    """Adapter: map the ledger fills record onto the Phase-2 ``parity.WindowFills`` / ``LegFill``.

    Read-only import of the Phase-2 types (composition, not modification). LegFill has no fee field
    today, so per-leg fee is dropped here (it rides in ``fills_record`` and always reaches S1 via the
    ledger). The LegFill tickers are the ACTUAL paired tickers, so parity._price_deltas always finds
    a comparable leg when the pair filled (the (a)-half of the F3 fix)."""
    from service.parity import LegFill, WindowFills  # read-only Phase-2 import

    rec = fills_record(state)
    legs = tuple(
        LegFill(
            ticker=lg["ticker"],
            side=lg["side"],
            count=int(state.net("high" if lg["ticker"] == state.high_ticker else "low")),
            avg_price=lg["avg_price"] if lg["avg_price"] is not None else Decimal(0),
        )
        for lg in rec["legs"]
    )
    return WindowFills(
        filled=bool(rec["filled"]),
        legs=legs,
        imbalance=bool(rec["imbalance"]),
        realized_payoff=rec["realized_payoff"],
    )


# ---------------------------------------------------------------------------
# Crash recovery: rebuild the ledger from journaled order_intent/order_response records.
# ---------------------------------------------------------------------------
def _leg_from_record(d: dict[str, Any]) -> IntentLeg:
    return IntentLeg(
        ticker=d["ticker"],
        side=d["side"],
        action=d["action"],
        count=int(d["count"]),
        limit_price=Decimal(str(d["limit_price"])),
        client_order_id=d["client_order_id"],
        reduce_only=d.get("reduce_only"),
        # Absent in pre-fix journals -> None (back-compat; rebuild never dispatches).
        exchange_index=d.get("exchange_index"),
    )


def intent_to_record(intent: Intent) -> dict[str, Any]:
    """Serialize an Intent to a journal-friendly dict (Decimals as strings)."""
    return {
        "window": intent.window,
        "source": intent.source,
        "purpose": intent.purpose,
        "t_minus_s": intent.t_minus_s,
        "legs": [
            {
                "ticker": leg.ticker,
                "side": leg.side,
                "action": leg.action,
                "count": leg.count,
                "limit_price": str(leg.limit_price),
                "client_order_id": leg.client_order_id,
                "reduce_only": leg.reduce_only,
                "exchange_index": leg.exchange_index,
            }
            for leg in intent.legs
        ],
    }


def intent_from_record(d: dict[str, Any]) -> Intent:
    return Intent(
        window=d["window"],
        source=d["source"],
        purpose=d["purpose"],
        legs=tuple(_leg_from_record(x) for x in d["legs"]),
        t_minus_s=d.get("t_minus_s"),
    )


def response_to_record(resp: OrderResponse) -> dict[str, Any]:
    return {
        "client_order_id": resp.client_order_id,
        "order_id": resp.order_id,
        "fill_count": str(resp.fill_count),
        "remaining_count": str(resp.remaining_count),
        # ``average_fill_price`` is SIDE-NORMALIZED (order's side-space); ``raw_reported_price`` keeps
        # the untouched venue value (YES-space for a NO order) so the journal preserves the raw truth
        # and re-derivation is exact. A record WITHOUT ``raw_reported_price`` is a pre-2026-08-26
        # (units-bug) journal whose ``average_fill_price`` is the raw venue value — rebuild_from_journal
        # normalizes those using the matching intent's side.
        "average_fill_price": None if resp.average_fill_price is None else str(resp.average_fill_price),
        "average_fee_paid": None if resp.average_fee_paid is None else str(resp.average_fee_paid),
        "raw_reported_price": None if resp.raw_reported_price is None else str(resp.raw_reported_price),
        "ts_ms": resp.ts_ms,
        "error": resp.error,
        "no_fill": resp.no_fill,
    }


def response_from_record(d: dict[str, Any]) -> OrderResponse:
    def _d(v: object) -> Decimal | None:
        return None if v is None else Decimal(str(v))

    return OrderResponse(
        client_order_id=d.get("client_order_id"),
        order_id=d.get("order_id"),
        fill_count=_d(d.get("fill_count")) or Decimal(0),
        remaining_count=_d(d.get("remaining_count")) or Decimal(0),
        average_fill_price=_d(d.get("average_fill_price")),
        average_fee_paid=_d(d.get("average_fee_paid")),
        ts_ms=d.get("ts_ms"),
        raw_reported_price=_d(d.get("raw_reported_price")),
        error=d.get("error"),
        no_fill=bool(d.get("no_fill", False)),
    )


def rebuild_from_journal(
    records: list[dict[str, Any]],
    window: str,
    source: str,
    high_ticker: str,
    low_ticker: str,
) -> LedgerState:
    """Replay journaled ``order_intent`` / ``order_response`` records into a LedgerState.

    ``records`` is the raw journal record list (each has ``kind`` and ``obj``). Only the two order
    record kinds move ledger state; everything else is ignored. Intents are applied before the
    responses that reference them, preserving journal order (intent-before-POST discipline)."""
    state = new_ledger(window, source, high_ticker, low_ticker)
    for rec in records:
        kind = rec.get("kind")
        obj = rec.get("obj")
        if kind == "order_intent":
            state = record_intent(state, intent_from_record(obj))
        elif kind == "order_response":
            resp = response_from_record(obj)
            # LEGACY MIGRATION: a record predating the units repair has no ``raw_reported_price`` KEY
            # at all, and its stored ``average_fill_price`` is the RAW venue value (YES-space for a NO
            # order). Normalize it now, using the side of the intent leg it was submitted for (already
            # recorded, since intents are journaled before their responses). New journals carry the key
            # (even as null) and are already side-normalized -> left untouched.
            if (
                "raw_reported_price" not in (obj or {})
                and resp.average_fill_price is not None
                and resp.client_order_id
            ):
                leg = state.cid_to_leg.get(resp.client_order_id)
                if leg is not None:
                    resp = normalize_fill_to_side(
                        replace(resp, raw_reported_price=resp.average_fill_price), leg.side
                    )
            state = record_response(state, resp)
    return state


__all__ = [
    "PURPOSE_ENTRY",
    "PURPOSE_REBALANCE_BUY",
    "PURPOSE_REBALANCE_SELL",
    "PURPOSE_FLATTEN",
    "IntentLeg",
    "Intent",
    "LegPosition",
    "LedgerState",
    "new_ledger",
    "record_intent",
    "record_response",
    "retries_for_side",
    "settlement_payoff",
    "fills_record",
    "to_window_fills",
    "rebuild_from_journal",
    "intent_to_record",
    "intent_from_record",
    "response_to_record",
    "response_from_record",
    "SUB_DOLLAR_FLIP",
    "Q1_STRANGLE",
]
