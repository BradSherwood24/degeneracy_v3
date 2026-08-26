"""pilot_ledger.py — the append-only per-window pilot ledger + a tiny query CLI.

Each window process appends ONE JSON object (one line) to ``pilot/ledger/pilot_ledger.jsonl`` at
close (run_window step g). The paired-replay report and the promotion-gate check read ONLY this
artifact (PLAN "Coding standards" 9). Money is kept as Decimal-safe strings on disk; the query layer
re-parses to Decimal so no float wobble enters the loss/slippage totals.

The promotion gates P1/P2 (falsifier) are COMPUTED PROPERTIES of the ledger, never stored booleans —
so a re-run of the query re-derives them from the raw per-window counters and the falsifier text
governs. The gate thresholds live here as named constants mirrored from the frozen falsifier; the
falsifier remains the authority (a freeze that re-pins a number must be mirrored here).

House law: this module places no orders, opens no socket, reads no sealed file. It is pure data +
file append/read + a CLI.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from service.ledger import settlement_payoff

logger = logging.getLogger(__name__)

DEFAULT_LEDGER_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ledger"
)
DEFAULT_LEDGER_PATH = os.path.join(DEFAULT_LEDGER_DIR, "pilot_ledger.jsonl")

# --- promotion-gate thresholds (mirrored from pilot/ceremony/falsifier.md P1/P2) ---
P1_MIN_FIRED = 10
P1_MIN_FILL_RATE = Decimal("0.60")
P1_MAX_MEAN_ABS_SLIP = Decimal("0.01")  # <= 1c per side
P1_MIN_DAYS = 1
P2_MIN_FIRED = 10          # FURTHER fired signals at the 2-pair rung
P2_MIN_SUB1 = 5            # incl. >= 5 sub-$1 entries at the 2-pair rung


def _dec(v: Any) -> Decimal:
    if v is None or v == "":
        return Decimal(0)
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return Decimal(0)


# ---------------------------------------------------------------------------
# Append / load
# ---------------------------------------------------------------------------
def append_entry(entry: dict[str, Any], path: str = DEFAULT_LEDGER_PATH) -> None:
    """Append one window summary object as a single JSON line (Decimals rendered as strings)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True, default=_json_default))
        f.write("\n")


def _json_default(obj: object) -> str:
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def load_entries(path: str = DEFAULT_LEDGER_PATH) -> list[dict[str, Any]]:
    """Read all ledger entries in append order. Missing file -> []."""
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        nonempty = [ln.strip() for ln in f if ln.strip()]
    out: list[dict[str, Any]] = []
    for i, line in enumerate(nonempty):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            # A crash mid-append (single writer, append-only) can leave a truncated FINAL line.
            # Tolerate ONLY the trailing line so one bad write cannot break every subsequent read
            # (operator CLI) or silently zero out the S4/A4 day-lock seed. Mid-file corruption is
            # not expected and is surfaced loudly.
            if i == len(nonempty) - 1:
                logger.warning("pilot_ledger: skipping truncated trailing line in %s", path)
                break
            raise
    return out


def entries_for_day(entries: list[dict[str, Any]], day: str) -> list[dict[str, Any]]:
    """Entries whose close_time UTC date == ``day`` (YYYY-MM-DD). Robust to 'Z'/'+00:00' spellings."""
    return [e for e in entries if str(e.get("close_time", ""))[:10] == day]


def s4_running_loss(entries: list[dict[str, Any]]) -> Decimal:
    """Total realized P&L across all entries (negative = net loss). Sums ``realized_delta`` — window
    entries AND settlement-backfill entries alike, so a backfilled winning payoff is reflected."""
    return sum((_dec(e.get("realized_delta")) for e in entries), Decimal(0))


# ---------------------------------------------------------------------------
# A5 — the box one-legged-entry alarm counter (rolling last N box fires).
# ---------------------------------------------------------------------------
BOX_SOURCE = "wide-box"
A5_ONE_LEGGED_RATE_MAX = Decimal("0.10")  # alarm when one-legged/fires exceeds this over the window


def box_fire_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The window rows that were a live box FIRE (``fires`` > 0 AND ``fired_source`` == box), in
    append order. Backfill rows (``fires`` == 0) and non-box rows are excluded."""
    return [
        e
        for e in entries
        if int(e.get("fires", 0) or 0) > 0 and e.get("fired_source") == BOX_SOURCE
    ]


def box_one_legged_rate(
    entries: list[dict[str, Any]], n: int = 20
) -> tuple[Decimal | None, int, int]:
    """(rate, one_legged, fires) over the LAST ``n`` box fires. ``rate`` is one_legged/fires, or None
    when there are no box fires yet. A row is one-legged iff ``box_one_legged`` is truthy."""
    fires = box_fire_entries(entries)[-n:]
    if not fires:
        return None, 0, 0
    one_legged = sum(1 for e in fires if e.get("box_one_legged"))
    return (Decimal(one_legged) / Decimal(len(fires))), one_legged, len(fires)


# ---------------------------------------------------------------------------
# Settlement backfill (BUG-2 repair, part c)
#
# A window that ends holding a naked/overhang leg books only that leg's cash OUTLAY at close (the
# safe direction — see run_window._build_ledger_entry) and records the held legs under
# ``unsettled_legs``. Once the market RESULT for each held ticker is known, this backfill appends a
# SEPARATE ledger entry whose ``realized_delta`` is the settlement PAYOFF (count*$1 for each held
# leg whose outcome won, $0 otherwise). Because the outlay was already booked, the payoff is purely
# additive: a losing leg adds $0 (the close loss stands); a winning leg adds count*$1.
#
# The result map is supplied explicitly (``--result TICKER=yes`` or ``--results-file``), which is
# the simplest CORRECT source and is fully testable offline. The auto-step alternative — reading the
# result from the proxy's /markets REST helper at the next window's reconcile-first — is documented
# in the build report; it is NOT wired here so this module keeps opening no socket (house law).
# ---------------------------------------------------------------------------
def already_backfilled(entries: list[dict[str, Any]], window: str) -> bool:
    """True iff a settlement-backfill entry for ``window`` is already present (idempotency guard)."""
    return any(e.get("backfill_of") == window for e in entries)


def find_unsettled_window(entries: list[dict[str, Any]], window: str) -> dict[str, Any] | None:
    """The most recent window entry whose close_time == ``window`` that still has unsettled legs."""
    match = None
    for e in entries:
        if str(e.get("close_time")) == window and e.get("unsettled_legs"):
            match = e
    return match


def build_backfill_entry(
    window_entry: dict[str, Any], results: dict[str, str], payoff: Decimal, now: float
) -> dict[str, Any]:
    """A ledger line recording a settlement backfill. ``fires``/``filled`` are zeroed so the promotion
    counters ignore it; only ``realized_delta`` feeds ``s4_running_loss`` and the day total.
    ``close_time`` mirrors the settled window so it lands on the right UTC day.

    ``realized_delta`` is the settlement ``payoff`` NET of any floor already booked at close
    (``floor_booked`` on the window entry). For a sub-$1 flip / corridor row ``floor_booked`` is
    absent (== 0), so the backfill is the raw payoff, exactly as before. For a wide-box both-filled
    row the $1 floor was booked at close and BOTH legs are recorded unsettled, so the payoff ($1 not
    pinned / $2 pinned) is netted by the $1 floor -> +$0 (not pinned) or +$1 (pinned), which is the
    'backfill +1 if pinned' the box wants."""
    floor = _dec(window_entry.get("floor_booked"))
    realized = payoff - floor
    return {
        "close_time": window_entry.get("close_time"),
        "mode": "backfill",
        "backfill_of": window_entry.get("close_time"),
        # F2 (box-report review 2026-08-26): tag the settled window's source/strategy so a report
        # joining a backfill to a fire by close_time can require it to be the SAME strategy, never
        # attribute another strategy's backfill that happened to share the close_time. Additive:
        # copied from the settled window entry (None on a legacy window that carried neither).
        "source": window_entry.get("fired_source"),
        "strategy": window_entry.get("strategy"),
        "pairs": window_entry.get("pairs"),
        "fires": 0,
        "filled": False,
        "realized_delta": str(realized),
        "settlement_payoff": str(payoff),
        "floor_netted": str(floor),
        "realized_unsettled": False,
        "settled_legs": window_entry.get("unsettled_legs"),
        "settlement_results": results,
        "flushed_at": now,
    }


# ---------------------------------------------------------------------------
# Promotion-gate counters (computed, never stored)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PromotionCounters:
    fired_count: int
    filled_count: int
    fill_rate: Decimal | None          # None when fired_count == 0
    imbalance_count: int               # unresolved imbalances (S2-eligible)
    sub1_violations: int               # S1 arithmetic violations
    mean_abs_slippage: Decimal | None  # per-side mean |slippage| across all filled legs
    sub1_entries: int
    calendar_days: int
    windows: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "fired_count": self.fired_count,
            "filled_count": self.filled_count,
            "fill_rate": None if self.fill_rate is None else str(self.fill_rate),
            "imbalance_count": self.imbalance_count,
            "sub1_violations": self.sub1_violations,
            "mean_abs_slippage": None if self.mean_abs_slippage is None else str(self.mean_abs_slippage),
            "sub1_entries": self.sub1_entries,
            "calendar_days": self.calendar_days,
            "windows": self.windows,
        }


def compute_counters(entries: list[dict[str, Any]]) -> PromotionCounters:
    """Fold the raw per-window fields into the promotion counters. A 'fired' window is one whose
    ``fires`` > 0 (a live FIRE — WouldFire/shakedown windows do not count toward the gates)."""
    fired = 0
    filled = 0
    imbalance = 0
    s1 = 0
    sub1 = 0
    slips: list[Decimal] = []
    days: set[str] = set()
    for e in entries:
        fires = int(e.get("fires", 0) or 0)
        if fires <= 0:
            continue
        fired += fires
        if e.get("filled"):
            filled += 1
        if e.get("imbalance_unresolved"):
            imbalance += 1
        if e.get("s1_violation"):
            s1 += 1
        if e.get("sub1_entry"):
            sub1 += 1
        for s in e.get("slippage_abs_per_side", []) or []:
            slips.append(_dec(s))
        ct = str(e.get("close_time", ""))
        if ct:
            days.add(ct[:10])
    fill_rate = (Decimal(filled) / Decimal(fired)) if fired > 0 else None
    mean_slip = (sum(slips, Decimal(0)) / Decimal(len(slips))) if slips else None
    return PromotionCounters(
        fired_count=fired,
        filled_count=filled,
        fill_rate=fill_rate,
        imbalance_count=imbalance,
        sub1_violations=s1,
        mean_abs_slippage=mean_slip,
        sub1_entries=sub1,
        calendar_days=len(days),
        windows=len(entries),
    )


def _rung_entries(entries: list[dict[str, Any]], pairs: int) -> list[dict[str, Any]]:
    return [e for e in entries if int(e.get("pairs", 0) or 0) == pairs]


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reasons: tuple[str, ...]
    counters: PromotionCounters

    def as_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "reasons": list(self.reasons), "counters": self.counters.as_dict()}


def p1_gate(entries: list[dict[str, Any]]) -> GateResult:
    """P1 (1 pair -> 2 pairs), falsifier: >= 10 fired AND fill rate >= 60% AND zero unresolved
    imbalances AND zero S1 violations AND mean |slippage| <= 1c per side AND >= 1 calendar day at
    1 pair. Computed over the 1-pair rung entries."""
    c = compute_counters(_rung_entries(entries, 1))
    reasons: list[str] = []
    if c.fired_count < P1_MIN_FIRED:
        reasons.append(f"fired {c.fired_count} < {P1_MIN_FIRED}")
    if c.fill_rate is None or c.fill_rate < P1_MIN_FILL_RATE:
        reasons.append(f"fill_rate {c.fill_rate} < {P1_MIN_FILL_RATE}")
    if c.imbalance_count > 0:
        reasons.append(f"unresolved imbalances {c.imbalance_count} > 0")
    if c.sub1_violations > 0:
        reasons.append(f"S1 violations {c.sub1_violations} > 0")
    if c.mean_abs_slippage is None or c.mean_abs_slippage > P1_MAX_MEAN_ABS_SLIP:
        reasons.append(f"mean |slippage| {c.mean_abs_slippage} > {P1_MAX_MEAN_ABS_SLIP}")
    if c.calendar_days < P1_MIN_DAYS:
        reasons.append(f"calendar days {c.calendar_days} < {P1_MIN_DAYS}")
    return GateResult(passed=not reasons, reasons=tuple(reasons), counters=c)


def p2_gate(entries: list[dict[str, Any]]) -> GateResult:
    """P2 (2 pairs -> readiness report), falsifier: >= 10 FURTHER fired at 2 pairs incl. >= 5 sub-$1
    AND P1 conditions still holding AND second-pair book-walk measured. Computed over the 2-pair rung
    entries; P1 holding is checked against the 1-pair rung."""
    c = compute_counters(_rung_entries(entries, 2))
    reasons: list[str] = []
    if c.fired_count < P2_MIN_FIRED:
        reasons.append(f"2-pair fired {c.fired_count} < {P2_MIN_FIRED}")
    if c.sub1_entries < P2_MIN_SUB1:
        reasons.append(f"2-pair sub-$1 entries {c.sub1_entries} < {P2_MIN_SUB1}")
    if c.imbalance_count > 0:
        reasons.append(f"2-pair unresolved imbalances {c.imbalance_count} > 0")
    if c.sub1_violations > 0:
        reasons.append(f"2-pair S1 violations {c.sub1_violations} > 0")
    if c.fill_rate is None or c.fill_rate < P1_MIN_FILL_RATE:
        reasons.append(f"2-pair fill_rate {c.fill_rate} < {P1_MIN_FILL_RATE}")
    if c.mean_abs_slippage is not None and c.mean_abs_slippage > P1_MAX_MEAN_ABS_SLIP:
        reasons.append(f"2-pair mean |slippage| {c.mean_abs_slippage} > {P1_MAX_MEAN_ABS_SLIP}")
    # second-pair book-walk must be measured on >= 1 two-pair window (reported by run_window)
    walk_measured = any(e.get("second_pair_book_walk") is not None for e in _rung_entries(entries, 2))
    if not walk_measured:
        reasons.append("second-pair book-walk not measured on any 2-pair window")
    # P1 must still hold
    p1 = p1_gate(entries)
    if not p1.passed:
        reasons.append("P1 conditions no longer hold: " + "; ".join(p1.reasons))
    return GateResult(passed=not reasons, reasons=tuple(reasons), counters=c)


# ---------------------------------------------------------------------------
# Query CLI
# ---------------------------------------------------------------------------
def _cmd_day(entries: list[dict[str, Any]], args: argparse.Namespace) -> int:
    rows = entries_for_day(entries, args.day)
    for e in rows:
        print(json.dumps(
            {
                "close_time": e.get("close_time"),
                "mode": e.get("mode"),
                "pairs": e.get("pairs"),
                "stand_down": e.get("stand_down"),
                "would_fires": e.get("would_fires"),
                "fires": e.get("fires"),
                "filled": e.get("filled"),
                "realized_delta": e.get("realized_delta"),
                "alarms": e.get("alarms"),
                "stops": e.get("stops"),
            },
            sort_keys=True,
        ))
    print(f"# {len(rows)} window(s) on {args.day}")
    return 0


def _cmd_loss(entries: list[dict[str, Any]], args: argparse.Namespace) -> int:
    if args.day:
        entries = entries_for_day(entries, args.day)
    total = s4_running_loss(entries)
    print(json.dumps({"realized_total": str(total), "windows": len(entries)}, sort_keys=True))
    return 0


def _cmd_gate(entries: list[dict[str, Any]], args: argparse.Namespace) -> int:
    result = p1_gate(entries) if args.which == "P1" else p2_gate(entries)
    print(json.dumps({args.which: result.as_dict()}, indent=2, sort_keys=True))
    return 0 if result.passed else 1


def _parse_results(pairs: list[str] | None, results_file: str | None) -> dict[str, str]:
    """Build the {ticker: 'yes'/'no'} result map from ``--result TICKER=yes`` pairs and/or a JSON
    ``--results-file``. Fail-closed on a malformed pair."""
    results: dict[str, str] = {}
    if results_file:
        with open(results_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("--results-file must be a JSON object of {ticker: 'yes'/'no'}")
        results.update({str(k): str(v) for k, v in data.items()})
    for p in pairs or []:
        if "=" not in p:
            raise ValueError(f"--result must be TICKER=yes|no, got {p!r}")
        k, v = p.split("=", 1)
        results[k.strip()] = v.strip()
    return results


def _cmd_backfill(entries: list[dict[str, Any]], args: argparse.Namespace) -> int:
    """Append a settlement-backfill entry for one settled window. Idempotent: refuses if the window
    was already backfilled (unless --force)."""
    window = args.window
    if already_backfilled(entries, window) and not args.force:
        print(json.dumps({"error": "already_backfilled", "window": window}, sort_keys=True))
        return 1
    entry = find_unsettled_window(entries, window)
    if entry is None:
        print(json.dumps({"error": "no_unsettled_window", "window": window}, sort_keys=True))
        return 1
    legs = entry.get("unsettled_legs") or []
    # F6: a missing/invalid result yields a clean one-line error, not a raw traceback.
    try:
        results = _parse_results(args.result, args.results_file)
        payoff = settlement_payoff(legs, results)  # fail-closed on a missing/invalid result
    except (KeyError, ValueError, OSError) as e:
        print(json.dumps(
            {"error": "invalid_results", "window": window, "detail": str(e),
             "unsettled_legs": legs}, sort_keys=True))
        return 1
    backfill = build_backfill_entry(entry, results, payoff, time.time())
    append_entry(backfill, args.ledger)
    print(json.dumps(
        {"window": window, "payoff": str(payoff), "settled_legs": legs, "results": results},
        sort_keys=True,
    ))
    return 0


def _cmd_summary(entries: list[dict[str, Any]], args: argparse.Namespace) -> int:
    print(json.dumps(
        {
            "windows": len(entries),
            "realized_total": str(s4_running_loss(entries)),
            "P1": p1_gate(entries).as_dict(),
            "P2": p2_gate(entries).as_dict(),
        },
        indent=2, sort_keys=True,
    ))
    return 0


def main(argv: list[str] | None = None) -> int:
    # --ledger is a PARENT option so it is accepted before OR after the subcommand. Use SUPPRESS so
    # the subparser's copy does NOT clobber a value supplied BEFORE the subcommand with its default
    # (the classic argparse shared-parent-default bug); the effective default is applied post-parse.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--ledger", default=argparse.SUPPRESS)

    ap = argparse.ArgumentParser(description="Query the pilot ledger (read-only).", parents=[common])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_day = sub.add_parser("day", parents=[common], help="entries for a UTC day (YYYY-MM-DD)")
    p_day.add_argument("day")
    p_day.set_defaults(func=_cmd_day)

    p_loss = sub.add_parser("loss", parents=[common], help="S4 running realized loss total")
    p_loss.add_argument("--day", default=None)
    p_loss.set_defaults(func=_cmd_loss)

    p_gate = sub.add_parser("gate", parents=[common], help="P1/P2 promotion-gate check")
    p_gate.add_argument("which", choices=["P1", "P2"])
    p_gate.set_defaults(func=_cmd_gate)

    p_sum = sub.add_parser("summary", parents=[common], help="windows + realized total + both gates")
    p_sum.set_defaults(func=_cmd_summary)

    p_bf = sub.add_parser("backfill", parents=[common],
                          help="append a settlement-backfill realized entry for a settled window")
    p_bf.add_argument("--window", required=True, help="the window close_time (e.g. 2026-08-24T05:00:00Z)")
    p_bf.add_argument("--result", action="append", default=[],
                      help="TICKER=yes|no settlement result (repeatable)")
    p_bf.add_argument("--results-file", default=None, help="JSON {ticker: 'yes'/'no'} result map")
    p_bf.add_argument("--force", action="store_true", help="backfill even if already backfilled")
    p_bf.set_defaults(func=_cmd_backfill)

    args = ap.parse_args(argv)
    if not hasattr(args, "ledger"):
        args.ledger = DEFAULT_LEDGER_PATH
    entries = load_entries(args.ledger)
    return args.func(entries, args)


if __name__ == "__main__":
    raise SystemExit(main())
