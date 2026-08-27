"""Integer-folded L2 order book (one market = ``IntBook``; a pair = ``WindowBook``).

Units (house law "integer arithmetic is exact"):
  * PRICE  -> MILS      = tenths of a cent = round(dollars * 1000).  0.4700 -> 470, 0.9920 -> 992.
  * SIZE   -> HUNDREDTHS = round(contracts * 100).  "0.01" contracts -> 1, "128.84" -> 12884.

Kalshi binary-market book semantics (identical to ``pilot/service/book.py`` BookMirror, which
is the authoritative Decimal implementation this integer book is cross-checked against):
  * ``yes_dollars_fp`` = resting YES BIDS ; ``no_dollars_fp`` = resting NO BIDS.
  * YES ask = 1 - best NO bid  (size = that NO bid's size).
  * NO  ask = 1 - best YES bid (size = that YES bid's size).

Dust: any level whose magnitude is < 0.005 contracts (< 0.5 hundredths) is treated as zero.
With integer hundredths a swept level is exactly 0, so no 1e-12 residue can resurrect it.
"""

from __future__ import annotations

from collections import namedtuple
from decimal import Decimal

MIL = 1000          # dollars -> mils
HUN = 100           # contracts -> hundredths
DUST_HUNDREDTHS = 0.5   # < 0.005 contracts == dust == zero

_YES = "yes"
_NO = "no"

TopOfBook = namedtuple(
    "TopOfBook",
    "yes_bid yes_bid_sz yes_ask yes_ask_sz no_bid no_bid_sz no_ask no_ask_sz ready",
)


def dollars_to_mils(x) -> int:
    """Dollars (str/float/Decimal) -> integer mils. Prices carry <=3 decimals; exact."""
    return int(round(float(x) * MIL))


def mils_to_dollars(m: int) -> Decimal:
    """Integer mils -> Decimal dollars (money-path exact; used for the fee call)."""
    return (Decimal(int(m)) / Decimal(MIL))


def _size_hundredths(x) -> int:
    return int(round(float(x) * HUN))


class IntBook:
    """Full-depth integer book for ONE market. Deterministic given its event stream."""

    __slots__ = ("yes_bids", "no_bids", "snapshotted")

    def __init__(self) -> None:
        self.yes_bids: dict[int, int] = {}   # mils -> hundredths
        self.no_bids: dict[int, int] = {}
        self.snapshotted = False

    # --- mutation ---
    def apply_snapshot(self, msg: dict) -> None:
        self.yes_bids = self._parse_levels(msg.get("yes_dollars_fp"))
        self.no_bids = self._parse_levels(msg.get("no_dollars_fp"))
        self.snapshotted = True

    @staticmethod
    def _parse_levels(raw) -> dict[int, int]:
        book: dict[int, int] = {}
        if not raw:
            return book
        for pair in raw:
            try:
                price_raw, size_raw = pair
            except (TypeError, ValueError):
                continue
            sz = _size_hundredths(size_raw)
            if abs(sz) >= DUST_HUNDREDTHS and sz > 0:
                book[dollars_to_mils(price_raw)] = sz
        return book

    def apply_delta(self, msg: dict) -> None:
        side = msg.get("side")
        if side == _YES:
            book = self.yes_bids
        elif side == _NO:
            book = self.no_bids
        else:
            return
        price = dollars_to_mils(msg.get("price_dollars"))
        new = book.get(price, 0) + _size_hundredths(msg.get("delta_fp"))
        if abs(new) < DUST_HUNDREDTHS or new <= 0:
            book.pop(price, None)
        else:
            book[price] = new

    # --- pure reads (mils / hundredths) ---
    def best_yes_bid(self):
        return self._best(self.yes_bids)

    def best_no_bid(self):
        return self._best(self.no_bids)

    @staticmethod
    def _best(book: dict[int, int]):
        best_p = -1
        best_s = 0
        for p, s in book.items():
            if s >= DUST_HUNDREDTHS and p > best_p:
                best_p, best_s = p, s
        return (best_p, best_s) if best_p >= 0 else None

    def yes_ask(self):
        """(mils, hundredths) YES ask = 1 - best NO bid; None if no NO bid."""
        nb = self.best_no_bid()
        return (MIL - nb[0], nb[1]) if nb else None

    def no_ask(self):
        """(mils, hundredths) NO ask = 1 - best YES bid; None if no YES bid."""
        yb = self.best_yes_bid()
        return (MIL - yb[0], yb[1]) if yb else None

    def depth_at(self, side: str, price_mils: int) -> int:
        """Contracts-hundredths resting at exactly ``price_mils`` on BID ``side`` (0 if none)."""
        book = self.yes_bids if side == _YES else self.no_bids if side == _NO else None
        if book is None:
            return 0
        s = book.get(int(price_mils), 0)
        return s if abs(s) >= DUST_HUNDREDTHS else 0

    def top_of_book(self) -> TopOfBook:
        yb, nb = self.best_yes_bid(), self.best_no_bid()
        ya, na = self.yes_ask(), self.no_ask()
        return TopOfBook(
            yes_bid=yb[0] if yb else None, yes_bid_sz=yb[1] if yb else None,
            yes_ask=ya[0] if ya else None, yes_ask_sz=ya[1] if ya else None,
            no_bid=nb[0] if nb else None, no_bid_sz=nb[1] if nb else None,
            no_ask=na[0] if na else None, no_ask_sz=na[1] if na else None,
            ready=self.snapshotted,
        )


class WindowBook:
    """A pair of ``IntBook``s keyed by ticker, fed the window's ws ``obj`` stream.

    ``feed(obj)`` applies snapshot/delta for known tickers (ignores others). Reads route by
    ticker. This is the reusable engine; ``maker_flip`` builds a compact per-event timeline on
    top of it for arbitrary-engine-time queries.
    """

    def __init__(self, tickers) -> None:
        self.books: dict[str, IntBook] = {t: IntBook() for t in tickers}

    def feed(self, obj: dict) -> str | None:
        """Apply one ws ``obj``. Returns the ticker touched (book-mutating) or None."""
        t = obj.get("type")
        msg = obj.get("msg") or {}
        tkr = msg.get("market_ticker")
        book = self.books.get(tkr)
        if book is None:
            return None
        if t == "orderbook_snapshot":
            book.apply_snapshot(msg)
            return tkr
        if t == "orderbook_delta":
            book.apply_delta(msg)
            return tkr
        return None

    def top_of_book(self, ticker: str) -> TopOfBook | None:
        b = self.books.get(ticker)
        return b.top_of_book() if b else None

    def size_at(self, ticker: str, side: str, price) -> int:
        b = self.books.get(ticker)
        if b is None:
            return 0
        return b.depth_at(side, dollars_to_mils(price) if not isinstance(price, int) else price)

    def ready(self) -> bool:
        """True once every leg has seen a snapshot (both legs' books trustworthy)."""
        return all(b.snapshotted for b in self.books.values())
