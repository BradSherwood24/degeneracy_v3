"""reconciler.py — the second thread's brain: PURE imbalance-protocol decision logic + a thin
positions/fills polling shell. The Reconciler NEVER posts; it hands intents to the Executor.

Two jobs (PLAN "Reconciler"):

(a) IMBALANCE PROTOCOL — ``propose_rebalance`` is a pure function of (ledger state, policy bounds,
    event-derived seconds-to-settle, current quotes). Brad's rulings (falsifier I1-I4):
      * retry-buy the deficient leg at the current ask, bounded by the PAIR-COST CEILING computed
        with ACTUAL fees paid so far (from responses) plus the projected retry cost — NEVER a buy
        that would push the pair above the ceiling ($1.0320/pair sub-$1; bucket-fair for strangle);
      * at most ``max_retries_per_side`` (5) retries per side;
      * no rebalance ORDER with < ``no_rebalance_after_s_to_settle`` (3 s) to settle -> sell-down
        only; with < ``no_orders_after_s_to_settle`` (1 s) -> no orders at all, the position rides
        to settlement and is reported;
      * else sell the overfilled side DOWN to match — target count rounds DOWN (min of the two leg
        counts); a partial sell-down re-evaluates on the next poll and iterates within the same
        bounds;
      * retries/sell-down exhausted or timed out -> GiveUp = STOP S2. End state is 1:1 or 0:0.

(b) POLLING SHELL — every N seconds GET /portfolio/positions (+ /portfolio/fills) via ``rest_get``
    and diff against the ledger-derived expected positions. ANY mismatch (a phantom fill, or a
    non-fill the ledger believes filled) = STOP S3. ``tick`` does one poll+diff; the loop cadence is
    the harness's concern. No live network in tests (``rest_get`` is injected).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from service._simlaw import fee
from service.ledger import (
    PURPOSE_REBALANCE_BUY,
    PURPOSE_REBALANCE_SELL,
    LedgerState,
    retries_for_side,
)

_ONE_PAIR = Decimal(1)


# ---------------------------------------------------------------------------
# Imbalance detection (pure)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Imbalance:
    high_net: Decimal
    low_net: Decimal
    matched: Decimal
    deficient: str  # "high" or "low" — the leg with the SMALLER net
    overfilled: str
    deficit: Decimal  # overfilled.net - deficient.net (> 0)


def detect_imbalance(state: LedgerState) -> Imbalance | None:
    """None iff the pair is already 1:1 / 0:0. Otherwise the deficient/overfilled split."""
    hi, lo = state.net("high"), state.net("low")
    if hi == lo:
        return None
    if hi < lo:
        deficient, overfilled, deficit = "high", "low", lo - hi
    else:
        deficient, overfilled, deficit = "low", "high", hi - lo
    return Imbalance(
        high_net=hi, low_net=lo, matched=min(hi, lo),
        deficient=deficient, overfilled=overfilled, deficit=deficit,
    )


# ---------------------------------------------------------------------------
# Quotes the shell resolves from the live book (held-outcome ask to buy / bid to sell)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RebalanceQuotes:
    """Held-outcome prices for each leg. ``*_buy`` = the ask to buy more of the held outcome;
    ``*_sell`` = the bid to sell the held outcome. None where the book side is empty."""

    high_buy: Decimal | None = None
    high_sell: Decimal | None = None
    low_buy: Decimal | None = None
    low_sell: Decimal | None = None

    def buy_price(self, which: str) -> Decimal | None:
        return self.high_buy if which == "high" else self.low_buy

    def sell_price(self, which: str) -> Decimal | None:
        return self.high_sell if which == "high" else self.low_sell


# ---------------------------------------------------------------------------
# Proposals (the Reconciler DECIDES; the harness turns these into Executor intents)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RetryBuy:
    which: str
    ticker: str
    side: str
    count: int
    limit_price: Decimal
    reason: str
    stop: None = None


@dataclass(frozen=True)
class SellDown:
    which: str
    ticker: str
    side: str
    count: int
    limit_price: Decimal
    reason: str
    stop: None = None


@dataclass(frozen=True)
class GiveUp:
    reason: str
    stop: str = "S2"


@dataclass(frozen=True)
class RideToSettlement:
    reason: str
    unresolved: bool = True  # if still imbalanced at window close -> S2 (I4), flagged by stops
    stop: None = None


@dataclass(frozen=True)
class Balanced:
    reason: str = "1:1 or 0:0"
    stop: None = None


Proposal = "RetryBuy | SellDown | GiveUp | RideToSettlement | Balanced"


def _leg_ticker_side(state: LedgerState, which: str) -> tuple[str, str]:
    if which == "high":
        return state.high_ticker, (state.high_side or "")
    return state.low_ticker, (state.low_side or "")


def propose_rebalance(
    state: LedgerState,
    params: Any,
    t_minus_s: float,
    quotes: RebalanceQuotes,
    ceiling_per_pair: Decimal,
) -> Any:
    """Pure imbalance decision. ``params`` is a PolicyParams (for ``imbalance`` bounds);
    ``ceiling_per_pair`` is the pair-cost ceiling for the fired source ($1.0320 sub-$1, or the
    bucket-fair EV>=0 total for the strangle). See module docstring for the full protocol."""
    imb = detect_imbalance(state)
    if imb is None:
        return Balanced()

    bounds = params.imbalance
    deficient, overfilled = imb.deficient, imb.overfilled
    def_ticker, def_side = _leg_ticker_side(state, deficient)
    over_ticker, over_side = _leg_ticker_side(state, overfilled)

    # I3: inside the no-orders cutoff, NO order at all — the position rides to settlement.
    if t_minus_s < bounds.no_orders_after_s_to_settle:
        return RideToSettlement(
            reason=(
                f"t-{t_minus_s:.3f}s < no_orders_after_s_to_settle "
                f"({bounds.no_orders_after_s_to_settle}s); imbalance {imb.high_net}:{imb.low_net} "
                f"rides to settlement (S2 at close if unresolved)"
            )
        )

    sell_only = t_minus_s < bounds.no_rebalance_after_s_to_settle
    cost_so_far = state.pair_net_cash_out()  # ACTUAL fills+fees on both legs

    # --- retry-buy branch (bounded by the ceiling with ACTUAL fees; never buy above it) ---
    if not sell_only:
        buy_retries = retries_for_side(state, def_ticker, PURPOSE_REBALANCE_BUY)
        buy_price = quotes.buy_price(deficient)
        if buy_retries < bounds.max_retries_per_side and buy_price is not None:
            projected = imb.deficit * (buy_price + fee(buy_price))
            target_pairs = imb.high_net if overfilled == "high" else imb.low_net
            ceiling_total = ceiling_per_pair * target_pairs
            if cost_so_far + projected <= ceiling_total:
                return RetryBuy(
                    which=deficient, ticker=def_ticker, side=def_side,
                    count=int(imb.deficit), limit_price=buy_price,
                    reason=(
                        f"retry-buy deficient {deficient} x{int(imb.deficit)} @ {buy_price}; "
                        f"pair cost so far {cost_so_far} + projected {projected} "
                        f"<= ceiling {ceiling_total} ({buy_retries + 1}/{bounds.max_retries_per_side})"
                    ),
                )
            # ceiling would break -> fall through to sell-down (NEVER a buy above the ceiling)

    # --- sell-down branch (target = min(nets), rounds DOWN) ---
    sell_retries = retries_for_side(state, over_ticker, PURPOSE_REBALANCE_SELL)
    sell_price = quotes.sell_price(overfilled)
    if sell_retries < bounds.max_retries_per_side and sell_price is not None:
        target = int(imb.matched)  # min of the two legs, integer floor (equal-at-lower)
        over_net = imb.high_net if overfilled == "high" else imb.low_net
        sell_count = int(over_net) - target
        if sell_count > 0:
            return SellDown(
                which=overfilled, ticker=over_ticker, side=over_side,
                count=sell_count, limit_price=sell_price,
                reason=(
                    f"sell-down overfilled {overfilled} x{sell_count} @ {sell_price} to match at "
                    f"{target} ({sell_retries + 1}/{bounds.max_retries_per_side})"
                ),
            )

    # nothing viable within the bounds -> GiveUp = STOP S2
    return GiveUp(
        reason=(
            f"imbalance {imb.high_net}:{imb.low_net} unrestorable within bounds "
            f"(sell_only={sell_only}, buy_retries={retries_for_side(state, def_ticker, PURPOSE_REBALANCE_BUY)}, "
            f"sell_retries={sell_retries}, buy_q={quotes.buy_price(deficient)}, "
            f"sell_q={quotes.sell_price(overfilled)}) -> S2"
        )
    )


# ---------------------------------------------------------------------------
# Positions polling shell (S3) — pure diff + a thin rest_get wrapper
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ReconcileResult:
    stop: str | None
    expected: dict[str, Decimal] = field(default_factory=dict)
    observed: dict[str, Decimal] = field(default_factory=dict)
    mismatches: tuple[str, ...] = ()


def expected_positions(state: LedgerState) -> dict[str, Decimal]:
    """Ledger-derived signed net position per ticker. Convention (live-verification item): a held
    YES outcome is +net, a held NO outcome is -net (Kalshi's `position` field is signed long-YES
    positive / long-NO negative). Zero-net legs are omitted."""
    out: dict[str, Decimal] = {}
    for which in ("high", "low"):
        pos = state.position(which)
        if pos is None:
            continue
        net = pos.net
        if net == 0:
            continue
        signed = net if pos.side == "yes" else -net
        out[pos.ticker] = out.get(pos.ticker, Decimal(0)) + signed
    return {k: v for k, v in out.items() if v != 0}


def parse_positions_response(body: Any) -> dict[str, Decimal]:
    """Parse a /portfolio/positions body into {ticker: signed_net}. Handles the ``market_positions``
    (and defensively ``positions``) list shapes. Non-numeric / missing positions fail closed to
    absent (a mismatch surfaces them)."""
    out: dict[str, Decimal] = {}
    if not isinstance(body, dict):
        return out
    rows = body.get("market_positions")
    if not isinstance(rows, list):
        rows = body.get("positions") if isinstance(body.get("positions"), list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticker = row.get("ticker")
        raw = row.get("position", row.get("net_position"))
        if not isinstance(ticker, str) or raw is None:
            continue
        try:
            val = Decimal(str(raw))
        except Exception:  # noqa: BLE001
            continue
        if val != 0:
            out[ticker] = val
    return out


def diff_positions(
    expected: dict[str, Decimal], observed: dict[str, Decimal]
) -> tuple[str, ...]:
    """Tickers whose expected != observed (either side present with a differing/absent value)."""
    tickers = set(expected) | set(observed)
    return tuple(
        sorted(t for t in tickers if expected.get(t, Decimal(0)) != observed.get(t, Decimal(0)))
    )


class PositionsReconciler:
    """Thin shell: poll positions via the injected ``rest_get`` and diff against the ledger."""

    def __init__(
        self,
        rest_get: Callable[..., Any],
        poll_seconds: float = 3.0,
    ) -> None:
        self._rest_get = rest_get
        self.poll_seconds = poll_seconds

    def poll_positions(self, params: dict | None = None) -> dict[str, Decimal]:
        body = self._rest_get("/portfolio/positions", params or {})
        return parse_positions_response(body)

    def poll_fills(self, params: dict | None = None) -> Any:
        """Corroboration only (journaled); the batch response is the primary fill truth."""
        return self._rest_get("/portfolio/fills", params or {})

    def tick(self, state: LedgerState, params: dict | None = None) -> ReconcileResult:
        """One poll+diff. ANY mismatch between exchange truth and the ledger => STOP S3."""
        observed = self.poll_positions(params)
        expected = expected_positions(state)
        mismatches = diff_positions(expected, observed)
        stop = "S3" if mismatches else None
        return ReconcileResult(stop=stop, expected=expected, observed=observed, mismatches=mismatches)
