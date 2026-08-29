"""box_report.py — the wide-box daily report: the instrument that turns fills into knowledge.

Reads the REAL journals (``pilot/journals/*.jsonl``) and the repaired pilot ledger
(``pilot/ledger/pilot_ledger.jsonl``) — never the sealed holdout, never a socket — and computes,
per fired box window and in aggregate, exactly the numbers ``pilot/ceremony/box_falsifier.md``
retires the strategy on (R1-R4, A1, A5, S4) plus the recording Brad asked for (level bumps, a
candle-staleness proxy, flatten outcomes, displayed ask sizes).

Two halves, kept apart so the math is testable without any I/O:
  * PURE computation (``compute_*``/``aggregate_*``/``render_text``): dicts in, dicts/str out. Money
    is Decimal throughout; only timestamps are floats. No file/clock/network access.
  * I/O (``load_*``, ``main``): discover journals, read the ledger + day-guard files, write the JSON
    artifacts, print the text table.

The falsifier is the AUTHORITY on every threshold; the constants below mirror its pins (Brad's
verbatim rulings 2026-08-26) and a freeze that re-pins a number must be mirrored here. Statuses are
mechanical: ``NOT YET (n/N)`` (gate not reached), ``HOLDING`` (reached, condition not met),
``TRIPPED`` (condition met). No judgment text.

House law: this module places no orders, opens no socket, reads no sealed file, touches no key/PEM.
It is pure data + read-only file access + a CLI.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import random
import time
from decimal import Decimal, InvalidOperation
from typing import Any

# --- source id (kept local; must equal service.box.WIDE_BOX — see the wiring test) ---
WIDE_BOX = "wide-box"

# --- falsifier pins (mirrored from pilot/ceremony/box_falsifier.md; the falsifier governs) ---
R1_MIN_FILLS = 30
R1_MAX_MEAN_SUMMED_SLIP_CENTS = Decimal("1.0")   # stop if mean summed slippage > +1.0c
R2_MIN_FILLS = 60
R2_MIN_MEAN_REALIZED_CENTS = Decimal("-3")       # stop if mean realized per pair < -3c
R3_MIN_FILLS = 60
R3_MIN_PIN_RATE = Decimal("0.80")                # stop if pin rate < 0.80
R3_BACKTEST_PIN_RATE = Decimal("0.90")           # the candle-backtest pin rate, reported beside it
R4_MIN_FILLS = 100                               # none of R1-R3 by here -> live-confirmed
A1_SLIP_THRESHOLD_CENTS = Decimal("2")           # a leg with fill - decided ask > 2c is flagged
A5_WINDOW = 20                                   # rolling last-20 box fires
A5_MAX_ONE_LEGGED_RATE = Decimal("0.10")         # alarm if one-legged/fires > 0.10
S4_DAILY_LOSS_CAP = Decimal("3.00")              # daily loss cap on the ACCOUNT BALANCE

# --- pre-registered shadow rule (observational only; NOT a falsifier, NOT a decision gate) ---
# Pre-registered 2026-08-28 (Brad): the live tick scan enters at the cheapest qualifying instant;
# 5 of the first 5 live misses had implied_pin <= 0.78 while all 20 boxes at >= 0.785 pinned
# (post-hoc split; frozen here so the forward record judges it). The rule under observation: skip a
# box whose implied_pin < 0.80 at decision. This report computes what the ledger would show if those
# fills were removed. It changes nothing about live decisions.
SHADOW_MIN_IMPLIED_PIN = Decimal("0.80")
SHADOW_RULE_REGISTERED = "2026-08-28"

# --- roster partition (box-v1.1 amendment, 2026-08-29) ---
# Every window summary row carries policy_sha / roster. The primary gates (R1-R4, A1, A5) and the
# headline count ONLY the CURRENT roster; box-v1 fires close as a frozen LEGACY line. These MUST equal
# service.box.FROZEN_BOX_POLICY_SHA256 / BOX_V1_POLICY_SHA256 (mirrored here to keep this report a pure
# data module with no decision-layer import — see the wiring test that asserts the equality).
CURRENT_BOX_POLICY_SHA256 = "cec4b1a29c5d46deac09fd7a46ec0e08b7603a1f6862758cdb60e97a477aa42c"
BOX_V1_POLICY_SHA256 = "480d46347c6d5e5b136d34df1555516cf1b3d3899b41611a2f0dafb786305eb3"
CURRENT_ROSTER = "box-v1.1"
LEGACY_ROSTER = "box-v1"  # windows with no policy_sha/roster tag are treated as box-v1 (legacy)

# --- status literals ---
STATUS_NOT_YET = "NOT YET"
STATUS_HOLDING = "HOLDING"
STATUS_TRIPPED = "TRIPPED"

_CENT = Decimal("100")
_TWO = Decimal("2")


# ---------------------------------------------------------------------------
# Decimal helpers
# ---------------------------------------------------------------------------
def _dec(v: Any) -> Decimal | None:
    """Parse a JSON scalar to Decimal, or None for absent/blank/unparseable (fail-soft: a report
    must never crash on a malformed field — it reports what it can and leaves the rest None)."""
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _dec0(v: Any) -> Decimal:
    d = _dec(v)
    return Decimal(0) if d is None else d


def _mean(xs: list[Decimal]) -> Decimal | None:
    return (sum(xs, Decimal(0)) / Decimal(len(xs))) if xs else None


def _stdev(xs: list[Decimal]) -> Decimal | None:
    """Sample standard deviation (n-1). None for n < 2."""
    n = len(xs)
    if n < 2:
        return None
    m = _mean(xs)
    assert m is not None
    var = sum(((x - m) * (x - m) for x in xs), Decimal(0)) / Decimal(n - 1)
    return Decimal(str(math.sqrt(float(var))))


def _se(xs: list[Decimal]) -> Decimal | None:
    sd = _stdev(xs)
    if sd is None:
        return None
    return sd / Decimal(str(math.sqrt(len(xs))))


def _bootstrap_ci(
    xs: list[Decimal], *, resamples: int = 2000, seed: int = 1729, alpha: float = 0.05
) -> tuple[Decimal, Decimal] | None:
    """Percentile bootstrap CI of the MEAN. Deterministic (fixed seed) so tests are stable. None for
    n < 2."""
    n = len(xs)
    if n < 2:
        return None
    rng = random.Random(seed)
    floats = [float(x) for x in xs]
    means: list[float] = []
    for _ in range(resamples):
        s = 0.0
        for _ in range(n):
            s += floats[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    lo = means[int((alpha / 2) * resamples)]
    hi = means[min(resamples - 1, int((1 - alpha / 2) * resamples))]
    return Decimal(str(lo)), Decimal(str(hi))


# ---------------------------------------------------------------------------
# Journal parsing (pure over already-loaded record lists)
# ---------------------------------------------------------------------------
def journal_close_time(records: list[dict[str, Any]]) -> str | None:
    """The window's close_time from a ``window_start``/``window_meta`` record (obj.close_time)."""
    for rec in records:
        if rec.get("kind") in ("window_start", "window_meta"):
            ct = rec.get("obj", {}).get("close_time")
            if ct:
                return str(ct)
    return None


def _records_of_kind(records: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [r.get("obj", {}) for r in records if r.get("kind") == kind]


def _flatten_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _records_of_kind(records, "box_flatten")


# ---------------------------------------------------------------------------
# Ledger indexing
# ---------------------------------------------------------------------------
def _is_box_backfill(row: dict[str, Any]) -> bool:
    return row.get("source") == WIDE_BOX or row.get("strategy") == "box"


def index_ledger(entries: list[dict[str, Any]]) -> tuple[dict[str, dict], dict[str, dict]]:
    """(box_fire_rows_by_window, backfill_rows_by_window). A box FIRE row: fires > 0 AND
    (strategy == 'box' OR fired_source == wide-box). A backfill row: carries ``backfill_of``.

    F2 (review 2026-08-26): the backfill join is NOT close_time alone. Among the backfill rows for a
    close_time, a WIDE_BOX-tagged row wins (the tag is written by ``build_backfill_entry``); a single
    untagged legacy row is tolerated only when it is the ONLY backfill for that close_time; a
    non-box-tagged row (a corridor / sub-$1 flip backfill that happened to share the close_time) or an
    ambiguous set of untagged rows yields NO box backfill (the window reads unsettled — the safe
    direction, never another strategy's payoff). Latest row wins within the chosen class."""
    fires: dict[str, dict] = {}
    backfill_candidates: dict[str, list[dict]] = {}
    for e in entries:
        bf = e.get("backfill_of")
        if bf:
            backfill_candidates.setdefault(str(bf), []).append(e)
            continue
        is_box = e.get("strategy") == "box" or e.get("fired_source") == WIDE_BOX
        if is_box and int(e.get("fires", 0) or 0) > 0:
            fires[str(e.get("close_time"))] = e
    backfills: dict[str, dict] = {}
    for ct, rows in backfill_candidates.items():
        box_rows = [r for r in rows if _is_box_backfill(r)]
        if box_rows:
            backfills[ct] = box_rows[-1]
        elif len(rows) == 1 and rows[0].get("source") is None and rows[0].get("strategy") is None:
            backfills[ct] = rows[0]  # legacy single untagged row -> best-effort
        # else: non-box-tagged or ambiguous untagged -> no box backfill (window reads unsettled)
    return fires, backfills


# ---------------------------------------------------------------------------
# Per-leg detail: join the decision (journal selection) to the fill (ledger)
# ---------------------------------------------------------------------------
def _selection_leg(selection: dict[str, Any], which: str) -> dict[str, Any]:
    """Normalize one leg of a box_fire/box_would_fire ``selection`` payload (which in
    {'hourly','m15'}) to a common shape (Decimals parsed)."""
    p = f"{which}_"
    return {
        "which": which,
        "ticker": selection.get(f"{p}ticker"),
        "side": selection.get(f"{p}side"),
        "decided_ask": _dec(selection.get(f"{p}ask")),
        "limit": _dec(selection.get(f"{p}limit")),
        "displayed_ask_size": selection.get(f"{p}ask_size"),
    }


def build_leg(sel_leg: dict[str, Any], fill_row: dict[str, Any] | None) -> dict[str, Any]:
    """One report leg: decision (decided ask, limit, displayed size) joined to the ACTUAL fill from
    the ledger. ``slippage_cents = (fill - decided ask) * 100`` (SIGNED, side-space); a ``level_bump``
    is a fill strictly above the decided ask (Brad: 'it's okay if an order bumps up a level, we'll
    note it')."""
    decided = sel_leg.get("decided_ask")
    fill_price = _dec(fill_row.get("avg_price")) if fill_row else None
    fee = _dec0(fill_row.get("avg_fee")) if fill_row else Decimal(0)
    slippage_cents: Decimal | None = None
    level_bump = False
    bump_cents: Decimal | None = None
    if fill_price is not None and decided is not None:
        slippage_cents = (fill_price - decided) * _CENT
        if fill_price > decided:
            level_bump = True
            bump_cents = (fill_price - decided) * _CENT
    return {
        "which": sel_leg.get("which"),
        "ticker": sel_leg.get("ticker"),
        "side": sel_leg.get("side"),
        "decided_ask": decided,
        "limit": sel_leg.get("limit"),
        "displayed_ask_size": sel_leg.get("displayed_ask_size"),
        "filled": fill_price is not None,
        "fill_price": fill_price,
        "fee": fee,
        "slippage_cents": slippage_cents,
        "level_bump": level_bump,
        "bump_cents": bump_cents,
    }


def _fills_by_ticker(ledger_row: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not ledger_row:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for f in ledger_row.get("fills", []) or []:
        # Only a real fill (avg_price present) counts as a filled leg.
        if f.get("avg_price") is not None:
            out[str(f.get("ticker"))] = f
    return out


def _flatten_summary(flatten_objs: list[dict[str, Any]],
                     ledger_row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Flatten attempts + outcome for a one-legged window. Attempts are the box_flatten stages that
    represent an actual attempt against a bid (``flat`` / ``miss_retry`` / ``giveup_hold``); outcome
    is the ledger's ``box_flatten_filled`` (True=flattened flat, False=held naked, None=none)."""
    if not flatten_objs and (ledger_row is None or ledger_row.get("box_flatten_filled") is None):
        return None
    stages = [str(o.get("stage")) for o in flatten_objs]
    attempts = sum(1 for s in stages if s in ("flat", "miss_retry", "giveup_hold"))
    filled = ledger_row.get("box_flatten_filled") if ledger_row else None
    if filled is True:
        outcome = "flattened"
    elif filled is False:
        outcome = "held_naked"
    else:
        outcome = "unknown"
    if "no_bid_hold" in stages:
        outcome = "held_no_bid"
    elif "cutoff_hold" in stages:
        outcome = "held_cutoff"
    return {"attempts": attempts, "outcome": outcome, "stages": stages}


# ---------------------------------------------------------------------------
# One fired (or would-fire) window record
# ---------------------------------------------------------------------------
def _side_from_selection(selection: dict[str, Any]) -> str:
    """below (BTC < A: buy 15M NO, hourly YES) vs above (BTC > A: buy 15M YES, hourly NO)."""
    return "below" if selection.get("m15_side") == "no" else "above"


def _outcome(fill_class: str, backfill_row: dict[str, Any] | None,
             flatten: dict[str, Any] | None) -> str:
    """The settlement outcome. Both-filled: pinned ($2 payoff) / not ($1) / unsettled (no backfill).
    One-legged: the flatten outcome, plus the naked leg's settle if backfilled. None: n/a."""
    if fill_class == "both":
        if backfill_row is None:
            return "unsettled"
        payoff = _dec(backfill_row.get("settlement_payoff"))
        if payoff is None:
            return "unsettled"
        return "pinned" if payoff >= _TWO else "not_pinned"
    if fill_class == "one-legged":
        base = flatten["outcome"] if flatten else "unknown"
        if backfill_row is not None:
            return f"{base}+settled"
        return base
    return "n/a"


def _roster_of(ledger_row: dict[str, Any] | None) -> tuple[str, str | None]:
    """(roster_name, policy_sha) for a window from its summary/fire ledger row. A row without the tag
    is treated as box-v1 (the legacy roster shipped before the tag existed)."""
    if not ledger_row:
        return LEGACY_ROSTER, None
    sha = ledger_row.get("policy_sha")
    roster = ledger_row.get("roster")
    if roster:
        return str(roster), (str(sha) if sha is not None else None)
    if sha == CURRENT_BOX_POLICY_SHA256:
        return CURRENT_ROSTER, str(sha)
    if sha == BOX_V1_POLICY_SHA256:
        return LEGACY_ROSTER, str(sha)
    return LEGACY_ROSTER, (str(sha) if sha is not None else None)


def _is_current_roster(w: dict[str, Any]) -> bool:
    """True iff the window belongs to the CURRENT roster (box-v1.1). The primary gates and headline
    count only these; every other window closes as a legacy line."""
    return w.get("policy_sha") == CURRENT_BOX_POLICY_SHA256 or w.get("roster") == CURRENT_ROSTER


def build_window(fire_obj: dict[str, Any], close_time: str,
                 ledger_row: dict[str, Any] | None,
                 backfill_row: dict[str, Any] | None,
                 flatten_objs: list[dict[str, Any]]) -> dict[str, Any]:
    """Assemble one fired/would-fire window record from the decision (journal) + fills (ledger)."""
    selection = fire_obj.get("selection", {}) or {}
    hourly_sel = _selection_leg(selection, "hourly")
    m15_sel = _selection_leg(selection, "m15")
    fills = _fills_by_ticker(ledger_row)
    hourly = build_leg(hourly_sel, fills.get(str(hourly_sel.get("ticker"))))
    m15 = build_leg(m15_sel, fills.get(str(m15_sel.get("ticker"))))

    # fill classification from the ledger (authoritative). No ledger row -> treat as none.
    if ledger_row is None:
        fill_class = "none"
    elif ledger_row.get("box_one_legged"):
        fill_class = "one-legged"
    elif ledger_row.get("filled"):
        fill_class = "both"
    elif hourly["filled"] or m15["filled"]:
        fill_class = "one-legged"
    else:
        fill_class = "none"

    flatten = _flatten_summary(flatten_objs, ledger_row) if fill_class == "one-legged" else None

    K = _dec(selection.get("strike_K"))
    A = _dec(selection.get("anchor_A"))
    width = abs(K - A) if (K is not None and A is not None) else None

    # actual cost paid = sum of filled legs' (fill price + fee); C_mid/C from the decision.
    paid_legs = [lg for lg in (hourly, m15) if lg["filled"]]
    c_paid = (sum((lg["fill_price"] + lg["fee"] for lg in paid_legs), Decimal(0))
              if paid_legs else None)
    fees_total = sum((lg["fee"] for lg in paid_legs), Decimal(0)) if paid_legs else Decimal(0)
    c_decision = _dec(selection.get("C"))
    c_mid = _dec(selection.get("C_mid"))
    implied_pin = _dec(selection.get("implied_pin"))

    # realized per pair = close-booked realized + settlement backfill (if any).
    realized: Decimal | None = None
    realized_settled = False
    if ledger_row is not None:
        realized = _dec0(ledger_row.get("realized_delta"))
        if backfill_row is not None:
            realized = realized + _dec0(backfill_row.get("realized_delta"))
            realized_settled = True
        elif not ledger_row.get("realized_unsettled", False):
            realized_settled = True  # fully closed at close (e.g. flattened round-trip)

    summed_slip_cents: Decimal | None = None
    if fill_class == "both" and hourly["slippage_cents"] is not None and m15["slippage_cents"] is not None:
        summed_slip_cents = hourly["slippage_cents"] + m15["slippage_cents"]

    # candle-staleness proxy: the actual PAIR cost paid vs the decision-time mid (both legs only —
    # a one-legged C_paid is a partial single-leg cost, not comparable to the two-leg C_mid), and the
    # decision-time fee+spread gap (available for every fire, fills or not).
    staleness_paid_vs_mid = (
        (c_paid - c_mid) * _CENT
        if (c_paid is not None and c_mid is not None and len(paid_legs) == 2)
        else None
    )
    staleness_decision_gap = ((c_decision - c_mid) * _CENT) if (c_decision is not None and c_mid is not None) else None

    roster, policy_sha = _roster_of(ledger_row)
    return {
        "close_time": close_time,
        "t_minus_s": fire_obj.get("t_minus_s"),
        "roster": roster,
        "policy_sha": policy_sha,
        "paper": False,
        "side": _side_from_selection(selection),
        "K": K,
        "A": A,
        "width": width,
        "hourly": hourly,
        "m15": m15,
        "fill_class": fill_class,
        "C_decision": c_decision,
        "C_mid": c_mid,
        "C_paid": c_paid,
        "implied_pin": implied_pin,
        "fees_total": fees_total,
        "summed_slip_cents": summed_slip_cents,
        "outcome": _outcome(fill_class, backfill_row, flatten),
        "realized": realized,
        "realized_settled": realized_settled,
        "flatten": flatten,
        "staleness_paid_vs_mid_cents": staleness_paid_vs_mid,
        "staleness_decision_gap_cents": staleness_decision_gap,
        "has_ledger": ledger_row is not None,
    }


def build_paper_window(obj: dict[str, Any], close_time: str,
                       backfill_row: dict[str, Any] | None, *, kind: str) -> dict[str, Any]:
    """A PAPER box window from a ``box_skip_implied`` / ``box_rescan_would_fire`` journal record (v1.1).
    No order was placed, so there is no fill/ledger row: it is scored on SETTLEMENT as paper using the
    SAME convention a fire uses — a settlement-backfill row keyed on the close_time gives the payoff
    ($2 pinned / $1 not), and ``paper realized = payoff - C_decision`` (C_decision is the fee-inclusive
    pair cost from the selection). Absent a backfill (the usual case for an un-traded skip, since the
    settlement sweep only settles rows WE hold) the window reads ``unsettled`` and contributes 0 to the
    settled PnL — its value in the forward record is the count and, once/if settlement data exists, the
    paper PnL. ``kind`` in {"skip", "rescan"}."""
    selection = obj.get("selection", {}) or {}
    implied_pin = _dec(selection.get("implied_pin"))
    if implied_pin is None:
        implied_pin = _dec(obj.get("implied_pin"))  # skip/rescan records also carry it top-level
    c_decision = _dec(selection.get("C"))
    K = _dec(selection.get("strike_K"))
    A = _dec(selection.get("anchor_A"))
    outcome = "unsettled"
    realized: Decimal | None = None
    realized_settled = False
    if backfill_row is not None:
        payoff = _dec(backfill_row.get("settlement_payoff"))
        if payoff is not None and c_decision is not None:
            realized = payoff - c_decision            # (2 if pinned else 1) - C_decision (fees in C)
            outcome = "pinned" if payoff >= _TWO else "not_pinned"
            realized_settled = True
    return {
        "close_time": close_time,
        "t_minus_s": obj.get("t_minus_s"),
        "roster": CURRENT_ROSTER,
        "policy_sha": CURRENT_BOX_POLICY_SHA256,
        "paper": True,
        "kind": kind,                                 # "skip" | "rescan"
        "fill_class": "none",                         # never a fill (no order)
        "side": _side_from_selection(selection),
        "K": K,
        "A": A,
        "width": abs(K - A) if (K is not None and A is not None) else None,
        "C_decision": c_decision,
        "implied_pin": implied_pin,
        "outcome": outcome,
        "realized": realized,
        "realized_settled": realized_settled,
    }


# ---------------------------------------------------------------------------
# Aggregate / retirement computations (pure)
# ---------------------------------------------------------------------------
def _gate_status(n: int, N: int, tripped: bool) -> str:
    if n < N:
        return f"{STATUS_NOT_YET} ({n}/{N})"
    return STATUS_TRIPPED if tripped else STATUS_HOLDING


def r1_slippage(windows: list[dict[str, Any]]) -> dict[str, Any]:
    """R1 SLIPPAGE: over the two-leg fills, mean (fill - decided ask) summed over both legs (cents),
    with SE; TRIPPED after 30 fills if the mean > +1.0c."""
    summed = [w["summed_slip_cents"] for w in windows
              if w["fill_class"] == "both" and w["summed_slip_cents"] is not None]
    n = len(summed)
    mean = _mean(summed)
    se = _se(summed)
    tripped = mean is not None and mean > R1_MAX_MEAN_SUMMED_SLIP_CENTS
    return {
        "n_two_leg_fills": n,
        "mean_summed_slippage_cents": mean,
        "se_cents": se,
        "pin_cents": R1_MAX_MEAN_SUMMED_SLIP_CENTS,
        "min_fills": R1_MIN_FILLS,
        "status": _gate_status(n, R1_MIN_FILLS, bool(tripped and n >= R1_MIN_FILLS)),
    }


def r2_economics(windows: list[dict[str, Any]]) -> dict[str, Any]:
    """R2 ECONOMICS. The STATUS is computed on the mean realized per FIRE over the SETTLED box fires
    with any fill (two-leg + one-legged; one-legged flatten losses INCLUDED — coordinator ruling
    2026-08-26; zero-fill fires excluded). TRIPPED after 60 SETTLED such fills if that mean < -3c.

    F1 (review 2026-08-26): only SETTLED fires enter the mean/SE/CI AND the 60-count gate. A wide-box
    both-filled fire is ``realized_unsettled`` at close, booked at the conservative $1-floor
    (-C_paid + $1): a fire that will settle PINNED reads -$0.90 there instead of +$0.10, so counting
    unsettled fires let settlement TIMING alone false-trip R2. A one-legged FLATTENED fire is settled
    immediately (its round-trip P&L is final). ``unsettled`` (fires with any fill not yet settled) and
    ``n_settled`` are reported; ``n_fills`` = all any-fill fires for transparency. The two-leg-only
    ``per_pair_mean_realized_cents`` is displayed but does NOT drive the status. R1 (slippage, known at
    fill) and R3 (already settled-gated for its rate) are unaffected."""
    settled = [w for w in windows
               if w["fill_class"] in ("both", "one-legged")
               and w["realized"] is not None and w["realized_settled"]]
    per_fire = [w["realized"] * _CENT for w in settled]
    per_pair = [w["realized"] * _CENT for w in settled if w["fill_class"] == "both"]
    n_settled = len(per_fire)
    n_all = sum(1 for w in windows
                if w["fill_class"] in ("both", "one-legged") and w["realized"] is not None)
    mean = _mean(per_fire)           # per-FIRE over SETTLED; the status is computed on THIS
    se = _se(per_fire)
    ci = _bootstrap_ci(per_fire)
    per_pair_mean = _mean(per_pair)  # two-leg-only, settled; displayed, not decisive
    unsettled = sum(1 for w in windows
                    if w["fill_class"] in ("both", "one-legged") and not w["realized_settled"])
    tripped = mean is not None and mean < R2_MIN_MEAN_REALIZED_CENTS
    return {
        "n_fills": n_all,                                # all any-fill fires (transparency)
        "n_settled": n_settled,                          # settled any-fill fires (the gate)
        "n_pairs": len(per_pair),                        # settled two-leg fills only
        "mean_realized_cents": mean,                     # per-FIRE over settled (decisive)
        "se_cents": se,
        "bootstrap_ci95_cents": ci,
        "per_pair_mean_realized_cents": per_pair_mean,   # two-leg-only, settled, displayed
        "unsettled": unsettled,
        "pin_cents": R2_MIN_MEAN_REALIZED_CENTS,
        "min_fills": R2_MIN_FILLS,
        "status": _gate_status(n_settled, R2_MIN_FILLS, bool(tripped and n_settled >= R2_MIN_FILLS)),
    }


def r3_pin_rate(windows: list[dict[str, Any]]) -> dict[str, Any]:
    """R3 PIN RATE: over the SETTLED two-leg fills, pin rate vs 0.80; TRIPPED after 60 fills if the
    pin rate < 0.80. Reports the 0.90 backtest and the mean implied pin beside it."""
    two_leg = [w for w in windows if w["fill_class"] == "both"]
    n = len(two_leg)
    settled = [w for w in two_leg if w["outcome"] in ("pinned", "not_pinned")]
    pinned = sum(1 for w in settled if w["outcome"] == "pinned")
    pin_rate = (Decimal(pinned) / Decimal(len(settled))) if settled else None
    implied = [w["implied_pin"] for w in windows if w["implied_pin"] is not None]
    implied_mean = _mean(implied)
    tripped = pin_rate is not None and pin_rate < R3_MIN_PIN_RATE
    return {
        "n_fills": n,
        "n_settled": len(settled),
        "pinned": pinned,
        "pin_rate": pin_rate,
        "pin_floor": R3_MIN_PIN_RATE,
        "backtest_pin_rate": R3_BACKTEST_PIN_RATE,
        "implied_pin_mean": implied_mean,
        "min_fills": R3_MIN_FILLS,
        "status": _gate_status(n, R3_MIN_FILLS, bool(tripped and n >= R3_MIN_FILLS)),
    }


def r4_match(windows: list[dict[str, Any]], r1: dict, r2: dict, r3: dict) -> dict[str, Any]:
    """R4 MATCH: none of R1-R3 tripped by 100 two-leg fills -> the candle number stands live-confirmed
    at 1 contract. TRIPPED here means CONFIRMED (a positive terminal, not a stop)."""
    n = sum(1 for w in windows if w["fill_class"] == "both")
    prior_tripped = any(r["status"] == STATUS_TRIPPED for r in (r1, r2, r3))
    confirmed = n >= R4_MIN_FILLS and not prior_tripped
    if n < R4_MIN_FILLS:
        status = f"{STATUS_NOT_YET} ({n}/{R4_MIN_FILLS})"
    elif confirmed:
        status = STATUS_TRIPPED  # confirmed: R4 condition met
    else:
        status = STATUS_HOLDING  # reached 100 but an R1-R3 already tripped
    return {
        "n_fills": n,
        "min_fills": R4_MIN_FILLS,
        "confirmed": confirmed,
        "note": "TRIPPED = candle number live-confirmed (positive terminal)",
        "status": status,
    }


def a1_slippage_outliers(windows: list[dict[str, Any]]) -> dict[str, Any]:
    """A1: every filled leg whose (fill - decided ask) > 2c, listed, with a running mean over the
    flagged legs. Visibility alarm — TRIPPED = at least one flagged leg."""
    filled_legs = 0
    flagged: list[dict[str, Any]] = []
    for w in windows:
        for lg in (w["hourly"], w["m15"]):
            if not lg["filled"] or lg["slippage_cents"] is None:
                continue
            filled_legs += 1
            if lg["slippage_cents"] > A1_SLIP_THRESHOLD_CENTS:
                flagged.append({
                    "close_time": w["close_time"],
                    "which": lg["which"],
                    "ticker": lg["ticker"],
                    "slippage_cents": lg["slippage_cents"],
                })
    running_mean = _mean([f["slippage_cents"] for f in flagged]) if flagged else None
    if filled_legs == 0:
        status = f"{STATUS_NOT_YET} (0 filled legs)"
    else:
        status = STATUS_TRIPPED if flagged else STATUS_HOLDING
    return {
        "threshold_cents": A1_SLIP_THRESHOLD_CENTS,
        "filled_legs": filled_legs,
        "flagged_count": len(flagged),
        "flagged": flagged,
        "running_mean_cents": running_mean,
        "status": status,
    }


def a5_one_legged(windows: list[dict[str, Any]]) -> dict[str, Any]:
    """A5: one-legged rate over the rolling last 20 box fires vs 0.10. TRIPPED if rate > 0.10."""
    fires = windows[-A5_WINDOW:]
    total = len(fires)
    one_legged = sum(1 for w in fires if w["fill_class"] == "one-legged")
    rate = (Decimal(one_legged) / Decimal(total)) if total else None
    if total == 0:
        status = f"{STATUS_NOT_YET} (0/{A5_WINDOW})"
    else:
        status = STATUS_TRIPPED if (rate is not None and rate > A5_MAX_ONE_LEGGED_RATE) else STATUS_HOLDING
    return {
        "window": A5_WINDOW,
        "considered": total,
        "one_legged": one_legged,
        "rate": rate,
        "max_rate": A5_MAX_ONE_LEGGED_RATE,
        "status": status,
    }


def level_bumps(windows: list[dict[str, Any]]) -> dict[str, Any]:
    """Level-bump count: legs whose fill > decided ask, with the distribution of bump sizes (cents).
    Brad: 'it's okay if an order bumps up a level, we'll note it and figure out our way around it.'"""
    bumps: list[Decimal] = []
    detail: list[dict[str, Any]] = []
    filled_legs = 0
    for w in windows:
        for lg in (w["hourly"], w["m15"]):
            if not lg["filled"]:
                continue
            filled_legs += 1
            if lg["level_bump"] and lg["bump_cents"] is not None:
                bumps.append(lg["bump_cents"])
                detail.append({
                    "close_time": w["close_time"], "which": lg["which"],
                    "ticker": lg["ticker"], "bump_cents": lg["bump_cents"],
                })
    bumps_sorted = sorted(bumps)
    dist = None
    if bumps_sorted:
        mid = len(bumps_sorted) // 2
        median = (bumps_sorted[mid] if len(bumps_sorted) % 2
                  else (bumps_sorted[mid - 1] + bumps_sorted[mid]) / _TWO)
        dist = {
            "min_cents": bumps_sorted[0],
            "median_cents": median,
            "max_cents": bumps_sorted[-1],
            "mean_cents": _mean(bumps_sorted),
        }
    return {
        "filled_legs": filled_legs,
        "count": len(bumps),
        "distribution": dist,
        "detail": detail,
    }


def candle_staleness(windows: list[dict[str, Any]]) -> dict[str, Any]:
    """Candle-staleness proxy: per fire, C paid vs C_mid at decision (and the decision-time C - C_mid
    fee+spread gap). Reports the per-fire values and their means."""
    paid = [w["staleness_paid_vs_mid_cents"] for w in windows
            if w["staleness_paid_vs_mid_cents"] is not None]
    gap = [w["staleness_decision_gap_cents"] for w in windows
           if w["staleness_decision_gap_cents"] is not None]
    return {
        "n_paid": len(paid),
        "mean_paid_vs_mid_cents": _mean(paid),
        "n_decision_gap": len(gap),
        "mean_decision_gap_cents": _mean(gap),
        "per_fire": [
            {
                "close_time": w["close_time"],
                "C_paid": w["C_paid"],
                "C_decision": w["C_decision"],
                "C_mid": w["C_mid"],
                "paid_vs_mid_cents": w["staleness_paid_vs_mid_cents"],
                "decision_gap_cents": w["staleness_decision_gap_cents"],
            }
            for w in windows
        ],
    }


def _shadow_group_stats(group: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts + settled-only PnL for one shadow group of two-leg windows. Settled = outcome in
    {pinned, not_pinned}; realized_sum/mean/pin_rate are over the SETTLED subset only."""
    pins = sum(1 for w in group if w["outcome"] == "pinned")
    misses = sum(1 for w in group if w["outcome"] == "not_pinned")
    unsettled = sum(1 for w in group if w["outcome"] == "unsettled")
    n_settled = pins + misses
    realized_sum = sum(
        (w["realized"] for w in group
         if w["outcome"] in ("pinned", "not_pinned") and w["realized"] is not None),
        Decimal(0),
    )
    mean = (realized_sum / Decimal(n_settled)) if n_settled else None
    pin_rate = (Decimal(pins) / Decimal(n_settled)) if n_settled else None
    return {
        "n": len(group),
        "n_settled": n_settled,
        "pins": pins,
        "misses": misses,
        "unsettled": unsettled,
        "realized_sum": realized_sum,
        "mean_realized_per_fill": mean,
        "pin_rate": pin_rate,
    }


def shadow_implied_rule(windows: list[dict[str, Any]], *,
                        paper_skips: list[dict[str, Any]] | None = None,
                        rescans: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """PRE-REGISTERED SHADOW RULE / SO-1 (observational; see SHADOW_MIN_IMPLIED_PIN comment). Spans
    BOTH rosters and now includes paper skips:

      * ``kept``    = TWO-LEG fills with implied_pin >= 0.80 (either roster).
      * ``skipped`` = TWO-LEG fills with implied_pin < 0.80 (v1 REAL fills that a box-v1.1 would skip)
                      PLUS the v1.1 ``box_skip_implied`` windows scored on settlement as PAPER. The
                      paper/real split is reported as ``n_real`` / ``n_paper``.
      * ``rescan``  = the v1.1 ``box_rescan_would_fire`` PAPER records (a skipped hour that re-qualified
                      at a later instant), for the literal-skip vs keep-scanning question.
      * ``unknown`` = two-leg fills with implied_pin missing.
      * ``all``     = every two-leg fill.

    Purely a what-if — it removes no live fill and drives no decision."""
    paper_skips = paper_skips or []
    rescans = rescans or []
    two_leg = [w for w in windows if w["fill_class"] == "both"]
    kept: list[dict[str, Any]] = []
    skipped_real: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    for w in two_leg:
        ip = w["implied_pin"]
        if ip is None:
            unknown.append(w)
        elif ip >= SHADOW_MIN_IMPLIED_PIN:
            kept.append(w)
        else:
            skipped_real.append(w)
    skipped_all = skipped_real + paper_skips
    all_n = len(two_leg)
    denom = all_n + len(paper_skips)  # the full box population (fills + paper skips)
    skipped_share = (Decimal(len(skipped_all)) / Decimal(denom)) if denom else None
    skipped_windows = [
        (w["close_time"], w["implied_pin"], w["outcome"], w["realized"]) for w in skipped_all
    ]
    skipped_stats = _shadow_group_stats(skipped_all)
    skipped_stats["n_real"] = len(skipped_real)
    skipped_stats["n_paper"] = len(paper_skips)
    return {
        "min_implied_pin": SHADOW_MIN_IMPLIED_PIN,
        "registered": SHADOW_RULE_REGISTERED,
        "all": _shadow_group_stats(two_leg),
        "kept": _shadow_group_stats(kept),
        "skipped": skipped_stats,
        "rescan": _shadow_group_stats(rescans),
        "unknown": _shadow_group_stats(unknown),
        "skipped_share": skipped_share,
        "skipped_windows": skipped_windows,
    }


def legacy_roster_summary(windows: list[dict[str, Any]]) -> dict[str, Any]:
    """The frozen LEGACY (box-v1) line: final n (two-leg fills) / pins / misses / realized over the
    non-current-roster fired windows. Computed, never hardcoded."""
    two_leg = [w for w in windows if w["fill_class"] == "both"]
    settled = [w for w in two_leg if w["outcome"] in ("pinned", "not_pinned")]
    pins = sum(1 for w in settled if w["outcome"] == "pinned")
    misses = sum(1 for w in settled if w["outcome"] == "not_pinned")
    realized_sum = sum(
        (w["realized"] for w in two_leg
         if w["realized"] is not None and w["realized_settled"]),
        Decimal(0),
    )
    rosters = sorted({str(w.get("roster") or LEGACY_ROSTER) for w in windows})
    return {
        "roster": rosters[0] if len(rosters) == 1 else LEGACY_ROSTER,
        "rosters": rosters,
        "fires": len(windows),
        "n_two_leg": len(two_leg),
        "n_settled": len(settled),
        "pins": pins,
        "misses": misses,
        "realized_sum": realized_sum,
    }


def aggregate_block(windows: list[dict[str, Any]], *,
                    shadow_windows: list[dict[str, Any]] | None = None,
                    paper_skips: list[dict[str, Any]] | None = None,
                    rescans: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The full aggregate block for a set of fired windows (per-day or cumulative).

    ``windows`` drives the primary gates R1-R4 / A1 / A5 and the headline — the caller passes the
    CURRENT-roster fires so legacy box-v1 windows never move a live gate. ``shadow_windows`` (default
    ``windows``) is the SO-1 population, which spans BOTH rosters; ``paper_skips`` / ``rescans`` are the
    v1.1 paper records folded into the SO-1 block."""
    fires = len(windows)
    two_leg = sum(1 for w in windows if w["fill_class"] == "both")
    one_legged = sum(1 for w in windows if w["fill_class"] == "one-legged")
    none = sum(1 for w in windows if w["fill_class"] == "none")
    fill_rate = (Decimal(two_leg) / Decimal(fires)) if fires else None
    r1 = r1_slippage(windows)
    r2 = r2_economics(windows)
    r3 = r3_pin_rate(windows)
    r4 = r4_match(windows, r1, r2, r3)
    return {
        "fires": fires,
        "two_leg_fills": two_leg,
        "one_legged": one_legged,
        "none": none,
        "fill_rate": fill_rate,
        "R1": r1,
        "R2": r2,
        "R3": r3,
        "R4": r4,
        "A1": a1_slippage_outliers(windows),
        "A5": a5_one_legged(windows),
        "level_bumps": level_bumps(windows),
        "candle_staleness": candle_staleness(windows),
        "shadow_implied_rule": shadow_implied_rule(
            shadow_windows if shadow_windows is not None else windows,
            paper_skips=paper_skips, rescans=rescans,
        ),
    }


# ---------------------------------------------------------------------------
# S4 (per UTC day) — from the day-guard file + the s4_balance_check journal records
# ---------------------------------------------------------------------------
def compute_s4(day: str, guard: dict[str, Any] | None,
               s4_records: list[dict[str, Any]]) -> dict[str, Any]:
    """S4: today's balance_start / latest balance / loss vs $3.00 (guard file) + the wake journal
    ``s4_balance_check`` records (latest by local_ts). ``ledger_vs_balance_delta`` reported beside it.
    TRIPPED if the loss >= cap OR any day-halting stop is latched today."""
    corrupt = bool(guard.get("corrupt")) if guard else False
    balance_start = _dec(guard.get("balance_start_dollars")) if guard else None
    latched = list(guard.get("latched", []) or []) if guard else []
    latched_kinds = sorted({str(x.get("kind")) for x in latched if x.get("kind")})
    s4_latched = "S4" in latched_kinds

    latest = None
    if s4_records:
        latest = max(s4_records, key=lambda r: r.get("local_ts", 0.0)).get("obj", {})
    balance_now = _dec(latest.get("balance_now_dollars")) if latest else None
    loss = _dec(latest.get("loss_dollars")) if latest else None
    ledger_vs_balance_delta = _dec(latest.get("ledger_vs_balance_delta")) if latest else None
    breached_flag = bool(latest.get("breached")) if latest else False

    have_data = balance_start is not None or loss is not None or bool(latched)
    tripped = bool(latched) or breached_flag or (loss is not None and loss >= S4_DAILY_LOSS_CAP)
    # F5 (review 2026-08-26): a corrupt guard file is NOT "no balance data" — its latch state is
    # UNKNOWN and arming is refused for the day. Surface it distinctly so it is never read as clean.
    if corrupt:
        status = "GUARD CORRUPT"
    elif not have_data:
        status = f"{STATUS_NOT_YET} (no balance data)"
    else:
        status = STATUS_TRIPPED if tripped else STATUS_HOLDING
    return {
        "day": day,
        "corrupt": corrupt,
        "balance_start_dollars": balance_start,
        "balance_now_dollars": balance_now,
        "loss_dollars": loss,
        "cap_dollars": S4_DAILY_LOSS_CAP,
        "ledger_vs_balance_delta": ledger_vs_balance_delta,
        "any_stop_latched": bool(latched),
        "latched_kinds": latched_kinds,
        "s4_latched": s4_latched,
        "status": status,
    }


# ---------------------------------------------------------------------------
# JSON serialization (Decimals -> strings, tuples -> lists)
# ---------------------------------------------------------------------------
def _jsonify(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _jsonify(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonify(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# Text rendering (pure)
# ---------------------------------------------------------------------------
def _f(d: Decimal | None, places: int = 4) -> str:
    if d is None:
        return "-"
    q = Decimal(1).scaleb(-places)
    return str(d.quantize(q))


def _fc(d: Decimal | None, places: int = 2) -> str:
    """Format a cents value with a sign, e.g. +1.50c / -3.00c."""
    if d is None:
        return "-"
    q = Decimal(1).scaleb(-places)
    v = d.quantize(q)
    return f"{'+' if v >= 0 else ''}{v}c"


def _render_window(w: dict[str, Any], lines: list[str]) -> None:
    tm = w["t_minus_s"]
    tm_s = f"T-{tm:.0f}s" if isinstance(tm, (int, float)) else "-"
    ip = w["implied_pin"]
    shadow_skip = (w["fill_class"] == "both" and ip is not None and ip < SHADOW_MIN_IMPLIED_PIN)
    lines.append(
        f"  {w['close_time']}  {tm_s}  side={w['side']:5}  "
        f"K={_f(w['K'],2)} A={_f(w['A'],2)} width={_f(w['width'],2)}  "
        f"[{w['fill_class']}]  outcome={w['outcome']}"
        f"{' [shadow-skip]' if shadow_skip else ''}"
    )
    for lg in (w["hourly"], w["m15"]):
        bump = "  <<LEVEL BUMP" if lg["level_bump"] else ""
        lines.append(
            f"      {lg['which']:6} {str(lg['ticker'] or '-'):32} side={str(lg['side'] or '-'):3} "
            f"ask={_f(lg['decided_ask'],4)} lim={_f(lg['limit'],4)} "
            f"size={lg['displayed_ask_size'] if lg['displayed_ask_size'] is not None else '-'} "
            f"fill={_f(lg['fill_price'],4)} slip={_fc(lg['slippage_cents'])} "
            f"fee={_f(lg['fee'],4)}{bump}"
        )
    lines.append(
        f"      C_paid={_f(w['C_paid'],4)} C_dec={_f(w['C_decision'],4)} C_mid={_f(w['C_mid'],4)} "
        f"implied_pin={_f(w['implied_pin'],4)}  realized={_f(w['realized'],4)}"
        f"{'' if w['realized_settled'] else ' (unsettled)'}"
    )
    if w["flatten"]:
        lines.append(
            f"      flatten: attempts={w['flatten']['attempts']} outcome={w['flatten']['outcome']}"
        )


def _render_aggregate(agg: dict[str, Any], lines: list[str]) -> None:
    lines.append(
        f"  fires={agg['fires']}  two-leg={agg['two_leg_fills']}  one-legged={agg['one_legged']}  "
        f"none={agg['none']}  fill_rate={_f(agg['fill_rate'],3)}"
    )
    r1 = agg["R1"]
    lines.append(
        f"  R1 SLIPPAGE  : {r1['status']:16}  n={r1['n_two_leg_fills']}  "
        f"mean_summed_slip={_fc(r1['mean_summed_slippage_cents'])} "
        f"SE={_fc(r1['se_cents'])}  pin=>{_fc(r1['pin_cents'])}"
    )
    r2 = agg["R2"]
    ci = r2["bootstrap_ci95_cents"]
    ci_s = f"[{_fc(ci[0])},{_fc(ci[1])}]" if ci else "-"
    lines.append(
        f"  R2 ECONOMICS : {r2['status']:16}  n_settled={r2['n_settled']}(of {r2['n_fills']} anyfill)  "
        f"mean_realized/fire={_fc(r2['mean_realized_cents'])} SE={_fc(r2['se_cents'])} "
        f"CI95={ci_s}  per_pair={_fc(r2['per_pair_mean_realized_cents'])}(n={r2['n_pairs']})  "
        f"pin=<{_fc(r2['pin_cents'])}  unsettled={r2['unsettled']}"
    )
    r3 = agg["R3"]
    lines.append(
        f"  R3 PIN RATE  : {r3['status']:16}  n={r3['n_fills']} settled={r3['n_settled']}  "
        f"pin_rate={_f(r3['pin_rate'],3)}  floor={_f(r3['pin_floor'],2)} "
        f"backtest={_f(r3['backtest_pin_rate'],2)} implied_pin_mean={_f(r3['implied_pin_mean'],4)}"
    )
    r4 = agg["R4"]
    lines.append(f"  R4 MATCH     : {r4['status']:16}  n={r4['n_fills']}  (TRIPPED = live-confirmed)")
    a1 = agg["A1"]
    lines.append(
        f"  A1 SLIP>2c   : {a1['status']:16}  flagged={a1['flagged_count']}/{a1['filled_legs']} "
        f"running_mean={_fc(a1['running_mean_cents'])}"
    )
    for f in a1["flagged"]:
        lines.append(f"      A1: {f['close_time']} {f['which']} {f['ticker']} {_fc(f['slippage_cents'])}")
    a5 = agg["A5"]
    lines.append(
        f"  A5 ONE-LEGGED: {a5['status']:16}  {a5['one_legged']}/{a5['considered']} "
        f"rate={_f(a5['rate'],3)}  max={_f(a5['max_rate'],2)}"
    )
    lb = agg["level_bumps"]
    dist = lb["distribution"]
    dist_s = (f"min={_fc(dist['min_cents'])} med={_fc(dist['median_cents'])} "
              f"max={_fc(dist['max_cents'])} mean={_fc(dist['mean_cents'])}") if dist else "-"
    lines.append(f"  LEVEL BUMPS  : count={lb['count']}/{lb['filled_legs']}  {dist_s}")
    cs = agg["candle_staleness"]
    lines.append(
        f"  CANDLE STALE : n_paid={cs['n_paid']} mean_paid_vs_mid={_fc(cs['mean_paid_vs_mid_cents'])}  "
        f"mean_decision_gap={_fc(cs['mean_decision_gap_cents'])}"
    )
    _render_shadow_rule(agg["shadow_implied_rule"], lines)


def _shadow_line(label: str, s: dict[str, Any]) -> str:
    return (
        f"      {label:8} n={s['n']:3}  pins/miss/unsettled={s['pins']}/{s['misses']}/{s['unsettled']}  "
        f"PnL=${_f(s['realized_sum'],4)}  mean/settled=${_f(s['mean_realized_per_fill'],4)}  "
        f"pin_rate={_f(s['pin_rate'],3)}"
    )


def _render_shadow_rule(sr: dict[str, Any], lines: list[str]) -> None:
    lines.append(
        "  SHADOW RULE (pre-registered 2026-08-28, observational): skip implied_pin < 0.80"
        "  [SO-1; spans box-v1 + box-v1.1; skipped includes v1.1 paper skips]"
    )
    lines.append(_shadow_line("ALL", sr["all"]))
    lines.append(_shadow_line("KEPT", sr["kept"]))
    skipped = sr["skipped"]
    lines.append(
        _shadow_line("SKIPPED", skipped)
        + f"  [real={skipped.get('n_real', 0)} paper={skipped.get('n_paper', 0)}]"
    )
    if "rescan" in sr:
        lines.append(_shadow_line("RESCAN", sr["rescan"]) + "  [paper]")
    share = sr["skipped_share"]
    share_s = "-" if share is None else f"{_f(share * _CENT, 1)}%"
    lines.append(f"      skipped share = {share_s} of the box population (fills + paper skips)")
    for ct, ip, outcome, realized in sr["skipped_windows"]:
        lines.append(
            f"        skip: {ct}  implied={_f(ip,4)}  {outcome}  realized=${_f(realized,4)}"
        )


def _render_legacy(lr: dict[str, Any] | None, lines: list[str]) -> None:
    """The frozen LEGACY box-v1 line (final counts; excluded from the live gates above)."""
    if not lr:
        return
    lines.append(
        f"  LEGACY {lr['roster']:8}: fires={lr['fires']} two_leg={lr['n_two_leg']} "
        f"pins/miss={lr['pins']}/{lr['misses']} (settled={lr['n_settled']})  "
        f"realized=${_f(lr['realized_sum'],4)}  [closed; not counted in the live gates]"
    )


def _render_s4(s4: dict[str, Any], lines: list[str]) -> None:
    if s4.get("corrupt"):
        lines.append("  S4 DAILY LOSS: GUARD CORRUPT - arming refused (guard file unparseable)")
        return
    lines.append(
        f"  S4 DAILY LOSS: {s4['status']:16}  start=${_f(s4['balance_start_dollars'],2)} "
        f"now=${_f(s4['balance_now_dollars'],2)} loss=${_f(s4['loss_dollars'],2)} "
        f"cap=${_f(s4['cap_dollars'],2)}  ledger_vs_balance={_f(s4['ledger_vs_balance_delta'],4)}"
    )
    lines.append(
        f"      latched today: {'YES ' + ','.join(s4['latched_kinds']) if s4['any_stop_latched'] else 'none'}"
    )


def render_text(report: dict[str, Any]) -> str:
    """Render the computed report dict to a plain-text block."""
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("WIDE-BOX DAILY REPORT - the instrument that turns fills into knowledge")
    lines.append("=" * 78)
    days = report.get("days", {})
    if not days:
        lines.append("(no box windows found in the journal set)")
    for day in sorted(days):
        d = days[day]
        lines.append("")
        lines.append(f"UTC DAY {day}")
        lines.append("-" * 78)
        fires = d["fires"]
        if fires:
            lines.append(f"FIRED BOX WINDOWS ({len(fires)}):")
            for w in fires:
                _render_window(w, lines)
        else:
            lines.append("FIRED BOX WINDOWS (0): none")
        wf = d["would_fires"]
        lines.append("")
        if wf:
            lines.append(f"SHADOW - WOULD-FIRE (not traded) ({len(wf)}):")
            for w in wf:
                lines.append(
                    f"  {w['close_time']}  side={w['side']:5}  K={_f(w['K'],2)} A={_f(w['A'],2)} "
                    f"width={_f(w['width'],2)}  hourly_ask={_f(w['hourly']['decided_ask'],4)} "
                    f"m15_ask={_f(w['m15']['decided_ask'],4)} C={_f(w['C_decision'],4)} "
                    f"implied_pin={_f(w['implied_pin'],4)}"
                )
        else:
            lines.append("SHADOW - WOULD-FIRE (0): none")
        skips = d.get("skipped_implied") or []
        if skips:
            lines.append("")
            lines.append(f"SKIPPED - IMPLIED-PIN FLOOR (box-v1.1, paper) ({len(skips)}):")
            for w in skips:
                lines.append(
                    f"  {w['close_time']}  [skipped-implied]  side={w['side']:5}  "
                    f"K={_f(w['K'],2)} A={_f(w['A'],2)} width={_f(w['width'],2)}  "
                    f"C={_f(w['C_decision'],4)} implied_pin={_f(w['implied_pin'],4)} "
                    f"outcome={w['outcome']} realized=${_f(w['realized'],4)}"
                )
        rescans = d.get("rescans") or []
        if rescans:
            lines.append(f"RESCAN - WOULD RE-QUALIFY (box-v1.1, paper) ({len(rescans)}):")
            for w in rescans:
                lines.append(
                    f"  {w['close_time']}  side={w['side']:5}  K={_f(w['K'],2)} A={_f(w['A'],2)}  "
                    f"C={_f(w['C_decision'],4)} implied_pin={_f(w['implied_pin'],4)} "
                    f"outcome={w['outcome']} realized=${_f(w['realized'],4)}"
                )
        lines.append("")
        lines.append("DAY AGGREGATES:")
        _render_aggregate(d["aggregates"], lines)
        _render_legacy(d.get("legacy_rosters"), lines)
        _render_s4(d["s4"], lines)
    cum = report.get("cumulative")
    if cum is not None:
        lines.append("")
        lines.append("=" * 78)
        lines.append("CUMULATIVE (all box fires across the journal set)")
        lines.append("=" * 78)
        _render_aggregate(cum["aggregates"], lines)
        _render_legacy(cum.get("legacy_rosters"), lines)
        s4s = cum.get("s4_days", [])
        latched_days = [s["day"] for s in s4s if s["any_stop_latched"]]
        corrupt_days = [s["day"] for s in s4s if s.get("corrupt")]
        lines.append(
            f"  S4 (daily): {len(s4s)} day(s) with balance data; "
            f"latched: {', '.join(latched_days) if latched_days else 'none'}; "
            f"GUARD CORRUPT: {', '.join(corrupt_days) if corrupt_days else 'none'}"
        )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Report assembly (pure over loaded inputs)
# ---------------------------------------------------------------------------
def build_report(
    journals: list[tuple[str, list[dict[str, Any]]]],
    ledger_entries: list[dict[str, Any]],
    guards: dict[str, dict[str, Any]],
    *,
    only_day: str | None = None,
) -> dict[str, Any]:
    """Assemble the full report dict from loaded journals, ledger and day-guards (all pure).

    ``journals`` is [(close_time_or_fallback, records)]; ``guards`` maps a UTC day to its parsed
    day-guard dict. The CUMULATIVE block always spans ALL fires (even with ``only_day`` set); only the
    per-day ``days`` map is filtered to ``only_day``."""
    fire_rows, backfill_rows = index_ledger(ledger_entries)

    all_fired: list[dict[str, Any]] = []
    all_would: list[dict[str, Any]] = []
    all_paper_skips: list[dict[str, Any]] = []
    all_rescans: list[dict[str, Any]] = []
    s4_records_by_day: dict[str, list[dict[str, Any]]] = {}

    for close_time, records in journals:
        ct = journal_close_time(records) or close_time
        day = str(ct)[:10] if ct else "unknown"
        for rec in records:
            if rec.get("kind") == "s4_balance_check":
                s4_records_by_day.setdefault(day, []).append(rec)
        flatten_objs = _flatten_records(records)
        for obj in _records_of_kind(records, "box_fire"):
            all_fired.append(
                build_window(obj, str(ct), fire_rows.get(str(ct)),
                             backfill_rows.get(str(ct)), flatten_objs)
            )
        for obj in _records_of_kind(records, "box_would_fire"):
            all_would.append(build_window(obj, str(ct), None, None, []))
        # v1.1 paper records (order-free): scored on settlement via a backfill row keyed on close_time
        # when one exists (usually absent for an un-traded skip -> reads unsettled).
        for obj in _records_of_kind(records, "box_skip_implied"):
            all_paper_skips.append(
                build_paper_window(obj, str(ct), backfill_rows.get(str(ct)), kind="skip")
            )
        for obj in _records_of_kind(records, "box_rescan_would_fire"):
            all_rescans.append(
                build_paper_window(obj, str(ct), backfill_rows.get(str(ct)), kind="rescan")
            )

    def _day_of(w: dict[str, Any]) -> str:
        return str(w["close_time"])[:10]

    # Roster partition: the primary gates and headline count ONLY the current roster; box-v1 fires
    # close as a legacy line but stay in the SO-1 shadow population.
    current_fired = [w for w in all_fired if _is_current_roster(w)]
    legacy_fired = [w for w in all_fired if not _is_current_roster(w)]

    days_present = sorted({_day_of(w) for w in all_fired} | {_day_of(w) for w in all_would}
                          | {_day_of(w) for w in all_paper_skips} | {_day_of(w) for w in all_rescans}
                          | set(s4_records_by_day) | set(guards))
    if only_day is not None:
        days_present = [d for d in days_present if d == only_day]

    days: dict[str, Any] = {}
    for day in days_present:
        day_fired_all = [w for w in all_fired if _day_of(w) == day]
        day_current = [w for w in current_fired if _day_of(w) == day]
        day_legacy = [w for w in legacy_fired if _day_of(w) == day]
        day_would = [w for w in all_would if _day_of(w) == day]
        day_skips = [w for w in all_paper_skips if _day_of(w) == day]
        day_rescans = [w for w in all_rescans if _day_of(w) == day]
        days[day] = {
            "date": day,
            "fires": day_fired_all,          # every fire (rendered); gates below count current only
            "would_fires": day_would,
            "skipped_implied": day_skips,    # v1.1 paper skips
            "rescans": day_rescans,          # v1.1 paper rescans
            "aggregates": aggregate_block(
                day_current, shadow_windows=day_fired_all,
                paper_skips=day_skips, rescans=day_rescans,
            ),
            "legacy_rosters": legacy_roster_summary(day_legacy) if day_legacy else None,
            "s4": compute_s4(day, guards.get(day), s4_records_by_day.get(day, [])),
        }

    cumulative = {
        "aggregates": aggregate_block(
            current_fired, shadow_windows=all_fired,
            paper_skips=all_paper_skips, rescans=all_rescans,
        ),
        "legacy_rosters": legacy_roster_summary(legacy_fired) if legacy_fired else None,
        "s4_days": [
            compute_s4(day, guards.get(day), s4_records_by_day.get(day, []))
            for day in sorted(set(s4_records_by_day) | set(guards))
        ],
    }
    return {"days": days, "cumulative": cumulative}


# ---------------------------------------------------------------------------
# I/O layer
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_PILOT = os.path.dirname(_HERE)
DEFAULT_JOURNAL_DIR = os.path.join(_PILOT, "journals")
DEFAULT_LEDGER = os.path.join(_PILOT, "ledger", "pilot_ledger.jsonl")
DEFAULT_OPS_DIR = os.path.join(_PILOT, "ops")
DEFAULT_OUT_DIR = os.path.join(_PILOT, "reports")


# The only record kinds the report reads. A live journal is dominated by hundreds of thousands of
# ``kalshi_ws`` frames per window; json.loads over all of them across 120 journals is minutes of work
# for records we never use. We keep ONLY these kinds and gate each line with a cheap substring test
# (the journal is written json.dumps(sort_keys=True), so the marker ``"kind": "<k>"`` is stable)
# BEFORE paying for json.loads — an ~orders-of-magnitude speedup, exact for the kinds we want.
REPORT_KINDS = (
    "window_start", "window_meta",
    "box_fire", "box_would_fire", "box_flatten",
    # v1.1 paper records (implied-pin floor)
    "box_skip_implied", "box_rescan_would_fire",
    "s4_balance_check",
)


_KIND_TOKEN = '"kind": "'


def load_journal_file(path: str, keep_kinds: tuple[str, ...] = REPORT_KINDS) -> list[dict[str, Any]]:
    """Read one flushed JSONL journal into a raw record list (each: idx, kind, local_ts, obj),
    keeping ONLY records whose kind is in ``keep_kinds``. Streams line by line (no full-file
    materialization) and reads the TOP-LEVEL kind directly from the ``"kind": "<value>"`` token BEFORE
    paying for json.loads (~0.3s over a 150MB / 440k-line journal vs minutes if every line is parsed).

    F3 (review 2026-08-26): the gate reads the ACTUAL top-level kind value, not a substring anywhere in
    the line. The journal is written ``json.dumps(sort_keys=True)`` (``journal.py``), so the top-level
    keys serialize sorted (idx, kind, local_ts, obj) and the FIRST ``"kind": "`` in the line is the
    top-level one — an ``obj`` that embeds ``"kind": "kalshi_ws"`` (or a keep-marker) in a nested field
    is never confused for the record's kind, in either direction. A truncated final line (or any
    unparseable line) is skipped, not raised."""
    keep = set(keep_kinds)
    tok = _KIND_TOKEN
    out: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            i = ln.find(tok)
            if i == -1:
                continue
            j = ln.find('"', i + len(tok))
            if j == -1:
                continue
            if ln[i + len(tok):j] not in keep:  # exact top-level kind value
                continue
            try:
                rec = json.loads(ln)
            except json.JSONDecodeError:
                continue  # a truncated final line (or any unparseable line) is skipped
            if rec.get("kind") in keep:  # belt-and-suspenders after the exact prefilter
                out.append(rec)
    return out


def _close_from_filename(path: str) -> str | None:
    """Inverse of run_window._safe_close for a fallback close_time: 20260826T050000Z -> ...ISO."""
    base = os.path.basename(path)
    stem = base[:-6] if base.endswith(".jsonl") else base
    if len(stem) == 16 and stem[8] == "T" and stem.endswith("Z"):
        y, mo, d = stem[0:4], stem[4:6], stem[6:8]
        h, mi, s = stem[9:11], stem[11:13], stem[13:15]
        return f"{y}-{mo}-{d}T{h}:{mi}:{s}Z"
    return None


def load_journals(journal_dir: str) -> list[tuple[str, list[dict[str, Any]]]]:
    out: list[tuple[str, list[dict[str, Any]]]] = []
    for path in sorted(glob.glob(os.path.join(journal_dir, "*.jsonl"))):
        if os.path.basename(path).startswith("summary"):
            continue
        try:
            records = load_journal_file(path)
        except Exception:  # noqa: BLE001 - one bad journal must not sink the report
            continue
        out.append((_close_from_filename(path) or path, records))
    return out


def load_ledger(path: str) -> list[dict[str, Any]]:
    if not os.path.exists(path):
        return []
    out: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        lines = [ln.strip() for ln in f if ln.strip()]
    for i, ln in enumerate(lines):
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                break
            raise
    return out


def load_guards(ops_dir: str) -> dict[str, dict[str, Any]]:
    """Read all ops/stops_YYYY-MM-DD.json day-guards into {day: parsed dict}. A malformed guard is
    kept as {'corrupt': True} so S4 can still report 'latched today unknown' rather than vanish."""
    guards: dict[str, dict[str, Any]] = {}
    for path in sorted(glob.glob(os.path.join(ops_dir, "stops_*.json"))):
        base = os.path.basename(path)
        day = base[len("stops_"):-len(".json")]
        try:
            with open(path, "r", encoding="utf-8") as f:
                guards[day] = json.load(f)
        except (OSError, ValueError):
            guards[day] = {"utc_day": day, "corrupt": True, "latched": []}
    return guards


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="The wide-box daily report (read-only).")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--day", default=None, help="report a single UTC day (YYYY-MM-DD)")
    g.add_argument("--all", action="store_true", help="report every UTC day found (default)")
    ap.add_argument("--journal-dir", default=DEFAULT_JOURNAL_DIR)
    ap.add_argument("--ledger", default=DEFAULT_LEDGER)
    ap.add_argument("--ops-dir", default=DEFAULT_OPS_DIR, help="dir holding stops_YYYY-MM-DD.json")
    ap.add_argument("--out", default=DEFAULT_OUT_DIR, help="output dir for the JSON artifacts")
    args = ap.parse_args(argv)

    journals = load_journals(args.journal_dir)
    ledger = load_ledger(args.ledger)
    guards = load_guards(args.ops_dir)
    report = build_report(journals, ledger, guards, only_day=args.day)

    text = render_text(report)
    print(text)

    os.makedirs(args.out, exist_ok=True)
    generated_at = time.time()
    for day, day_report in report["days"].items():
        obj = _jsonify({"generated_at": generated_at, "day": day, **day_report})
        path = os.path.join(args.out, f"box_{day}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, sort_keys=True)
        print(f"wrote {path}")
    cum_obj = _jsonify({"generated_at": generated_at, **report["cumulative"]})
    cum_path = os.path.join(args.out, "box_cumulative.json")
    with open(cum_path, "w", encoding="utf-8") as f:
        json.dump(cum_obj, f, indent=2, sort_keys=True)
    print(f"wrote {cum_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
