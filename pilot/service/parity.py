"""parity.py — the five-bin paired-replay harness (Phase 2: bins 1-2; bins 3-5 when fills exist).

For one window it compares:
  (a) the LIVE decision — decide() run over the Phase-1 JOURNAL (replayed book), the exact book
      the live service saw; and
  (b) the SIM decision — the PILOT roster applied to the frozen tape sim's ``tape_points.csv``
      slice for that close_time (print-based C), with first-entry semantics.

and bins the pair (commission "the five bins"):
  1. sim fired / live never saw it            (feed gap, latency)
  2. live fired / sim didn't                  (signal-engine divergence)
  3. both fired / live didn't fill            (fillability)  -- needs fills
  4. both filled / different price            (slippage)     -- needs fills
  5. both filled, same price, payoff matches  (sim tells the truth) -- needs fills
  + imbalance placeholder; + no-signal-both.

Bins 3-5 require a per-window fills record (Phase 3 supplies it). Absent => SHAKEDOWN mode,
bins 1-2 only.

F15 neutrality gate (PLAN review disposition; commission shakedown gate): a live/sim fire/no-fire
DISAGREEMENT is a FAILURE, not a note — even when the cause is the DOCUMENTED book-vs-prints C
delta. ``run_parity`` reports every bin-1/bin-2 window as a neutrality failure and the harness
``passed`` iff there are none.

The sim-side roster application mirrors the covered-prefix, per-window-first-entry approach of
claudes-corner/viz/aggregate.py, narrowed to the pilot roster (single entry per window, both
sources mutually exclusive, sub-$1 flip winning a same-moment tie).
"""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Iterable, Iterator

from service._simlaw import TAPE_FIELDNAMES, WINDOW_S
from service.book import BookMirror
from service.policy import Q1_STRANGLE, SUB_DOLLAR_FLIP, PolicyParams
from service.signal import (
    FIRE,
    WOULD_FIRE,
    Action,
    BookUpdate,
    WindowState,
    decide,
)
from service.ws_client import _parse_server_ts

# bin labels
BIN_SIM_ONLY = 1
BIN_LIVE_ONLY = 2
BIN_BOTH_NO_FILL = 3
BIN_BOTH_DIFF_PRICE = 4
BIN_BOTH_MATCH = 5
LABEL_NO_SIGNAL = "no_signal_both"
LABEL_BOTH_FIRED = "both_fired_pending_fills"
LABEL_IMBALANCE = "imbalance"
# Phase-3 review (F3 fix): both fired + filled but NO fills leg is comparable to the sim's paired
# high/low leg prices -> a data/schema fault, NEVER a bin-5 "sim tells the truth" certification.
LABEL_UNCOMPARABLE = "both_filled_uncomparable"

_WS_KIND = "kalshi_ws"
_BOOK_TYPES = ("orderbook_snapshot", "orderbook_delta")


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SimEntry:
    fired: bool
    source: str | None = None
    t_minus_s: float | None = None
    C: Decimal | None = None
    ev: Decimal | None = None
    high_leg_price: Decimal | None = None
    low_leg_price: Decimal | None = None
    pin: int | None = None
    payoff: Decimal | None = None


@dataclass(frozen=True)
class LiveEntry:
    fired: bool
    source: str | None = None
    t_minus_s: float | None = None
    C: Decimal | None = None
    ev: Decimal | None = None
    high_limit: Decimal | None = None
    low_limit: Decimal | None = None


@dataclass(frozen=True)
class LegFill:
    ticker: str
    side: str
    count: int
    avg_price: Decimal


@dataclass(frozen=True)
class WindowFills:
    """Phase-3 fill record for a window (minimal schema; Phase 3 fills in the real one)."""

    filled: bool
    legs: tuple[LegFill, ...] = ()
    imbalance: bool = False
    realized_payoff: Decimal | None = None


# ---------------------------------------------------------------------------
# SIM side: read tape_points.csv + apply the pilot roster
# ---------------------------------------------------------------------------
def load_sim_window(path: str, close_time: str) -> list[dict]:
    """Stream ``tape_points.csv`` and return only the rows for ``close_time`` (the 3GB full-run
    file is streamed once; contiguity is NOT assumed — every matching row is collected)."""
    out: list[dict] = []
    with open(path, newline="", encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        if rdr.fieldnames != TAPE_FIELDNAMES:
            raise ValueError(
                f"tape_points header mismatch: {rdr.fieldnames} != {TAPE_FIELDNAMES}"
            )
        for row in rdr:
            if row["close_time"] == close_time:
                out.append(row)
    return out


def _fresh_row(row: dict, freshness_max_leg_age_s: float) -> bool:
    return (
        max(float(row["high_leg_age_s"]), float(row["low_leg_age_s"]))
        <= freshness_max_leg_age_s
    )


def sim_entry_for_window(rows: Iterable[dict], params: PolicyParams) -> SimEntry:
    """Apply the PILOT roster to one window's tape rows; return the FIRST qualifying entry
    (window-level mutual exclusion, sub-$1 flip wins a same-t tie)."""
    fresh_bound = params.freshness_max_leg_age_s
    q1_active_quintile = 0
    # Mirror decide()'s FIRING WINDOW exactly: decide() will not fire outside
    # [no_orders_after_s_to_settle, WINDOW_S] (t-1s settle cutoff -> StandDown; t>900 -> warmup).
    # The sim-side must apply the SAME bounds, else a tape moment that qualifies only inside the
    # 1s settle cutoff makes the sim "fire" while live stands down -> a SPURIOUS bin-1/bin-2 F15
    # failure that is NOT the documented book-vs-prints delta but a harness modeling gap (REVIEW
    # phase2 F1). The tape already restricts to <=900, so the lower bound is the binding one.
    t_floor = float(params.no_orders_after_s_to_settle)

    best_sub: dict | None = None   # earliest-in-time (largest t_minus_s) qualifying flip row
    best_str: dict | None = None

    for row in rows:
        if not _fresh_row(row, fresh_bound):
            continue
        t = float(row["t_minus_s"])
        if t < t_floor or t > WINDOW_S:
            continue
        direction = row["direction"]
        if direction == "flip":
            if SUB_DOLLAR_FLIP not in params.sources_for_quintile(int(row["quintile"])):
                continue
            if Decimal(row["C"]) < params.sub_dollar_C_max:
                if best_sub is None or t > float(best_sub["t_minus_s"]):
                    best_sub = row
        elif direction == "strangle":
            q = int(row["quintile"])
            if q != q1_active_quintile:
                continue
            if Q1_STRANGLE not in params.sources_for_quintile(q):
                continue
            if Decimal(row["ev"]) >= params.q1_strangle_ev_min:
                if best_str is None or t > float(best_str["t_minus_s"]):
                    best_str = row

    # window-level race: the earlier moment (larger t_minus_s) wins; tie -> sub-$1 flip priority.
    winner_source: str | None = None
    winner: dict | None = None
    if best_sub is not None and best_str is not None:
        if float(best_sub["t_minus_s"]) >= float(best_str["t_minus_s"]):
            winner_source, winner = SUB_DOLLAR_FLIP, best_sub
        else:
            winner_source, winner = Q1_STRANGLE, best_str
    elif best_sub is not None:
        winner_source, winner = SUB_DOLLAR_FLIP, best_sub
    elif best_str is not None:
        winner_source, winner = Q1_STRANGLE, best_str

    if winner is None:
        return SimEntry(fired=False)
    return SimEntry(
        fired=True,
        source=winner_source,
        t_minus_s=float(winner["t_minus_s"]),
        C=Decimal(winner["C"]),
        ev=Decimal(winner["ev"]),
        high_leg_price=Decimal(winner["high_leg_price"]),
        low_leg_price=Decimal(winner["low_leg_price"]),
        pin=int(winner["pin"]),
        payoff=Decimal(winner["payoff"]),
    )


# ---------------------------------------------------------------------------
# LIVE side: replay journal -> BookUpdate events -> decide()
# ---------------------------------------------------------------------------
def live_book_events(
    records: Iterable[dict], high_ticker: str, low_ticker: str
) -> Iterator[BookUpdate]:
    """Reconstruct BookUpdate events for the two paired legs from a journal's raw WS records.

    Uses the SAME book semantics as service.replay (a fresh BookMirror per market, snapshot/delta
    applied in idx order); yields a BookUpdate only for the high/low legs. server_ts is the frame's
    server timestamp (_parse_server_ts on the payload) so freshness matches the live gate; it falls
    back to the record's local_ts when the frame carries no server ts.
    """
    relevant = {high_ticker, low_ticker}
    books: dict[str, BookMirror] = {}
    for rec in records:
        if rec.get("kind") != _WS_KIND:
            continue
        env = rec.get("obj") or {}
        msg_type = env.get("type")
        if msg_type not in _BOOK_TYPES:
            continue
        payload = env.get("msg") or {}
        market = payload.get("market_ticker") if isinstance(payload, dict) else None
        if not market:
            continue
        book = books.get(market)
        if book is None:
            book = BookMirror()
            books[market] = book
        if msg_type == "orderbook_snapshot":
            book.apply_snapshot(payload)
        else:
            book.apply_delta(payload)
        if market not in relevant:
            continue
        server_ts = _parse_server_ts(payload)
        if server_ts is None:
            server_ts = float(rec.get("local_ts", 0.0))
        yield BookUpdate(market=market, top=book.top_of_book(), server_ts=server_ts)


def live_entry_for_window(
    records: Iterable[dict], state0: WindowState, params: PolicyParams
) -> LiveEntry:
    """Run decide() over the journal's book events; return the first Fire/WouldFire as a LiveEntry."""
    st = state0
    for ev in live_book_events(records, state0.high_ticker, state0.low_ticker):
        st, actions = decide(params, st, ev)
        for a in actions:
            if a.kind in (FIRE, WOULD_FIRE):
                high_limit = next((lg.limit_price for lg in a.legs if lg.ticker == state0.high_ticker), None)
                low_limit = next((lg.limit_price for lg in a.legs if lg.ticker == state0.low_ticker), None)
                return LiveEntry(
                    fired=True,
                    source=a.source,
                    t_minus_s=a.t_minus_s,
                    C=a.C,
                    ev=a.ev,
                    high_limit=high_limit,
                    low_limit=low_limit,
                )
    return LiveEntry(fired=False)


# ---------------------------------------------------------------------------
# Binning
# ---------------------------------------------------------------------------
@dataclass
class WindowParity:
    close_time: str
    bin: object                       # int 1-5 or a LABEL_* string
    sim: SimEntry
    live: LiveEntry
    neutrality_ok: bool
    source_match: bool | None = None
    detail: dict = field(default_factory=dict)


def _price_deltas(sim: SimEntry, fills: WindowFills, high_ticker: str, low_ticker: str) -> dict:
    """Per-leg |live avg fill - sim leg price| (sim high/low leg prices vs the fills)."""
    out: dict[str, object] = {}
    for lg in fills.legs:
        if lg.ticker == high_ticker and sim.high_leg_price is not None:
            out["high_delta"] = str(abs(lg.avg_price - sim.high_leg_price))
        elif lg.ticker == low_ticker and sim.low_leg_price is not None:
            out["low_delta"] = str(abs(lg.avg_price - sim.low_leg_price))
    return out


def assign_bin(
    close_time: str,
    sim: SimEntry,
    live: LiveEntry,
    fills: WindowFills | None = None,
    high_ticker: str = "",
    low_ticker: str = "",
) -> WindowParity:
    """Bin one window. bins 3-5 only when both fired AND ``fills`` is provided."""
    if not sim.fired and not live.fired:
        return WindowParity(close_time, LABEL_NO_SIGNAL, sim, live, neutrality_ok=True)

    if sim.fired and not live.fired:
        return WindowParity(
            close_time, BIN_SIM_ONLY, sim, live, neutrality_ok=False,
            detail={"cause": "sim fired, live did not — feed gap / latency / book-vs-prints C "
                             "(documented delta flipping a fire decision is an F15 FAILURE)"},
        )
    if live.fired and not sim.fired:
        return WindowParity(
            close_time, BIN_LIVE_ONLY, sim, live, neutrality_ok=False,
            detail={"cause": "live fired, sim did not — signal-engine divergence / book-vs-prints C "
                             "(documented delta flipping a fire decision is an F15 FAILURE)"},
        )

    # both fired
    source_match = sim.source == live.source
    if fills is None:
        return WindowParity(
            close_time, LABEL_BOTH_FIRED, sim, live, neutrality_ok=True,
            source_match=source_match,
            detail={"note": "both fired; bins 3-5 need a fills record (shakedown => bins 1-2 only)"}
            | ({"source_mismatch": True} if not source_match else {}),
        )

    if fills.imbalance:
        return WindowParity(
            close_time, LABEL_IMBALANCE, sim, live, neutrality_ok=True,
            source_match=source_match, detail={"imbalance": True},
        )
    if not fills.filled:
        return WindowParity(
            close_time, BIN_BOTH_NO_FILL, sim, live, neutrality_ok=True, source_match=source_match
        )
    deltas = _price_deltas(sim, fills, high_ticker, low_ticker)
    if not deltas:
        # PHASE-3 REVIEW FIX (F3, Phase-2 handoff): both fired and fills.filled, but NO fills leg is
        # comparable to the sim's paired high/low leg prices (wrong ticker, legs=(), or the sim has
        # no leg price). Pre-fix this fell through to BIN_BOTH_MATCH with an EMPTY price_deltas — a
        # "the sim tells the truth" receipt backed by zero comparisons, the exact dishonest-
        # measurement direction the harness exists to prevent. Require >=1 comparable leg before any
        # bin-5 verdict; absent one is a data fault, not a match. neutrality_ok stays True (this is
        # not a fire/no-fire flip). The Phase-3 ledger.to_window_fills keys legs to the ACTUAL paired
        # tickers, so in practice this branch fires only on a genuine schema/pairing fault.
        return WindowParity(
            close_time, LABEL_UNCOMPARABLE, sim, live, neutrality_ok=True,
            source_match=source_match,
            detail={"cause": "both filled but no leg comparable to the sim's paired tickers — "
                             "cannot certify a price/payoff match (F3 guard)"},
        )
    any_diff = any(Decimal(v) != 0 for v in deltas.values())
    if any_diff:
        return WindowParity(
            close_time, BIN_BOTH_DIFF_PRICE, sim, live, neutrality_ok=True,
            source_match=source_match, detail={"price_deltas": deltas},
        )
    return WindowParity(
        close_time, BIN_BOTH_MATCH, sim, live, neutrality_ok=True,
        source_match=source_match,
        detail={"price_deltas": deltas, "realized_payoff": (str(fills.realized_payoff)
                if fills.realized_payoff is not None else None)},
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
@dataclass
class ParityWindowInput:
    close_time: str
    journal_records: list[dict]       # replayed journal records (Phase-1 format)
    state0: WindowState               # window meta seed for decide() (from quintile.py at wake)
    sim_rows: list[dict]              # tape_points rows for this close_time
    fills: WindowFills | None = None


def run_parity(inputs: list[ParityWindowInput], params: PolicyParams) -> dict:
    """Bin every window; return the aggregate report dict (bins, neutrality failures, passed)."""
    results: list[WindowParity] = []
    for w in inputs:
        sim = sim_entry_for_window(w.sim_rows, params)
        live = live_entry_for_window(w.journal_records, w.state0, params)
        wp = assign_bin(
            w.close_time, sim, live, w.fills, w.state0.high_ticker, w.state0.low_ticker
        )
        results.append(wp)

    bin_counts: dict[str, int] = {}
    for r in results:
        bin_counts[str(r.bin)] = bin_counts.get(str(r.bin), 0) + 1
    neutrality_failures = [r.close_time for r in results if not r.neutrality_ok]
    return {
        "n_windows": len(results),
        "bin_counts": bin_counts,
        "neutrality_failures": neutrality_failures,
        "n_neutrality_failures": len(neutrality_failures),
        "passed": len(neutrality_failures) == 0,
        "windows": [_window_to_dict(r) for r in results],
    }


def _entry_to_dict(e) -> dict:
    d = asdict(e)
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in d.items()}


def _window_to_dict(r: WindowParity) -> dict:
    return {
        "close_time": r.close_time,
        "bin": r.bin,
        "neutrality_ok": r.neutrality_ok,
        "source_match": r.source_match,
        "sim": _entry_to_dict(r.sim),
        "live": _entry_to_dict(r.live),
        "detail": r.detail,
    }


def render_text(report: dict) -> str:
    """Human-readable block summarizing the five-bin outcome."""
    L: list[str] = []
    L.append("# PARITY REPORT (five-bin)")
    L.append(f"windows: {report['n_windows']}")
    L.append(f"passed (F15 neutrality): {report['passed']}")
    L.append(f"neutrality failures: {report['n_neutrality_failures']}")
    L.append("")
    L.append("bin counts:")
    bin_names = {
        "1": "sim-only (bin 1)",
        "2": "live-only (bin 2)",
        "3": "both/no-fill (bin 3)",
        "4": "both/diff-price (bin 4)",
        "5": "both/match (bin 5)",
        LABEL_NO_SIGNAL: "no-signal-both",
        LABEL_BOTH_FIRED: "both-fired (pending fills)",
        LABEL_IMBALANCE: "imbalance",
        LABEL_UNCOMPARABLE: "both-filled-uncomparable (F3 guard)",
    }
    for k, v in sorted(report["bin_counts"].items()):
        L.append(f"  {bin_names.get(k, k)}: {v}")
    L.append("")
    if report["neutrality_failures"]:
        L.append("NEUTRALITY FAILURES (a live/sim fire/no-fire flip — F15):")
        for w in report["windows"]:
            if not w["neutrality_ok"]:
                L.append(f"  {w['close_time']}: bin {w['bin']} — {w['detail'].get('cause','')}")
    else:
        L.append("No fire/no-fire flips: every window agrees live vs sim (F15 neutrality holds).")
    return "\n".join(L) + "\n"
