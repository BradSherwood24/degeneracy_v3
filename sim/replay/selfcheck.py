"""SELFCHECK: reconcile trade prints against orderbook deltas, and report receipt lag.

Reconciliation law (from the task spec, Kalshi book semantics):
  * a ``trade`` with taker_side "yes" at yes_price p  <->  a NEGATIVE delta on side "no"
    at price (1 - p)   (taker BOUGHT YES == hit NO bids)
  * a ``trade`` with taker_side "no"  at yes_price p  <->  a NEGATIVE delta on side "yes"
    at price p          (taker SOLD YES == hit YES bids)
Coincidence is required at the SAME ts_ms and SAME ticker.

The ``Reconciler`` consumes ws ``obj``s one at a time (so a single journal pass can feed both
the reconciler and the backtest). It is memory-bounded: negative-delta signatures and pending
trades are held only within a sliding ``MATCH_WINDOW_MS`` of engine time (> the observed max
ts_ms backstep of ~1024 ms) and evicted/finalized as engine time advances.

Receipt lag = local_ts*1000 - ts_ms (includes receipt-clock drift; reported for information
only, never used for ordering).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .book import dollars_to_mils

MATCH_WINDOW_MS = 3000


def _pct(sorted_vals, q):
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


@dataclass
class ReconStats:
    ticker_scope: tuple
    n_trades: int = 0
    n_matched: int = 0
    n_deltas: int = 0
    n_neg_deltas: int = 0
    lags_ms: list = field(default_factory=list)

    @property
    def rate(self):
        return (self.n_matched / self.n_trades) if self.n_trades else None

    def lag_p50(self):
        s = sorted(self.lags_ms)
        return _pct(s, 0.50)

    def lag_p99(self):
        s = sorted(self.lags_ms)
        return _pct(s, 0.99)


class Reconciler:
    """Streaming trade<->delta reconciler for ONE window. Feed every ws ``obj`` via ``feed``;
    call ``finalize`` once at end; read ``stats``."""

    def __init__(self, tickers=None) -> None:
        # tickers: restrict reconciliation to these (the 2 legs). None => all tickers seen.
        self._scope = set(tickers) if tickers else None
        self.stats = ReconStats(ticker_scope=tuple(sorted(self._scope)) if self._scope else ())
        self._neg: dict[int, set] = {}          # ts_ms -> set((ticker, side, price_mils))
        self._pending: dict[int, list] = {}     # ts_ms -> list((ticker, side, price_mils))
        self._max_ts = -1

    def feed(self, obj: dict) -> None:
        t = obj.get("type")
        msg = obj.get("msg") or {}
        tkr = msg.get("market_ticker")
        if self._scope is not None and tkr not in self._scope:
            return
        ts = msg.get("ts_ms")
        lts = obj.get("_local_ts")
        if ts is not None and lts is not None:
            self.stats.lags_ms.append(lts * 1000.0 - ts)

        if t == "orderbook_delta":
            self.stats.n_deltas += 1
            if ts is None:
                return
            try:
                delta = float(msg.get("delta_fp"))
            except (TypeError, ValueError):
                return
            if delta < 0:
                self.stats.n_neg_deltas += 1
                key = (tkr, msg.get("side"), dollars_to_mils(msg.get("price_dollars")))
                self._neg.setdefault(ts, set()).add(key)
                if ts > self._max_ts:
                    self._max_ts = ts
                self._evict()
        elif t == "trade":
            self.stats.n_trades += 1
            if ts is None:
                return
            taker = msg.get("taker_side")
            if taker == "yes":
                # bought YES -> hit NO bids at (1 - p) == no_price_dollars
                exp = (tkr, "no", dollars_to_mils(msg.get("no_price_dollars")))
            elif taker == "no":
                # sold YES -> hit YES bids at yes_price p
                exp = (tkr, "yes", dollars_to_mils(msg.get("yes_price_dollars")))
            else:
                return
            self._pending.setdefault(ts, []).append(exp)
            if ts > self._max_ts:
                self._max_ts = ts
            self._evict()

    def _evict(self) -> None:
        horizon = self._max_ts - MATCH_WINDOW_MS
        old = [ts for ts in self._pending if ts < horizon]
        for ts in old:
            self._resolve_ts(ts)
        # drop stale negative-delta buckets that can no longer match a future trade
        stale = [ts for ts in self._neg if ts < horizon]
        for ts in stale:
            del self._neg[ts]

    def _resolve_ts(self, ts: int) -> None:
        neg = self._neg.get(ts, set())
        for exp in self._pending.pop(ts, []):
            if exp in neg:
                self.stats.n_matched += 1

    def finalize(self) -> ReconStats:
        for ts in list(self._pending.keys()):
            self._resolve_ts(ts)
        self._neg.clear()
        return self.stats
