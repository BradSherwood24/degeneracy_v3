"""ledger.py — the append-only per-window V3.2 ledger (one JSON line per window).

Each ``run_v32`` process appends ONE object to ``pilot/ledger/v32_ledger.jsonl`` at close. The report
(``service.v32.report``) reads ONLY this artifact plus the journals. Money is kept as Decimal-safe
strings on disk; readers re-parse to Decimal so no float wobble enters the totals.

House law: this module places no orders, opens no socket, reads no sealed file — it is pure data +
file append/read. Phase 2 records shadow observations and would-be order counts; the REAL fills and
settlement backfill are Phase 3 — those field slots are present and left empty here so the schema is
stable across phases (a Phase-3 backfill appends its own row; the report tolerates the empty slots).
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from typing import Any

DEFAULT_V32_LEDGER_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "ledger"
)
DEFAULT_V32_LEDGER_PATH = os.path.join(DEFAULT_V32_LEDGER_DIR, "v32_ledger.jsonl")


def _json_default(obj: object) -> str:
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def append_v32_ledger_row(row: dict[str, Any], path: str = DEFAULT_V32_LEDGER_PATH) -> None:
    """Append one window row as a single JSON line (Decimals rendered as strings)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True, default=_json_default))
        f.write("\n")


def load_v32_rows(path: str = DEFAULT_V32_LEDGER_PATH) -> list[dict[str, Any]]:
    """Read all rows in append order. Missing file -> []. Tolerates only a truncated trailing line
    (a crash mid-append; single writer, append-only)."""
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        nonempty = [ln.strip() for ln in f if ln.strip()]
    out: list[dict[str, Any]] = []
    for i, line in enumerate(nonempty):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            if i == len(nonempty) - 1:
                break
            raise
    return out


def _shadow_summary(state) -> dict[str, Any]:
    """Per-E shadow fills + locks read off the final V32State (the in-process ideal fill rule).

    ``{E: {filled, n, offer, print, lock, W_at_completion}}`` — the sim statistic on live data. locks
    are Decimal-safe strings; an incomplete (awaiting) shadow leaves ``lock`` None."""
    out: dict[str, Any] = {}
    if state is None:
        return out
    for key, sub in getattr(state, "shadows", {}).items():
        entry: dict[str, Any] = {"E": sub.E, "filled": sub.filled, "n": sub.n}
        f = sub.fill
        if f is not None:
            entry.update({
                "offer": f.offer,
                "print": f.print_price,
                "count": f.count,
                "W_at_completion": f.W_at_completion,
                "lock": f.lock,
            })
        out[key] = entry
    return out


def build_v32_ledger_row(
    *,
    close_time: str,
    resolved_mode: str,
    effective_mode: str,
    degrade: str | None,
    params,
    state,
    driver_counts: dict[str, int],
    executor_counts: dict[str, int],
    ws_counts: dict[str, int],
    strike_count: int,
    strike_generations: int,
    bucket_count: int,
    bucket_generations: int,
    strike_lag_seconds: float | None,
    bucket_lag_seconds: float | None,
    journal_path: str | None,
    record_count: int,
    stand_down_reason: str | None,
    now: float,
    params_sha: str | None = None,
    # Recording-only co-settling KXBTC15M leg (V3.2 is the single tape recorder; it never trades 15M).
    m15_tickers: list[str] | None = None,
    m15_frames: int = 0,
    # Phase-3 money-math slots (defaults preserve the Phase-2 row shape exactly).
    armed: bool = False,
    fills: list[Any] | None = None,
    wing_fills: list[Any] | None = None,
    held_legs: list[Any] | None = None,
    realized_lock: Decimal | None = None,
    one_legged: bool | None = None,
    realized_unsettled: bool = False,
    settlement: dict[str, Any] | None = None,
    realized_delta: Decimal | None = None,
    rests_placed: int = 0,
    rests_rejected: int = 0,
    wing_batches: int = 0,
    exec_price_mismatches: list[Any] | None = None,
    # cancel / venue-truth counters (2026-09-14 shard fix; 2026-09-15 quote-end-race adds the last two)
    cancels_attempted: int = 0,
    cancels_confirmed: int = 0,
    cancel_404s: int = 0,
    cancels_via_status: int = 0,
    cancels_expired: int = 0,
    rest_invariant_violations: int = 0,
    degrade_reason: str | None = None,
    # Last-quoted spot bucket + stand-down bookkeeping, captured WHILE quoting by the driver (see
    # ``run_v32.V32Driver._capture_quote``). These win over the post-quote-end ``state`` so the row
    # shows the bucket we actually rested on rather than the reset-at-close spot (which is nulled).
    last_quoted_bucket_ticker: str | None = None,
    last_quoted_Sd: int | None = None,
    last_quoted_Su: int | None = None,
    last_rest_price: Decimal | None = None,
    last_desired_n: Decimal | None = None,
    spot_buckets_quoted: list[int] | None = None,
    quote_end_cancel: bool = False,
    real_stand_downs: int | None = None,
    last_stand_down_reason: str | None = None,
    lag_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One window row. In Phase 2 ``armed`` is always False; Phase 3 sets it True on an armed window
    and fills the money-math slots (real ``fills``/``wing_fills``, the ``realized_lock`` per set, the
    ``one_legged`` flag, ``held_legs`` for the settlement backfill, and ``exec_price_mismatches``).
    Discovery counts, would-be/real order counts, replaces, shadow fills-per-E with locks, alarms,
    per-connection lag, and the stand-down reason are all recorded so the report and the arming
    checklist read from one artifact."""
    spot_Sd_state = getattr(state, "spot_Sd", None) if state is not None else None
    spot_Su_state = getattr(state, "spot_Su", None) if state is not None else None
    bucket_ticker_state = None
    if state is not None and spot_Sd_state is not None:
        bucket_ticker_state = getattr(state, "bucket_tickers", {}).get(spot_Sd_state)
    # Prefer the LAST QUOTED bucket (captured while quoting) over the reset-at-close state.
    spot_Sd = last_quoted_Sd if last_quoted_Sd is not None else spot_Sd_state
    spot_Su = last_quoted_Su if last_quoted_Su is not None else spot_Su_state
    bucket_ticker = last_quoted_bucket_ticker or bucket_ticker_state
    # Stand-downs = real alarm/staleness/no-spot events (driver excludes the T-5 quote-end cancel);
    # legacy callers pass no ``real_stand_downs`` and fall back to the raw STAND_DOWN action count.
    stand_downs = int(real_stand_downs) if real_stand_downs is not None \
        else int(driver_counts.get("stand_down", 0))
    # The row's stand_down_reason carries the full-standdown reason (the _stand_down path) or, absent
    # that, the last REAL stand-down record seen while quoting.
    row_stand_down_reason = stand_down_reason if stand_down_reason is not None else last_stand_down_reason
    row: dict[str, Any] = {
        "close_time": close_time,
        "mode": resolved_mode,
        "effective_mode": effective_mode,
        "armed": bool(armed),
        "degrade": degrade,
        "degrade_reason": degrade_reason,
        "params_sha": params.sha256 if params is not None else params_sha,
        "stand_down": stand_down_reason is not None,
        "stand_down_reason": row_stand_down_reason,
        # discovery
        "strike_count": strike_count,
        "strike_generations": strike_generations,
        "bucket_count": bucket_count,
        "bucket_generations": bucket_generations,
        # recording-only co-settling 15M leg (list of tickers + frames tapped this window)
        "m15_tickers": list(m15_tickers or []),
        "m15_frames": int(m15_frames),
        # last-quoted spot bucket (captured while quoting; falls back to end-of-window state)
        "spot_bucket_ticker": bucket_ticker,
        "Sd": spot_Sd,
        "Su": spot_Su,
        "last_rest_price": (str(last_rest_price) if last_rest_price is not None else None),
        "last_desired_n": (str(last_desired_n) if last_desired_n is not None else None),
        "spot_buckets_quoted": list(spot_buckets_quoted or []),
        "quote_end_cancel": bool(quote_end_cancel),
        # would-be order activity (Phase 2 sends none)
        "would_places": int(driver_counts.get("would_place_rest", 0)),
        "would_cancels": int(driver_counts.get("would_cancel_rest", 0)),
        "would_takes": int(driver_counts.get("would_take_wings", 0)),
        "would_retries": int(driver_counts.get("would_retry_wing", 0)),
        "replaces": getattr(state, "replace_count", 0) if state is not None else 0,
        "sets_done": getattr(state, "sets_done", 0) if state is not None else 0,
        "stand_downs": stand_downs,
        "late_fills": int(driver_counts.get("late_fill", 0)),
        # shadow (the ideal fill rule running live) — per E, with locks
        "shadow": _shadow_summary(state),
        # alarms
        "alarms": int(ws_counts.get("alarm", 0)),
        "synth_counts": dict(executor_counts),
        # per-connection lag (the two-connection topology's measured gauges)
        "strike_lag_seconds": strike_lag_seconds,
        "bucket_lag_seconds": bucket_lag_seconds,
        # per-connection lag distribution {strikes,buckets: {mean,p99,last,n}} — the report reads the
        # p99 here for the falsifier scoreboard's data-age line (mirror of the summary's lag_stats)
        "lag_stats": lag_stats or {},
        # journal
        "journal_path": journal_path,
        "record_count": record_count,
        "ws_counts": dict(ws_counts),
        # Phase-3 money math (empty in Phase 2 / dry: defaults preserve the old shape)
        "fills": fills or [],
        "wing_fills": wing_fills or [],
        "held_legs": held_legs or [],
        "realized_lock": (str(realized_lock) if realized_lock is not None else None),
        "one_legged": one_legged,
        "rests_placed": int(rests_placed),
        "rests_rejected": int(rests_rejected),
        "wing_batches": int(wing_batches),
        "exec_price_mismatches": exec_price_mismatches or [],
        "cancels_attempted": int(cancels_attempted),
        "cancels_confirmed": int(cancels_confirmed),
        "cancel_404s": int(cancel_404s),
        "cancels_via_status": int(cancels_via_status),
        "cancels_expired": int(cancels_expired),
        "rest_invariant_violations": int(rest_invariant_violations),
        # settlement backfill slots (the sweep appends its own backfill row)
        "realized_unsettled": bool(realized_unsettled),
        "unsettled_legs": (held_legs or []) if realized_unsettled else [],
        "settlement": settlement,
        "realized_delta": (str(realized_delta) if realized_delta is not None else None),
        "flushed_at": now,
    }
    return row


# ---------------------------------------------------------------------------
# Settlement backfill (Phase 3) — mirror pilot_ledger.build_backfill_entry
# ---------------------------------------------------------------------------
def v32_set_floor_dollars(num_legs_held: int, count: int = 1) -> Decimal:
    """The GUARANTEED payoff floor of a V3.2 pin position held to settlement, per the fixed geometry
    (bucket-NO(B) + YES@Sd + NO@Su, adjacent strikes bounding B): three legs pay $2 at EVERY
    settlement; any two of the three pay >= $1; a lone leg is directional ($0 floor). So floor =
    max(0, held - 1) dollars per contract. Booked conservatively at close; the backfill corrects it."""
    return Decimal(max(0, int(num_legs_held) - 1)) * Decimal(str(count))


def v32_pending_credit(rows: list[dict[str, Any]], utc_day: str) -> tuple[Decimal, Decimal]:
    """The (pessimistic, optimistic) BAND on settlement credit still owed today by un-backfilled
    unsettled windows (S4 floor-netting RULING, Phase 4 — coordinator 2026-09-13). Σ over rows
    (realized_unsettled, this UTC day, no backfill yet), per set by the legs held to settlement:

      * complete 3-leg pin -> guaranteed floor $2.00, upside $0.00 -> (2.00, 2.00)  (pays $2 everywhere)
      * any 2-leg subset   -> guaranteed floor $1.00, upside $1.00 -> (1.00, 2.00)  ($1 floor, $2 best)
      * a lone leg          -> guaranteed floor $0.00, upside $1.00 -> (0.00, 1.00)  (directional 0..1)

    ``pessimistic`` = Σ guaranteed floor (``v32_set_floor_dollars``) — the credit that arrives under
    EVERY resolution of the unfinalized legs; ``optimistic`` = Σ best-case payoff ``min(#legs, 2)`` =
    floor + upside. The banded S4 nets the guaranteed floor into BOTH bounds (an unsettled-but-
    guaranteed complete pin's cash dip is credited back, never spuriously latching a day loss), while
    the upside is credited only into the optimistic bound (a lone leg's optimistic $1 can still resolve
    to $0, and a 2-leg subset's $2 to $1). Feeds ``v32_s4_decision`` so a pending settlement can only
    move the latch number within this band, never past what a resolution can actually deliver."""
    done: set[str] = {str(r.get("backfill_of")) for r in rows if r.get("backfill_of")}
    pessimistic = Decimal(0)
    optimistic = Decimal(0)
    for r in rows:
        if not r.get("realized_unsettled"):
            continue
        if str(r.get("close_time", ""))[:10] != utc_day:
            continue
        if str(r.get("close_time")) in done:
            continue
        legs = r.get("unsettled_legs") or r.get("held_legs") or []
        n_legs = len(legs)
        pessimistic += v32_set_floor_dollars(n_legs)   # guaranteed floor: 3->2, 2->1, 1->0, 0->0
        optimistic += Decimal(min(n_legs, 2))          # best-case payoff: 3->2, 2->2, 1->1, 0->0
    return pessimistic, optimistic


def build_v32_backfill_row(
    window_entry: dict[str, Any], results: dict[str, str], payoff: Decimal,
    floor_booked: Decimal, now: float,
) -> dict[str, Any]:
    """A ledger line recording a V3.2 settlement backfill (mirror of
    ``pilot_ledger.build_backfill_entry``). ``realized_delta`` is the settlement ``payoff`` NET of the
    conservative floor already booked at close, so a complete set (payoff $2, floor $2) corrects by $0
    and a one-legged set corrects by its true settlement minus the $1 floor."""
    realized = Decimal(str(payoff)) - Decimal(str(floor_booked))
    return {
        "close_time": window_entry.get("close_time"),
        "mode": "backfill",
        "backfill_of": window_entry.get("close_time"),
        "armed": True,
        "held_legs": window_entry.get("unsettled_legs") or window_entry.get("held_legs"),
        "settlement_results": results,
        "settlement_payoff": str(Decimal(str(payoff))),
        "floor_netted": str(Decimal(str(floor_booked))),
        "realized_delta": str(realized),
        "realized_unsettled": False,
        "flushed_at": now,
    }


def v32_settlement_backfill_sweep(
    rows: list[dict[str, Any]],
    fetch_result,
    now: float,
) -> list[dict[str, Any]]:
    """Compute the backfill rows to append for every prior window still ``realized_unsettled`` whose
    held tickers have all settled. Idempotent (skips a window that already has a ``backfill_of`` row);
    fail-closed (a window with any unsettled/unavailable leg is left for a later wake — no row).

    ``fetch_result(ticker) -> 'yes' | 'no' | None`` is injected (the proxy /markets read in prod, a
    fake in tests). Uses ``service.ledger.settlement_payoff`` (which pays $1/contract where the held
    leg's side matches the market result — so a complete pin yields exactly $2)."""
    from service.ledger import settlement_payoff

    done: set[str] = {str(r.get("backfill_of")) for r in rows if r.get("backfill_of")}
    # most-recent unsettled row per window wins
    pending: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("realized_unsettled") and (r.get("unsettled_legs") or r.get("held_legs")):
            pending[str(r.get("close_time"))] = r
    out: list[dict[str, Any]] = []
    for window, entry in pending.items():
        if window in done:
            continue
        legs = entry.get("unsettled_legs") or entry.get("held_legs") or []
        results: dict[str, str] = {}
        incomplete = False
        for leg in legs:
            tk = leg["ticker"] if isinstance(leg, dict) else leg[0]
            res = fetch_result(tk)
            if res not in ("yes", "no"):
                incomplete = True
                break
            results[tk] = res
        if incomplete:
            continue  # wait for a later wake
        payoff = settlement_payoff(legs, results)
        floor = v32_set_floor_dollars(len(legs))
        out.append(build_v32_backfill_row(entry, results, payoff, floor, now))
    return out
