"""ledger.py — the append-only per-window V3.3 ledger (one JSON line per window) + count/rung-aware
money math for the rolling K-rung ladder.

Each ``run_v33`` process appends ONE object to ``ledger/v33_ledger.jsonl`` at close. In ``dry`` the row
carries the DRY-SIMULATED ladder fills (``dry_sim`` true — clearly labelled, NEVER counted as realised);
in ``armed`` it carries the real fills and the settlement backfill corrects the conservative floor. Money
is Decimal-safe strings on disk. House law: this module places no orders, opens no socket, reads no
sealed file.

WHAT IS RUNG-AWARE (vs V3.2's single-set money math):
  * Per FILL EVENT (a rung fill): ``rung`` + ``E_rung`` (the derived margin label), ``price`` (the
    resting n), ``count``, ``W_at_fill``, ``n_top`` (captured on the RungFill), ``lock_solved`` =
    ``lock_value(price, W_at_fill)`` (the falsifier's per-rung SOLVED reference — the price/W economic
    value, NOT the integer label, per the L1 R4 contract note), and ``realized_lock`` (per contract,
    using the WINGS ACTUALLY PAID for that fill's coalesced batch).
  * Per WING BATCH: ``held_legs`` (bucket-NO + each filled wing), ``floor`` (count-aware
    ``v32_set_floor_dollars(held, count)``), the batch's realized lock, one_legged.
  * Per ROW: a LADDER summary (rungs filled, shallowest/deepest margin, contracts, ladder lock, roll
    integrity, amends/cancels/creates) + the ``dry_sim`` flag.

The floor geometry is IDENTICAL to V3.2's pin (bucket-NO + YES@Sd + NO@Su), so ``v32_set_floor_dollars``
and ``service.ledger.settlement_payoff`` are REUSED (imported) — the ladder just books K of them.
"""

from __future__ import annotations

import json
import math
import os
from decimal import Decimal
from typing import Any

from service._simlaw import fee as _fee
from service._simlaw import fee_rate as _FEE_RATE
from service.paths import ledger_dir_v33, ledger_path_v33
from service.v33.core import lock_value
# The pin floor + settlement payoff are geometry-identical to V3.2 -> reuse (no reimplementation).
from service.v32.ledger import v32_set_floor_dollars as v33_set_floor_dollars  # noqa: F401

DEFAULT_V33_LEDGER_DIR = ledger_dir_v33()
DEFAULT_V33_LEDGER_PATH = ledger_path_v33()

_ZERO = Decimal(0)
_ONE = Decimal(1)
_CENT = Decimal("0.01")

REALIZED_DELTA_NOTE = (
    "floor - total cost incl. all fees x count (venue per-fill fee ceil(0.07*p*(1-p)*count)); ladder = "
    "K one-lot rungs, one wing pair per coalesced batch; dry_sim rows are SIMULATED (ideal fill rule), "
    "NEVER realised. Falsifier reads per-rung realized_lock + lock_solved (lock_value(price, W_at_fill), "
    "not the integer E_rung label)."
)


def _json_default(obj: object) -> str:
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def append_v33_ledger_row(row: dict[str, Any], path: str = DEFAULT_V33_LEDGER_PATH) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True, default=_json_default))
        f.write("\n")


def load_v33_rows(path: str = DEFAULT_V33_LEDGER_PATH) -> list[dict[str, Any]]:
    """Read all rows in append order. Missing file -> []. Tolerates only a truncated trailing line."""
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


def _fee_total(price: Any, count: int) -> Decimal:
    """Venue per-FILL taker fee: ``ceil(0.07*p*(1-p)*count, $0.0001)`` (kalshi-fee-exact), on the SAME
    frozen ``_FEE_RATE`` as ``_fee`` — equal to ``_fee(price)`` at count 1, and NEVER ``_fee(price)*N``
    (the per-contract fee already rounded up once). Mirrors ``run_v32._fee_total``."""
    p = price if isinstance(price, Decimal) else Decimal(str(price))
    raw = _FEE_RATE * p * (_ONE - p) * Decimal(int(count)) * Decimal(10000)
    return Decimal(math.ceil(raw)) / Decimal(10000)


def _shadow_summary(state) -> dict[str, Any]:
    """Per-E shadow fills + locks read off the final V33State (the imported V3.2 shadow)."""
    out: dict[str, Any] = {}
    if state is None:
        return out
    for key, sub in getattr(state, "shadows", {}).items():
        entry: dict[str, Any] = {"E": sub.E, "filled": sub.filled, "n": sub.n}
        f = sub.fill
        if f is not None:
            entry.update({"offer": f.offer, "print": f.print_price, "count": f.count,
                          "W_at_completion": f.W_at_completion, "lock": f.lock})
        out[key] = entry
    return out


def _bucket_ticker_for_fill(state, rf) -> str | None:
    """The bucket-NO ticker a rung fill ACTUALLY rested on. MUST-FIX-3: the RungFill now carries
    ``bucket_ticker`` captured AT FILL TIME, so a rest-and-fill across a bucket change within one window
    prices the held bucket-NO leg against the RIGHT market (else the settlement backfill mis-settles).
    Falls back to the fill's ``bucket_Sd`` -> current ticker, then the ladder's / spot bucket (older
    fills / an unrecoverable ticker)."""
    if rf is not None:
        tk = getattr(rf, "bucket_ticker", None)
        if tk:
            return tk
        sd = getattr(rf, "bucket_Sd", None)
        if sd is not None:
            tk = getattr(state, "bucket_tickers", {}).get(sd)
            if tk:
                return tk
    for sd in (getattr(state, "rest_bucket_Sd", None), getattr(state, "spot_Sd", None)):
        if sd is not None:
            tk = getattr(state, "bucket_tickers", {}).get(sd)
            if tk:
                return tk
    return None


def compute_ladder_money_math(state, *, dry_sim: bool) -> dict[str, Any]:
    """Fold the completed window's rung fills + coalesced wing batches into the ledger's rung-aware
    money-math slots. STATE-DERIVED (works for both a dry_sim window and an armed one, since the core
    books rung fills + wings in both). Returns kwargs for ``build_v33_ledger_row``; all empty when
    nothing filled.

    Per-rung fill events (``rung_fills``): rung/E_rung/price/count/server_ts/W_at_fill/n_top +
    ``lock_solved`` (``lock_value(price, W_at_fill)``) + ``realized_lock`` (per contract, using the wings
    actually paid for that fill's batch). Per-batch (``wing_batch_sets``): held legs, floor, realized
    lock, completed/one_legged. ``floor_booked`` = Σ per-batch count-aware floor; ``realized_delta`` =
    floor - cash paid (rest n x count + wing price x count + per-fill fee_total)."""
    rest_fills = list(getattr(state, "rest_fills", ()) or ())
    wing_legs = list(getattr(state, "wing_legs", ()) or ())
    wing_batches = list(getattr(state, "wing_batches", ()) or ())
    if not rest_fills:
        return {"dry_sim": bool(dry_sim), "rung_fills": [], "wing_batch_sets": [],
                "held_legs": [], "floor_booked": None, "realized_delta": None, "realized_lock": None,
                "one_legged": bool(getattr(state, "one_legged", False)), "realized_unsettled": False,
                "lots_filled": 0}

    # index the batch a rung fill belongs to (by identity of the RungFill in the batch's .fills).
    batch_of_fill: dict[int, int] = {}
    for b in wing_batches:
        for rf in b.fills:
            batch_of_fill[id(rf)] = b.index
    # NIT-3 (reviewer): a rung fill still in the OPEN coalesce group at compute time has NO wing pair yet.
    # Its guaranteed floor is genuinely $0 (a lone bucket-NO leg is directional: v33_set_floor_dollars(1,
    # count) == 0), so accruing its rest cost with no floor is CORRECT, not an understatement — an
    # un-hedged rung is honestly a loss until its wings book. In practice the core flushes coalesce_open
    # and takes the wings by close (the pump ticks past wing_coalesce_ms), so no rung is left un-batched
    # (proved by test_close_time_flush_takes_wings_for_last_coalesce_group). A fill left un-batched is
    # counted as a naked rest cost (fail-safe / conservative), never optimistically.

    held: list[dict[str, Any]] = []
    floor = _ZERO
    batch_records: list[dict[str, Any]] = []
    batch_wpaid: dict[int, Decimal] = {}
    batch_completed: dict[int, bool] = {}
    for b in wing_batches:
        legs = [lg for lg in wing_legs if lg.batch == b.index]
        completed = bool(legs) and all(lg.status == "filled" for lg in legs)
        batch_completed[b.index] = completed
        w_paid = _ZERO
        held_this = 0
        bt = _bucket_ticker_for_fill(state, b.fills[0] if b.fills else None)
        if bt:
            held_this += 1
            held.append({"ticker": bt, "side": "no", "count": int(b.total_count)})
        for lg in legs:
            if lg.status == "filled":
                held_this += 1
                held.append({"ticker": lg.ticker, "side": lg.side, "count": int(lg.count)})
                if lg.fill_price is not None:
                    w_paid += lg.fill_price + _fee(lg.fill_price)
        batch_wpaid[b.index] = w_paid
        floor += v33_set_floor_dollars(held_this, int(b.total_count))
        # per-batch realized lock (Σ per rung fill count * lock_value(price, w_paid)) when completed.
        batch_lock = None
        if completed:
            batch_lock = sum((f.count * lock_value(f.price, w_paid) for f in b.fills), _ZERO)
        batch_records.append({
            "index": b.index,
            "fill_count": int(b.total_count),
            "rungs": [int(f.rung) for f in b.fills],
            "prices": [str(f.price) for f in b.fills],
            "completed": bool(completed),
            "one_legged": bool(b.one_legged),
            "held_legs": held_this,
            "realized_lock": (str(batch_lock) if batch_lock is not None else None),
        })

    # per-rung fill records (the falsifier's per-margin key).
    rung_records: list[dict[str, Any]] = []
    realized_lock_first: Decimal | None = None
    for rf in rest_fills:
        w = rf.W
        lock_solved = lock_value(rf.price, w) if w is not None else None
        bidx = batch_of_fill.get(id(rf))
        realized = None
        if bidx is not None and batch_completed.get(bidx):
            realized = lock_value(rf.price, batch_wpaid.get(bidx, _ZERO))
            if realized_lock_first is None:
                realized_lock_first = realized
        rung_records.append({
            "rung": int(rf.rung),
            "E_rung": str(rf.E_rung),
            "price": str(rf.price),
            "count": int(rf.count),
            "server_ts": rf.server_ts,
            "W_at_fill": (str(w) if w is not None else None),
            "n_top": (str(rf.n_top) if rf.n_top is not None else None),
            "lock_solved": (str(lock_solved) if lock_solved is not None else None),
            "realized_lock": (str(realized) if realized is not None else None),
            "coid": rf.coid,
            "order_id": rf.order_id,
            "batch": bidx,
        })

    # cash paid = Σ rung rest cost (n x count, maker fee 0) + Σ wing cost (price x count + fee_total).
    cost = _ZERO
    for rf in rest_fills:
        cost += rf.price * Decimal(int(rf.count))   # bucket-NO maker leg, fee 0 on crypto
    for lg in wing_legs:
        if lg.status == "filled" and lg.fill_price is not None:
            cost += lg.fill_price * Decimal(int(lg.count)) + _fee_total(lg.fill_price, int(lg.count))
    realized_delta = floor - cost
    lots_filled = sum(int(rf.count) for rf in rest_fills)
    return {
        "dry_sim": bool(dry_sim),
        "rung_fills": rung_records,
        "wing_batch_sets": batch_records,
        "held_legs": held,
        "floor_booked": floor,
        "realized_delta": realized_delta,
        "realized_lock": realized_lock_first,   # the shallowest completed rung's lock (info)
        "one_legged": bool(getattr(state, "one_legged", False)),
        "realized_unsettled": bool(held) and not bool(dry_sim),   # dry_sim never awaits settlement
        "lots_filled": int(lots_filled),
    }


def _ladder_summary(state, money: dict[str, Any], driver_counts: dict[str, int],
                    executor_counts: dict[str, int]) -> dict[str, Any]:
    """The per-row LADDER block: rungs filled, shallowest/deepest margin, contracts, ladder lock, roll
    integrity (single-order ratio), and amends/cancels/creates (the venue activity)."""
    rungs = money.get("rung_fills") or []
    e_rungs = [Decimal(str(r["E_rung"])) for r in rungs]
    ladder_lock = _ZERO
    for r in rungs:
        rl = r.get("realized_lock")
        if rl is not None:
            ladder_lock += Decimal(str(rl)) * Decimal(int(r["count"]))
    roll_count = int(getattr(state, "roll_count", 0) or 0)
    single = int(getattr(state, "roll_single_order_count", 0) or 0)
    return {
        "rungs_filled": int(getattr(state, "rungs_filled", 0) or 0),
        "contracts": int(money.get("lots_filled", 0) or 0),
        "shallowest_margin_c": (str(min(e_rungs) * 100) if e_rungs else None),
        "deepest_margin_c": (str(max(e_rungs) * 100) if e_rungs else None),
        "ladder_lock": str(ladder_lock),
        "roll_count": roll_count,
        "roll_single_order_count": single,
        "roll_single_order_ratio": (str(Decimal(single) / Decimal(roll_count)) if roll_count else None),
        "replaces": int(getattr(state, "replace_count", 0) or 0),
        "wing_batches": len(money.get("wing_batch_sets") or []),
        # venue activity: armed executor counters, else the dry would_* driver counts.
        "creates": int(executor_counts.get("rest_acked", 0)
                       or driver_counts.get("would_place_rest", 0)),
        "amends_attempted": int(executor_counts.get("amend_post", 0)
                                or driver_counts.get("would_amend_rest", 0)),
        "cancels": int(executor_counts.get("cancel_delete", 0)
                       or driver_counts.get("would_cancel_rest", 0)),
        "batch_creates": int(executor_counts.get("rest_batch_post", 0)),
    }


def build_v33_ledger_row(
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
    bucket_count: int,
    journal_path: str | None,
    record_count: int,
    stand_down_reason: str | None,
    now: float,
    params_sha: str | None = None,
    armed: bool = False,
    dry_sim: bool = False,
    degrade_reason: str | None = None,
    strike_lag_seconds: float | None = None,
    bucket_lag_seconds: float | None = None,
    lag_stats: dict[str, Any] | None = None,
    last_quoted_bucket_ticker: str | None = None,
    last_quoted_Sd: int | None = None,
    last_quoted_Su: int | None = None,
    # money-math slots (defaults preserve the no-fill row shape).
    rung_fills: list[Any] | None = None,
    wing_batch_sets: list[Any] | None = None,
    held_legs: list[Any] | None = None,
    floor_booked: Decimal | None = None,
    realized_delta: Decimal | None = None,
    realized_lock: Decimal | None = None,
    one_legged: bool | None = None,
    realized_unsettled: bool = False,
    lots_filled: int = 0,
    settlement: dict[str, Any] | None = None,
    m15_tickers: list[str] | None = None,
    m15_frames: int = 0,
    deep_obs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One V3.3 window row. ``dry_sim`` marks a row whose fills were the DRY ideal-fill SIMULATION
    (never realised money). The LADDER summary + the per-rung / per-batch money math ride every row.
    ``deep_obs`` is the SO-3 deep-end observation ladder summary (16..25c; observation only)."""
    money = {
        "rung_fills": rung_fills or [],
        "wing_batch_sets": wing_batch_sets or [],
        "lots_filled": int(lots_filled),
    }
    ladder = _ladder_summary(state, money, driver_counts, executor_counts) if state is not None else {}
    row: dict[str, Any] = {
        "roster": "DegeneracyV3_3",
        "close_time": close_time,
        "mode": resolved_mode,
        "effective_mode": effective_mode,
        "armed": bool(armed),
        "dry_sim": bool(dry_sim),
        "degrade": degrade,
        "degrade_reason": degrade_reason,
        "params_sha": params.sha256 if params is not None else params_sha,
        "stand_down": stand_down_reason is not None,
        "stand_down_reason": stand_down_reason,
        "strike_count": int(strike_count),
        "bucket_count": int(bucket_count),
        "spot_bucket_ticker": last_quoted_bucket_ticker,
        "Sd": last_quoted_Sd,
        "Su": last_quoted_Su,
        "ladder": ladder,
        "replaces": getattr(state, "replace_count", 0) if state is not None else 0,
        "roll_count": getattr(state, "roll_count", 0) if state is not None else 0,
        "roll_single_order_count": getattr(state, "roll_single_order_count", 0) if state is not None else 0,
        "rungs_filled": getattr(state, "rungs_filled", 0) if state is not None else 0,
        "sets_done": getattr(state, "sets_done", 0) if state is not None else 0,
        "would_places": int(driver_counts.get("would_place_rest", 0)),
        "would_amends": int(driver_counts.get("would_amend_rest", 0)),
        "would_cancels": int(driver_counts.get("would_cancel_rest", 0)),
        "would_takes": int(driver_counts.get("would_take_wings", 0)),
        "shadow": _shadow_summary(state),
        "alarms": int(ws_counts.get("alarm", 0)),
        "synth_counts": dict(executor_counts),
        "strike_lag_seconds": strike_lag_seconds,
        "bucket_lag_seconds": bucket_lag_seconds,
        "lag_stats": lag_stats or {},
        "journal_path": journal_path,
        "record_count": int(record_count),
        "ws_counts": dict(ws_counts),
        "m15_tickers": list(m15_tickers or []),
        "m15_frames": int(m15_frames),
        # money math (rung/count aware)
        "rung_fills": rung_fills or [],
        "wing_batch_sets": wing_batch_sets or [],
        "held_legs": held_legs or [],
        "floor_booked": (str(floor_booked) if floor_booked is not None else None),
        "realized_delta": (str(realized_delta) if realized_delta is not None else None),
        "realized_lock": (str(realized_lock) if realized_lock is not None else None),
        "one_legged": one_legged,
        "lots_filled": int(lots_filled),
        "realized_unsettled": bool(realized_unsettled),
        "unsettled_legs": (held_legs or []) if realized_unsettled else [],
        "settlement": settlement,
        "realized_delta_note": (REALIZED_DELTA_NOTE if realized_delta is not None else None),
        "deep_obs": deep_obs or {},
        "flushed_at": now,
    }
    return row


# ---------------------------------------------------------------------------
# Settlement backfill (armed) — mirror of v32 (count-aware; dry_sim rows are SKIPPED)
# ---------------------------------------------------------------------------
def _v33_floor_booked_for_entry(entry: dict[str, Any], legs: list[Any]) -> Decimal:
    """The count-aware guaranteed floor a window row ACTUALLY booked. Priority: explicit ``floor_booked``;
    else Σ ``v33_set_floor_dollars(held_legs, fill_count)`` per ``wing_batch_sets``; else the legs
    present, count-aware (fail-closed to the smaller count)."""
    fb = entry.get("floor_booked")
    if fb is not None:
        return Decimal(str(fb))
    batches = entry.get("wing_batch_sets")
    if isinstance(batches, list) and batches:
        total = _ZERO
        for b in batches:
            total += v33_set_floor_dollars(int(b.get("held_legs", 0) or 0),
                                           int(b.get("fill_count", 1) or 1))
        return total
    counts = []
    for lg in legs:
        c = lg["count"] if isinstance(lg, dict) else (lg[2] if len(lg) > 2 else 1)
        try:
            counts.append(int(c))
        except (TypeError, ValueError):
            counts.append(1)
    cnt = min(counts) if counts else 1
    return v33_set_floor_dollars(len(legs), cnt)


def v33_pending_credit(rows: list[dict[str, Any]], utc_day: str) -> tuple[Decimal, Decimal]:
    """The (pessimistic, optimistic) band on settlement credit still owed today by un-backfilled ARMED
    (never dry_sim) unsettled windows. Count + bucket aware, identical banding to ``v32_pending_credit``:
    floor via ``v33_set_floor_dollars(held, count)``, upside via ``min(held, 2) x count``."""
    done: set[str] = {str(r.get("backfill_of")) for r in rows if r.get("backfill_of")}
    pessimistic = _ZERO
    optimistic = _ZERO
    for r in rows:
        if not r.get("realized_unsettled") or r.get("dry_sim"):
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
                pessimistic += v33_set_floor_dollars(held, cnt)
                optimistic += Decimal(min(held, 2)) * Decimal(cnt)
            continue
        legs = r.get("unsettled_legs") or r.get("held_legs") or []
        counts = []
        for lg in legs:
            c = lg["count"] if isinstance(lg, dict) else (lg[2] if len(lg) > 2 else 1)
            try:
                counts.append(int(c))
            except (TypeError, ValueError):
                counts.append(1)
        cnt = min(counts) if counts else 1
        pessimistic += v33_set_floor_dollars(len(legs), cnt)
        optimistic += Decimal(min(len(legs), 2)) * Decimal(cnt)
    return pessimistic, optimistic


def build_v33_backfill_row(
    window_entry: dict[str, Any], results: dict[str, str], payoff: Decimal,
    floor_booked: Decimal, now: float, legs_priced: int | None = None,
    backfill_note: str | None = None,
) -> dict[str, Any]:
    realized = Decimal(str(payoff)) - Decimal(str(floor_booked))
    return {
        "roster": "DegeneracyV3_3",
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


def v33_settlement_backfill_sweep(rows: list[dict[str, Any]], fetch_result, now: float
                                  ) -> list[dict[str, Any]]:
    """Backfill rows for every prior ARMED window still ``realized_unsettled`` whose held tickers have
    settled. Idempotent; fail-closed; SKIPS ``dry_sim`` rows (they never settle real money). Mirrors
    ``v32_settlement_backfill_sweep``."""
    from service.ledger import settlement_payoff

    done: set[str] = {str(r.get("backfill_of")) for r in rows if r.get("backfill_of")}
    pending: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("dry_sim"):
            continue
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
            continue
        payoff = settlement_payoff(legs, results)
        floor = _v33_floor_booked_for_entry(entry, legs)
        note = ("floor_booked (explicit)" if entry.get("floor_booked") is not None
                else ("reconstructed from wing_batch_sets"
                      if isinstance(entry.get("wing_batch_sets"), list) and entry.get("wing_batch_sets")
                      else "reconstructed from legs (fail-closed)"))
        out.append(build_v33_backfill_row(entry, results, payoff, floor, now,
                                          legs_priced=len(legs), backfill_note=note))
    return out
