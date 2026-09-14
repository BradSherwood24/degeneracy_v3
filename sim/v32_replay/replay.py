"""replay.py — the single-pass streaming engine for one V3.2 window journal.

ONE pass over the raw frames (memory-light: one record at a time; 15M books are counted, never folded).
It reconstructs per-strike / per-bucket ``BookMirror`` tops, drives every pricing model (ideal shadow,
lagging MS, OLD minute-candle), resolves each fill's completion as-of its target strike tops, and
collects the per-window book metrics and the SIM-vs-MS comparison.

Determinism: frames are consumed in journal order (= live arrival order), timestamps come only from the
frames, so the whole replay is reproducible from the file alone.
"""

from __future__ import annotations

import statistics
from collections import defaultdict, deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

from service.book import BookMirror, TopOfBook
from service.v32.events import parse_strike_ticker

from .frames import FRAME_DELTA, FRAME_SNAPSHOT, FRAME_TRADE, RawFrame, WindowMeta
from .models import (
    BASE_CELL, GRID_DEB, GRID_E, GRID_TOL, LAT_MS, Fill, IdealModel, LaggingModel, OldModel,
    maker_fill_decision,
)
from .pricing import bucket_cap, compute_W, lock_value, select_spot, solve_n, wing_cost

_ONE = Decimal(1)
_TWO = Decimal(2)
_CENT = Decimal("0.01")
_BASE_E = Decimal("0.10")
_HISTORY_RETAIN_MS = 12_000     # keep ~12 s of strike top history for the +1.5 s completion lookback


def _median(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return s[min(n - 1, max(0, int(round(q * (n - 1)))))]


@dataclass
class WingProbe:
    trade_ts: float
    target_ts: float
    Sd: int
    Su: int
    W_now: Decimal | None


@dataclass
class WindowResult:
    close_time: str
    resolved_mode: str
    effective_mode: str
    params_sha: str | None
    armed: bool
    # frame accounting
    total_frames: int = 0
    driving_frames: int = 0
    m15_frames: int = 0
    strike_frames: int = 0
    bucket_frames: int = 0
    bucket_trades: int = 0
    spot_bucket_yes_trades: int = 0
    self_flagged_fills: int = 0
    # metrics
    metrics: dict = field(default_factory=dict)
    # fills, keyed by model label
    ideal_fills: list[Fill] = field(default_factory=list)
    lag_fills: dict = field(default_factory=dict)     # (E,tol,deb) -> Fill|None
    lag_replaces: dict = field(default_factory=dict)  # (E,tol,deb) -> int
    old_fill: Fill | None = None
    old_replaces: int = 0
    base_fill: Fill | None = None
    # sim-vs-ms comparison
    comparison: dict = field(default_factory=dict)
    wing_drift_cents: list[float] = field(default_factory=list)
    # calibration raw samples (pooled by calibration.aggregate across windows)
    calib: dict = field(default_factory=dict)


class WindowEngine:
    def __init__(self, meta: WindowMeta, params, width: int = 100) -> None:
        self.meta = meta
        self.params = params
        self.width = width
        self.close_epoch = meta.close_epoch
        self.quote_start_s = getattr(params, "quote_start_s", 900)
        self.quote_end_s = getattr(params, "quote_end_s", 300)
        # bucket floor lookup + 15M ticker set
        self._bucket_floor: dict[str, int] = {
            tk: int(round(fl)) for tk, (fl, _cap) in meta.bucket_map.items()
        }
        self._m15 = set(meta.m15_tickers)
        # books
        self.books: dict[str, BookMirror] = {}
        self.strike_top: dict[int, TopOfBook] = {}
        self.bucket_top: dict[int, TopOfBook] = {}
        # strike top history for completion lookback: floor -> deque[(ts_ms, yes_ask, yes_bid)]
        self.strike_hist: dict[int, deque] = defaultdict(deque)
        # clock
        self.now: float = -1e18
        # models
        self.ideals = [IdealModel(E) for E in GRID_E]
        self.lags: dict[tuple, LaggingModel] = {}
        for E in GRID_E:
            for tol in GRID_TOL:
                for deb in GRID_DEB:
                    self.lags[(E, tol, deb)] = LaggingModel(E, tol, deb, LAT_MS)
        self.old = OldModel(BASE_CELL[0], BASE_CELL[1], BASE_CELL[2], LAT_MS)
        self._pending: list[Fill] = []
        self._probes: list[WingProbe] = []
        # OLD minute-sample state
        self._old_spot_Sd: int | None = None
        self._old_cap: Decimal | None = None
        self._last_minute: int | None = None
        # metrics accumulators
        self._bucket_last_ts: dict[int, float] = {}
        self._bucket_gaps: dict[int, list[float]] = defaultdict(list)
        self._bucket_tick_ct: dict[int, int] = defaultdict(int)
        self._strike_gaps: list[float] = []
        self._strike_last_ts: dict[int, float] = {}
        self._spot_tick_ct: dict[int, int] = defaultdict(int)
        self._spot_spread_c: list[float] = []
        self._spot_top_depth: list[float] = []
        self._spot_yes_ask_size: list[float] = []
        self._spot_no_bid_size: list[float] = []
        self._spot_last_ts: dict[int, float] = {}
        self._spot_gaps: dict[int, list[float]] = defaultdict(list)
        # comparison accumulators (sampled at minute boundaries)
        self._cmp_spot_total = 0
        self._cmp_spot_differ = 0
        self._cmp_cap_diff_c: list[float] = []
        # frame counters + drift buffer
        self.result_total = 0
        self.driving = 0
        self._m15_ct = 0
        self.strike_frames = 0
        self.bucket_frames = 0
        self.bucket_trades = 0
        self.spot_bucket_yes_trades = 0
        self._wing_drift: list[float] = []          # B residual W(+1.5)-W(trade), cents
        # ---- calibration accumulators (per bucket trade in-window) ----
        self._n_shadow_10: Decimal | None = None    # the E=0.10 ms-continuous n (the sim's fill rule)
        self._minute_spot: int | None = None        # candle-proxy spot at the last minute boundary
        self._minute_bucket_yesbid: dict[int, Decimal] = {}   # per-bucket yes_bid at last boundary
        self._calib_trades = 0
        self._calib_spot_agree = 0
        self._calib_cap_err: list[float] = []       # (cap_ms - cap_candle) cents
        self._calib_eval = 0                        # spot-yes trades with a defined n_shadow
        self._calib_regime = {"i": 0, "ii": 0, "iii": 0}
        self._calib_maker_fills = 0                 # fills under the spread-aware maker rule
        self._calib_strict_fills = 0                # fills under the sim's strict rule (p > offer)
        self._calib_print_sizes: list[float] = []   # print sizes of strict-qualifying prints
        self._calib_fill_by_window = (0, 0, 0)      # (maker_fills, strict_fills, eval) this window

    # ---- window predicate ----
    def _in_window(self, t: float) -> bool:
        ttc = self.close_epoch - t
        return self.quote_end_s <= ttc <= self.quote_start_s

    # ---- book helpers ----
    def _book(self, market: str) -> BookMirror:
        b = self.books.get(market)
        if b is None:
            b = BookMirror()
            self.books[market] = b
        return b

    def _classify(self, market: str):
        fl = self._bucket_floor.get(market)
        if fl is not None:
            return ("bucket", fl)
        if market in self._m15 or market.startswith("KXBTC15M"):
            return ("m15", None)
        k = parse_strike_ticker(market)
        if k is not None:
            return ("strike", k)
        return (None, None)

    # ---- ingest one frame ----
    def feed(self, fr: RawFrame) -> None:
        self.result_total += 1
        kind, floor = self._classify(fr.market)
        if kind == "m15":
            self._m15_ct += 1
            return
        if kind is None:
            return
        if fr.server_ts is not None and fr.server_ts > self.now:
            self.now = fr.server_ts
            self._drain_pending()
        if fr.kind in (FRAME_SNAPSHOT, FRAME_DELTA):
            book = self._book(fr.market)
            if fr.kind == FRAME_SNAPSHOT:
                book.apply_snapshot(fr.msg)
            else:
                book.apply_delta(fr.msg)
            top = book.top_of_book()
            if kind == "strike":
                self.strike_top[floor] = top
            else:
                self.bucket_top[floor] = top
            # snapshots carry no server_ts -> fold the book but never drive (live-recorder parity)
            if fr.server_ts is None:
                return
            self._on_book_tick(kind, floor, fr.server_ts, top)
        elif fr.kind == FRAME_TRADE:
            if kind != "bucket" or fr.server_ts is None:
                return
            self._on_trade(floor, fr)

    # ---- book tick ----
    def _on_book_tick(self, kind: str, floor: int, t: float, top: TopOfBook) -> None:
        # strike history (for completion lookback), regardless of window
        if kind == "strike":
            h = self.strike_hist[floor]
            h.append((t, top.yes_ask, top.yes_bid))
            cutoff = t - _HISTORY_RETAIN_MS / 1000.0
            while h and h[0][0] < cutoff:
                h.popleft()
        if not self._in_window(t):
            return
        self.driving += 1
        # metrics: per-market tick gaps
        if kind == "bucket":
            self.bucket_frames += 1
            last = self._bucket_last_ts.get(floor)
            if last is not None:
                self._bucket_gaps[floor].append((t - last) * 1000.0)
            self._bucket_last_ts[floor] = t
            self._bucket_tick_ct[floor] += 1
        else:
            self.strike_frames += 1
            last = self._strike_last_ts.get(floor)
            if last is not None:
                self._strike_gaps.append((t - last) * 1000.0)
            self._strike_last_ts[floor] = t

        # ---- MS-continuous context ----
        spot_Sd = select_spot(self.bucket_top)
        spot_Su = spot_Sd + self.width if spot_Sd is not None else None
        W = compute_W(self.strike_top, spot_Sd, spot_Su) if spot_Sd is not None else None
        cap = bucket_cap(self.bucket_top, spot_Sd) if spot_Sd is not None else None

        # spot-bucket metric samples
        if spot_Sd is not None:
            self._spot_tick_ct[spot_Sd] += 1
            btop = self.bucket_top.get(spot_Sd)
            if btop is not None and btop.yes_bid is not None and btop.yes_ask is not None:
                self._spot_spread_c.append(float((btop.yes_ask - btop.yes_bid) * 100))
                sizes = [s for s in (btop.yes_bid_size, btop.yes_ask_size) if s is not None]
                if sizes:
                    self._spot_top_depth.append(float(min(sizes)))
                if btop.yes_ask_size is not None:
                    self._spot_yes_ask_size.append(float(btop.yes_ask_size))
                if btop.no_bid_size is not None:
                    self._spot_no_bid_size.append(float(btop.no_bid_size))
            last = self._spot_last_ts.get(spot_Sd)
            if last is not None:
                self._spot_gaps[spot_Sd].append((t - last) * 1000.0)
            self._spot_last_ts[spot_Sd] = t

        # solve desired n ONCE per E (all TOL/DEB cells with that E share it), then drive the models.
        if spot_Sd is not None and W is not None and cap is not None:
            nd_by_E = {E: solve_n(_TWO - E - W, cap) for E in GRID_E}
        else:
            nd_by_E = {E: None for E in GRID_E}
        self._n_shadow_10 = nd_by_E.get(_BASE_E)
        for m in self.ideals:
            m.on_tick(t, spot_Sd, spot_Su, nd_by_E[m.E])
        for m in self.lags.values():
            m.on_tick(t, spot_Sd, spot_Su, nd_by_E[m.E])

        # ---- OLD minute-candle sampling ----
        minute = int(t // 60)
        if self._last_minute is None or minute != self._last_minute:
            self._last_minute = minute
            old_Sd = select_spot(self.bucket_top)          # sampled snapshot at the boundary
            old_cap = bucket_cap(self.bucket_top, old_Sd) if old_Sd is not None else None
            self._old_spot_Sd = old_Sd
            self._old_cap = old_cap
            # calibration: the candle-proxy spot + every bucket's yes_bid at this boundary
            self._minute_spot = old_Sd
            self._minute_bucket_yesbid = {
                fl: bt.yes_bid for fl, bt in self.bucket_top.items() if bt.yes_bid is not None
            }
            # comparison: ms-continuous spot/cap vs minute-sampled (identical HERE at the boundary,
            # but the OLD spot/cap then STAY FIXED until the next boundary while MS keeps moving; the
            # divergence is what fill-count / lock diffs below capture. We still record the boundary
            # sample for the cap-drift-within-minute measurement on the following ticks.)
        # measure how far MS spot/cap has drifted from the last minute sample (SIM-vs-MS, continuous)
        if self._old_spot_Sd is not None and spot_Sd is not None:
            self._cmp_spot_total += 1
            if spot_Sd != self._old_spot_Sd:
                self._cmp_spot_differ += 1
            if cap is not None and self._old_cap is not None:
                self._cmp_cap_diff_c.append(float((cap - self._old_cap) * 100))

        # drive OLD model with the minute-sampled spot/cap and ms-strike W
        if self._old_spot_Sd is not None:
            old_Su = self._old_spot_Sd + self.width
            oldW = compute_W(self.strike_top, self._old_spot_Sd, old_Su)
            old_nd = (solve_n(_TWO - self.old.E - oldW, self._old_cap)
                      if (oldW is not None and self._old_cap is not None) else None)
            self.old.on_tick(t, self._old_spot_Sd, old_Su, old_nd)

    # ---- trade ----
    def _on_trade(self, floor: int, fr: RawFrame) -> None:
        if not self._in_window(fr.server_ts):
            return
        self.bucket_trades += 1
        side = fr.msg.get("taker_side")
        if side != "yes":
            return
        # YES-space price + size
        yp = _to_dec(fr.msg.get("yes_price_dollars"))
        if yp is None:
            npd = _to_dec(fr.msg.get("no_price_dollars"))
            yp = (_ONE - npd) if npd is not None else None
        if yp is None:
            return
        size = _to_dec(fr.msg.get("count_fp")) or Decimal(0)
        t = fr.server_ts
        # best YES ask on the bucket BEFORE this print (book-swept check)
        btop = self.bucket_top.get(floor)
        best_yes_ask = btop.yes_ask if btop is not None else None
        # spot context at the trade instant
        spot_Sd = select_spot(self.bucket_top)
        spot_Su = spot_Sd + self.width if spot_Sd is not None else None
        W_now = compute_W(self.strike_top, spot_Sd, spot_Su) if spot_Sd is not None else None
        if spot_Sd == floor:
            self.spot_bucket_yes_trades += 1
            # wing-drift probe (B residual W(+1.5) - W(trade))
            self._probes.append(WingProbe(t, t + 1.5, spot_Sd, spot_Su, W_now))
            self._collect_calibration(floor, yp, size, best_yes_ask, spot_Sd)
        # drive ideal + lagging models (they self-gate on floor == their spot)
        for m in self.ideals:
            m.on_trade(t, floor, yp, size, W_now, best_yes_ask)
            if m.fill is not None and m.fill not in self._pending and m.fill.lock is None:
                self._register_fill(m.fill)
        for m in self.lags.values():
            m.on_trade(t, floor, yp, size, W_now, best_yes_ask)
            if m.fill is not None and m.fill not in self._pending and m.fill.lock is None:
                self._register_fill(m.fill)
        # OLD model: its spot is the minute-sample; drive with W at print - 1 s handled at resolve
        oldW_now = None
        if self._old_spot_Sd is not None:
            oldW_now = compute_W(self.strike_top, self._old_spot_Sd,
                                 self._old_spot_Sd + self.width)
        self.old.on_trade(t, floor, yp, size, oldW_now, best_yes_ask)
        if self.old.fill is not None and self.old.fill not in self._pending and self.old.fill.lock is None:
            self._register_fill(self.old.fill)

    def _collect_calibration(self, floor: int, yp: Decimal, size: Decimal,
                             best_yes_ask: Decimal | None, ms_spot: int) -> None:
        """Per spot-bucket YES trade: spot agreement, cap error, maker-rule regime + P(fill), size."""
        self._calib_trades += 1
        # (a) spot-bucket agreement: ms choice at the trade vs the candle-proxy at the last boundary
        if self._minute_spot is not None and ms_spot == self._minute_spot:
            self._calib_spot_agree += 1
        # (b) cap error: (1 - yes_bid) - 0.01 at ms vs at the minute boundary, for this bucket
        btop = self.bucket_top.get(floor)
        yb_ms = btop.yes_bid if btop is not None else None
        yb_candle = self._minute_bucket_yesbid.get(floor)
        cap_ms = ((_ONE - yb_ms) - _CENT) if yb_ms is not None else None
        cap_candle = ((_ONE - yb_candle) - _CENT) if yb_candle is not None else None
        if cap_ms is not None and cap_candle is not None:
            self._calib_cap_err.append(float((cap_ms - cap_candle) * 100))   # cents
        # (c) spread-aware maker fill rule vs the sim's strict rule, per regime
        m_add = s_add = e_add = 0
        if self._n_shadow_10 is not None:
            offer = _ONE - self._n_shadow_10
            regime, is_fill = maker_fill_decision(offer, best_yes_ask, yp)
            self._calib_eval += 1
            e_add = 1
            self._calib_regime[regime] += 1
            if is_fill:
                self._calib_maker_fills += 1
                m_add = 1
            if yp > offer:                       # the sim's strict rule
                self._calib_strict_fills += 1
                self._calib_print_sizes.append(float(size))
                s_add = 1
        self._calib_fill_by_window = (self._calib_fill_by_window[0] + m_add,
                                      self._calib_fill_by_window[1] + s_add,
                                      self._calib_fill_by_window[2] + e_add)

    def _register_fill(self, f: Fill) -> None:
        if f.completion_target_ts <= self.now:
            self._resolve_fill(f)
        else:
            self._pending.append(f)

    def _drain_pending(self) -> None:
        # resolve any fill whose completion target has been reached
        if self._pending:
            still: list[Fill] = []
            for f in self._pending:
                if f.completion_target_ts <= self.now:
                    self._resolve_fill(f)
                else:
                    still.append(f)
            self._pending = still
        if self._probes:
            still_p: list[WingProbe] = []
            for pr in self._probes:
                if pr.target_ts <= self.now:
                    self._resolve_probe(pr)
                else:
                    still_p.append(pr)
            self._probes = still_p

    def _asof_strike(self, floor: int, target: float):
        """(yes_ask, yes_bid) of a strike as-of ``target`` (last history entry with ts <= target, ts in
        epoch seconds), else the current top, else (None, None). Mirrors the sim's ``at()`` bisect."""
        h = self.strike_hist.get(floor)
        if h:
            chosen = None
            for entry in h:            # deque is time-ordered; take the last entry <= target
                if entry[0] <= target:
                    chosen = entry
                else:
                    break
            if chosen is not None:
                return chosen[1], chosen[2]
        top = self.strike_top.get(floor)
        if top is not None:
            return top.yes_ask, top.yes_bid
        return None, None

    def _resolve_fill(self, f: Fill) -> None:
        ya, _ = self._asof_strike(f.spot_Sd, f.completion_target_ts)
        _, yb_su = self._asof_strike(f.spot_Su, f.completion_target_ts)
        if ya is not None and yb_su is not None and Decimal(0) < ya < _ONE:
            na = _ONE - yb_su
            if Decimal(0) < na < _ONE:
                W = wing_cost(ya, na)
                f.W_completion = W
                f.lock = lock_value(f.n, W)   # pinned money law: 2 - (n+fee(n)) - W
                f.complete = True
                return
        f.complete = False   # could not price the completion

    def _resolve_probe(self, pr: WingProbe) -> None:
        if pr.W_now is None:
            return
        ya, _ = self._asof_strike(pr.Sd, pr.target_ts)
        _, yb_su = self._asof_strike(pr.Su, pr.target_ts)
        if ya is not None and yb_su is not None and Decimal(0) < ya < _ONE:
            na = _ONE - yb_su
            if Decimal(0) < na < _ONE:
                W_future = wing_cost(ya, na)
                # B residual on live timing: W(trade + 1.5 s) - W(trade), cents
                self._wing_drift.append(float((W_future - pr.W_now) * 100))

    # ---- run ----
    def run(self, frames: Iterable[RawFrame]) -> WindowResult:
        for fr in frames:
            self.feed(fr)
        # advance clock to +inf so all pending completions resolve at end (using current/last tops)
        self.now = 1e18
        self._drain_pending()
        return self._finalize()

    def _finalize(self) -> WindowResult:
        m = self.meta
        res = WindowResult(
            close_time=m.close_time, resolved_mode=m.resolved_mode,
            effective_mode=m.effective_mode, params_sha=m.params_sha,
            armed=(m.effective_mode == "armed"),
        )
        res.total_frames = getattr(self, "result_total", 0)
        res.driving_frames = getattr(self, "driving", 0)
        res.m15_frames = getattr(self, "_m15_ct", 0)
        res.strike_frames = getattr(self, "strike_frames", 0)
        res.bucket_frames = getattr(self, "bucket_frames", 0)
        res.bucket_trades = getattr(self, "bucket_trades", 0)
        res.spot_bucket_yes_trades = getattr(self, "spot_bucket_yes_trades", 0)
        # modal spot bucket
        modal_spot = max(self._spot_tick_ct, key=self._spot_tick_ct.get) if self._spot_tick_ct else None
        # per-bucket median gaps
        all_bucket_gaps = [g for gs in self._bucket_gaps.values() for g in gs]
        per_bucket_medians = [statistics.median(gs) for gs in self._bucket_gaps.values() if gs]
        spot_gaps = self._spot_gaps.get(modal_spot, []) if modal_spot is not None else []
        total_frames = max(1, res.total_frames)
        res.metrics = {
            "modal_spot_Sd": modal_spot,
            "spot_bucket_count": len(self._spot_tick_ct),
            "bucket_tick_gap_ms_median_overall": _median(all_bucket_gaps),
            "bucket_tick_gap_ms_median_of_bucket_medians": _median(per_bucket_medians),
            "spot_tick_gap_ms_median": _median(spot_gaps),
            "spot_spread_cents_median": _median(self._spot_spread_c),
            "spot_spread_cents_p90": _pct(self._spot_spread_c, 0.9),
            "spot_top_depth_median": _median(self._spot_top_depth),
            "spot_yes_ask_size_median": _median(self._spot_yes_ask_size),
            "spot_no_bid_size_median": _median(self._spot_no_bid_size),
            "strike_tick_gap_ms_median": _median(self._strike_gaps),
            "n_bucket_trades": res.bucket_trades,
            "n_spot_bucket_yes_trades": res.spot_bucket_yes_trades,
            "m15_frame_share": res.m15_frames / total_frames,
            "strike_frame_share": res.strike_frames / total_frames,
            "bucket_frame_share": res.bucket_frames / total_frames,
        }
        # fills
        res.ideal_fills = [mm.fill for mm in self.ideals if mm.fill is not None]
        for key, mm in self.lags.items():
            res.lag_fills[key] = mm.fill
            res.lag_replaces[key] = mm.replaces
        res.base_fill = self.lags[BASE_CELL].fill
        res.old_fill = self.old.fill
        res.old_replaces = self.old.replaces
        res.wing_drift_cents = self._wing_drift
        # comparison
        base_fill = res.base_fill
        old_fill = res.old_fill
        res.comparison = {
            "spot_samples": self._cmp_spot_total,
            "spot_differ": self._cmp_spot_differ,
            "spot_differ_frac": (self._cmp_spot_differ / self._cmp_spot_total)
            if self._cmp_spot_total else None,
            "cap_diff_cents_median": _median([abs(x) for x in self._cmp_cap_diff_c]),
            "cap_diff_cents_p90": _pct([abs(x) for x in self._cmp_cap_diff_c], 0.9),
            "base_fill": base_fill is not None,
            "old_fill": old_fill is not None,
            "base_lock_cents": float(base_fill.lock * 100) if base_fill and base_fill.lock is not None else None,
            "old_lock_cents": float(old_fill.lock * 100) if old_fill and old_fill.lock is not None else None,
            "lock_diff_cents": (
                float((base_fill.lock - old_fill.lock) * 100)
                if base_fill and old_fill and base_fill.lock is not None and old_fill.lock is not None
                else None
            ),
            "wing_drift_cents_median": _median(self._wing_drift),
            "wing_drift_cents_p90": _pct([abs(x) for x in self._wing_drift], 0.9),
        }
        # calibration raw samples (this window) for the cross-window aggregate
        res.calib = {
            "n_trades": self._calib_trades,
            "spot_agree": self._calib_spot_agree,
            "cap_err_c": list(self._calib_cap_err),
            "eval": self._calib_eval,
            "regime": dict(self._calib_regime),
            "maker_fills": self._calib_maker_fills,
            "strict_fills": self._calib_strict_fills,
            "print_sizes": list(self._calib_print_sizes),
            "B_resid_c": list(self._wing_drift),
            "base_replaces": self.lags[BASE_CELL].replaces,
            "fill_window": self._calib_fill_by_window,
        }
        return res


def _to_dec(x) -> Decimal | None:
    if x is None:
        return None
    try:
        d = Decimal(str(x))
    except Exception:  # noqa: BLE001
        return None
    return d if d.is_finite() else None
