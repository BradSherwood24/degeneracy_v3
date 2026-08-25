"""Rung 1.5 — the tape sim (commissioned 2026-08-19, Brad's order).

Brad's words: "lets just build out a sim that compares time prices of both markets. See
how many points where C would be lower than EV within those 15 minutes."

For every census-eligible hour window T in the input range, walk the EXECUTED-TRADES tape
through the final 15 minutes (T-900s .. T) and measure, moment by moment, what a taker
actually paid for the corridor pair (C), then count when C was below the pair's expected
value (EV+ = fair - C > 0).

LAW — this module FORKS NOTHING. It imports the frozen Rung 1 law wholesale:
  * pairing / sigma-hat (A3.2) / exclusion taxonomy / outcomes  <- census.build_census
  * per-leg fee (audited formula)                               <- census.fee
  * G                                                           <- census.hole_G
  * G/sigma quintile edges + bucket assignment                 <- gate_fit.quintile_edges,
                                                                   gate_fit.bucket_of,
                                                                   gate_fit.read_ok_rows
  * the SEAL (17 sealed UTC days) + trade-tape load            <- loader.load_trades / SealError

Eligibility: a window is census-eligible iff build_census assigns it status OK or NO_PAIR
(both have a valid pair, non-degenerate G, a valid sigma-hat, and a consistent pin/escape
outcome). NO_PAIR windows are excluded ONLY on candle-quoting at T-5 (an economics test on
the candle series); the tape sim prices from TRADES, not candles, so those windows are kept
and priced from their own tape. EXCL_* windows (no anchor / no leg / degenerate G / missing
sigma tape / missing print) carry no valid pair or outcome and are dropped, receipted by
build_census's own inventory.

Honest-fills law (commission "The price series"):
  * We buy YES on the HIGH line and NO on the LOW line.
  * At time t a leg's achievable price is the price paid by the MOST RECENT TAKER ON OUR
    SIDE of that leg: for the YES leg, the most recent H-market trade with
    taker_outcome_side == "yes" (its yes_price_dollars); for the NO leg, the most recent
    L-market trade with taker_outcome_side == "no" (its no_price_dollars). No quote model,
    no bid/complement inference. A "no" fill can NEVER set the YES-leg price and vice-versa.
  * Staleness: a leg's price is live for STALENESS_S (default 60s) after its fill; a moment
    counts only when BOTH legs are live.
  * C(t) = leg1 + fee(leg1) + leg2 + fee(leg2).

EV:
  * fair_bucket = 1 - P_hat(pin | quintile of this window's G/sigma), the per-quintile pin
    rate from census_train.csv OK rows (edges + rates reproduce gate.json exactly).
  * fair_lin = 1 - 0.006 * G (the 0.6pp/$ census law).  EV = fair - C.

See sim/ceremony/rung15_build_report.md CONFESSIONS for every judgment call.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import os
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from loader import (
    SealError,           # noqa: F401  (re-exported for callers/tests)
    close_epoch,
    data_path,
    load_markets,
    load_trades,
    parse_created_epoch,
    sha256_file,
)
from census import IntegrityError, build_census, fee, hole_G
from gate_fit import bucket_of, quintile_edges, read_ok_rows

# ---- Frozen constants ------------------------------------------------------
STALENESS_S_DEFAULT = 60          # commission default; overridable via --staleness
WINDOW_S = 900                    # final 15 minutes
LIN_SLOPE = Decimal("0.006")      # 0.6pp/$ census law -> fair_lin = 1 - 0.006*G
N_MIN_BUCKETS = 15                # per-minute histogram of the last 15 minutes
DIRECTIONS = ("strangle", "flip")  # A15.4: strangle = buy outsides; flip = buy insides
HARD_FLOOR = Decimal("1")         # A15.4: C_flip < $1 is a riskless entry (flip pays >= $1)
COMPLEMENT_TOL = Decimal("0.0001")  # A15.10 F2: yes+no must equal 1 within this tolerance

# The census EV curve is drawn ONLY from this exact frozen artifact (A3.10 spirit):
CENSUS_TRAIN_SHA256 = (
    "580d143fa5d3581a8bbee9d5cc2b45f800d25fa84db7967c3cf591a8ac7bb247"
)

_OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
_DEFAULT_CENSUS = os.path.join(_OUT_DIR, "census_train.csv")


# ---------------------------------------------------------------------------
# Pure primitives
# ---------------------------------------------------------------------------
# parse_ts kept as a thin alias so callers/tests referencing tape_sim.parse_ts still work;
# the single implementation lives in loader (A15.1: parse epoch, never the string).
parse_ts = parse_created_epoch


def cost_C(leg_a: Decimal, leg_b: Decimal) -> Decimal:
    """C = leg_a + fee(leg_a) + leg_b + fee(leg_b), each leg on its own traded price.
    Reuses the audited census.fee per leg. Direction-agnostic: for the strangle the two
    legs are YES-on-H and NO-on-L; for the flip they are NO-on-H and YES-on-L."""
    return leg_a + fee(leg_a) + leg_b + fee(leg_b)


def fair_linear(G: float, direction: str = "strangle") -> Decimal:
    """Linear-law fair value. Strangle: fair_lin = 1 - 0.006*G. Flip: fair_lin = 1 + 0.006*G
    (A15.4). The 0.006 is the 0.6pp-per-$ census law."""
    base = LIN_SLOPE * Decimal(str(G))
    return (Decimal(1) - base) if direction == "strangle" else (Decimal(1) + base)


def flip_settlement_payout(h_result: str, l_result: str) -> Decimal:
    """A15.4 payoff-floor verification, from the SAME result fields census uses.

    The flip buys NO on the HIGH line and YES on the LOW line:
      * NO-on-H pays $1 iff the H market settled ``no`` (print did NOT clear the high line);
      * YES-on-L pays $1 iff the L market settled ``yes`` (print DID clear the low line).
    Total payout is $2 on a pin (H:no AND L:yes -> both flip legs pay) and $1 on an escape
    (exactly one flip leg pays), in BOTH orientations and under BOTH settlement rules. The
    $0 case would require H:yes AND L:no (print above H and below L) — the impossible branch
    census's ``classify_outcome`` rejects — so it is a hard fail here too."""
    high_pays = Decimal(1) if h_result == "no" else Decimal(0)    # NO-on-H
    low_pays = Decimal(1) if l_result == "yes" else Decimal(0)    # YES-on-L
    payout = high_pays + low_pays
    if payout == 0:
        raise IntegrityError(
            f"flip payout $0 (H:{h_result}/L:{l_result}) — impossible corridor "
            f"(print above H and below L)"
        )
    return payout


class EVCurve:
    """The bucket-fair EV curve: per-G/sigma-quintile pin rate from census_train.csv.

    Verifies the census sha before use and refuses on mismatch (commission law). The
    quintile edges and per-bucket pin rates reproduce gate.json exactly (both derive from
    gate_fit over the same OK rows).
    """

    def __init__(self, edges: List[float], pin_rate: List[float],
                 bucket_n: List[int], census_sha: str):
        self.edges = edges
        self.pin_rate = pin_rate
        self.fair = [Decimal(1) - Decimal(str(pr)) for pr in pin_rate]        # strangle
        self.fair_flip = [Decimal(1) + Decimal(str(pr)) for pr in pin_rate]  # A15.4 flip
        self.bucket_n = bucket_n
        self.census_sha = census_sha

    def fair_for(self, direction: str, quintile: int) -> Decimal:
        return self.fair[quintile] if direction == "strangle" else self.fair_flip[quintile]

    @classmethod
    def from_census(cls, census_csv: str,
                    expected_sha: str = CENSUS_TRAIN_SHA256) -> "EVCurve":
        if not os.path.exists(census_csv):
            raise IntegrityError(f"census csv not found: {census_csv}")
        sha = sha256_file(census_csv)
        if sha != expected_sha:
            raise IntegrityError(
                f"census sha mismatch for {census_csv}: got {sha}, "
                f"expected {expected_sha} (refusing to build the EV curve on an "
                f"unverified census)"
            )
        rows = read_ok_rows(census_csv)
        if not rows:
            raise IntegrityError("census has no OK rows; cannot build EV curve")
        gos = [r["gos"] for r in rows]
        edges = quintile_edges(gos)
        n_buckets = len(edges) + 1
        pins = [0] * n_buckets
        ns = [0] * n_buckets
        for r in rows:
            b = bucket_of(r["gos"], edges)
            ns[b] += 1
            if r["pin"]:
                pins[b] += 1
        if any(n == 0 for n in ns):
            raise IntegrityError(f"empty G/sigma quintile bucket(s): {ns}")
        pin_rate = [pins[b] / ns[b] for b in range(n_buckets)]
        return cls(edges, pin_rate, ns, sha)

    def assign(self, gos: float) -> int:
        return bucket_of(gos, self.edges)


# ---------------------------------------------------------------------------
# The tape walk (pure; the heart of the sim)
# ---------------------------------------------------------------------------
def _side_fills(trades: List[dict], want_side: str, price_field: str,
                ) -> List[Tuple[float, Decimal]]:
    """Extract (epoch, Decimal price) for the SAME-side fills where taker_outcome_side ==
    want_side (the carried-buy source). Kept as a primitive/test hook. The refutation law
    (A15.9) needs opposite-side prints too — see ``_leg_stream``.
    """
    out = []
    for t in trades:
        if t.get("taker_outcome_side") != want_side:
            continue
        out.append((parse_created_epoch(t["created_time"]), Decimal(str(t[price_field]))))
    return out


def _leg_stream(trades: List[dict], side_S: str, field_S: str, field_opp: str,
                ) -> List[Tuple[float, str, Decimal]]:
    """Build ONE leg's full event stream for the A15.9 cross-side book.

    We buy side S on this leg's single market. Every trade on that market prints at the
    touch of one side, so it is evidence about our side S:
      * a SAME-side taker (taker_outcome_side == S) SETS our carried buy price = field_S
        (the price actually paid on our side);
      * an OPPOSITE-side taker proves the OTHER side's ask, i.e. our side's BID =
        1 - opposite_price (= field_S under verified exact complementarity, but computed as
        1 - field_opp to mirror the law's wording). It can only REFUTE, never create.
    Returns (epoch, kind in {"SAME","OPP"}, price_S) where price_S is our carried buy price
    for a SAME print and the implied bid(S) for an OPP print. HONEST-FILLS GUARD: a carried
    BUY price is only ever a SAME print; OPP prints never become a buy price.
    """
    out = []
    for t in trades:
        ts = parse_created_epoch(t["created_time"])
        # A15.10 F2 hardening: the two sides must be exact complements. A violation would
        # make the implied bid (1 - opposite) wrong and silently mis-refute -> hard fail.
        y = Decimal(str(t["yes_price_dollars"]))
        n = Decimal(str(t["no_price_dollars"]))
        if abs(y + n - Decimal(1)) > COMPLEMENT_TOL:
            raise IntegrityError(
                f"non-complementary trade {t.get('trade_id')}: yes={y} no={n} "
                f"(sum {y + n} != 1)"
            )
        if t.get("taker_outcome_side") == side_S:
            out.append((ts, "SAME", Decimal(str(t[field_S]))))
        else:
            out.append((ts, "OPP", Decimal(1) - Decimal(str(t[field_opp]))))
    return out


def _walk_direction(T: int, high_stream: List[Tuple[float, str, Decimal]],
                    low_stream: List[Tuple[float, str, Decimal]], fair: Decimal,
                    fair_lin: Decimal, settle_amount: Decimal,
                    staleness_s: int = STALENESS_S_DEFAULT,
                    refute: bool = True) -> Tuple[List[dict], dict]:
    """Walk one window for ONE direction. Direction-agnostic core shared by strangle & flip.

    ``high_stream`` / ``low_stream`` are each a chronological stream of (epoch, kind, price)
    where kind is "SAME" (a fill on the side we buy — sets the carried buy price) or "OPP"
    (an opposite-side print on the same book — carries the implied bid). An EVALUATION EVENT
    is a SAME fill in [T-900, T] on either leg while the OTHER leg is LIVE.

    A15.9 cross-side refutation (when ``refute``): a leg's carried buy price p_S dies at the
    FIRST later OPP print whose implied bid >= p_S (ask >= bid, so the p_S ask cannot still
    stand) — refute-only, no tick assumption (>=, exact). A refuted leg is NOT live until its
    next SAME fill. When ``refute`` is False, OPP prints are dropped entirely (the pre-A15.9
    behavior, used only for the counterfactual comparison).

    Per event: C = leg + fee + leg + fee; ev = fair - C; ev_lin = fair_lin - C; payoff =
    settle_amount - C; hard_floor = C < $1. Dwell carries a moment forward until the next
    fill (any kind, so a refuting OPP truncates it), or until a live leg's staleness expires.
    """
    merged: List[Tuple[float, str, str, Decimal]] = (
        [(ts, "HIGH", kind, p) for ts, kind, p in high_stream]
        + [(ts, "LOW", kind, p) for ts, kind, p in low_stream]
    )
    if not refute:
        merged = [m for m in merged if m[2] == "SAME"]
    # A15.1: parsed-epoch order. Tie-break: at an equal timestamp process OPP before SAME so
    # a same-instant SAME fill is the SURVIVING evidence (its fresh carried price is not
    # refuted by a co-timestamp opposite print). (confessed tie-break)
    merged.sort(key=lambda x: (x[0], 0 if x[2] == "OPP" else 1))
    merged = [m for m in merged if m[0] <= T]       # nothing after settlement

    carried: Dict[str, Optional[Tuple[float, Decimal]]] = {"HIGH": None, "LOW": None}
    refuted: Dict[str, bool] = {"HIGH": False, "LOW": False}
    events: List[dict] = []

    def _live(leg: str, now: float) -> bool:
        c = carried[leg]
        return (c is not None and not refuted[leg] and (now - c[0]) <= staleness_s)

    for i, (ts, leg, kind, price) in enumerate(merged):
        if kind == "OPP":
            # refute-only: an opposite print never sets or extends a carried price.
            c = carried[leg]
            if c is not None and not refuted[leg] and (ts - c[0]) <= staleness_s \
                    and price >= c[1]:
                refuted[leg] = True
            continue

        # kind == SAME: sets a fresh carried buy price (clears any prior refutation).
        carried[leg] = (ts, price)
        refuted[leg] = False

        if ts < T - WINDOW_S:                       # warmup fills seed prices, no event
            continue

        other = "LOW" if leg == "HIGH" else "HIGH"
        if not _live(other, ts):
            continue                                 # only one leg live -> not a moment

        hts, hp = carried["HIGH"]    # type: ignore[misc]
        lts, lp = carried["LOW"]     # type: ignore[misc]
        C = cost_C(hp, lp)
        ev = fair - C
        ev_lin = fair_lin - C
        payoff = settle_amount - C
        hard_floor = C < HARD_FLOOR

        expiry = min(hts, lts) + staleness_s
        # A15.10: dwell keys on STATE CHANGES only. The interval ends at the first of: the
        # next SAME fill on either leg (new carried state), the first REFUTING opposite print
        # against THIS event's carried prices (hp/lp are fixed until the next SAME), staleness
        # expiry, or T. CONSISTENT (non-refuting) opposite prints are TRANSPARENT — they are
        # skipped here (they change nothing; keying on them wrongly deleted dwell). Scanning
        # only the OPPs of this inter-SAME interval keeps this O(total fills) overall.
        boundary_ts = float(T)
        for jts, jleg, jkind, jprice in merged[i + 1:]:
            if jkind == "SAME":
                boundary_ts = jts                     # any SAME = new state
                break
            # jkind == "OPP": refuting iff its implied bid >= the carried price it hits
            carried_p = hp if jleg == "HIGH" else lp
            if jprice >= carried_p:
                boundary_ts = jts                     # first refuting opposite print
                break
            # else: consistent opposite print -> transparent, keep scanning
        dwell = min(boundary_ts, expiry, float(T)) - ts
        if dwell < 0:
            dwell = 0.0

        events.append({
            "t_minus_s": T - ts,
            "fill_side": leg,
            "high_leg_price": hp,
            "low_leg_price": lp,
            "high_leg_age_s": ts - hts,
            "low_leg_age_s": ts - lts,
            "C": C,
            "fair": fair,
            "fair_lin": fair_lin,
            "ev": ev,
            "ev_lin": ev_lin,
            "payoff": payoff,
            "hard_floor": hard_floor,
            "dwell_s": dwell,
        })

    ev_plus = [e for e in events if e["ev"] > 0]
    hardfloor = [e for e in events if e["hard_floor"]]
    first_evplus = ev_plus[0] if ev_plus else None
    first_hardfloor = hardfloor[0] if hardfloor else None
    summary = {
        "n_events": len(events),
        "n_ev_plus": len(ev_plus),
        "ev_plus_seconds": float(sum(e["dwell_s"] for e in ev_plus)),
        "first_evplus_payoff": (first_evplus["payoff"] if first_evplus else None),
        "first_evplus_t_minus_s": (first_evplus["t_minus_s"] if first_evplus else None),
        "n_hardfloor": len(hardfloor),
        "hardfloor_seconds": float(sum(e["dwell_s"] for e in hardfloor)),
        "first_hardfloor_payoff": (first_hardfloor["payoff"] if first_hardfloor else None),
        "first_hardfloor_t_minus_s": (first_hardfloor["t_minus_s"]
                                      if first_hardfloor else None),
        # A15.5: C at the first hard-floor moment. The GUARANTEED (riskless) floor of
        # entering there is (1 - C); the pin bonus (a further +1) is NOT guaranteed.
        "first_hardfloor_C": (first_hardfloor["C"] if first_hardfloor else None),
        "min_C": (min(e["C"] for e in events) if events else None),
    }
    return events, summary


def walk_window(T: int, yes_trades: List[dict], no_trades: List[dict],
                fair_bucket: Decimal, G: float, pin: bool, quintile: int,
                staleness_s: int = STALENESS_S_DEFAULT) -> Tuple[List[dict], dict]:
    """STRANGLE walk (buy YES on the HIGH line, NO on the LOW line). Thin wrapper over the
    direction-agnostic core, kept for the reviewed strangle contract: ``yes_trades`` are the
    H-market trades, ``no_trades`` the L-market trades; events expose ``yes_price``/
    ``no_price``/``yes_age_s``/``no_age_s``/``ev_bucket`` aliases alongside the generic keys.
    Includes A15.9 cross-side refutation (``refute=True``) via the leg streams."""
    high_stream, low_stream = _direction_legs("strangle", yes_trades, no_trades)
    settle = Decimal(0) if pin else Decimal(1)                         # strangle settlement
    events, summary = _walk_direction(
        T, high_stream, low_stream, fair_bucket, fair_linear(G, "strangle"),
        settle, staleness_s)
    out = []
    for e in events:
        out.append({**e,
                    "yes_price": e["high_leg_price"], "no_price": e["low_leg_price"],
                    "yes_age_s": e["high_leg_age_s"], "no_age_s": e["low_leg_age_s"],
                    "fair_bucket": e["fair"], "ev_bucket": e["ev"], "quintile": quintile})
    summary["pin"] = pin
    summary["quintile"] = quintile
    return out, summary


# ---------------------------------------------------------------------------
# Pairing: reconstruct the two leg tickers from census's OWN chosen A / K
# ---------------------------------------------------------------------------
def _round2(x: float) -> float:
    return round(x, 2)


def build_ticker_map(dates: List[str], acknowledge_sealed_read: bool = False,
                     data_root: Optional[str] = None):
    """Map each top-of-hour close_time -> {15M market, list of 1H markets} using the SAME
    load path census uses. tape_sim never re-runs the nearest-strike LAW: it matches the
    1H leg by census's already-chosen threshold K (row['threshold_K']), so pairing cannot
    diverge. Returns (m15_by_ct, h1_by_ct, shas)."""
    kw = {} if data_root is None else {"data_root": data_root}
    m15, sh_m15 = load_markets("15-minute", dates, acknowledge_sealed_read, **kw)
    m1h, sh_m1h = load_markets("1-hour", dates, acknowledge_sealed_read, **kw)
    dateset = set(dates)
    m15_by_ct: Dict[str, dict] = {}
    for r in m15:
        ct = r["close_time"]
        existing = m15_by_ct.get(ct)
        # mirror census: prefer the strike-bearing market if a strike-less one collides.
        if existing is None:
            m15_by_ct[ct] = r
        elif existing.get("floor_strike") is None and r.get("floor_strike") is not None:
            m15_by_ct[ct] = r
    h1_by_ct: Dict[str, List[dict]] = {}
    for r in m1h:
        if r["_assigned_day"] in dateset:
            h1_by_ct.setdefault(r["close_time"], []).append(r)
    return m15_by_ct, h1_by_ct, {**sh_m15, **sh_m1h}


def legs_for_row(row: dict, m15_by_ct: Dict[str, dict], h1_by_ct: Dict[str, List[dict]]):
    """Return (h_series, h_ticker, l_series, l_ticker) for an eligible census row.

    Cross-checks A and K reconstructed here against census's row values with zero tolerance
    (hard fail on any drift) so the tape can never be walked against a mis-paired leg.
    """
    ct = row["close_time"]
    A = float(row["anchor_A"])
    K = float(row["threshold_K"])
    m15 = m15_by_ct.get(ct)
    if m15 is None:
        raise IntegrityError(f"eligible row {ct} has no 15M market in ticker map")
    if _round2(float(m15["floor_strike"])) != _round2(A):
        raise IntegrityError(
            f"anchor drift at {ct}: census A={A} vs 15M floor_strike={m15['floor_strike']}"
        )
    cands = [r for r in h1_by_ct.get(ct, [])
             if _round2(float(r["floor_strike"])) == _round2(K)]
    if len(cands) != 1:
        raise IntegrityError(
            f"1H leg drift at {ct}: expected exactly one 1H market with K={K}, "
            f"found {len(cands)}"
        )
    h1 = cands[0]
    if row["orientation"] == "A_above_K":
        return "15-minute", m15["ticker"], "1-hour", h1["ticker"]
    if row["orientation"] == "K_above_A":
        return "1-hour", h1["ticker"], "15-minute", m15["ticker"]
    raise IntegrityError(f"unexpected orientation {row['orientation']!r} at {ct}")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
# A15.4: one tape_points.csv with a `direction` column (strangle|flip). Leg columns are
# generic (high/low) because the two legs mean different sides per direction:
#   strangle high_leg = YES-on-H, low_leg = NO-on-L
#   flip     high_leg = NO-on-H,  low_leg = YES-on-L
TAPE_FIELDNAMES = [
    "date", "close_time", "direction", "t_minus_s", "fill_side", "G", "sigma_hat",
    "g_over_sigma", "quintile", "high_leg_price", "low_leg_price", "high_leg_age_s",
    "low_leg_age_s", "C", "fair", "fair_lin", "ev", "ev_lin", "hard_floor", "pin",
    "payoff", "dwell_s",
]


def _csv_event_row(direction: str, e: dict, date: str, close_time: str,
                   G: float, sig: float, gos: float, quintile: int, pin: bool) -> dict:
    """Assemble one tape_points.csv row. A15.8: the ``hard_floor`` column is populated only
    for FLIP rows — a sub-$1 strangle is NOT riskless (a pin still loses -C), so strangle
    rows leave ``hard_floor`` BLANK."""
    return {
        "date": date, "close_time": close_time, "direction": direction,
        "t_minus_s": f"{e['t_minus_s']:.6f}", "fill_side": e["fill_side"],
        "G": f"{G:.2f}", "sigma_hat": f"{sig:.6f}", "g_over_sigma": f"{gos:.6f}",
        "quintile": quintile,
        "high_leg_price": f"{e['high_leg_price']:.4f}",
        "low_leg_price": f"{e['low_leg_price']:.4f}",
        "high_leg_age_s": f"{e['high_leg_age_s']:.3f}",
        "low_leg_age_s": f"{e['low_leg_age_s']:.3f}",
        "C": f"{e['C']:.4f}", "fair": f"{e['fair']:.6f}",
        "fair_lin": f"{e['fair_lin']:.6f}", "ev": f"{e['ev']:.6f}",
        "ev_lin": f"{e['ev_lin']:.6f}",
        "hard_floor": ("1" if e["hard_floor"] else "0") if direction == "flip" else "",
        "pin": "1" if pin else "0",
        "payoff": f"{e['payoff']:.4f}", "dwell_s": f"{e['dwell_s']:.3f}",
    }


def _direction_legs(direction: str, h_trades: List[dict], l_trades: List[dict]):
    """Build each leg's A15.9 event stream (SAME + OPP prints) for one direction. This is the
    honest-fills choke-point: a carried BUY price only ever comes from a SAME print; OPP
    prints (the opposite side of the same book) carry the implied bid and can only refute."""
    if direction == "strangle":       # buy YES on HIGH line, NO on LOW line
        high = _leg_stream(h_trades, "yes", "yes_price_dollars", "no_price_dollars")
        low = _leg_stream(l_trades, "no", "no_price_dollars", "yes_price_dollars")
    else:                             # flip: buy NO on HIGH line, YES on LOW line
        high = _leg_stream(h_trades, "no", "no_price_dollars", "yes_price_dollars")
        low = _leg_stream(l_trades, "yes", "yes_price_dollars", "no_price_dollars")
    return high, low


def run(dates: List[str], out_dir: str, staleness_s: int = STALENESS_S_DEFAULT,
        census_csv: str = _DEFAULT_CENSUS, directions: Tuple[str, ...] = DIRECTIONS,
        compare_no_refute: bool = False,
        acknowledge_sealed_read: bool = False, data_root: Optional[str] = None) -> dict:
    """Build the tape points + report + receipt over ``dates`` for the requested
    ``directions``. The primary pass applies A15.9 cross-side refutation (the law). When
    ``compare_no_refute`` is set, a second counterfactual pass with refutation OFF is run and
    a before/after comparison is emitted (used for the smoke; off by default so the week run
    stays a single pass). Returns the aggregate dict (also printed to stdout by main)."""
    ev_curve = EVCurve.from_census(census_csv)

    # census is the authority on eligibility + pair + sigma-hat + outcome (no fork).
    kw = {} if data_root is None else {"data_root": data_root}
    rows, census_receipt = build_census(dates, acknowledge_sealed_read, **kw)
    eligible = [r for r in rows if r["status"] in ("OK", "NO_PAIR")]
    n_no_pair = sum(1 for r in eligible if r["status"] == "NO_PAIR")   # A15.3

    m15_by_ct, h1_by_ct, mkt_shas = build_ticker_map(
        dates, acknowledge_sealed_read, data_root=data_root)

    # A15.2: pre-check trades-file existence per day; genuinely missing days are recorded
    # and skipped, and the run continues (no unreachable path, no hard crash on missing).
    def _exists(series: str, d: str) -> bool:
        return os.path.exists(data_path(series, "trades", d, **kw))

    present_15 = [d for d in dates if _exists("15-minute", d)]
    present_1h = [d for d in dates if _exists("1-hour", d)]
    missing_trade_days = sorted({d for d in dates
                                 if d not in present_15 or d not in present_1h})

    tr15, sh_tr15 = load_trades("15-minute", present_15, acknowledge_sealed_read, **kw)
    tr1h, sh_tr1h = load_trades("1-hour", present_1h, acknowledge_sealed_read, **kw)
    trade_shas = {**sh_tr15, **sh_tr1h}

    def tape(series: str, ticker: str) -> List[dict]:
        return (tr15 if series == "15-minute" else tr1h).get(ticker, [])

    def do_walks(refute: bool):
        """One full pass over eligible windows x directions. Returns (all_events, summaries).
        ``refute`` toggles A15.9 cross-side refutation."""
        all_events: List[dict] = []
        summaries: Dict[str, List[dict]] = {d: [] for d in directions}
        for row in eligible:
            ct = row["close_time"]
            T = close_epoch(ct)
            G = float(row["G"])
            sig = float(row["sigma_hat"])
            gos = float(row["g_over_sigma"])
            pin = row["pin_escape"] == "PIN"
            quintile = ev_curve.assign(gos)

            h_series, h_ticker, l_series, l_ticker = legs_for_row(row, m15_by_ct, h1_by_ct)
            h_trades = tape(h_series, h_ticker)     # the HIGH-line market
            l_trades = tape(l_series, l_ticker)     # the LOW-line market

            # A15.4: verify the flip payoff floor in code from the SAME result fields census
            # uses — $2 on pin, $1 on escape — for THIS window's actual orientation/outcome.
            flip_payout = flip_settlement_payout(row["H_result"], row["L_result"])
            if flip_payout != (Decimal(2) if pin else Decimal(1)):
                raise IntegrityError(
                    f"flip settlement mismatch at {ct}: payout {flip_payout} but "
                    f"{'pin' if pin else 'escape'}"
                )

            for direction in directions:
                high, low = _direction_legs(direction, h_trades, l_trades)
                fair = ev_curve.fair_for(direction, quintile)
                fl = fair_linear(G, direction)
                if direction == "strangle":
                    settle = Decimal(0) if pin else Decimal(1)
                else:
                    settle = flip_payout      # 2 on pin, 1 on escape (verified above)
                events, summary = _walk_direction(
                    T, high, low, fair, fl, settle, staleness_s, refute=refute)
                summary.update({"date": row["date"], "close_time": ct, "pin": pin,
                                "quintile": quintile, "direction": direction})
                summaries[direction].append(summary)
                for e in events:
                    all_events.append(_csv_event_row(
                        direction, e, row["date"], ct, G, sig, gos, quintile, pin))
        return all_events, summaries

    # primary pass = the LAW (A15.9 refutation ON)
    all_events, summaries = do_walks(refute=True)
    agg = _aggregate(summaries, all_events, ev_curve, staleness_s, dates, directions,
                     len(eligible), n_no_pair, len(rows), missing_trade_days)

    # optional counterfactual pass (refutation OFF) for the before/after comparison
    compare = None
    if compare_no_refute:
        base_events, base_summaries = do_walks(refute=False)
        base_agg = _aggregate(base_summaries, base_events, ev_curve, staleness_s, dates,
                              directions, len(eligible), n_no_pair, len(rows),
                              missing_trade_days)
        compare = {d: _refute_delta(base_agg["directions_agg"][d],
                                    agg["directions_agg"][d]) for d in directions}
    agg["refute_compare"] = compare

    # ---- write outputs ----
    os.makedirs(out_dir, exist_ok=True)
    points_path = os.path.join(out_dir, "tape_points.csv")
    report_path = os.path.join(out_dir, "tape_report.md")
    receipt_path = os.path.join(out_dir, "tape_receipt.json")

    with open(points_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TAPE_FIELDNAMES)
        w.writeheader()
        for e in all_events:
            w.writerow(e)

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(render_report(agg))

    receipt = {
        "dates": list(dates),
        "directions": list(directions),
        "staleness_s": staleness_s,
        "window_s": WINDOW_S,
        "cross_side_refutation_A15_9": True,
        "refute_compare_vs_pre_A15_9": compare,
        "census_csv": os.path.basename(census_csv),
        "census_csv_sha256": ev_curve.census_sha,
        "quintile_edges_gos": ev_curve.edges,
        "quintile_pin_rate": ev_curve.pin_rate,
        "quintile_bucket_n_train": ev_curve.bucket_n,
        "n_hours_total": len(rows),
        "n_eligible_windows": len(eligible),
        "n_eligible_no_pair": n_no_pair,
        "n_evaluation_events": len(all_events),
        "per_direction": {d: {
            "n_windows_with_events": agg["directions_agg"][d]["n_windows_with_events"],
            "n_windows_with_evplus": agg["directions_agg"][d]["n_windows_with_evplus"],
            "n_evaluation_events": agg["directions_agg"][d]["n_evaluation_events"],
            "n_evplus_events": agg["directions_agg"][d]["n_evplus_events"],
            "n_hardfloor_events": agg["directions_agg"][d]["n_hardfloor_events"],
            **({"hardfloor_policy": {
                k: agg["directions_agg"][d]["policy_first_hardfloor"][k]
                for k in ("n_entered", "guaranteed_floor_total", "realized_total",
                          "n_sub_tick_floors")
            }} if d == "flip" else {}),
        } for d in directions},
        "missing_trade_days": missing_trade_days,
        "input_file_sha256": {
            **{os.path.relpath(p, os.path.dirname(_OUT_DIR)): s
               for p, s in sorted(mkt_shas.items())},
            **{os.path.relpath(p, os.path.dirname(_OUT_DIR)): s
               for p, s in sorted(trade_shas.items())},
        },
        "census_input_file_sha256": census_receipt.get("input_file_sha256", {}),
        "census_status_counts": census_receipt.get("status_counts", {}),
    }
    with open(receipt_path, "w", encoding="utf-8") as f:
        json.dump(receipt, f, indent=2, sort_keys=True)

    agg["_paths"] = {"points": points_path, "report": report_path, "receipt": receipt_path}
    return agg


def _aggregate_direction(direction, window_summaries, all_events, ev_curve) -> dict:
    evs = [e for e in all_events if e["direction"] == direction]
    n_buckets = len(ev_curve.edges) + 1

    n_windows_with_events = sum(1 for w in window_summaries if w["n_events"] > 0)
    n_windows_with_evplus = sum(1 for w in window_summaries if w["n_ev_plus"] > 0)
    n_windows_with_hardfloor = sum(1 for w in window_summaries if w["n_hardfloor"] > 0)
    n_evplus_events = sum(w["n_ev_plus"] for w in window_summaries)
    n_hardfloor_events = sum(w["n_hardfloor"] for w in window_summaries)
    total_evplus_seconds = float(sum(w["ev_plus_seconds"] for w in window_summaries))
    total_hardfloor_seconds = float(sum(w["hardfloor_seconds"] for w in window_summaries))

    # per-minute histogram of EV+ events (minute 0 = final minute before close). A15.3:
    # these are event counts (trade-multiplicity-weighted); dwell-seconds is time-faithful.
    per_minute = [0] * N_MIN_BUCKETS
    for e in evs:
        if Decimal(e["ev"]) > 0:
            m = int(float(e["t_minus_s"])) // 60
            if 0 <= m < N_MIN_BUCKETS:
                per_minute[m] += 1

    by_q = []
    for q in range(n_buckets):
        ws = [w for w in window_summaries if w["quintile"] == q]
        q_evs = [e for e in evs if int(e["quintile"]) == q]
        q_evplus = [e for e in q_evs if Decimal(e["ev"]) > 0]
        q_hard = [e for e in q_evs if e["hard_floor"] == "1"]
        cs = [Decimal(e["C"]) for e in q_evs]
        by_q.append({
            "quintile": q,
            "n_windows": len(ws),
            "n_events": len(q_evs),
            "n_evplus": len(q_evplus),
            "n_hardfloor": len(q_hard),
            "evplus_seconds": float(sum(w["ev_plus_seconds"] for w in ws)),
            "mean_C": (float(sum(cs) / len(cs)) if cs else None),
            "pin_rate_train": ev_curve.pin_rate[q],
            "fair_train": float(ev_curve.fair_for(direction, q)),
        })

    def _policy(payoff_key):
        entered = [w for w in window_summaries if w[payoff_key] is not None]
        payoffs = [w[payoff_key] for w in entered]
        total = float(sum(payoffs)) if payoffs else 0.0
        pins = sum(1 for w in entered if w["pin"])
        return {
            "n_entered": len(entered),
            "n_pins": pins,
            "n_escapes": len(entered) - pins,
            "total_payoff": total,
            "mean_payoff": (total / len(payoffs)) if payoffs else None,
        }

    out = {
        "direction": direction,
        "n_windows_with_events": n_windows_with_events,
        "n_windows_with_evplus": n_windows_with_evplus,
        "n_windows_with_hardfloor": n_windows_with_hardfloor,
        "n_evaluation_events": len(evs),
        "n_evplus_events": n_evplus_events,
        "n_hardfloor_events": n_hardfloor_events,
        "total_evplus_seconds": total_evplus_seconds,
        "total_hardfloor_seconds": total_hardfloor_seconds,
        "per_minute_evplus": per_minute,
        "by_quintile": by_q,
        "policy_first_evplus": _policy("first_evplus_payoff"),
    }
    if direction == "flip":
        out["policy_first_hardfloor"] = _hardfloor_policy(window_summaries)
    return out


# A15.6: fixed buckets for the guaranteed-floor distribution. First bucket is the SUB-TICK
# band (< $0.01) — a floor smaller than one price tick, which the review flagged as the bulk.
FLOOR_BUCKETS = (
    ("<$0.01 (sub-tick)", Decimal("0"), Decimal("0.01")),
    ("$0.01-$0.05", Decimal("0.01"), Decimal("0.05")),
    ("$0.05-$0.10", Decimal("0.05"), Decimal("0.10")),
    ("$0.10-$0.25", Decimal("0.10"), Decimal("0.25")),
    (">=$0.25", Decimal("0.25"), None),
)


def _floor_histogram(floors: List[Decimal]) -> List[Tuple[str, int]]:
    hist = []
    for label, lo, hi in FLOOR_BUCKETS:
        if hi is None:
            n = sum(1 for g in floors if g >= lo)
        else:
            n = sum(1 for g in floors if lo <= g < hi)
        hist.append((label, n))
    return hist


def _hardfloor_policy(window_summaries) -> dict:
    """A15.5/A15.6: 'enter at first hard-floor moment' decomposed.

    GUARANTEED floor = (1 - C) at the entry moment — the only RISKLESS component (the flip
    pays >= $1 in every branch, so 1 - C is banked regardless of pin/escape). REALIZED
    payoff = settle - C (adds the +$1 pin bonus, which is NOT guaranteed). The riskless label
    attaches ONLY to the guaranteed total. Also reports the per-window guaranteed-floor
    magnitudes, a sub-tick count (< $0.01), and a magnitude histogram."""
    entered = [w for w in window_summaries if w["first_hardfloor_payoff"] is not None]
    realized = [w["first_hardfloor_payoff"] for w in entered]
    guaranteed = [Decimal(1) - w["first_hardfloor_C"] for w in entered]
    pins = sum(1 for w in entered if w["pin"])
    floors_sorted = sorted(float(g) for g in guaranteed)
    return {
        "n_entered": len(entered),
        "n_pins": pins,
        "n_escapes": len(entered) - pins,
        "guaranteed_floor_total": float(sum(guaranteed)) if guaranteed else 0.0,
        "guaranteed_floor_mean": (float(sum(guaranteed) / len(guaranteed))
                                  if guaranteed else None),
        "realized_total": float(sum(realized)) if realized else 0.0,
        "realized_mean": (float(sum(realized) / len(realized)) if realized else None),
        "floor_magnitudes_sorted": floors_sorted,
        "n_sub_tick_floors": sum(1 for g in guaranteed if g < Decimal("0.01")),
        "floor_histogram": _floor_histogram(guaranteed),
    }


def _refute_delta(base: dict, law: dict) -> dict:
    """A15.9 before/after: ``base`` = refutation OFF (pre-A15.9), ``law`` = refutation ON.
    Reports survivor counts for evaluation events, EV+ events/dwell, hard-floor events/dwell."""
    def surv(after, before):
        return {"before": before, "after": after,
                "survived_pct": (100.0 * after / before) if before else None}
    return {
        "evaluation_events": surv(law["n_evaluation_events"], base["n_evaluation_events"]),
        "evplus_events": surv(law["n_evplus_events"], base["n_evplus_events"]),
        "evplus_dwell_seconds": surv(law["total_evplus_seconds"],
                                     base["total_evplus_seconds"]),
        "hardfloor_events": surv(law["n_hardfloor_events"], base["n_hardfloor_events"]),
        "hardfloor_dwell_seconds": surv(law["total_hardfloor_seconds"],
                                        base["total_hardfloor_seconds"]),
    }


def _aggregate(summaries, all_events, ev_curve, staleness_s, dates, directions,
               n_eligible, n_no_pair, n_hours_total, missing_trade_days) -> dict:
    return {
        "dates": list(dates),
        "direction_names": list(directions),
        "staleness_s": staleness_s,
        "n_hours_total": n_hours_total,
        "n_eligible_windows": n_eligible,
        "n_eligible_no_pair": n_no_pair,
        "n_evaluation_events": len(all_events),
        "missing_trade_days": missing_trade_days,
        "directions_agg": {d: _aggregate_direction(d, summaries[d], all_events, ev_curve)
                           for d in directions},
    }


def _fmt(x, nd=4):
    return "n/a" if x is None else f"{x:.{nd}f}"


def render_report(agg: dict) -> str:
    dirs = agg["directions_agg"]
    L = []
    L.append("# TAPE REPORT — Rung 1.5 (TRAIN)\n")
    L.append(f"Dates: {', '.join(agg['dates'])}  ")
    L.append(f"Directions: {', '.join(agg['direction_names'])}  ")
    L.append(f"Staleness: {agg['staleness_s']}s; window: last {WINDOW_S}s (T-900..T)  ")
    L.append(f"Census-eligible windows: {agg['n_eligible_windows']} "
             f"(of {agg['n_hours_total']} top-of-hour grid rows; "
             f"{agg['n_eligible_no_pair']} of them census-status NO_PAIR — tape-priceable "
             f"but candle-unpriceable, fair imputed from OK-row quintiles, A15.3)  ")
    if agg["missing_trade_days"]:
        L.append(f"Missing trade-file days (skipped, run continued): "
                 f"{', '.join(agg['missing_trade_days'])}  ")
    L.append("")
    L.append("> **Measure note (A15.3):** event counts are TRADE-MULTIPLICITY-WEIGHTED "
             "(every fill on a live leg emits an event, so busy microseconds inflate "
             "counts). **DWELL-SECONDS is the time-faithful measure** — it credits each "
             "moment's C the wall-clock time it actually stood, so simultaneous duplicate "
             "trades contribute ~0. Read seconds for exposure, counts for activity.\n")

    L.append("> **Cross-side refutation (A15.9, ACTIVE):** within each leg YES and NO share "
             "one book, so every trade prints the touch. A carried buy price p_S (from the "
             "last same-side taker fill) DIES at the first later opposite-side print whose "
             "implied bid (1 − opposite price) ≥ p_S — because ask ≥ bid, the p_S ask can no "
             "longer stand. Carried lifetime = min(staleness horizon, first refuting print). "
             "Refute-only (no fabricated availability), no tick assumption (≥ exact). A "
             "refuted leg is not live until its next same-side fill.\n")

    # ---- A15.9 before/after (only when the counterfactual pass was run) ----
    if agg.get("refute_compare"):
        L.append("## A15.9 impact — survivors vs the pre-A15.9 (no-refutation) run\n")
        L.append("> **Dwell semantics (A15.10):** the DWELL clock keys on STATE CHANGES "
                 "only — an interval ends at the next same-side fill, the first REFUTING "
                 "opposite print, staleness expiry, or T. Consistent (non-refuting) opposite "
                 "prints are transparent (they change nothing). The survived-% below is thus "
                 "the TRUE refutation share: 'before' is the pre-A15.9 same-side print-to-"
                 "print dwell, 'after' is the same clock with refuting prints (only) "
                 "truncating it. (Event counts are unaffected by A15.10 — they never keyed "
                 "on consistent prints.)\n")
        L.append("| direction | metric | before | after | survived % |")
        L.append("|---|---|---|---|---|")
        metrics = [("evaluation events", "evaluation_events"),
                   ("EV+ events", "evplus_events"),
                   ("EV+ dwell-seconds", "evplus_dwell_seconds"),
                   ("hard-floor events", "hardfloor_events"),
                   ("hard-floor dwell-seconds", "hardfloor_dwell_seconds")]
        for d in dirs:
            cmp = agg["refute_compare"][d]
            for label, key in metrics:
                s = cmp[key]
                bef = f"{s['before']:.1f}" if "dwell" in key else str(s["before"])
                aft = f"{s['after']:.1f}" if "dwell" in key else str(s["after"])
                pct = _fmt(s["survived_pct"], 1) if s["survived_pct"] is not None else "n/a"
                L.append(f"| {d} | {label} | {bef} | {aft} | {pct} |")
        L.append("")

    # ---- direction-by-direction headline table ----
    L.append("## Directions side by side\n")
    L.append("| metric | " + " | ".join(dirs.keys()) + " |")
    L.append("|" + "---|" * (len(dirs) + 1))

    def row(label, fn):
        return "| " + label + " | " + " | ".join(fn(dirs[d]) for d in dirs) + " |"

    L.append(row("windows w/ events", lambda a: str(a["n_windows_with_events"])))
    L.append(row("windows w/ EV+ moment", lambda a: str(a["n_windows_with_evplus"])))
    L.append(row("evaluation events", lambda a: str(a["n_evaluation_events"])))
    L.append(row("EV+ events", lambda a: str(a["n_evplus_events"])))
    L.append(row("EV+ dwell-seconds", lambda a: f"{a['total_evplus_seconds']:.1f}"))
    # Hard-floor (C < $1) is RISKLESS only for the flip (flip pays >= $1 in every branch);
    # a strangle with C < $1 is NOT riskless (pin still loses -C), so it shows n/a here.
    L.append(row("windows w/ hard-floor (C<$1, flip-riskless)",
                 lambda a: str(a["n_windows_with_hardfloor"])
                 if a["direction"] == "flip" else "n/a"))
    L.append(row("hard-floor events",
                 lambda a: str(a["n_hardfloor_events"])
                 if a["direction"] == "flip" else "n/a"))
    L.append(row("hard-floor dwell-seconds",
                 lambda a: f"{a['total_hardfloor_seconds']:.1f}"
                 if a["direction"] == "flip" else "n/a"))
    L.append("")

    # ---- per direction detail ----
    for d, a in dirs.items():
        fair_label = "1-pin" if d == "strangle" else "1+pin"
        L.append(f"## {d.upper()} — detail\n")
        if d == "strangle":
            L.append("Legs: BUY YES on the HIGH line, BUY NO on the LOW line. "
                     "Payoff: escape -> 1-C, pin -> -C.\n")
        else:
            L.append("Legs: BUY NO on the HIGH line, BUY YES on the LOW line (the FLIP). "
                     "Payoff floor: escape -> 1-C, pin -> 2-C (flip always pays >= $1; "
                     "C_flip < $1 is a riskless entry).\n")
        L.append("### Per-minute EV+ events (minute 0 = final minute)\n")
        L.append("| minute (T-m) | EV+ events |")
        L.append("|---|---|")
        for m in range(N_MIN_BUCKETS):
            L.append(f"| T-{m:02d}..T-{m+1:02d} min | {a['per_minute_evplus'][m]} |")
        L.append("")
        L.append(f"### By G/sigma quintile\n")
        hf = " hard-floor |" if d == "flip" else ""
        L.append("| q | windows | events | EV+ events |" + hf +
                 " EV+ sec | mean C | train pin | train fair (" + fair_label + ") |")
        L.append("|---|---|---|---|" + ("---|" if d == "flip" else "") +
                 "---|---|---|---|")
        for b in a["by_quintile"]:
            hfcell = f" {b['n_hardfloor']} |" if d == "flip" else ""
            L.append(
                f"| {b['quintile']} | {b['n_windows']} | {b['n_events']} | "
                f"{b['n_evplus']} |" + hfcell +
                f" {b['evplus_seconds']:.1f} | {_fmt(b['mean_C'])} | "
                f"{_fmt(b['pin_rate_train'])} | {_fmt(b['fair_train'])} |"
            )
        L.append("")
        pe = a["policy_first_evplus"]
        L.append("### Policy: enter once at the FIRST EV+ moment of each window\n")
        L.append(f"Windows entered: {pe['n_entered']} "
                 f"({pe['n_escapes']} escape, {pe['n_pins']} pin)  ")
        L.append(f"Total realized payoff: {_fmt(pe['total_payoff'])}  ")
        L.append(f"Mean realized payoff / entered window: {_fmt(pe['mean_payoff'])}\n")
        if d == "flip":
            ph = a["policy_first_hardfloor"]
            L.append("### Policy: enter once at the FIRST hard-floor moment (C_flip < $1)\n")
            L.append(f"Windows entered: {ph['n_entered']} "
                     f"({ph['n_escapes']} escape, {ph['n_pins']} pin)  ")
            L.append(f"**GUARANTEED floor Σ(1−C) [the ONLY riskless component]: "
                     f"{_fmt(ph['guaranteed_floor_total'])}**  "
                     f"(mean/window {_fmt(ph['guaranteed_floor_mean'])})  ")
            L.append(f"Pin-inclusive REALIZED payoff Σ(settle−C): "
                     f"{_fmt(ph['realized_total'])}  "
                     f"(mean/window {_fmt(ph['realized_mean'])})  ")
            L.append("> The realized figure includes the +$1 pin bonus, which is NOT "
                     "guaranteed. The **riskless** claim attaches ONLY to the guaranteed "
                     "floor Σ(1−C) above (A15.5).\n")
            # A15.6: distribution of guaranteed-floor magnitudes.
            L.append("#### Guaranteed-floor magnitude distribution (A15.6)\n")
            L.append(f"Sub-tick floors (< $0.01): **{ph['n_sub_tick_floors']} of "
                     f"{ph['n_entered']}** entered windows.\n")
            L.append("| floor size (1−C) | windows |")
            L.append("|---|---|")
            for label, n in ph["floor_histogram"]:
                L.append(f"| {label} | {n} |")
            L.append("")
            mags = ", ".join(f"{m:.4f}" for m in ph["floor_magnitudes_sorted"])
            L.append(f"Per-window guaranteed floors (sorted): [{mags}]\n")
            # A15.7: staleness caveat.
            L.append("> **Availability caveat (A15.7):** hard-floor moments are counted at "
                     f"the {agg['staleness_s']}s staleness tolerance and are NOT proven to be "
                     "simultaneously-available riskless boxes — a fill on one leg is paired "
                     "with the OTHER leg's most-recent fill up to the tolerance old, so the "
                     "two prices need not be quotable at the same instant. The reviewer "
                     "measured only ~36% of hard-floor dwell surviving a 5s tolerance on "
                     "2026-06-13. The orchestrator runs the week at BOTH 60s and 5s "
                     "staleness; read the 5s pass for the tighter availability bound.\n")

    L.append("## Comparison vs the candle C-ladder (Rung 1)\n")
    L.append("Rung 1's candle census found the STRANGLE EV negative in every quintile at "
             "every candle snapshot (gate empty; all buckets' BASE EV < 0). This tape sim "
             "asks Brad's narrower question on the LIVE trade tape of the final 15 minutes: "
             "do transient moments occur where a taker's ACTUAL paid C dips below the pair's "
             "EV? The flip direction (A15.4) additionally counts hard-floor moments "
             "(C_flip < $1), which are riskless regardless of any pin model — the flip pays "
             "at least $1 in every escape/boundary branch and $2 on a pin. EV+ / hard-floor "
             "counts are dips that OCCURRED; the policy lines are what a taker entering at "
             "the first such moment would have realized (fidelity/selection caveats "
             "apply).\n")
    return "\n".join(L) + "\n"


def _daterange(start: str, end: str) -> List[str]:
    d0 = _dt.date.fromisoformat(start)
    d1 = _dt.date.fromisoformat(end)
    out = []
    d = d0
    while d <= d1:
        out.append(d.isoformat())
        d += _dt.timedelta(days=1)
    return out


def _print_agg(agg: dict) -> None:
    print(f"tape_sim aggregates ({', '.join(agg['dates'])}), staleness={agg['staleness_s']}s")
    print(f"  eligible windows        : {agg['n_eligible_windows']} "
          f"(NO_PAIR: {agg['n_eligible_no_pair']})")
    print(f"  evaluation events (all) : {agg['n_evaluation_events']}")
    for d, a in agg["directions_agg"].items():
        print(f"  [{d}]")
        print(f"    windows w/ events     : {a['n_windows_with_events']}")
        print(f"    windows w/ EV+ moment : {a['n_windows_with_evplus']}")
        print(f"    evaluation events     : {a['n_evaluation_events']}")
        print(f"    EV+ events            : {a['n_evplus_events']}")
        print(f"    EV+ dwell-seconds     : {a['total_evplus_seconds']:.1f}")
        pe = a["policy_first_evplus"]
        print(f"    policy(first EV+): entered={pe['n_entered']} "
              f"(esc {pe['n_escapes']}/pin {pe['n_pins']}) "
              f"total={_fmt(pe['total_payoff'])} mean={_fmt(pe['mean_payoff'])}")
        if d == "flip":
            print(f"    hard-floor events     : {a['n_hardfloor_events']} "
                  f"(windows {a['n_windows_with_hardfloor']}, "
                  f"dwell {a['total_hardfloor_seconds']:.1f}s)")
            ph = a["policy_first_hardfloor"]
            print(f"    policy(first hard-floor): entered={ph['n_entered']} "
                  f"(esc {ph['n_escapes']}/pin {ph['n_pins']})")
            print(f"      GUARANTEED floor Sigma(1-C) [riskless]={_fmt(ph['guaranteed_floor_total'])} "
                  f"(mean {_fmt(ph['guaranteed_floor_mean'])}); "
                  f"realized(incl pin bonus)={_fmt(ph['realized_total'])} "
                  f"(mean {_fmt(ph['realized_mean'])})")
            print(f"      sub-tick floors (<$0.01): {ph['n_sub_tick_floors']}/{ph['n_entered']}")
    if agg.get("missing_trade_days"):
        print(f"  missing-trade days      : {agg['missing_trade_days']}")
    if agg.get("refute_compare"):
        print("  A15.9 survivors vs pre-A15.9 (no-refute):")
        for d, cmp in agg["refute_compare"].items():
            ev = cmp["evaluation_events"]; ep = cmp["evplus_events"]
            hf = cmp["hardfloor_events"]; hd = cmp["hardfloor_dwell_seconds"]
            print(f"    [{d}] eval {ev['before']}->{ev['after']} "
                  f"({_fmt(ev['survived_pct'],1)}%); EV+ {ep['before']}->{ep['after']} "
                  f"({_fmt(ep['survived_pct'],1)}%); hard-floor {hf['before']}->{hf['after']} "
                  f"({_fmt(hf['survived_pct'],1)}%); hf-dwell "
                  f"{hd['before']:.1f}->{hd['after']:.1f}s ({_fmt(hd['survived_pct'],1)}%)")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Rung 1.5 tape sim (train only)")
    ap.add_argument("--start", required=True, help="range start YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="range end YYYY-MM-DD")
    ap.add_argument("--out", default=_OUT_DIR, help="output directory")
    ap.add_argument("--staleness", type=int, default=STALENESS_S_DEFAULT,
                    help=f"leg price staleness in seconds (default {STALENESS_S_DEFAULT})")
    ap.add_argument("--direction", choices=["strangle", "flip", "both"], default="both",
                    help="which direction(s) to measure (default both)")
    ap.add_argument("--compare-no-refute", action="store_true",
                    help="also run a counterfactual pass with A15.9 refutation OFF and emit "
                         "the before/after comparison (extra pass; default off)")
    ap.add_argument("--census", default=_DEFAULT_CENSUS,
                    help="census CSV for the EV curve (sha-verified)")
    args = ap.parse_args(argv)

    dates = _daterange(args.start, args.end)
    directions = DIRECTIONS if args.direction == "both" else (args.direction,)
    # acknowledge_sealed_read is NEVER set here: the loader refuses sealed dates.
    agg = run(dates, args.out, staleness_s=args.staleness, directions=directions,
              compare_no_refute=args.compare_no_refute, census_csv=args.census)
    _print_agg(agg)
    print(f"wrote {agg['_paths']['points']}")
    print(f"wrote {agg['_paths']['report']}")
    print(f"wrote {agg['_paths']['receipt']}")


if __name__ == "__main__":
    main()
