"""models.py — the pricing models the replay lab runs side by side on one journal.

Three families, all scoring the SAME reconstructed ms feed, differing only in rule/data source:

  * ``IdealModel(E)`` — the OPTIMISTIC / no-lag shadow: re-solve n every book tick with no requote
    gate; a spot-bucket YES print strictly above the offer (1 - n) fills once; completion at the wing
    asks AT THE TRADE TICK (no +1.5 s lag). This is ``pf_ms_requote.py``'s ideal rule, fed ms books.

  * ``LaggingModel(E, TOL, DEB, LAT)`` — the executor model of ``pf_ms_requote2.py`` fed the MS BUCKET
    BOOK instead of the minute candle: replace only when |dn| >= TOL and >= DEB ms since the last
    replace; the new quote goes live +LAT ms; fill = a spot-bucket YES print strictly above 1 - n_rest;
    completion at the wing asks at trade + 1.5 s. Also records the ``book_swept`` flag (the bucket's
    best YES ask before the print was <= our offer) and the through-print size.

  * ``OldModel(E, TOL, DEB, LAT)`` — the SIM's data sourcing: spot selection + cap from the bucket book
    SAMPLED AT MINUTE BOUNDARIES (the minute-candle equivalent), wings from ms strike books. Run next to
    the matching LaggingModel to isolate the bucket-data-source effect.

Money math is the pinned law (``solve_n`` / ``wing_cost`` / ``lock_value`` / ``fee``); the engine
resolves each fill's completion W as-of the target ts and writes the lock.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

_ONE = Decimal(1)

# The lagging grid pinned by the task.
GRID_E = (Decimal("0.08"), Decimal("0.10"), Decimal("0.12"))
GRID_TOL = (Decimal("0.01"), Decimal("0.02"), Decimal("0.03"))
GRID_DEB = (0, 2000, 5000)
LAT_MS = 200
BASE_CELL = (Decimal("0.10"), Decimal("0.02"), 5000)   # E, TOL, DEB
LAGGING_COMPLETION_LAG_MS = 1500
OLD_WING_LEAD_MS = -1000   # OLD model prices completion wings at print - 1 s (as the sim did)


def maker_fill_decision(o: Decimal, a: Decimal | None, p: Decimal) -> tuple[str, bool]:
    """The SPREAD-AWARE maker fill rule (coordinator ruling 2026-09-14, replacing 'book-swept').

    Our resting bucket-NO bid at n is a YES ask at ``o = 1 - n``. ``a`` is the market's best YES ask
    immediately before the print (from the ms bucket book); ``p`` is the YES-taker print price.

      * regime (iii) ``o > a``: our offer is ABOVE the market ask -> a post_only order there would not
        rest at/inside the top (or be rejected). No fill; the tick is a no-quote. Only happens when the
        budget (2 - E - W) forces n well below the cap, lifting our offer 1 - n above the market ask.
      * regime (ii) ``o == a``: we JOIN the existing best-ask level. Fill iff ``p > o`` (the level was
        swept THROUGH; queue-independent) — the sim's strict rule.
      * regime (i) ``o < a`` (we are the best ask, alone; also when ``a`` is unknown): by price priority a
        YES taker who paid the higher market ask ``a`` would have hit OUR lower offer first. Fill iff
        ``p >= o`` (any buyer crossing at/through our level takes us; there is no queue at our price but
        us, so queue position is irrelevant).
    """
    if a is not None and o > a:
        return "iii", False
    if a is not None and o == a:
        return "ii", p > o
    return "i", p >= o


@dataclass
class Fill:
    """One recorded fill. ``lock`` / ``W_completion`` are filled in by the engine once the completion
    ts is reached (as-of the target strike tops)."""

    model: str                 # e.g. "ideal", "lag", "old"
    E: Decimal
    tol: Decimal | None
    deb: int | None
    spot_Sd: int
    spot_Su: int
    n: Decimal
    offer: Decimal             # 1 - n (our YES ask)
    print_price: Decimal
    print_size: Decimal
    trade_ts: float            # epoch seconds
    completion_target_ts: float
    regime: str = "i"          # maker-rule regime (i / ii / iii) at the fill
    since_replace_ms: float | None = None   # ms since our offer was (re)placed (PESSIMISTIC LAT drop)
    W_completion: Decimal | None = None
    lock: Decimal | None = None
    complete: bool = False


class IdealModel:
    """No-lag continuous-requote shadow for one E (OPTIMISTIC)."""

    def __init__(self, E: Decimal) -> None:
        self.E = E
        self.n: Decimal | None = None
        self.spot_Sd: int | None = None
        self.spot_Su: int | None = None
        self.fill: Fill | None = None

    def on_tick(self, t: float, spot_Sd: int | None, spot_Su: int | None,
                nd: Decimal | None) -> None:
        """``nd`` is the pre-solved desired n for this E (the engine solves once per E per tick)."""
        self.spot_Sd = spot_Sd
        self.spot_Su = spot_Su
        self.n = nd if spot_Sd is not None else None

    def on_trade(self, t: float, floor: int, yp: Decimal, size: Decimal,
                 W_now: Decimal | None, best_yes_ask: Decimal | None) -> None:
        if self.fill is not None or self.n is None or self.spot_Sd is None:
            return
        if floor != self.spot_Sd:
            return
        if W_now is None:               # completable-now proxy (ideal completes at the trade tick)
            return
        offer = _ONE - self.n
        # OPTIMISTIC keeps the sim's strict rule (p > offer); regime recorded for information.
        if yp > offer:
            regime, _ = maker_fill_decision(offer, best_yes_ask, yp)
            self.fill = Fill(
                model="ideal", E=self.E, tol=None, deb=None,
                spot_Sd=self.spot_Sd, spot_Su=self.spot_Su or (self.spot_Sd + 100),
                n=self.n, offer=offer, print_price=yp, print_size=size,
                trade_ts=t, completion_target_ts=t,       # no lag
                regime=regime,
            )


class LaggingModel:
    """Lagging-quote executor for one (E, TOL, DEB) cell fed the ms bucket book (BASE family)."""

    model_name = "lag"

    def __init__(self, E: Decimal, tol: Decimal, deb: int, lat: int = LAT_MS,
                 completion_lag_ms: int = LAGGING_COMPLETION_LAG_MS) -> None:
        self.E = E
        self.tol = tol
        self.deb = deb
        self.lat = lat
        self.completion_lag_ms = completion_lag_ms
        # executor state (mirrors pf_ms_requote2's inner loop)
        self.n_rest: Decimal | None = None
        self.pending: Decimal | None = None
        self.live_at: float | None = None
        self.last_rep: float = -1e18
        self.replaces: int = 0
        self.prev_s: int | None = None
        self.forced: bool = False
        self.filled: bool = False
        self.fill: Fill | None = None
        self.spot_Sd: int | None = None
        self.spot_Su: int | None = None

    def _promote(self, t: float) -> None:
        if self.pending is not None and self.live_at is not None and t >= self.live_at:
            self.n_rest = self.pending
            self.pending = None

    def on_tick(self, t: float, spot_Sd: int | None, spot_Su: int | None,
                nd: Decimal | None) -> None:
        """``nd`` is the pre-solved desired n for this cell's E (engine solves once per E per tick)."""
        if self.filled:
            return
        self._promote(t)
        self.spot_Sd = spot_Sd
        self.spot_Su = spot_Su
        # bucket change: cancel the old rest, force a fresh place on the next valid solve.
        if spot_Sd != self.prev_s:
            self.n_rest = None
            self.pending = None
            self.forced = True
            self.prev_s = spot_Sd
        if spot_Sd is None or nd is None:
            return
        if self.pending is not None:
            return                       # a replace is in flight; wait for it to go live
        gate = (
            self.forced
            or self.n_rest is None
            or (abs(nd - self.n_rest) >= self.tol and (t - self.last_rep) >= self.deb / 1000.0)
        )
        if not gate:
            return
        if self.n_rest is not None and nd == self.n_rest:
            return
        self.pending = nd
        self.live_at = t + self.lat / 1000.0
        self.last_rep = t
        self.replaces += 1
        self.forced = False

    def on_trade(self, t: float, floor: int, yp: Decimal, size: Decimal,
                 W_now: Decimal | None, best_yes_ask: Decimal | None) -> None:
        if self.filled:
            return
        self._promote(t)
        if self.n_rest is None or self.spot_Sd is None or floor != self.spot_Sd:
            return
        if W_now is None:                # completable-now proxy (matches pf's "skip if W2 None")
            return
        offer = _ONE - self.n_rest
        # SPREAD-AWARE maker fill rule (replaces 'book-swept').
        regime, is_fill = maker_fill_decision(offer, best_yes_ask, yp)
        if not is_fill:
            return
        since_replace_ms = (t - self.last_rep) * 1000.0 if self.last_rep > -1e17 else None
        self.fill = Fill(
            model=self.model_name, E=self.E, tol=self.tol, deb=self.deb,
            spot_Sd=self.spot_Sd, spot_Su=self.spot_Su or (self.spot_Sd + 100),
            n=self.n_rest, offer=offer, print_price=yp, print_size=size,
            trade_ts=t, completion_target_ts=t + self.completion_lag_ms / 1000.0,
            regime=regime, since_replace_ms=since_replace_ms,
        )
        self.filled = True


class OldModel(LaggingModel):
    """The sim's data sourcing: spot/cap from the minute-sampled bucket book, wings from ms strike
    books (completion priced at print - 1 s). Fed a SEPARATE (minute-sampled) spot/W/cap by the engine;
    the executor logic is inherited unchanged."""

    model_name = "old"

    def __init__(self, E: Decimal, tol: Decimal, deb: int, lat: int = LAT_MS) -> None:
        super().__init__(E, tol, deb, lat, completion_lag_ms=OLD_WING_LEAD_MS)
