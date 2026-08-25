"""Golden replay — deterministic book reconstruction from a journal's raw WS records.

The recorder tap journals every non-control WS envelope BEFORE it is dispatched to the live book,
so the journal contains exactly what the live BookMirror saw. Replay feeds those same envelopes
through a FRESH BookMirror per market and yields the resulting top-of-book after each book-affecting
frame. Because BookMirror is deterministic and the journal preserves order (via `idx`), replaying
the same journal twice yields a byte-identical sequence — the determinism property Phase 1's golden
test pins.

Parity with live: a frame the live client fail-closed (missing `market_ticker`) is skipped here too,
so the reconstruction matches the live dispatch decision exactly.

Seq-gap note (documented limitation): sid/seq are consulted live for gap detection but are NOT
journaled, so replay cannot re-derive the seq-gap-driven `suspect` flag or the reconnect. It does
not need to: a live seq gap forces a reconnect whose FRESH orderbook_snapshot IS journaled, and
replaying that snapshot rebuilds the book wholesale (apply_snapshot clears suspect and replaces all
levels) — so the post-gap state is reproduced exactly. The `suspect` flag in a replayed top reflects
malformed deltas (which ARE journaled and replay-visible), not reconnects.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from service.book import BookMirror, TopOfBook

WS_KIND = "kalshi_ws"
_BOOK_TYPES = ("orderbook_snapshot", "orderbook_delta")


def replay_records(records: Any) -> Iterator[tuple[int, str, TopOfBook]]:
    """Core reconstruction over an iterable of journal records (each a dict with idx/kind/obj).

    Yields (local_index, market_ticker, top_of_book) after every applied orderbook frame, in idx
    order. Deterministic and side-effect free (a fresh set of BookMirrors per call)."""
    books: dict[str, BookMirror] = {}
    for rec in records:
        if rec.get("kind") != WS_KIND:
            continue
        env = rec.get("obj") or {}
        msg_type = env.get("type")
        if msg_type not in _BOOK_TYPES:
            continue
        payload = env.get("msg") or {}
        market = payload.get("market_ticker") if isinstance(payload, dict) else None
        if not market:
            continue  # live fail-closed the unattributed frame; replay matches that decision
        book = books.get(market)
        if book is None:
            book = BookMirror()
            books[market] = book
        if msg_type == "orderbook_snapshot":
            book.apply_snapshot(payload)
        else:
            book.apply_delta(payload)
        yield rec["idx"], market, book.top_of_book()


def replay_books(journal: Any) -> Iterator[tuple[int, str, TopOfBook]]:
    """Replay a Journal (anything exposing `iter_records()`) into (idx, market, top_of_book)."""
    return replay_records(journal.iter_records())
