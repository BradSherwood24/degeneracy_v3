"""reconcile.py — gate E: window accounting reconciled against EXECUTOR truth (2026-10-03 02:00Z).

The ledger row's economic facts (``lots_filled``, ``one_legged``, the S1_LEGGED day-guard occurrence)
and the alarm count must be reconciled against what the EXECUTOR observed — its ``rest_fill`` events /
``self.fills`` / the venue's ``/portfolio/fills`` — not read off the core's bookkeeping alone. On
2026-10-03 02:00Z the core lost two owned orders, the executor filled and journaled them, and the
core-derived ledger row read ``lots 0 / one_legged False / alarms 0`` while the executor had seen two
naked lots and journaled ten alarms. #119 (gates A–D) fixed the core's fill path; this module is the
defence IN DEPTH: the ledger must not DEPEND on the core being right. Given executor truth and the core's
booked set (+ the completed-hedge coverage), it reconciles the two and surfaces any executor-known fill
the core did not book.

House law: pure (no network, no proxy, no key, no seal read); every count is a ``Decimal`` (Kalshi crypto
fills are fractional, 0.01 granularity); fail-closed (an executor fill not covered by a COMPLETED wing
pair is counted ONE-LEGGED, whether or not the core booked it).

Two entry points share one engine (``reconcile``):
  * LIVE (``reconcile_live``, wired into ``run_v33._finalize``): executor truth = the driver's observed
    ws/poll fills + the executor's ``self.fills`` rest legs (+ an injected best-effort venue
    ``/portfolio/fills`` when armed and reachable); core truth = ``state.rest_fills`` /
    ``state.one_legged``; completed-hedge coverage = the completed wing batches. Produces the reconciled
    ``lots_filled`` / ``one_legged`` and, per fill, any quantity the core did not book (``unbooked_fill``)
    and a ``ledger_reconcile_mismatch`` when the two disagree.
  * REBUILD (``rebuild_from_records``, the ledger rebuild / daily-replay path): executor truth = the
    journal's ``rest_fill`` records; completed-hedge coverage = the journal's ``take_wings`` records; the
    alarm count = the journal's ``alarm`` records. No core truth is available from a journal, so the
    rebuild reconciles executor truth against the HEDGE receipts directly (which is exactly the
    defence-in-depth question: were the rest-filled lots hedged?).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Mapping

_ZERO = Decimal(0)
_COUNT_Q = Decimal("0.01")
# fractional-count comparison epsilon: fills are 0.01-granular, so anything under half a tick is "equal".
_EPS = Decimal("0.005")

# Executor-truth source classes. A SUM source reports per-EVENT increments (the ws fill channel; the
# amend-cross fill; each venue /portfolio/fills record) -> its per-order total is the SUM of its events. A
# MAX source reports a CUMULATIVE figure (the order-status poll; the cancel-confirm filled_count) -> its
# per-order total is the MAX seen. The reconciled per-order truth is then the MAX across the source totals,
# so two channels reporting the SAME fill (e.g. a ws echo of an amend cross) never double-count.
_SUM_SOURCES = frozenset({"ws", "amend", "venue"})
_MAX_SOURCES = frozenset({"poll", "cancel", "cancel_confirm"})


def _dc(v: Any) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v))


def _q(v: Decimal) -> Decimal:
    return v.quantize(_COUNT_Q)


def _num(v: Decimal) -> Any:
    """Integral -> int (byte-stable with the ledger's other counts), fractional -> the 2dp Decimal."""
    d = _q(v)
    return int(d) if d == d.to_integral_value() else d


def _s(v: Any) -> str | None:
    return None if v is None else str(v)


@dataclass(frozen=True)
class ExecFill:
    """One fill the EXECUTOR observed. ``source`` picks the SUM/MAX aggregation rule (see above)."""

    count: Decimal
    coid: str | None = None
    order_id: str | None = None
    price: Decimal | None = None
    ticker: str | None = None
    source: str = "ws"


@dataclass(frozen=True)
class ReconcileResult:
    exec_lots: Decimal                     # executor-truth total (max across sources, summed over orders)
    core_lots: Decimal                     # core-booked total (0 on the rebuild path)
    lots_filled: Decimal                   # reconciled = Σ max(executor, core) per order (union of keys)
    hedged_lots: Decimal                   # lots covered by a COMPLETED wing pair
    one_legged: bool                       # reconciled: any rest-filled lot not covered by a completed pair
    one_legged_contracts: Decimal          # the falsifier's one-legged COUNT, reconciled
    unbooked_fills: tuple[dict, ...]       # per order: executor-known quantity the core did not book
    reconcile_mismatch: bool               # core vs executor disagree on some order
    mismatch_detail: dict | None           # {"core_lots":, "executor_lots":} when they disagree

    def as_row_fields(self) -> dict[str, Any]:
        """The additive ledger-row slots (gate E). ``lots_filled`` / ``one_legged`` OVERRIDE the
        core-derived values on the row; the rest are new, defence-in-depth receipts."""
        return {
            "lots_filled": _num(self.lots_filled),
            "one_legged": bool(self.one_legged),
            "one_legged_contracts": _num(self.one_legged_contracts),
            "unbooked_fills": list(self.unbooked_fills),
            "reconcile_mismatch": bool(self.reconcile_mismatch),
            "reconcile_detail": self.mismatch_detail,
            "exec_lots": _num(self.exec_lots),
            "core_lots": _num(self.core_lots),
            "hedged_lots": _num(self.hedged_lots),
        }


def _canon_key(coid: str | None, order_id: str | None) -> str | None:
    """The stable per-order key. ``order_id`` is the venue identity that survives an amend (the coid
    changes on amend), so it is preferred; a fill with no venue id (a never-acked orphan) keys by coid."""
    if order_id:
        return f"oid:{order_id}"
    if coid:
        return f"coid:{coid}"
    return None


def aggregate_exec_fills(fills: Iterable[ExecFill]) -> dict[str, dict[str, Any]]:
    """Fold executor fills into per-order truth. Within an order, SUM the increment sources and MAX the
    cumulative sources; the order's truth is the MAX across the two so a ws echo of an amend cross (the
    same lot on two channels) is never counted twice."""
    out: dict[str, dict[str, Any]] = {}
    for f in fills:
        key = _canon_key(f.coid, f.order_id)
        if key is None:
            continue
        e = out.setdefault(key, {"by_source": {}, "coid": None, "order_id": None,
                                 "price": None, "ticker": None})
        src = f.source or "ws"
        cnt = _dc(f.count)
        if src in _MAX_SOURCES:
            e["by_source"][src] = max(e["by_source"].get(src, _ZERO), cnt)
        else:  # SUM sources (ws/amend/venue) and any unknown source
            e["by_source"][src] = e["by_source"].get(src, _ZERO) + cnt
        if e["coid"] is None and f.coid is not None:
            e["coid"] = f.coid
        if e["order_id"] is None and f.order_id is not None:
            e["order_id"] = f.order_id
        if e["price"] is None and f.price is not None:
            e["price"] = f.price
        if e["ticker"] is None and f.ticker is not None:
            e["ticker"] = f.ticker
    for e in out.values():
        e["truth"] = max(e["by_source"].values()) if e["by_source"] else _ZERO
    return out


def _aggregate_core_fills(core_rest_fills: Iterable[Any]) -> dict[str, Decimal]:
    """Fold the core's booked RungFills into per-order booked lots, keyed the SAME way as executor truth
    (so the two reconcile on identical keys). A RungFill carries both ``coid`` and ``order_id``."""
    out: dict[str, Decimal] = {}
    for rf in core_rest_fills:
        key = _canon_key(getattr(rf, "coid", None), getattr(rf, "order_id", None))
        if key is None:
            continue
        out[key] = out.get(key, _ZERO) + _dc(getattr(rf, "count", 0))
    return out


def reconcile(
    *,
    exec_fills: Iterable[ExecFill],
    core_rest_fills: Iterable[Any] | None = None,
    hedged_lots: Decimal = _ZERO,
    core_one_legged: bool = False,
) -> ReconcileResult:
    """The shared engine. ``core_rest_fills=None`` is the REBUILD path (no core truth from a journal): the
    executor fills are reconciled against ``hedged_lots`` (completed-hedge coverage) only, and no
    core-vs-executor mismatch / unbooked is computed. When ``core_rest_fills`` is supplied (LIVE), every
    executor-known fill is checked against the core's booked quantity for the SAME order; the unbooked
    quantity is surfaced and any disagreement is a mismatch.

    ``one_legged`` / ``one_legged_contracts`` are reconciled the SAME way on both paths: any rest-filled
    lot NOT covered by a COMPLETED wing pair is one-legged (``lots_filled - hedged_lots``), OR'd with the
    core's own ``one_legged`` mirror (belt — a one-legged batch's lots are already un-hedged here)."""
    hedged = _dc(hedged_lots)
    agg = aggregate_exec_fills(exec_fills)
    exec_lots = sum((e["truth"] for e in agg.values()), _ZERO)
    core_list = list(core_rest_fills) if core_rest_fills is not None else None

    # No executor truth to reconcile against (a DRY / no-fill window — the dry simulation never journals a
    # ``rest_fill``): pass the core's own numbers through unchanged. Nothing to flag; the row stays
    # byte-identical to the pre-gate-E row. (An armed window that genuinely saw zero fills lands here too;
    # with no contradicting executor evidence, trusting the core is the only safe reading.)
    if not agg:
        core_lots = sum((_dc(getattr(rf, "count", 0)) for rf in (core_list or ())), _ZERO)
        lots_filled = core_lots
        one_legged_contracts = max(_ZERO, _q(lots_filled - hedged))
        return ReconcileResult(
            exec_lots=_ZERO, core_lots=core_lots, lots_filled=lots_filled, hedged_lots=hedged,
            one_legged=bool(core_one_legged or one_legged_contracts > _EPS),
            one_legged_contracts=one_legged_contracts, unbooked_fills=(),
            reconcile_mismatch=False, mismatch_detail=None)

    unbooked: list[dict[str, Any]] = []
    mismatch = False
    if core_list is not None:
        core = _aggregate_core_fills(core_list)
        core_lots = sum(core.values(), _ZERO)
        lots = _ZERO
        for key, e in agg.items():
            truth = e["truth"]
            booked = core.get(key, _ZERO)
            lots += max(truth, booked)
            if truth - booked > _EPS:
                unbooked.append({"coid": e["coid"], "order_id": e["order_id"],
                                 "count": _num(truth - booked), "price": _s(e["price"]),
                                 "ticker": e["ticker"], "exec_count": _num(truth),
                                 "core_count": _num(booked)})
            if abs(truth - booked) > _EPS:
                mismatch = True
        # core-only orders (booked by the core, never seen by executor truth): add their booked lots so the
        # reconciled total is the UNION, not just the executor side.
        for key, booked in core.items():
            if key not in agg:
                lots += booked
                if booked > _EPS:
                    mismatch = True
        lots_filled = lots
    else:
        core_lots = _ZERO
        lots_filled = exec_lots

    one_legged_contracts = max(_ZERO, _q(lots_filled - hedged))
    one_legged = bool(core_one_legged or one_legged_contracts > _EPS)
    mismatch_detail = ({"core_lots": _num(core_lots), "executor_lots": _num(exec_lots)}
                       if mismatch else None)
    return ReconcileResult(
        exec_lots=exec_lots, core_lots=core_lots, lots_filled=lots_filled, hedged_lots=hedged,
        one_legged=one_legged, one_legged_contracts=one_legged_contracts,
        unbooked_fills=tuple(unbooked), reconcile_mismatch=mismatch, mismatch_detail=mismatch_detail,
    )


# ---------------------------------------------------------------------------
# LIVE: reconcile a finished window's driver/executor/core state
# ---------------------------------------------------------------------------
def executor_truth_fills(driver: Any, venue_fills: Iterable[Mapping[str, Any]] | None = None
                         ) -> list[ExecFill]:
    """Gather every rung fill the EXECUTOR knows at window end, from (1) the driver's observed ws/poll
    fills (captured as they are journaled — INDEPENDENT of whether the core booked them), (2) the
    executor's ``self.fills`` rest legs (the amend-cross / cancel-confirm bookings), and (3) an optional,
    best-effort venue ``/portfolio/fills`` list (armed, reachable through the proxy; absent in dry/tests).
    Wing legs are NOT rung fills and are excluded."""
    out: list[ExecFill] = list(getattr(driver, "_exec_truth_fills", ()) or ())
    executor = getattr(driver, "executor", None)
    for f in (getattr(executor, "fills", ()) or ()):
        if (f.get("leg") if isinstance(f, Mapping) else None) != "rest":
            continue
        out.append(ExecFill(count=_dc(f.get("count", 0)), coid=f.get("client_order_id"),
                            order_id=f.get("order_id"), price=f.get("price"), ticker=f.get("ticker"),
                            source=str(f.get("path") or "amend")))
    for vf in (venue_fills or ()):
        out.append(ExecFill(count=_dc(vf.get("count", 0)), coid=vf.get("client_order_id"),
                            order_id=vf.get("order_id"), price=vf.get("price"),
                            ticker=vf.get("ticker") or vf.get("market_ticker"), source="venue"))
    return out


def _hedged_lots_from_state(state: Any) -> Decimal:
    """Lots covered by a COMPLETED wing pair (``leg_count`` honours print-through pre-hedges)."""
    total = _ZERO
    for b in (getattr(state, "wing_batches", ()) or ()):
        if getattr(b, "completed", False):
            total += _dc(getattr(b, "leg_count", 0))
    return total


def reconcile_live(driver: Any, venue_fills: Iterable[Mapping[str, Any]] | None = None
                   ) -> ReconcileResult:
    """Reconcile a finished LIVE window. In dry / no-fill windows the executor-truth set is EMPTY (the dry
    simulation never journals a ``rest_fill``), so the result passes the core's own numbers through
    unchanged (lots_filled = core lots, no unbooked, no mismatch) — an existing dry/healthy row is
    byte-identical."""
    state = getattr(driver, "state", None)
    return reconcile(
        exec_fills=executor_truth_fills(driver, venue_fills),
        core_rest_fills=list(getattr(state, "rest_fills", ()) or ()),
        hedged_lots=_hedged_lots_from_state(state),
        core_one_legged=bool(getattr(state, "one_legged", False)),
    )


def reconcile_exec_truth_only(driver: Any, venue_fills: Iterable[Mapping[str, Any]] | None = None
                              ) -> ReconcileResult:
    """The STRICTER fail-safe (2026-10-03 gate E item 4): reconcile executor truth against the
    completed-hedge coverage ALONE, with NO core input (``core_rest_fills=None``). Used when the full
    ``reconcile_live`` raises mid-window: it must NEVER drop the window back to the blind core (the 02:00Z
    failure mode read core one_legged False while two lots were naked). lots_filled = executor truth,
    one_legged = executor lots not covered by a completed wing pair — so a naked fill is still surfaced on
    the row and counted one-legged even when the core-vs-executor reconcile could not run."""
    state = getattr(driver, "state", None)
    return reconcile(
        exec_fills=executor_truth_fills(driver, venue_fills),
        core_rest_fills=None,
        hedged_lots=_hedged_lots_from_state(state),
        core_one_legged=bool(getattr(state, "one_legged", False)),
    )


# ---------------------------------------------------------------------------
# Alarm accounting (gate E item 3): driver + core + executor + ws, with a breakdown
# ---------------------------------------------------------------------------
def alarm_breakdown(*, driver_counts: Mapping[str, int], executor_counts: Mapping[str, int],
                    ws_counts: Mapping[str, int], reconcile_alarms: int = 0) -> dict[str, int]:
    """The auditable split behind the single ``alarms`` number. Every alarm lands in the journal under
    kind ``alarm`` EXCEPT ``rest_invariant_phantom`` (journaled under its own kind, never routed through
    the executor's ``_record_alarm``); it is surfaced separately and NOT folded into the headline (so a
    rebuild that counts journal ``alarm`` records matches this — see ``rebuild_from_records``).

      * core     = the pure core's ALARM actions (orphan_rung_fill_hedged, cancel_unattributed,
                   orphan_rung_fill_unpriced, orphan_fill_not_bucket) — ``driver.counts['alarm']`` (the
                   driver bumps this when it journals a V33ActionKind.ALARM as kind ``alarm``).
      * executor = the executor's ``_record_alarm`` calls — ``executor.counts['alarm']``
                   (rest_invariant_violation, standdown_sweep, cancel_failed, wing 429, …).
      * driver   = the driver's own operational alarms journaled directly as kind ``alarm``
                   (executor_standdown, pump_runaway, *_dispatch_error, *_ingest_error, …) —
                   ``driver.counts['driver_alarm']``.
      * ws       = the WS connection recorder's alarms — ``ws_counts['alarm']``.
      * reconcile= gate E's own ``ledger_reconcile_mismatch`` raised at finalize (0 or 1 per window).
      * phantom  = ``executor.counts['rest_invariant_phantom']`` (informational; NOT in ``total``).
    """
    core = int(driver_counts.get("alarm", 0) or 0)
    driver = int(driver_counts.get("driver_alarm", 0) or 0)
    executor = int(executor_counts.get("alarm", 0) or 0)
    ws = int(ws_counts.get("alarm", 0) or 0)
    phantom = int(executor_counts.get("rest_invariant_phantom", 0) or 0)
    rec = int(reconcile_alarms or 0)
    total = core + driver + executor + ws + rec
    return {"total": total, "driver": driver, "core": core, "executor": executor, "ws": ws,
            "reconcile": rec, "executor_phantom": phantom}


# ---------------------------------------------------------------------------
# REBUILD: a reconciled row from journal events (the ledger rebuild / daily replay)
# ---------------------------------------------------------------------------
# The journal rest_fill record carries a per-EVENT INCREMENT for BOTH paths: the ws handler journals the
# event count, and the poll handler journals the DELTA over what the core had booked (run_v33.on_poll_fill).
# Both are increments, so the rebuild must SUM every rest_fill per order (a single lot is journaled by
# EITHER channel, never both, when the core books healthily). They therefore share ONE sum bucket
# ("journal") — the pre-fix split that put poll in a MAX bucket under-counted an order with two poll deltas
# (MAX 0.60 over deltas 0.40+0.60 instead of SUM 1.00); this direction HIDES a naked lot in an audit, so
# it is summed. (Over-counting only in the rare core-fail case where both channels re-report the same lot
# — the SAFE direction for the one-legged pin.)
_REBUILD_SOURCE = "journal"


def _records_iter(records: Iterable[Any]):
    """Yield (kind, obj) from journal records. Accepts the full streamed record ``{"kind","obj",...}`` or
    a bare ``{kind: obj}``-free dict already shaped as the obj (not used here)."""
    for r in records:
        if not isinstance(r, Mapping):
            continue
        yield str(r.get("kind")), (r.get("obj") or {})


def rebuild_from_records(records: Iterable[Any]) -> dict[str, Any]:
    """Rebuild a window's reconciled accounting from its JOURNAL records (defence in depth: it depends
    only on executor truth (``rest_fill``) + hedge receipts (``take_wings``) + the ``alarm`` records, never
    on the core's internal bookkeeping). Returns the reconciled row fields plus the alarm breakdown.

    Rebuilding the 2026-10-03 02:00Z window reads lots 2 (0.40 + 0.60 + 1), one_legged True (2 contracts,
    no ``take_wings`` ever emitted), alarms 10 (the journal's ten kind-``alarm`` records: nine
    rest_invariant_violation + one executor_standdown; the lone rest_invariant_phantom is journaled under
    its own kind and surfaced as ``executor_phantom``, not in the headline)."""
    exec_fills: list[ExecFill] = []
    hedged_lots = _ZERO
    alarms = 0
    phantom = 0
    alarm_names: dict[str, int] = {}
    for kind, obj in _records_iter(records):
        if kind == "rest_fill":
            exec_fills.append(ExecFill(
                count=_dc(obj.get("count", 0)), coid=obj.get("client_order_id"),
                order_id=obj.get("order_id"), price=obj.get("rest_price"),
                ticker=obj.get("market"), source=_REBUILD_SOURCE))
        elif kind == "take_wings":
            # the lots the core committed to a two-legged hedge (retry_wing is a single-leg top-up, not a
            # new coalesced take -> excluded, so it never double-counts coverage).
            hedged_lots += _dc(obj.get("count", 0))
        elif kind == "alarm":
            alarms += 1
            nm = str(obj.get("alarm"))
            alarm_names[nm] = alarm_names.get(nm, 0) + 1
        elif kind == "rest_invariant_phantom":
            phantom += 1
    res = reconcile(exec_fills=exec_fills, core_rest_fills=None, hedged_lots=hedged_lots)
    fields = res.as_row_fields()
    fields["alarms"] = alarms
    fields["alarms_breakdown"] = {"total": alarms, "journal_alarm_records": alarms,
                                  "executor_phantom": phantom, "by_name": alarm_names}
    return fields
