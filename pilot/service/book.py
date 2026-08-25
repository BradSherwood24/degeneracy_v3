"""BookMirror — full-depth per-market order book from orderbook_snapshot + orderbook_delta.

Ported from degeneracy_v2 `signals/order_book.py` (its WS-derived-book semantics), ADAPTED for
the pilot:

  * ONE representation for money, and it is Decimal (house law: Decimal for money, no floats on
    the money path). PRICES are Decimal DOLLARS (e.g. Decimal("0.44"), Decimal("0.001") for the
    deci-cent 15-minute ladder); SIZES are Decimal CONTRACTS. Every incoming numeric is coerced
    via Decimal(str(x)) so the wire's float/string forms never leak float artifacts onto the book.
  * ONE market per BookMirror instance (multi-market keying lives in the WS client / harness,
    which holds a dict[ticker -> BookMirror]). The container itself is ticker-agnostic.
  * Full depth is retained (the pilot reads top-of-book, but 5-pair scaling and every future
    implementation read depth — recycling inventory: "lift + keep full depth").
  * FAIL CLOSED: a malformed delta or a seq-gap resnapshot marks the book SUSPECT until the next
    snapshot rebuilds it. `suspect` starts True (no snapshot seen yet is not a trustworthy book).

Two V2 mechanisms are deliberately NOT ported (see build report CONFESSIONS): the REST-authoritative
BBO cache (`rest_*`, `authoritative_*`, `freshest_*`, `wsreal_*`) — the pilot's book is WS-only, and
REST BBO reads (used at wake / for σ̂) go through the proxy REST path, not this mirror — and V2's
float-residue machinery (`_ws_real_ask`, residue_topped_count), which existed only to paper over
float delta-arithmetic; Decimal arithmetic is exact, so a level that returns to 0 is removed
exactly and no phantom residue accumulates.

Kalshi binary-market identity (unchanged from V2): a side's ASK is derived as `1 - opposite_side
best bid`, and the size resting at that ask is the opposite side's best-bid size. Crossed books are
reported faithfully, never hidden (V2 review #7).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

# The two orderbook sides as they appear on the Kalshi wire.
_YES = "yes"
_NO = "no"
ONE = Decimal(1)


def _to_decimal(value: object) -> Decimal | None:
    """Coerce a wire numeric (float/int/str) to Decimal, or None if it cannot be parsed.

    Uses str() first so a float like 0.44 becomes Decimal("0.44"), not the 0.44000000000000006
    binary expansion — the book must never carry float artifacts (house law: Decimal for money).

    NON-FINITE values (NaN, +/-Infinity) are rejected as unparseable (-> None). `Decimal(str(x))`
    would otherwise HAPPILY build Decimal('NaN')/Decimal('Infinity') from a wire "nan"/"inf" or a
    float nan/inf, which is doubly bad: (a) it never trips the fail-closed suspect path (a NaN/Inf
    level would sit in the book unflagged and can top best_bid, yielding a plausible-looking garbage
    top that could fire an order), and (b) a NaN delta makes `new_size <= 0` raise
    decimal.InvalidOperation, which — unguarded in replay.replay_records — aborts the whole window's
    golden replay / paired report. Fail closed to None instead (house law: Decimal for money)."""
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except Exception:  # noqa: BLE001 - any unparseable value is fail-closed to None
        return None
    return d if d.is_finite() else None


@dataclass(frozen=True)
class Level:
    """A single price level: price (Decimal dollars) and size (Decimal contracts)."""

    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class TopOfBook:
    """Immutable top-of-book snapshot — the golden-replay comparison unit (dataclass equality).

    Every field is a Decimal or None; `suspect` marks a book that has seen a malformed delta or a
    seq-gap resnapshot and has not yet been rebuilt from a fresh snapshot (fail-closed: callers must
    treat a suspect top as untrustworthy)."""

    yes_bid: Decimal | None
    yes_bid_size: Decimal | None
    yes_ask: Decimal | None
    yes_ask_size: Decimal | None
    no_bid: Decimal | None
    no_bid_size: Decimal | None
    no_ask: Decimal | None
    no_ask_size: Decimal | None
    suspect: bool


class BookMirror:
    """Full-depth WS-derived order book for ONE market. Deterministic given its input events.

    All mutation is via `apply_snapshot` / `apply_delta` / `mark_suspect`; all reads are pure.
    """

    def __init__(self) -> None:
        # Full-depth bid books: price(Decimal) -> size(Decimal). Asks are derived (binary identity).
        self.yes_bids: dict[Decimal, Decimal] = {}
        self.no_bids: dict[Decimal, Decimal] = {}
        # Fail-closed until the first snapshot: an un-snapshotted book is not trustworthy.
        self.suspect: bool = True
        # Observability: count of malformed deltas seen since construction (never resets).
        self.malformed_delta_count: int = 0

    # === mutation ===

    def apply_snapshot(self, msg: dict) -> None:
        """Replace the book from an `orderbook_snapshot` message and clear the suspect flag.

        Keeps only levels with strictly positive size (Kalshi snapshots can carry zero-count
        placeholder levels that would otherwise pollute best-bid). A snapshot is the authoritative
        rebuild point: it always clears `suspect`, healing a book that a gap/malformed delta soured.
        """
        self.yes_bids = self._parse_levels(msg.get("yes_dollars_fp"))
        self.no_bids = self._parse_levels(msg.get("no_dollars_fp"))
        self.suspect = False

    @staticmethod
    def _parse_levels(raw: object) -> dict[Decimal, Decimal]:
        book: dict[Decimal, Decimal] = {}
        if not isinstance(raw, Iterable):
            return book
        for pair in raw:
            try:
                price_raw, size_raw = pair
            except (TypeError, ValueError):
                continue
            price = _to_decimal(price_raw)
            size = _to_decimal(size_raw)
            if price is None or size is None:
                continue
            if size > 0:
                book[price] = size
        return book

    def apply_delta(self, msg: dict) -> None:
        """Apply one `orderbook_delta` (single price-level update).

        FAIL CLOSED: an unrecognized side, an unparseable price, or an unparseable delta marks the
        book SUSPECT (and is counted) rather than being silently dropped as V2 did — a book we
        cannot apply an update to cleanly is not a book we can trust until the next snapshot.
        """
        side = msg.get("side")
        price = _to_decimal(msg.get("price_dollars"))
        delta = _to_decimal(msg.get("delta_fp"))
        if side == _YES:
            book = self.yes_bids
        elif side == _NO:
            book = self.no_bids
        else:
            self._mark_malformed()
            return
        if price is None or delta is None:
            self._mark_malformed()
            return
        new_size = book.get(price, Decimal(0)) + delta
        if new_size <= 0:
            book.pop(price, None)
        else:
            book[price] = new_size

    def _mark_malformed(self) -> None:
        self.malformed_delta_count += 1
        self.suspect = True

    def mark_suspect(self) -> None:
        """Invalidate the book (seq-gap / reconnect): reads stay available but flagged suspect
        until the next snapshot rebuilds it. Does NOT clear the levels — a reconnect's fresh
        snapshot will replace them wholesale via `apply_snapshot`."""
        self.suspect = True

    # === pure reads ===

    def _book(self, side: str) -> dict[Decimal, Decimal] | None:
        if side == _YES:
            return self.yes_bids
        if side == _NO:
            return self.no_bids
        return None

    def best_bid(self, side: str) -> Level | None:
        """Best (highest-price) bid on `side`, counting only positive-size levels."""
        book = self._book(side)
        if not book:
            return None
        positive = [(p, s) for p, s in book.items() if s > 0]
        if not positive:
            return None
        price, size = max(positive, key=lambda ps: ps[0])
        return Level(price, size)

    def best_yes_ask(self) -> Level | None:
        """YES ask via Kalshi binary identity: price = 1 - best NO bid, size = that NO bid's size."""
        return self._derived_ask(_NO)

    def best_no_ask(self) -> Level | None:
        """NO ask via Kalshi binary identity: price = 1 - best YES bid, size = that YES bid's size."""
        return self._derived_ask(_YES)

    def _derived_ask(self, opposite_side: str) -> Level | None:
        opp = self.best_bid(opposite_side)
        if opp is None:
            return None
        return Level(ONE - opp.price, opp.size)

    def depth_at(self, side: str, price: Decimal) -> Decimal | None:
        """Contracts resting at exactly `price` on the given BID `side` (None if no such level).

        `side` is a bid side ("yes"/"no"); this reports the raw WS-book depth, not a derived ask.
        Accepts any numeric price (coerced to Decimal) so callers need not pre-quantize.
        """
        book = self._book(side)
        if book is None:
            return None
        key = _to_decimal(price)
        if key is None:
            return None
        return book.get(key)

    def top_of_book(self) -> TopOfBook:
        """Snapshot the four best quotes (both bids + both derived asks) and the suspect flag."""
        yb = self.best_bid(_YES)
        nb = self.best_bid(_NO)
        ya = self.best_yes_ask()
        na = self.best_no_ask()
        return TopOfBook(
            yes_bid=yb.price if yb else None,
            yes_bid_size=yb.size if yb else None,
            yes_ask=ya.price if ya else None,
            yes_ask_size=ya.size if ya else None,
            no_bid=nb.price if nb else None,
            no_bid_size=nb.size if nb else None,
            no_ask=na.price if na else None,
            no_ask_size=na.size if na else None,
            suspect=self.suspect,
        )
