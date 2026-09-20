"""report.py — a read-only per-window report over the V3.2 ledger + journals.

``python -m service.v32.report [--days N]`` prints one row per window: mode, spot bucket, replaces,
shadow fills by E with locks, data-age (per-connection lag) stats, and stand-downs, then a totals
block. Reads ONLY ``ledger/v32_ledger.jsonl`` (and, when present, the summaries) — no network, no
sealed file, no orders. Once Phase 3 books real fills the same table gains the live columns from the
row's ``fills``/``settlement`` slots; Phase 2 shows the shadow (the ideal fill rule running live) and
the would-be order counts.
"""

from __future__ import annotations

import argparse
import json
import math
from decimal import Decimal, InvalidOperation
from typing import Any

from service._simlaw import fee_rate as _FEE_RATE
from service.v32.falsifier_pins import (
    V32_CAPTURE_RATIO_MIN,
    V32_FALSIFIER_MAX_EXEC_GAP_CENTS,
    V32_FALSIFIER_MAX_ONE_LEGGED,
    V32_FALSIFIER_MIN_FILL_RATE_PER_DAY,
    V32_FALSIFIER_MIN_MEAN_LOCK_CENTS,
    V32_FALSIFIER_MIN_N,
    V32_FALSIFIER_MIN_PCT_POSITIVE,
    V32_FALSIFIER_SHADOW_GAP_E,
)
from service.v32.ledger import (
    DEFAULT_V32_LEDGER_PATH,
    _v32_floor_booked_for_entry,
    load_v32_rows,
    v32_set_floor_dollars,
)
from service.v32.params import load_v32_params

_ONE = Decimal("1")


def _dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _shadow_below_min(sub: dict[str, Any], n_min: Decimal) -> bool:
    """True when a shadow fill record's derived n (1 - offer) is below ``n_min`` -- the live path
    stands down (``n_below_min``) at such an n and would never have rested there, so the shadow fill is
    not a live-reachable counterfactual (Registration 3 nit, 2026-09-19). The record's top-level ``n``
    is the end-of-window resolved n (usually None), so n is derived from the fill's stored ``offer``; a
    record without an ``offer`` (pre-offer shape) is NOT excluded."""
    offer = _dec(sub.get("offer"))
    if offer is None:
        return False
    return (_ONE - offer) < n_min


def _recent_days(rows: list[dict[str, Any]], days: int | None) -> list[dict[str, Any]]:
    """Rows on the most recent ``days`` UTC calendar days (by close_time). None -> all rows."""
    if days is None:
        return rows
    ds = sorted({str(r.get("close_time", ""))[:10] for r in rows if r.get("close_time")}, reverse=True)
    keep = set(ds[:days])
    return [r for r in rows if str(r.get("close_time", ""))[:10] in keep]


def _fmt(v: Any, width: int) -> str:
    return str("" if v is None else v).rjust(width)[:max(width, 3)] if isinstance(v, (int, float)) \
        else str("" if v is None else v).ljust(width)


def _shadow_cell(shadow: dict[str, Any], e_key: str) -> str:
    """A compact 'n@lock' cell for one E: e.g. '0.45/+10.4c', or '-' when no shadow fill."""
    sub = shadow.get(e_key)
    if not sub or not sub.get("filled"):
        return "-"
    n = _dec(sub.get("n"))
    lock = _dec(sub.get("lock"))
    n_s = f"{n:.2f}" if n is not None else "?"
    if lock is None:
        return f"{n_s}/pending"
    return f"{n_s}/{lock * 100:+.1f}c"


def build_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold the rows into per-window lines + totals (pure; the CLI renders it)."""
    # the union of shadow Es seen (sorted), for stable columns
    e_keys: list[str] = sorted({k for r in rows for k in (r.get("shadow") or {}).keys()})
    windows: list[dict[str, Any]] = []
    tot_would_places = 0
    tot_replaces = 0
    tot_stand_downs = 0
    tot_late_fills = 0
    tot_invariant_violations = 0
    tot_invariant_phantoms = 0
    tot_invariant_rechecks = 0
    # amend-first replace totals (Brad 2026-09-15) — additive; older rows lack the keys -> .get default 0.
    tot_amends = 0
    tot_amends_confirmed = 0
    tot_amends_failed = 0
    tot_amend_fallbacks = 0
    tot_fills_on_amend = 0
    shadow_fills: dict[str, int] = {k: 0 for k in e_keys}
    shadow_locks: dict[str, list[Decimal]] = {k: [] for k in e_keys}
    lags: list[Decimal] = []
    for r in rows:
        shadow = r.get("shadow") or {}
        for k in e_keys:
            sub = shadow.get(k)
            if sub and sub.get("filled"):
                shadow_fills[k] += 1
                lk = _dec(sub.get("lock"))
                if lk is not None:
                    shadow_locks[k].append(lk)
        for lag_field in ("strike_lag_seconds", "bucket_lag_seconds"):
            lg = _dec(r.get(lag_field))
            if lg is not None:
                lags.append(lg)
        tot_would_places += int(r.get("would_places", 0) or 0)
        tot_replaces += int(r.get("replaces", 0) or 0)
        tot_late_fills += int(r.get("late_fills", 0) or 0)
        tot_invariant_violations += int(r.get("rest_invariant_violations", 0) or 0)
        tot_invariant_phantoms += int(r.get("rest_invariant_phantoms", 0) or 0)
        tot_invariant_rechecks += int(r.get("rest_invariant_rechecks", 0) or 0)
        tot_amends += int(r.get("amends_attempted", 0) or 0)
        tot_amends_confirmed += int(r.get("amends_confirmed", 0) or 0)
        tot_amends_failed += int(r.get("amends_failed", 0) or 0)
        tot_amend_fallbacks += int(r.get("amend_fallbacks", 0) or 0)
        tot_fills_on_amend += int(r.get("fills_on_amend", 0) or 0)
        if r.get("stand_down"):
            tot_stand_downs += 1
        windows.append(
            {
                "close_time": r.get("close_time"),
                "mode": r.get("effective_mode") or r.get("mode"),
                "bucket": r.get("spot_bucket_ticker") or ("STAND DOWN" if r.get("stand_down") else "-"),
                "Sd": r.get("Sd"),
                "last_rest": r.get("last_rest_price"),
                "replaces": r.get("replaces", 0),
                "would_places": r.get("would_places", 0),
                "late_fills": r.get("late_fills", 0),
                "shadow": {k: _shadow_cell(shadow, k) for k in e_keys},
                "m15_frames": int(r.get("m15_frames", 0) or 0),
                "strike_lag": r.get("strike_lag_seconds"),
                "bucket_lag": r.get("bucket_lag_seconds"),
                "stand_down_reason": r.get("stand_down_reason"),
            }
        )
    mean_locks = {
        k: (sum(v, Decimal(0)) / Decimal(len(v))) if v else None for k, v in shadow_locks.items()
    }
    mean_lag = (sum(lags, Decimal(0)) / Decimal(len(lags))) if lags else None
    return {
        "e_keys": e_keys,
        "windows": windows,
        "falsifier": build_falsifier_scoreboard(rows),
        "reconciliation": build_ledger_reconciliation(rows),
        "totals": {
            "windows": len(rows),
            "would_places": tot_would_places,
            "replaces": tot_replaces,
            "amends": tot_amends,
            "amends_confirmed": tot_amends_confirmed,
            "amends_failed": tot_amends_failed,
            "amend_fallbacks": tot_amend_fallbacks,
            "fills_on_amend": tot_fills_on_amend,
            "stand_downs": tot_stand_downs,
            "late_fills": tot_late_fills,
            "rest_invariant_violations": tot_invariant_violations,
            "rest_invariant_phantoms": tot_invariant_phantoms,
            "rest_invariant_rechecks": tot_invariant_rechecks,
            "shadow_fills": shadow_fills,
            "shadow_mean_lock": mean_locks,
            "mean_lag_seconds": mean_lag,
        },
    }


def _percentile(sorted_vals: list[Decimal], pct: Decimal) -> Decimal | None:
    """Nearest-rank percentile of an already-sorted list (pct in [0,100]). None on empty."""
    if not sorted_vals:
        return None
    n = len(sorted_vals)
    # nearest-rank: rank = ceil(pct/100 * n), clamped to [1, n]
    import math
    rank = int(math.ceil(float(pct) / 100.0 * n))
    rank = max(1, min(n, rank))
    return sorted_vals[rank - 1]


def _lag_stats_p99(rows: list[dict[str, Any]], conn: str) -> Decimal | None:
    """The MAX over windows of one connection's per-window ``lag_stats[conn]['p99']`` (the honest
    worst-tail data-age across the reported windows). None when NO row carries a lag_stats p99 for
    ``conn`` (legacy rows predating the field, or a run that never sampled)."""
    vals: list[Decimal] = []
    for r in rows:
        sub = (r.get("lag_stats") or {}).get(conn)
        if isinstance(sub, dict):
            v = _dec(sub.get("p99"))
            if v is not None:
                vals.append(v)
    return max(vals) if vals else None


def _row_set_events(r: dict[str, Any]) -> list[dict[str, Any]]:
    """The set (rest-fill) events of one armed ledger row, each ``{lock, completed, one_legged}`` with
    ``lock`` in CENTS per contract (None for an incomplete/one-legged set).

    PARTIAL-FILL WINGS (Brad 2026-09-18): a row's ``wing_batch_sets`` list carries one entry per rest
    fill event (a wing batch); each completed batch is one set. A row WITHOUT that list (an older ledger
    row, or a dry window) falls back to the single-set scalar (``realized_lock`` / ``one_legged`` /
    ``realized_unsettled``) — so the scoreboard over an existing ledger is byte-identical to the
    pre-partial report."""
    batches = r.get("wing_batch_sets")
    if isinstance(batches, list) and batches:
        out: list[dict[str, Any]] = []
        for b in batches:
            lk = _dec(b.get("realized_lock"))
            out.append({
                "lock": (lk * 100) if lk is not None else None,
                "completed": lk is not None,
                "one_legged": bool(b.get("one_legged")),
            })
        return out
    # legacy single-set fallback (unchanged semantics)
    rlock = _dec(r.get("realized_lock"))
    is_fill = bool(r.get("realized_unsettled")) or rlock is not None or bool(r.get("one_legged"))
    if not is_fill:
        return []
    return [{
        "lock": (rlock * 100) if rlock is not None else None,
        "completed": rlock is not None,
        "one_legged": bool(r.get("one_legged")),
    }]


def build_falsifier_scoreboard(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The pre-registered falsifier scoreboard, computed from the SAME [pin] constants the document
    commits to (``service.v32.falsifier_pins``). Pure; the CLI renders it.

    A *completed set* is one REST-FILL EVENT (a wing batch) whose both wings filled (PARTIAL-FILL WINGS,
    Brad 2026-09-18); a *one-legged set* is a rest-fill event flagged ``one_legged``. Realized lock is
    reported PER CONTRACT so per-set stats stay comparable to the size-1 history, and the fill rate is
    SET EVENTS per armed day. When a row carries the per-set ``wing_batch_sets`` list (contracts>=1
    windows written by the partial-fill build) the sets are read from it; a row without it (an older
    ledger row, a dry window) falls back to the single-set scalar (``realized_lock``/``one_legged``), so
    the scoreboard over an EXISTING ledger is byte-identical to the pre-partial report. The verdict
    (``ALIVE-so-far`` / ``KILL`` / ``n<MIN_N pending``) is decided ONLY once ``n`` completed sets exist
    and applies every pinned threshold; any miss at n >= MIN_N is a KILL (no re-spec)."""
    e = V32_FALSIFIER_SHADOW_GAP_E
    n = 0                                     # completed sets
    legged = 0
    fills_total = 0                           # rest-fill events (completed + incomplete + one-legged)
    fill_days: set[str] = set()
    armed_days: set[str] = set()
    live_locks_c: list[Decimal] = []          # realized lock in cents (completed sets), per contract
    shadow_locks_c: list[Decimal] = []        # shadow E=0.10 lock in cents (any window it filled)
    gaps_c: list[Decimal] = []                # shadow E=0.10 - live lock in cents (both present)
    replaces: list[Decimal] = []
    strike_lags: list[Decimal] = []
    bucket_lags: list[Decimal] = []
    # CAPTURE RATIO (MEASUREMENT CLARIFICATION 3, 2026-09-19): a bounded per-WINDOW fraction --
    # numerator = armed+bucket windows with BOTH a shadow E=0.10 fill AND >= 1 completed live set;
    # denominator = armed+bucket windows with a shadow E=0.10 fill. Both count each window 0/1, so a
    # two-set window (contracts>=2) counts once and a live-set-without-shadow window never pushes the
    # ratio above 1 (N1 fix, PR #66 review). A window the live path stood down (for any pilot-side
    # reason) has no completed set and so counts as a MISS -- that is what the gate should catch. A
    # below-n_min shadow fill is NOT live-reachable and is excluded from the denominator (see below).
    capture_live_sets = 0                     # armed+bucket windows with a shadow fill AND a live set
    capture_shadow_fills = 0                  # armed+bucket windows the shadow E=0.10 (validly) filled
    # n_min gate (Registration 3 nit): the live path stands down (n_below_min) when the solved n <
    # params.n_min, so a shadow fill whose derived n (1 - offer) is below n_min is not a live-reachable
    # counterfactual (same class as the T-15..T-5 window gate). Exclude it from the capture denominator
    # AND the shadow lock / exec-gap stats, deriving n from the fill's stored ``offer`` (the record's
    # top-level ``n`` is the end-of-window resolved n, usually None). New rows suppress it upstream
    # (``shadow_fills_below_min``); this excludes it from EXISTING rows too so history reads the same.
    n_min = load_v32_params().n_min
    derived_shadow_below_min = 0
    for r in rows:
        if not r.get("armed"):
            continue
        # armed+bucket gate for the capture ratio (effective_mode is the registered arming mark).
        capture_window = r.get("effective_mode") == "armed" and bool(r.get("spot_bucket_ticker"))
        day = str(r.get("close_time", ""))[:10]
        armed_days.add(day)
        rep = _dec(r.get("replaces"))
        if rep is not None:
            replaces.append(rep)
        for bucket, lag_field in ((strike_lags, "strike_lag_seconds"),
                                  (bucket_lags, "bucket_lag_seconds")):
            lg = _dec(r.get(lag_field))
            if lg is not None:
                bucket.append(lg)
        shadow = r.get("shadow") or {}
        sub = shadow.get(e)
        shadow_filled = bool(sub and sub.get("filled"))
        below_min = shadow_filled and _shadow_below_min(sub, n_min)
        if below_min:
            derived_shadow_below_min += 1
        # a VALID shadow fill is filled AND at/above n_min (live-reachable); below-min is excluded from
        # the denominator and the lock/exec-gap stats.
        shadow_valid = shadow_filled and not below_min
        slock = _dec(sub.get("lock")) if shadow_valid else None
        if slock is not None:
            shadow_locks_c.append(slock * 100)
        events = _row_set_events(r)  # one entry per rest-fill event (batch), or the legacy single set
        if events:
            fill_days.add(day)
        row_has_set = False
        for ev in events:
            fills_total += 1
            if ev["one_legged"]:
                legged += 1
            if ev["lock"] is not None:  # a completed set
                n += 1
                row_has_set = True
                live_locks_c.append(ev["lock"])
                if slock is not None:
                    gaps_c.append(slock * 100 - ev["lock"])
        # bounded per-window capture: one shadow-filled window, +1 to the numerator iff it also had a set
        if capture_window and shadow_valid:
            capture_shadow_fills += 1
            if row_has_set:
                capture_live_sets += 1

    n_days = len(armed_days)  # distinct armed UTC calendar days (retained key; NOT the fill-rate denom)
    # armed_windows = number of armed windows the pilot actually RAN (ledger rows whose effective_mode
    # is armed, whatever their stand-down reason). A dark hour (reboot/proxy-down/task-not-started)
    # leaves no row and so contributes 0. Backfill rows carry no effective_mode, so they never count
    # here (they would double-count a window otherwise). Registered clarification 2026-09-15 ~13:30Z.
    armed_windows = sum(1 for r in rows if r.get("effective_mode") == "armed")
    # "armed evaluation days" = armed_windows / 24 (an hour is a 24th of a day). fill_rate is the
    # sets-per-day the >= 2.0 [pin] gate reads; None only when NO armed window ran.
    armed_days = Decimal(armed_windows) / Decimal(24)
    # measurement-integrity gauge: would-be shadow fills SUPPRESSED by the T-15..T-5 window gate
    # (prints outside the live quoting window the live path could never have taken). Additive: older
    # ledger rows lack the key, so ``.get(...,0)``. Registered clarification 2026-09-15 ~18:10Z / PR #54.
    shadow_fills_outside_window = sum(int(r.get("shadow_fills_outside_window", 0) or 0) for r in rows)
    # would-be shadow fills SUPPRESSED because the solved n < n_min (the live path stands down
    # n_below_min). New rows carry the upstream counter ``shadow_fills_below_min``; existing rows are
    # caught by the fold's derived-from-offer exclusion. Sum both for the honest total (a suppressed
    # new row has no filled shadow record, so the two never double-count the same window).
    shadow_fills_below_min = (sum(int(r.get("shadow_fills_below_min", 0) or 0) for r in rows)
                              + derived_shadow_below_min)
    # fills_total (rest-fill events = wing batches) is accumulated in the fold above.
    slocks = sorted(live_locks_c)
    mean_lock = (sum(live_locks_c, Decimal(0)) / Decimal(n)) if n else None
    median_lock = _percentile(slocks, Decimal(50)) if n else None
    p10_lock = _percentile(slocks, Decimal(10)) if n else None
    min_lock = slocks[0] if n else None
    pos = sum(1 for x in live_locks_c if x > 0)
    pct_positive = (Decimal(pos) * 100 / Decimal(n)) if n else None
    fill_rate = (Decimal(fills_total) / armed_days) if armed_windows else None
    # CAPTURE RATIO (Registration 3): what fraction of the pumps the shadow proved were available did
    # the live path actually capture. None when the shadow never filled an armed+bucket window (no
    # availability to measure execution against).
    capture_ratio = (Decimal(capture_live_sets) / Decimal(capture_shadow_fills)
                     if capture_shadow_fills else None)
    shadow_mean = ((sum(shadow_locks_c, Decimal(0)) / Decimal(len(shadow_locks_c)))
                   if shadow_locks_c else None)
    exec_gap = (sum(gaps_c, Decimal(0)) / Decimal(len(gaps_c))) if gaps_c else None
    replaces_mean = (sum(replaces, Decimal(0)) / Decimal(len(replaces))) if replaces else None
    strike_p99 = _percentile(sorted(strike_lags), Decimal(99)) if strike_lags else None
    bucket_p99 = _percentile(sorted(bucket_lags), Decimal(99)) if bucket_lags else None
    # data-age p99 from the per-window lag_stats (max across windows) — read over ALL rows (not just
    # armed), since it is an operational health gauge; the scoreboard prefers these and falls back to
    # the legacy per-row *_lag_seconds p99 (armed rows) only when no row carries lag_stats.
    strike_stats_p99 = _lag_stats_p99(rows, "strikes")
    bucket_stats_p99 = _lag_stats_p99(rows, "buckets")

    # --- verdict from the [pin] constants -------------------------------------------------------
    if n < V32_FALSIFIER_MIN_N:
        verdict = f"n<{V32_FALSIFIER_MIN_N} pending (n={n})"
    else:
        fails: list[str] = []
        if mean_lock is None or mean_lock < V32_FALSIFIER_MIN_MEAN_LOCK_CENTS:
            fails.append(f"mean lock {mean_lock}c < +{V32_FALSIFIER_MIN_MEAN_LOCK_CENTS}c")
        if pct_positive is None or pct_positive < V32_FALSIFIER_MIN_PCT_POSITIVE:
            fails.append(f"%positive {pct_positive} < {V32_FALSIFIER_MIN_PCT_POSITIVE}")
        # MEASUREMENT CLARIFICATION 3 (2026-09-19): the fill-rate gate is SUPERSEDED here by the
        # capture ratio (fill rate stays computed above and printed as info). A missing ratio (the
        # shadow never filled an armed+bucket window) is a fail -- there is availability we cannot show
        # we captured.
        if capture_ratio is None or capture_ratio < V32_CAPTURE_RATIO_MIN:
            fails.append(f"capture ratio {capture_ratio} < {V32_CAPTURE_RATIO_MIN}")
        if exec_gap is None or exec_gap > V32_FALSIFIER_MAX_EXEC_GAP_CENTS:
            fails.append(f"exec gap {exec_gap}c > {V32_FALSIFIER_MAX_EXEC_GAP_CENTS}c")
        if legged > V32_FALSIFIER_MAX_ONE_LEGGED:
            fails.append(f"one-legged {legged} > {V32_FALSIFIER_MAX_ONE_LEGGED}")
        verdict = "ALIVE-so-far" if not fails else ("KILL: " + "; ".join(fails))

    return {
        "shadow_gap_E": e,
        "n": n,
        "fills_total": fills_total,
        "one_legged": legged,
        "n_days": n_days,
        "armed_windows": armed_windows,
        "armed_days": armed_days,
        "mean_lock_c": mean_lock,
        "median_lock_c": median_lock,
        "p10_lock_c": p10_lock,
        "min_lock_c": min_lock,
        "pct_positive": pct_positive,
        "fill_rate_per_day": fill_rate,
        "capture_live_sets": capture_live_sets,
        "capture_shadow_fills": capture_shadow_fills,
        "capture_ratio": capture_ratio,
        "shadow_mean_lock_c": shadow_mean,
        "exec_gap_c": exec_gap,
        "replaces_per_hour_mean": replaces_mean,
        "strike_lag_p99_s": strike_p99,
        "bucket_lag_p99_s": bucket_p99,
        "strike_lag_stats_p99_s": strike_stats_p99,
        "bucket_lag_stats_p99_s": bucket_stats_p99,
        "shadow_fills_outside_window": shadow_fills_outside_window,
        "shadow_fills_below_min": shadow_fills_below_min,
        "verdict": verdict,
    }


def _c(v: Decimal | None, prec: int = 1) -> str:
    return f"{v:+.{prec}f}c" if v is not None else "n/a"


def _num(v: Decimal | None, prec: int, suffix: str = "") -> str:
    return f"{v:.{prec}f}{suffix}" if v is not None else "n/a"


def _pct(ratio: Decimal | None, prec: int = 1) -> str:
    """A capture-ratio fraction rendered as a percent (None -> 'n/a' when the shadow never filled)."""
    return f"{ratio * 100:.{prec}f}%" if ratio is not None else "n/a"


def _render_scoreboard(sb: dict[str, Any]) -> list[str]:
    e = sb["shadow_gap_E"]
    pct = sb["pct_positive"]
    rate = sb["fill_rate_per_day"]
    # data-age p99 = MAX of the per-window lag_stats p99 across windows (the honest worst tail);
    # falls back to the legacy per-row mean-lag p99 only for rows predating the lag_stats field.
    strike_age = sb.get("strike_lag_stats_p99_s")
    if strike_age is None:
        strike_age = sb.get("strike_lag_p99_s")
    bucket_age = sb.get("bucket_lag_stats_p99_s")
    if bucket_age is None:
        bucket_age = sb.get("bucket_lag_p99_s")
    return [
        "",
        "FALSIFIER SCOREBOARD (DegeneracyV3_2, continuous-requote pump-fader, E=0.10) -- [pin] gates",
        "-" * 78,
        f"  completed sets n = {sb['n']}   (rest fills total = {sb['fills_total']}, one-legged = "
        f"{sb['one_legged']})   armed windows = {sb['armed_windows']}   "
        f"armed days = {sb['armed_windows']}/24 = {_num(sb['armed_days'], 2)}",
        f"  realized lock: mean {_c(sb['mean_lock_c'])}  median {_c(sb['median_lock_c'])}  "
        f"p10 {_c(sb['p10_lock_c'])}  min {_c(sb['min_lock_c'])}",
        f"  %positive = {_num(pct, 1) if pct is not None else 'n/a'}   "
        f"fill rate = {_num(rate, 2, '/day') if rate is not None else 'n/a'} "
        f"(info, superseded as a gate by Registration 3; pin was "
        f"{V32_FALSIFIER_MIN_FILL_RATE_PER_DAY}/day)",
        f"  capture ratio = live {sb.get('capture_live_sets', 0)} / shadow "
        f"{sb.get('capture_shadow_fills', 0)} = {_pct(sb.get('capture_ratio'))}  "
        f"(>= {V32_CAPTURE_RATIO_MIN * 100:.0f}% [pin] Registration 3)",
        f"  shadow E={e}: mean lock {_c(sb['shadow_mean_lock_c'])}   "
        f"execution gap (shadow-live) {_c(sb['exec_gap_c'])}",
        f"  replaces/hour mean = {_num(sb['replaces_per_hour_mean'], 1)}   "
        f"data-age p99 (max/window): strike {_num(strike_age, 2, 's')}  "
        f"bucket {_num(bucket_age, 2, 's')}",
        f"  shadow fills outside window (suppressed, T-15..T-5 gate) = "
        f"{sb.get('shadow_fills_outside_window', 0)}",
        f"  shadow fills below n_min (suppressed, live n_below_min) = "
        f"{sb.get('shadow_fills_below_min', 0)}",
        f"  VERDICT: {sb['verdict']}",
    ]


def _recon_fill_fee(f: dict[str, Any]) -> Decimal:
    """The TOTAL venue fee actually charged for one fill record, read-only: ``fee_total`` when the
    row carries it (new rows), else the frozen law ``ceil(0.07*p*(1-p)*count, $0.0001)`` — the maker
    rest leg (``fee`` 0) is fee-free, and a count-1 taker fill's per-contract ``fee`` IS the venue
    total (keeps size-1 rows byte-identical). Mirrors ``run_v32._fill_total_fee`` without importing it."""
    ft = f.get("fee_total")
    if ft is not None:
        return _dec(ft) or Decimal(0)
    per = f.get("fee")
    per_d = _dec(per) or Decimal(0)
    if per_d == 0:
        return Decimal(0)
    count = int(f.get("count", 0) or 0)
    if count <= 1:
        return per_d
    p = _dec(f.get("price", 0)) or Decimal(0)
    raw = _FEE_RATE * p * (Decimal(1) - p) * Decimal(count) * Decimal(10000)
    return Decimal(math.ceil(raw)) / Decimal(10000)


def _recon_row_cost(row: dict[str, Any]) -> Decimal:
    """Σ over the row's rest + wing fills of ``price x count + total_fee`` (the cash actually paid),
    recomputing each fee count-aware via ``_recon_fill_fee``."""
    cost = Decimal(0)
    for f in list(row.get("fills") or []) + list(row.get("wing_fills") or []):
        p = _dec(f.get("price", 0)) or Decimal(0)
        c = Decimal(int(f.get("count", 0) or 0))
        cost += p * c + _recon_fill_fee(f)
    return cost


def build_ledger_reconciliation(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per completed/partial SET, the STORED (window + backfill) realized_delta vs the CORRECTED
    count-aware number (read-only; the historical over/under-credit stays in the append-only ledger).

    Corrected floor = ``_v32_floor_booked_for_entry`` (count-aware, bucket leg included via
    ``wing_batch_sets`` or the explicit ``floor_booked``). Corrected cost = ``_recon_row_cost`` (total
    fees x count). Corrected window delta = floor - cost. A COMPLETE set pays exactly $2 x count at
    settlement, so its corrected payoff = $2 x count and its corrected backfill correction = payoff -
    floor = $0; the corrected per-set total is then floor - cost + 0 = the venue balance move. An
    incomplete/one-legged set with a backfill row keeps that row's payoff (priced over the legs it
    held). Only set-bearing armed windows (``realized_delta`` present) appear."""
    bfmap: dict[str, dict[str, Any]] = {
        str(r.get("backfill_of")): r for r in rows if r.get("mode") == "backfill" and r.get("backfill_of")
    }
    out: list[dict[str, Any]] = []
    for r in rows:
        if r.get("mode") == "backfill":
            continue
        if r.get("realized_delta") is None:
            continue
        ct = str(r.get("close_time"))
        legs = r.get("unsettled_legs") or r.get("held_legs") or []
        floor = _v32_floor_booked_for_entry(r, legs)
        cost = _recon_row_cost(r)
        corr_window = floor - cost
        batches = r.get("wing_batch_sets")
        if isinstance(batches, list) and batches:
            total_count = sum(int(b.get("fill_count", 0) or 0) for b in batches)
            complete = all(b.get("completed") for b in batches)
        else:
            total_count = int(r.get("lots_filled") or 0) or sum(
                int(lg["count"]) if isinstance(lg, dict) else int(lg[2]) for lg in legs) // max(1, len(legs))
            complete = (_dec(r.get("realized_lock")) is not None) and not bool(r.get("one_legged"))
        bf = bfmap.get(ct)
        if complete and total_count:
            corr_payoff = Decimal(2) * Decimal(total_count)
            corr_backfill: Decimal | None = corr_payoff - floor
        elif bf is not None:
            corr_backfill = (_dec(bf.get("settlement_payoff")) or Decimal(0)) - floor
        else:
            corr_backfill = None  # still pending -> no correction booked yet
        corr_total = corr_window + (corr_backfill if corr_backfill is not None else Decimal(0))
        stored_window = _dec(r.get("realized_delta")) or Decimal(0)
        stored_backfill = _dec(bf.get("realized_delta")) if bf is not None else None
        stored_total = stored_window + (stored_backfill if stored_backfill is not None else Decimal(0))
        out.append({
            "close_time": ct,
            "size": total_count,
            "complete": complete,
            "backfilled": bf is not None,
            "stored_window": stored_window,
            "stored_backfill": stored_backfill,
            "stored_total": stored_total,
            "corr_window": corr_window,
            "corr_backfill": corr_backfill,
            "corr_total": corr_total,
        })
    return out


def _render_reconciliation(recon: list[dict[str, Any]]) -> list[str]:
    if not recon:
        return []
    lines = [
        "",
        "LEDGER RECONCILIATION (count-aware settlement backfill, read-only; ledger unchanged)",
        "-" * 92,
        "  " + "close_time".ljust(22) + "sz".rjust(3) + "cmpl".rjust(6)
        + "stored_win".rjust(12) + "stored_bf".rjust(11) + "stored_tot".rjust(12)
        + "corr_win".rjust(12) + "corr_bf".rjust(10) + "corr_tot".rjust(11),
    ]

    def d(v: Decimal | None, w: int) -> str:
        return ("-" if v is None else f"{v:+.4f}").rjust(w)

    tot_stored = Decimal(0)
    tot_corr = Decimal(0)
    for e in recon:
        tot_stored += e["stored_total"]
        tot_corr += e["corr_total"]
        lines.append(
            "  " + str(e["close_time"]).ljust(22)
            + str(e["size"]).rjust(3)
            + ("Y" if e["complete"] else "n").rjust(6)
            + d(e["stored_window"], 12) + d(e["stored_backfill"], 11) + d(e["stored_total"], 12)
            + d(e["corr_window"], 12) + d(e["corr_backfill"], 10) + d(e["corr_total"], 11)
        )
    lines.append("-" * 92)
    lines.append(
        f"  sets = {len(recon)}   stored_total = {tot_stored:+.4f}   "
        f"corrected_total = {tot_corr:+.4f}   delta(stored-corr) = {tot_stored - tot_corr:+.4f}"
    )
    lines.append(
        "  corr_tot = the venue balance move per set (complete set: $2 x size settlement nets the "
        "count-aware floor -> corr_bf 0)"
    )
    return lines


def _render(report: dict[str, Any]) -> str:
    e_keys = report["e_keys"]
    lines: list[str] = []
    header = ["close_time".ljust(22), "mode".ljust(9), "bucket".ljust(26),
              "Sd".rjust(7), "lastRest".rjust(8), "repl".rjust(5), "wPlc".rjust(5)]
    for k in e_keys:
        header.append(("sh_E" + k).rjust(14))
    header += ["m15".rjust(6), "sLag".rjust(6), "bLag".rjust(6)]
    lines.append("  ".join(header))
    lines.append("-" * (len(lines[0])))
    for w in report["windows"]:
        lr = w.get("last_rest")
        lr_s = (f"{float(lr):.2f}" if lr not in (None, "") else "-")
        row = [str(w["close_time"]).ljust(22), str(w["mode"]).ljust(9),
               str(w["bucket"]).ljust(26),
               (str(w.get("Sd")) if w.get("Sd") is not None else "-").rjust(7),
               lr_s.rjust(8),
               str(w["replaces"]).rjust(5),
               str(w["would_places"]).rjust(5)]
        for k in e_keys:
            row.append(str(w["shadow"].get(k, "-")).rjust(14))
        row.append(str(w.get("m15_frames", 0)).rjust(6))
        sl = w.get("strike_lag")
        bl = w.get("bucket_lag")
        row.append((f"{float(sl):.1f}" if sl is not None else "-").rjust(6))
        row.append((f"{float(bl):.1f}" if bl is not None else "-").rjust(6))
        line = "  ".join(row)
        if w.get("stand_down_reason"):
            line += f"   [stand down: {w['stand_down_reason']}]"
        lines.append(line)
    t = report["totals"]
    lines.append("-" * (len(lines[0]) if lines else 40))
    lines.append(
        f"windows={t['windows']}  would_places={t['would_places']}  replaces={t['replaces']}  "
        f"stand_downs={t['stand_downs']}  late_fills={t['late_fills']}"
    )
    lines.append(
        f"  rest_invariant: violations={t.get('rest_invariant_violations', 0)}  "
        f"phantoms={t.get('rest_invariant_phantoms', 0)}  "
        f"rechecks={t.get('rest_invariant_rechecks', 0)} (read-path lag, no stand-down)"
    )
    # amend-first replace totals (Brad 2026-09-15): amends attempted/confirmed/failed, cancel+create
    # fallbacks, and fills booked by an amend that crossed. Shown next to replaces.
    lines.append(
        f"  amends={t.get('amends', 0)}  confirmed={t.get('amends_confirmed', 0)}  "
        f"failed={t.get('amends_failed', 0)}  fallbacks(cancel+create)={t.get('amend_fallbacks', 0)}  "
        f"fills_on_amend={t.get('fills_on_amend', 0)}"
    )
    for k in e_keys:
        ml = t["shadow_mean_lock"].get(k)
        ml_s = f"{ml * 100:+.2f}c" if ml is not None else "n/a"
        lines.append(f"  shadow E={k}: fills={t['shadow_fills'].get(k, 0)}  mean_lock={ml_s}")
    mlag = t["mean_lag_seconds"]
    lines.append(f"  mean data-age (lag) = {float(mlag):.2f}s" if mlag is not None
                 else "  mean data-age (lag) = n/a")
    if report.get("falsifier") is not None:
        lines.extend(_render_scoreboard(report["falsifier"]))
    if report.get("reconciliation"):
        lines.extend(_render_reconciliation(report["reconciliation"]))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="V3.2 per-window report (read-only).")
    ap.add_argument("--days", type=int, default=None,
                    help="Only the most recent N UTC days (default: all).")
    ap.add_argument("--ledger", default=DEFAULT_V32_LEDGER_PATH)
    ap.add_argument("--json", action="store_true", help="Emit the report as JSON instead of a table.")
    args = ap.parse_args(argv)

    rows = _recent_days(load_v32_rows(args.ledger), args.days)
    report = build_report(rows)
    if args.json:
        print(json.dumps(report, sort_keys=True, default=lambda o: str(o)))
    else:
        print(_render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
