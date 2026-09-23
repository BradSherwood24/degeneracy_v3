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

from service.paths import ledger_dir_v32, ledger_path_v32

# Writable ledger location routes through service.paths so DV3_DATA_DIR can relocate it; with
# DV3_DATA_DIR unset both values are byte-identical to the historic pilot/ledger default.
DEFAULT_V32_LEDGER_DIR = ledger_dir_v32()
DEFAULT_V32_LEDGER_PATH = ledger_path_v32()


def _json_default(obj: object) -> str:
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


# Human-readable gloss stamped on every armed money-math row so a reader never mis-reads ``realized_delta``.
REALIZED_DELTA_NOTE = (
    "floor - total cost incl. all fees x count (venue per-fill fee ceil(0.07*p*(1-p)*count)); "
    "per-set truth = balance delta at settlement; falsifier reads realized_lock (per contract, core "
    "state), not this field. Each fill carries fee (per contract) + fee_total (all lots) + fee_source."
)


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
    # BUCKET-LEG + COUNT-AWARE FLOOR (Fable 2026-09-20): the count-aware guaranteed floor ACTUALLY
    # netted into ``realized_delta`` at close (Σ per batch ``v32_set_floor_dollars(held_this,
    # fill_count)``). Additive: absent -> None so older rows still parse; the backfill reads it to
    # net the exact floor that was booked (never a count-blind $1).
    floor_booked: Decimal | None = None,
    rests_placed: int = 0,
    rests_rejected: int = 0,
    wing_batches: int = 0,
    exec_price_mismatches: list[Any] | None = None,
    # PARTIAL-FILL WINGS (Brad 2026-09-18) — additive per-set money math. At ``contracts`` = 1 a window
    # is one set and these carry the single-set values (rest_fills=1, wing_batch_sets=1 entry,
    # lots_filled 0/1, partials 0); every existing key keeps its exact meaning. ``wing_batch_sets`` (a
    # list) is named apart from the existing int ``wing_batches`` count to avoid colliding with it.
    rest_fills: list[Any] | None = None,
    wing_batch_sets: list[Any] | None = None,
    lots_filled: int = 0,
    lots_unfilled_at_quote_end: int = 0,
    partial_fills: int = 0,
    # cancel / venue-truth counters (2026-09-14 shard fix; 2026-09-15 quote-end-race adds the last two)
    cancels_attempted: int = 0,
    cancels_confirmed: int = 0,
    cancel_404s: int = 0,
    cancels_via_status: int = 0,
    cancels_expired: int = 0,
    rest_invariant_violations: int = 0,
    # read-path-lag phantom invariant counters (2026-09-15 18:00Z fix): phantoms are filtered
    # resting-list hits (NOT real violations); rechecks count the re-read-before-declare passes.
    rest_invariant_phantoms: int = 0,
    rest_invariant_rechecks: int = 0,
    # amend-first replace counters (Brad 2026-09-15) — additive, default 0 so older rows still parse.
    amends_attempted: int = 0,
    amends_confirmed: int = 0,
    amends_failed: int = 0,
    amend_fallbacks: int = 0,
    fills_on_amend: int = 0,
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
        # would-be shadow fills SUPPRESSED by the quoting-window gate (print outside T-15..T-5, which
        # the live path could never have taken). Additive: ``.get``/default 0 so older rows still parse.
        "shadow_fills_outside_window": int(driver_counts.get("shadow_fill_outside_window", 0)),
        # would-be shadow fills SUPPRESSED because the solved n < n_min (the live path stands down
        # n_below_min and would never rest there). Additive (Registration 3 nit, 2026-09-19).
        "shadow_fills_below_min": int(driver_counts.get("shadow_fill_below_min", 0)),
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
        # PARTIAL-FILL WINGS additive slots (per-set money math)
        "rest_fills": rest_fills or [],
        "wing_batch_sets": wing_batch_sets or [],
        "lots_filled": int(lots_filled),
        "lots_unfilled_at_quote_end": int(lots_unfilled_at_quote_end),
        "partial_fills": int(partial_fills),
        "cancels_attempted": int(cancels_attempted),
        "cancels_confirmed": int(cancels_confirmed),
        "cancel_404s": int(cancel_404s),
        "cancels_via_status": int(cancels_via_status),
        "cancels_expired": int(cancels_expired),
        "rest_invariant_violations": int(rest_invariant_violations),
        "rest_invariant_phantoms": int(rest_invariant_phantoms),
        "rest_invariant_rechecks": int(rest_invariant_rechecks),
        # amend-first replace counters (Brad 2026-09-15) — surfaced next to `replaces` by the report.
        "amends_attempted": int(amends_attempted),
        "amends_confirmed": int(amends_confirmed),
        "amends_failed": int(amends_failed),
        "amend_fallbacks": int(amend_fallbacks),
        "fills_on_amend": int(fills_on_amend),
        # settlement backfill slots (the sweep appends its own backfill row)
        "realized_unsettled": bool(realized_unsettled),
        "unsettled_legs": (held_legs or []) if realized_unsettled else [],
        "settlement": settlement,
        "realized_delta": (str(realized_delta) if realized_delta is not None else None),
        # count-aware floor actually netted into realized_delta (bucket-NO + each filled wing, per
        # batch x fill_count). The backfill nets THIS, not a count-blind $1.
        "floor_booked": (str(floor_booked) if floor_booked is not None else None),
        "realized_delta_note": (REALIZED_DELTA_NOTE if realized_delta is not None else None),
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


def _v32_floor_booked_for_entry(
    entry: dict[str, Any], legs: list[Any]
) -> Decimal:
    """The count-aware guaranteed floor a window row ACTUALLY booked into ``realized_delta`` at close.

    Priority: (1) the explicit ``floor_booked`` key (new rows, Fable 2026-09-20); (2) reconstruct it
    from ``wing_batch_sets`` = Σ ``v32_set_floor_dollars(held_legs, fill_count)`` per batch (rows the
    partial-fill build wrote, which carry the per-batch held count + fill_count even when the row's
    ``unsettled_legs`` list dropped the bucket leg); (3) fall back to the legs present, count-aware
    (an old-shape row has count-1 legs, so this equals the pre-count ``v32_set_floor_dollars(len(legs))``
    — fail-closed as today)."""
    fb = entry.get("floor_booked")
    if fb is not None:
        return Decimal(str(fb))
    batches = entry.get("wing_batch_sets")
    if isinstance(batches, list) and batches:
        total = Decimal(0)
        for b in batches:
            held = int(b.get("held_legs", 0) or 0)
            cnt = int(b.get("fill_count", 1) or 1)
            total += v32_set_floor_dollars(held, cnt)
        return total
    counts = []
    for lg in legs:
        c = lg["count"] if isinstance(lg, dict) else (lg[2] if len(lg) > 2 else 1)
        try:
            counts.append(int(c))
        except (TypeError, ValueError):
            counts.append(1)
    # Fail closed on a malformed mixed-count legs list: take the SMALLER count (never the optimistic
    # larger one) so a reconstructed floor can only UNDER-credit, never over-credit. A well-formed set
    # has one count across its legs, so this is exact for every real row.
    cnt = min(counts) if counts else 1
    return v32_set_floor_dollars(len(legs), cnt)


def v32_pending_credit(rows: list[dict[str, Any]], utc_day: str) -> tuple[Decimal, Decimal]:
    """The (pessimistic, optimistic) BAND on settlement credit still owed today by un-backfilled
    unsettled windows (S4 floor-netting RULING, Phase 4 — coordinator 2026-09-13). Σ over rows
    (realized_unsettled, this UTC day, no backfill yet), per set by the legs held to settlement.

    COUNT + BUCKET AWARE (Fable 2026-09-20): the band scales by the SET SIZE (``count`` contracts) and
    reads the per-batch held-leg count from ``wing_batch_sets`` when present, so a complete size-2 pin
    (which a count-blind band scored at $1/$2 while its guaranteed cash was $4) is credited its true
    guaranteed floor. Per set of ``held`` legs at ``count`` contracts:

      * complete 3-leg pin -> floor $2.00 x count, upside $0.00           (pays $2/contract everywhere)
      * any 2-leg subset   -> floor $1.00 x count, upside $1.00 x count   ($1 floor, $2 best / contract)
      * a lone leg          -> floor $0.00,         upside $1.00 x count   (directional 0..$1 / contract)

    So a complete size-2 pin -> (4.00, 4.00); a 2-leg size-2 subset -> (2.00, 4.00); a lone size-2 leg
    -> (0.00, 2.00). ``pessimistic`` = Σ guaranteed floor (``v32_set_floor_dollars(held, count)``); the
    credit that arrives under EVERY resolution. ``optimistic`` = Σ best-case ``min(held, 2) x count`` =
    floor + upside. The banded S4 nets the guaranteed floor into BOTH bounds (an unsettled-but-
    guaranteed complete pin's cash dip is credited back, never spuriously latching a day loss), while
    the upside is credited only into the optimistic bound. Feeds ``v32_s4_decision`` so a pending
    settlement can only move the latch number within this band, never past what a resolution delivers.

    When a row carries ``wing_batch_sets`` the sets are read from it (each batch's held count x its
    fill_count); a row without it (a truly old ledger row) falls back to the legs present with their
    counts — an old-shape size-1 row has count-1 legs so its band is unchanged from the pre-count code."""
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
        batches = r.get("wing_batch_sets")
        if isinstance(batches, list) and batches:
            for b in batches:
                held = int(b.get("held_legs", 0) or 0)
                cnt = int(b.get("fill_count", 1) or 1)
                pessimistic += v32_set_floor_dollars(held, cnt)   # floor: 3->2, 2->1, 1->0 (x count)
                optimistic += Decimal(min(held, 2)) * Decimal(cnt)  # best: 3->2, 2->2, 1->1 (x count)
            continue
        legs = r.get("unsettled_legs") or r.get("held_legs") or []
        n_legs = len(legs)
        counts = []
        for lg in legs:
            c = lg["count"] if isinstance(lg, dict) else (lg[2] if len(lg) > 2 else 1)
            try:
                counts.append(int(c))
            except (TypeError, ValueError):
                counts.append(1)
        # Fail closed on a malformed mixed-count legs list: the SMALLER count. For the pessimistic
        # (guaranteed floor) bound an under-stated floor only makes the day loss look larger (fail-safe
        # for S4); a well-formed set has one count across its legs, so this is exact for every real row.
        cnt = min(counts) if counts else 1
        pessimistic += v32_set_floor_dollars(n_legs, cnt)   # guaranteed floor (count-aware)
        optimistic += Decimal(min(n_legs, 2)) * Decimal(cnt)  # best-case payoff (count-aware)
    return pessimistic, optimistic


def build_v32_backfill_row(
    window_entry: dict[str, Any], results: dict[str, str], payoff: Decimal,
    floor_booked: Decimal, now: float,
    legs_priced: int | None = None, backfill_note: str | None = None,
) -> dict[str, Any]:
    """A ledger line recording a V3.2 settlement backfill (mirror of
    ``pilot_ledger.build_backfill_entry``). ``realized_delta`` is the settlement ``payoff`` NET of the
    count-aware floor already booked at close, so a COMPLETE set (which pays exactly $2 x count and had
    a $2 x count floor booked) corrects by $0 at ANY size, and a one-legged / partial set corrects by
    its true settlement minus the floor that was actually netted. ``legs_priced`` (additive) records how
    many held legs — bucket-NO + each filled wing — ``settlement_payoff`` priced; ``backfill_note`` says
    how the floor was sourced (explicit ``floor_booked`` vs reconstructed)."""
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
        "legs_priced": legs_priced,
        "backfill_note": backfill_note,
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
        # Net the floor ACTUALLY booked at close (count-aware, incl. the bucket leg): the explicit
        # ``floor_booked`` key when present, else reconstructed from ``wing_batch_sets`` / the legs.
        # A count-blind ``v32_set_floor_dollars(len(legs))`` netted $1 for every set (the bug), so a
        # complete size-2 pin over-corrected by $3.
        floor = _v32_floor_booked_for_entry(entry, legs)
        note = ("floor_booked (explicit)" if entry.get("floor_booked") is not None
                else ("reconstructed from wing_batch_sets"
                      if isinstance(entry.get("wing_batch_sets"), list) and entry.get("wing_batch_sets")
                      else "reconstructed from legs (fail-closed)"))
        out.append(build_v32_backfill_row(
            entry, results, payoff, floor, now, legs_priced=len(legs), backfill_note=note))
    return out
