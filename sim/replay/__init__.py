"""Engine-time L2 orderbook REPLAYER over the pilot's WS journals.

This package streams a pilot WS journal (``pilot/journals/2026*.jsonl``), folds BOTH
legs of a corridor pair in ENGINE TIME (``ts_ms``, the Kalshi matching-engine clock —
verified equal to our own private-fill ts to the ms), and exposes top-of-book / depth
reads plus a query-at-arbitrary-engine-time helper. On top of it, ``maker_flip`` runs
the sub-$1 FLIP maker backtest.

House law honored here:
  * The fee is IMPORTED from ``sim/census.py`` (``fee``), never retyped; the maker fee is
    ``maker_mult * fee(price)``.
  * ``WINDOW_S`` and ``STALENESS_S_DEFAULT`` are imported from ``sim/tape_sim.py``.
  * Nothing in this package reads ``historical-data/`` or anything under
    ``sim/out/sealed_eval/``. It reads only ``pilot/journals/`` (WS receipts) and writes
    only under ``sim/out/replay/`` (gitignored).

Integer folding: prices are carried as MILS (tenths of a cent, ``round(price*1000)``) and
sizes as HUNDREDTHS of a contract (``round(size*100)``). A prior float book resurrected
swept levels via 1e-12 dust; integer arithmetic is exact and a level that returns to zero
is removed exactly. Any level whose magnitude is < 0.005 contracts (< 0.5 hundredths) is
treated as zero.
"""

from .book import IntBook, WindowBook, mils_to_dollars, dollars_to_mils, DUST_HUNDREDTHS
from .journal import iter_ws, read_window_header, WindowHeader, resolve_close_epoch

__all__ = [
    "IntBook",
    "WindowBook",
    "mils_to_dollars",
    "dollars_to_mils",
    "DUST_HUNDREDTHS",
    "iter_ws",
    "read_window_header",
    "WindowHeader",
    "resolve_close_epoch",
]
