"""signal.py — the PURE decision core: ``decide(params, state, event) -> (new_state, actions)``.

House law (PLAN "Coding standards" 1/2): all strategy logic lives here and in pure helpers.
No clock reads, no network, no disk, no globals. Time comes ONLY from event timestamps, so the
same function runs live and in replay bit-identically (the golden-determinism test pins this).

WHAT IT REPRODUCES — the frozen sim law (tape_sim) mapped onto a LIVE order book, one mapping
at a time (each is called out in the Phase-2 build report CONFESSIONS):

  * C from ASKS, not prints. The sim priced C from the most-recent same-side taker PRINT
    (honest-fills law). Live we price C from the current best ASK we can actually cross:
      - flip  C = (high-leg NO-ask + fee) + (low-leg YES-ask + fee)     [buy insides]
      - strangle C = (high-leg YES-ask + fee) + (low-leg NO-ask + fee)  [buy outsides]
    "high leg" = the higher-strike market of the pair, "low leg" = the lower. fee is the audited
    census fee, IMPORTED (never retyped). This book-vs-prints C is a NAMED structural live/sim
    delta, reported by the parity harness.

  * A15.9 (cross-side refutation) / A15.10 (dwell) are PRINT-tape concepts: a carried print price
    dies when an opposite-side print refutes it; dwell measures how long a price stood. A live
    book has NO carried price — the ask IS current by construction. The faithful live mapping is:
    a fire requires the two legs' quotes to be SIMULTANEOUSLY FRESH (the freshness gate) and the
    book NOT SUSPECT. Stated here and flagged as a named live/sim delta for the parity report.

  * Freshness gate: BOTH legs' book age <= freshness_max_leg_age_s (1.0s), where a leg's age is
    (event ts - that leg's last book-update ts). Unknown age (never updated) or a suspect book =>
    STALE => never fires (fail closed). The sim's 60s staleness horizon is the looser outer bound;
    the 1s freshness gate binds and subsumes it for firing.

  * Triggers (PILOT roster, from the frozen policy config):
      - sub-$1 flip (ALL quintiles): first moment flip-C < sub_dollar_C_max (STRICT <).
      - Q1-strangle (ONLY quintile 0, and only if the ladder did not stand the strangle down):
        strangle-EV = fair_bucket(strangle, q0) - strangle-C >= q1_strangle_ev_min, where
        fair_bucket comes from the sha-verified census EV curve (tape_sim.EVCurve).
      - NO flip-EV anywhere.

  * Entry is per WINDOW: the FIRST qualifying source fires (WouldFire in shakedown, Fire live),
    then the window is DONE for BOTH sources ("both sources can't fire — first qualifying wins",
    race by event order). Same-event tie-break: sub-$1 flip has priority (the arithmetic floor).

  * No orders with < no_orders_after_s_to_settle (1s) to settlement -> StandDown, no fire.

  * Only the final WINDOW_S (900s, imported from the sim) is an entry window; earlier events seed
    book state but never fire (warmup), exactly as the sim only evaluates T-900..T.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal

from service._simlaw import WINDOW_S, close_epoch, fee
from service.book import TopOfBook
from service.policy import Q1_STRANGLE, SUB_DOLLAR_FLIP, PolicyParams

# --- action kinds ---
WOULD_FIRE = "WOULD_FIRE"
FIRE = "FIRE"
STAND_DOWN = "STAND_DOWN"

# --- leg sides (Kalshi YES-perspective outcome we BUY on that leg) ---
BUY_YES = "yes"
BUY_NO = "no"


# ---------------------------------------------------------------------------
# Events (each carries the ONLY notion of "now": a server-derived timestamp)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BookUpdate:
    """A top-of-book update for ONE market leg, at server-derived time ``server_ts`` (epoch s)."""

    market: str
    top: TopOfBook
    server_ts: float


@dataclass(frozen=True)
class ClockTick:
    """A time-only event (no book change): server-derived ``server_ts`` (epoch s)."""

    server_ts: float


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LegOrder:
    """One leg of an intended pair: buy ``count`` of ``side`` on ``ticker`` at ``limit_price``
    (the OBSERVED ask for that leg)."""

    ticker: str
    side: str
    count: int
    limit_price: Decimal


@dataclass(frozen=True)
class Action:
    """A decision emitted by decide(). ``kind`` in {WOULD_FIRE, FIRE, STAND_DOWN}."""

    kind: str
    source: str | None = None          # SUB_DOLLAR_FLIP / Q1_STRANGLE (None for STAND_DOWN)
    legs: tuple[LegOrder, ...] = ()
    count: int = 0
    C: Decimal | None = None
    ev: Decimal | None = None
    t_minus_s: float | None = None
    reason: str | None = None          # populated for STAND_DOWN


# ---------------------------------------------------------------------------
# Window state (immutable; decide returns a NEW state via dataclasses.replace)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class WindowState:
    close_time: str
    T: int                              # settlement epoch (seconds)
    high_ticker: str                    # higher-strike leg
    low_ticker: str                     # lower-strike leg
    quintile: int
    fair_strangle_q: Decimal            # EVCurve.fair_for("strangle", quintile)
    strangle_disabled: bool = False     # ladder-map deviation stood the strangle down
    shakedown: bool = False             # WouldFire-only mode (no orders exist yet)
    G: Decimal | None = None            # window G (|K-A|), carried for the journal/report
    sigma_hat: float | None = None      # carried for the journal/report

    # live book state (per leg): latest top + its last-update server ts
    high_top: TopOfBook | None = None
    low_top: TopOfBook | None = None
    high_ts: float | None = None
    low_ts: float | None = None

    # entry bookkeeping (window-level mutual exclusion)
    entered: bool = False
    fired_source: str | None = None
    standdown_emitted: bool = False

    @classmethod
    def new(
        cls,
        close_time: str,
        high_ticker: str,
        low_ticker: str,
        quintile: int,
        fair_strangle_q: Decimal,
        *,
        strangle_disabled: bool = False,
        shakedown: bool = False,
        G: Decimal | None = None,
        sigma_hat: float | None = None,
        T: int | None = None,
    ) -> "WindowState":
        return cls(
            close_time=close_time,
            T=int(close_epoch(close_time)) if T is None else int(T),
            high_ticker=high_ticker,
            low_ticker=low_ticker,
            quintile=quintile,
            fair_strangle_q=fair_strangle_q,
            strangle_disabled=strangle_disabled,
            shakedown=shakedown,
            G=G,
            sigma_hat=sigma_hat,
        )


# ---------------------------------------------------------------------------
# Pure cost primitives (C from asks; fee is the imported audited census fee)
# ---------------------------------------------------------------------------
def _leg_cost(price: Decimal | None) -> Decimal | None:
    """price + audited_fee(price), or None if the ask is absent."""
    if price is None:
        return None
    return price + fee(price)


def flip_cost(high_top: TopOfBook, low_top: TopOfBook) -> Decimal | None:
    """Flip C = (high-leg NO-ask + fee) + (low-leg YES-ask + fee). None if either ask absent."""
    hi = _leg_cost(high_top.no_ask)
    lo = _leg_cost(low_top.yes_ask)
    if hi is None or lo is None:
        return None
    return hi + lo


def strangle_cost(high_top: TopOfBook, low_top: TopOfBook) -> Decimal | None:
    """Strangle C = (high-leg YES-ask + fee) + (low-leg NO-ask + fee). None if either ask absent."""
    hi = _leg_cost(high_top.yes_ask)
    lo = _leg_cost(low_top.no_ask)
    if hi is None or lo is None:
        return None
    return hi + lo


def _leg_age(now: float, last_ts: float | None) -> float | None:
    """Age of a leg's book data at ``now``; None if never updated OR the ts is in the future
    (both fail-closed to 'unknown -> stale')."""
    if last_ts is None:
        return None
    age = now - last_ts
    if age < 0:
        return None
    return age


def _both_fresh(state: WindowState, now: float, params: PolicyParams) -> bool:
    """Both legs known, not suspect, and within the freshness bound at ``now`` (fail closed)."""
    ht, lt = state.high_top, state.low_top
    if ht is None or lt is None:
        return False
    if ht.suspect or lt.suspect:
        return False
    ha = _leg_age(now, state.high_ts)
    la = _leg_age(now, state.low_ts)
    if ha is None or la is None:
        return False
    bound = params.freshness_max_leg_age_s
    return ha <= bound and la <= bound


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------
def decide(
    params: PolicyParams, state: WindowState, event: BookUpdate | ClockTick
) -> tuple[WindowState, list[Action]]:
    """Pure decision. Returns (new_state, actions). See module docstring for the full law."""
    now = event.server_ts
    st = state

    # 1) fold a book update into state (unrelated legs are ignored — the pilot subscribes the
    #    whole hourly ladder but only the paired high/low tickers move the decision).
    if isinstance(event, BookUpdate):
        if event.market == st.high_ticker:
            st = replace(st, high_top=event.top, high_ts=now)
        elif event.market == st.low_ticker:
            st = replace(st, low_top=event.top, low_ts=now)
        else:
            return st, []

    # 2) window already entered -> done for both sources.
    if st.entered:
        return st, []

    t_minus = st.T - now

    # 3) no orders inside the settle cutoff (emit StandDown once).
    if t_minus < params.no_orders_after_s_to_settle:
        if not st.standdown_emitted:
            st = replace(st, standdown_emitted=True)
            return st, [
                Action(
                    kind=STAND_DOWN,
                    t_minus_s=t_minus,
                    reason=(
                        f"within no-orders-to-settle cutoff "
                        f"(t-{t_minus:.3f}s < {params.no_orders_after_s_to_settle}s)"
                    ),
                )
            ]
        return st, []

    # 4) warmup: outside the final WINDOW_S, seed book state but never fire (as the sim).
    if t_minus > WINDOW_S:
        return st, []

    # 5) freshness / not-suspect gate (the live mapping of A15.9/A15.10). Fail closed.
    if not _both_fresh(st, now, params):
        return st, []

    ht, lt = st.high_top, st.low_top
    assert ht is not None and lt is not None  # guaranteed by _both_fresh
    active = params.sources_for_quintile(st.quintile)

    # 6) evaluate qualifiers in fixed priority order: sub-$1 flip FIRST (arithmetic floor wins a
    #    same-event tie), then Q1-strangle. First qualifier fires; window then done for both.
    qualifiers: list[tuple[str, Decimal, Decimal | None, tuple[LegOrder, ...]]] = []

    if SUB_DOLLAR_FLIP in active:
        c_flip = flip_cost(ht, lt)
        if c_flip is not None and c_flip < params.sub_dollar_C_max:
            legs = (
                LegOrder(st.high_ticker, BUY_NO, 1, ht.no_ask),   # buy NO on the high leg
                LegOrder(st.low_ticker, BUY_YES, 1, lt.yes_ask),  # buy YES on the low leg
            )
            qualifiers.append((SUB_DOLLAR_FLIP, c_flip, None, legs))

    if Q1_STRANGLE in active and st.quintile == 0 and not st.strangle_disabled:
        c_str = strangle_cost(ht, lt)
        if c_str is not None:
            ev = st.fair_strangle_q - c_str
            if ev >= params.q1_strangle_ev_min:
                legs = (
                    LegOrder(st.high_ticker, BUY_YES, 1, ht.yes_ask),  # buy YES on the high leg
                    LegOrder(st.low_ticker, BUY_NO, 1, lt.no_ask),     # buy NO on the low leg
                )
                qualifiers.append((Q1_STRANGLE, c_str, ev, legs))

    if not qualifiers:
        return st, []

    source, C, ev, legs = qualifiers[0]
    kind = WOULD_FIRE if st.shakedown else FIRE
    st = replace(st, entered=True, fired_source=source)
    return st, [
        Action(
            kind=kind,
            source=source,
            legs=legs,
            count=1,
            C=C,
            ev=ev,
            t_minus_s=t_minus,
        )
    ]
