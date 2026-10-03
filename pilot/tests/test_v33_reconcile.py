"""GATE E (2026-10-03 02:00Z naked fill): window accounting reconciled against EXECUTOR truth.

The ledger row's ``lots_filled`` / ``one_legged`` / the S1_LEGGED day-guard occurrence and the ``alarms``
count must come from what the EXECUTOR observed (``rest_fill`` / ``self.fills`` / the venue's
``/portfolio/fills``), never the core's bookkeeping alone. On 02:00Z the core lost two owned orders; the
executor filled + journaled them; the core-derived row read lots 0 / one_legged False / alarms 0 while the
executor had seen two naked lots and journaled ten alarms.

FAKES ONLY: no network, no proxy, no key, no gz-journal read, no holdout / seal read. The 02:00Z event
order comes from the committed ``fixtures/v33/naked_fill_20261003T020000Z.json`` (post-seal, 2026-10-03)."""

from __future__ import annotations

import json
import os
from decimal import Decimal
from types import SimpleNamespace

from service.v33.core import RungFill, WingBatch
from service.v33.reconcile import (
    ExecFill,
    alarm_breakdown,
    executor_truth_fills,
    rebuild_from_records,
    reconcile,
    reconcile_live,
)
from service.v33.stops import S1_LEGGED, count_legged, record_legged_occurrence, v33_day_guard_path
from service.stops import read_day_guard

_HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(_HERE, "fixtures", "v33", "naked_fill_20261003T020000Z.json")
D = Decimal


def _fix():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


FX = _fix()
B = FX["bucket_ticker"]


def _exec_from_fixture() -> list[ExecFill]:
    """The three 02:00Z ws rest fills as executor truth (#23: 0.40 + 0.60, #27: 1)."""
    return [ExecFill(count=D(f["count"]), coid=f["coid"], order_id=f["order_id"],
                     price=D(f["price"]), ticker=f["market"], source=f["path"]) for f in FX["fills"]]


def _core_rungfills_from_fixture() -> list[RungFill]:
    """What the FIXED core (#119 gate A) books for the same fills: #23 = 1 lot, #27 = 1 lot."""
    out: list[RungFill] = []
    for coid in (FX["fills"][0]["coid"], FX["fills"][2]["coid"]):   # #23, #27 (0.40+0.60 -> 1, and 1)
        f = next(x for x in FX["fills"] if x["coid"] == coid)
        out.append(RungFill(rung=4, E_rung=D("0.08"), price=D(f["price"]), count=D(1), server_ts=0.0,
                            coid=coid, order_id=f["order_id"], bucket_ticker=B, bucket_Sd=84600))
    return out


# ---------------------------------------------------------------------------
# 1. the 02:00Z rebuild: lots 2, one_legged True, alarms 10 (+ phantom surfaced)
# ---------------------------------------------------------------------------
def _incident_journal_records() -> list[dict]:
    """The 02:00Z journal event stream, synthesized from the committed fixture + the authoritative counts
    read once from the gz journal (reported in the build): ten kind-``alarm`` records (nine
    rest_invariant_violation via the executor's _record_alarm + one driver executor_standdown), one
    rest_invariant_phantom (its own kind), three ws rest_fill, zero take_wings. Tests never open the gz."""
    recs: list[dict] = []
    for r in FX["rejections"]:                      # nine pre-flight rejections -> nine executor alarms
        recs.append({"kind": "rest_invariant_violation", "obj": {"coid_attempted": r["coid"]}})
        recs.append({"kind": "alarm", "obj": {"alarm": "rest_invariant_violation",
                                              "coid_attempted": r["coid"]}})
    recs.append({"kind": "alarm", "obj": {"alarm": "executor_standdown",
                                          "reason": "rest_invariant_violation"}})
    recs.append({"kind": "rest_invariant_phantom", "obj": {"coid": "v33-2026-10-03T02:00:00Z-30"}})
    for f in FX["fills"]:
        recs.append({"kind": "rest_fill", "obj": {"client_order_id": f["coid"], "order_id": f["order_id"],
                                                  "count": f["count"], "rest_price": f["price"],
                                                  "market": f["market"], "path": f["path"]}})
    return recs


def test_rebuild_02z_row_from_journal_reads_lots2_onelegged_alarms10():
    out = rebuild_from_records(_incident_journal_records())
    assert out["lots_filled"] == 2
    assert out["one_legged"] is True
    assert out["one_legged_contracts"] == 2
    assert out["alarms"] == 10                                   # ten kind-"alarm" records
    br = out["alarms_breakdown"]
    assert br["total"] == 10 and br["journal_alarm_records"] == 10
    assert br["by_name"] == {"rest_invariant_violation": 9, "executor_standdown": 1}
    assert br["executor_phantom"] == 1                          # phantom surfaced, NOT in the headline 10
    assert out["exec_lots"] == 2 and out["hedged_lots"] == 0


def test_rebuild_catches_unbooked_regardless_of_core_prefix_and_fixed():
    """The reconciliation catches an unbooked fill whether the core booked it or not (gate E = defence in
    depth). PRE-#119 core (booked nothing): two unbooked, one-legged, mismatch. FIXED core (booked + hedged
    both): zero unbooked, not one-legged, no mismatch -- same two lots either way."""
    ex = _exec_from_fixture()
    pre = reconcile(exec_fills=ex, core_rest_fills=[], hedged_lots=D(0))     # core lost both
    assert pre.lots_filled == 2 and pre.one_legged is True
    assert len(pre.unbooked_fills) == 2 and pre.reconcile_mismatch is True
    assert {u["coid"] for u in pre.unbooked_fills} == {FX["fills"][0]["coid"], FX["fills"][2]["coid"]}

    fixed = reconcile(exec_fills=ex, core_rest_fills=_core_rungfills_from_fixture(), hedged_lots=D(2))
    assert fixed.lots_filled == 2 and fixed.one_legged is False
    assert fixed.unbooked_fills == () and fixed.reconcile_mismatch is False


# ---------------------------------------------------------------------------
# 2. core and executor agree -> no mismatch, reconciled passes core through
# ---------------------------------------------------------------------------
def test_core_and_executor_agree_no_mismatch():
    ex = [ExecFill(count=D(1), coid="a", order_id="oa", source="ws"),
          ExecFill(count=D(1), coid="b", order_id="ob", source="ws")]
    core = [RungFill(rung=0, E_rung=D("0.05"), price=D("0.45"), count=D(1), server_ts=0, coid="a",
                     order_id="oa"),
            RungFill(rung=1, E_rung=D("0.06"), price=D("0.44"), count=D(1), server_ts=0, coid="b",
                     order_id="ob")]
    r = reconcile(exec_fills=ex, core_rest_fills=core, hedged_lots=D(2), core_one_legged=False)
    assert r.lots_filled == 2 and r.core_lots == 2 and r.exec_lots == 2
    assert r.one_legged is False and r.reconcile_mismatch is False and r.unbooked_fills == ()


def test_dry_no_executor_truth_passes_core_through_unchanged():
    """DRY (the sim never journals a rest_fill): empty executor truth -> the reconciled row equals the
    core's own numbers, NO spurious mismatch (so an existing dry/healthy row stays byte-identical)."""
    core = [RungFill(rung=0, E_rung=D("0.05"), price=D("0.45"), count=D(1), server_ts=0, coid="a",
                     order_id="oa")]
    r = reconcile(exec_fills=[], core_rest_fills=core, hedged_lots=D(1), core_one_legged=False)
    assert r.lots_filled == 1 and r.exec_lots == 0
    assert r.reconcile_mismatch is False and r.unbooked_fills == () and r.one_legged is False


# ---------------------------------------------------------------------------
# 3. executor knows a fill the core does not -> unbooked, one-legged, S1 occurrence, mismatch
# ---------------------------------------------------------------------------
def test_executor_knows_fill_core_does_not_records_s1_to_tmp_guard(tmp_path):
    ex = [ExecFill(count=D(1), coid="ghost", order_id="oid-ghost", price=D("0.20"), ticker=B, source="ws")]
    r = reconcile(exec_fills=ex, core_rest_fills=[], hedged_lots=D(0), core_one_legged=False)
    assert r.one_legged is True and r.one_legged_contracts == 1
    assert len(r.unbooked_fills) == 1 and r.reconcile_mismatch is True
    assert r.mismatch_detail == {"core_lots": 0, "executor_lots": 1}
    # the S1_LEGGED day-guard occurrence is driven by the RECONCILED one_legged (what main() wires) -- to a
    # TMP guard path, never ops/.
    guard = v33_day_guard_path(str(tmp_path), "2026-10-03")
    assert r.one_legged
    n = record_legged_occurrence(guard, "2026-10-03", "2026-10-03T02:00:00Z", "reconciled one-legged", 0.0)
    assert n == 1 and count_legged(read_day_guard(guard, "2026-10-03")) == 1
    assert read_day_guard(guard, "2026-10-03").latched[0]["kind"] == S1_LEGGED


# ---------------------------------------------------------------------------
# 4. venue GET injected as failing -> venue_fills_unavailable journaled, row still reconciles
# ---------------------------------------------------------------------------
class _FakeJournal:
    def __init__(self):
        self.records: list[tuple[str, dict]] = []

    def append(self, kind, obj, ts):
        self.records.append((kind, obj))


class _RaisingProxy:
    def rest_get(self, path, params=None):
        raise RuntimeError("proxy down")


def test_venue_fetch_failure_journals_unavailable_and_still_reconciles_from_executor():
    from service.run_v33 import _fetch_venue_fills_v33

    journal = _FakeJournal()
    driver = SimpleNamespace(executor=SimpleNamespace(_by_order_id={"oid-x": "cx"}))
    venue = _fetch_venue_fills_v33(_RaisingProxy(), "2026-10-03T02:00:00Z", driver, journal, lambda: 0.0)
    assert venue is None
    assert any(k == "venue_fills_unavailable" for k, _ in journal.records)

    # the window still reconciles from the executor's own ws/poll fills (venue_fills absent).
    drv = SimpleNamespace(
        _exec_truth_fills=_exec_from_fixture(),
        executor=SimpleNamespace(fills=[]),
        state=SimpleNamespace(rest_fills=(), one_legged=False, wing_batches=()))
    r = reconcile_live(drv, venue)
    assert r.exec_lots == 2 and r.lots_filled == 2 and r.one_legged is True


def test_reconcile_live_dry_passthrough_with_completed_batch():
    """``reconcile_live`` on a DRY driver (no executor truth) passes the core through: lots = core, not
    one-legged (completed batch covers them), no mismatch -- the dry/healthy row is unperturbed. Also
    exercises ``_hedged_lots_from_state`` over the wing batches."""
    rf = [RungFill(rung=0, E_rung=D("0.05"), price=D("0.45"), count=D(1), server_ts=0, coid="a",
                   order_id="oa"),
          RungFill(rung=1, E_rung=D("0.06"), price=D("0.44"), count=D(1), server_ts=0, coid="b",
                   order_id="ob")]
    batch = WingBatch(index=0, server_ts=0, fills=tuple(rf), taken=True, completed=True)
    drv = SimpleNamespace(
        _exec_truth_fills=[],                                   # DRY: the sim journals no rest_fill
        executor=SimpleNamespace(fills=[]),
        state=SimpleNamespace(rest_fills=tuple(rf), one_legged=False, wing_batches=(batch,)))
    r = reconcile_live(drv, None)
    assert r.lots_filled == 2 and r.hedged_lots == 2
    assert r.one_legged is False and r.reconcile_mismatch is False and r.unbooked_fills == ()


def test_venue_fills_included_and_filtered_to_our_orders():
    from service.run_v33 import _fetch_venue_fills_v33

    class _P:
        def rest_get(self, path, params=None):
            return {"fills": [{"order_id": "oid-x", "count_fp": "1", "ticker": B, "price": "0.20"},
                              {"order_id": "foreign", "count_fp": "5", "ticker": "KXOTHER"}]}

    journal = _FakeJournal()
    driver = SimpleNamespace(executor=SimpleNamespace(_by_order_id={"oid-x": "cx"}))
    venue = _fetch_venue_fills_v33(_P(), "2026-10-03T02:00:00Z", driver, journal, lambda: 0.0)
    assert venue == [{"order_id": "oid-x", "client_order_id": None, "count": "1", "price": "0.20",
                      "ticker": B}]
    assert any(k == "venue_fills_fetched" for k, _ in journal.records)


# ---------------------------------------------------------------------------
# 5. alarm breakdown sums to alarms; executor alarms counted
# ---------------------------------------------------------------------------
def test_alarm_breakdown_sums_and_counts_executor():
    # the 02:00Z counters: executor _record_alarm x9, driver executor_standdown x1, core 0, ws 0, phantom 1.
    br = alarm_breakdown(
        driver_counts={"alarm": 0, "driver_alarm": 1},       # core ALARM actions = 0; driver ops = 1
        executor_counts={"alarm": 9, "rest_invariant_phantom": 1},
        ws_counts={"alarm": 0}, reconcile_alarms=0)
    assert br["total"] == 10
    assert br["executor"] == 9 and br["driver"] == 1 and br["core"] == 0 and br["ws"] == 0
    assert br["executor_phantom"] == 1                       # surfaced, NOT in total
    assert br["total"] == br["driver"] + br["core"] + br["executor"] + br["ws"] + br["reconcile"]


def test_alarm_breakdown_counts_reconcile_and_core_and_ws():
    br = alarm_breakdown(
        driver_counts={"alarm": 2, "driver_alarm": 3},       # 2 core ALARM actions, 3 driver ops alarms
        executor_counts={"alarm": 4},
        ws_counts={"alarm": 1}, reconcile_alarms=1)
    assert br["core"] == 2 and br["driver"] == 3 and br["executor"] == 4 and br["ws"] == 1
    assert br["reconcile"] == 1 and br["total"] == 11


# ---------------------------------------------------------------------------
# executor-truth gathering: ws/poll capture + executor.fills rest legs + venue, wings excluded
# ---------------------------------------------------------------------------
def test_executor_truth_fills_gathers_driver_executor_and_venue_excludes_wings():
    driver = SimpleNamespace(
        _exec_truth_fills=[ExecFill(count=D("0.4"), coid="a", order_id="oa", source="ws")],
        executor=SimpleNamespace(fills=[
            {"leg": "rest", "count": 1, "client_order_id": "b", "order_id": "ob", "price": D("0.2"),
             "ticker": B, "path": "amend"},
            {"leg": "wing", "count": 1, "client_order_id": "w", "order_id": "ow"}]))   # excluded
    venue = [{"order_id": "oc", "client_order_id": "c", "count": "1", "ticker": B}]
    fills = executor_truth_fills(driver, venue)
    srcs = sorted(f.source for f in fills)
    assert srcs == ["amend", "venue", "ws"]
    assert all(f.source != "wing" for f in fills)


def test_ws_echo_of_amend_cross_not_double_counted():
    """A ws echo of an amend-cross fill (same lot on two channels) is MAXed, not summed."""
    r = reconcile(exec_fills=[ExecFill(count=D(1), coid="a", order_id="oa", source="amend"),
                              ExecFill(count=D(1), coid="a", order_id="oa", source="ws")],
                  core_rest_fills=[RungFill(rung=0, E_rung=D("0.05"), price=D("0.45"), count=D(1),
                                            server_ts=0, coid="a", order_id="oa")], hedged_lots=D(1))
    assert r.exec_lots == 1 and r.lots_filled == 1 and r.reconcile_mismatch is False


def test_poll_cumulative_maxed_over_ws_partial():
    r = reconcile(exec_fills=[ExecFill(count=D("0.4"), coid="a", order_id="oa", source="ws"),
                              ExecFill(count=D("1.0"), coid="a", order_id="oa", source="poll")],
                  core_rest_fills=None, hedged_lots=D(0))
    assert r.exec_lots == 1 and r.one_legged is True


# ---------------------------------------------------------------------------
# ledger row: alarms_breakdown sets the headline; reconcile fields ride the row
# ---------------------------------------------------------------------------
def test_ledger_row_carries_alarms_breakdown_and_reconcile_fields():
    from service.v33.ledger import build_v33_ledger_row

    ex = _exec_from_fixture()
    recon = reconcile(exec_fills=ex, core_rest_fills=[], hedged_lots=D(0))
    br = alarm_breakdown(driver_counts={"alarm": 0, "driver_alarm": 1},
                         executor_counts={"alarm": 9, "rest_invariant_phantom": 1},
                         ws_counts={"alarm": 0}, reconcile_alarms=1)
    row = build_v33_ledger_row(
        close_time="2026-10-03T02:00:00Z", resolved_mode="armed", effective_mode="armed", degrade=None,
        params=None, state=None, driver_counts={}, executor_counts={}, ws_counts={"alarm": 0},
        strike_count=0, bucket_count=0, journal_path=None, record_count=0, stand_down_reason=None,
        now=0.0, armed=True, params_sha="x",
        one_legged=recon.as_row_fields()["one_legged"], lots_filled=recon.as_row_fields()["lots_filled"],
        alarms_breakdown=br, reconcile=recon.as_row_fields())
    assert row["alarms"] == br["total"] == 11                # 9 exec + 1 driver + 1 reconcile
    assert row["alarms_breakdown"]["executor"] == 9
    assert row["lots_filled"] == 2 and row["one_legged"] is True
    assert row["one_legged_contracts"] == 2 and row["reconcile_mismatch"] is True
    assert len(row["unbooked_fills"]) == 2


def test_ledger_row_alarms_fallback_to_ws_when_no_breakdown():
    """Back-compat: a row built WITHOUT a breakdown (stand-down rows, old callers) keeps the pre-gate-E
    behaviour -- ``alarms`` = ws recorder alarms only."""
    from service.v33.ledger import build_v33_ledger_row

    row = build_v33_ledger_row(
        close_time="2026-10-03T02:00:00Z", resolved_mode="dry", effective_mode="dry", degrade=None,
        params=None, state=None, driver_counts={}, executor_counts={}, ws_counts={"alarm": 3},
        strike_count=0, bucket_count=0, journal_path=None, record_count=0, stand_down_reason=None,
        now=0.0, params_sha="x")
    assert row["alarms"] == 3 and row["alarms_breakdown"] == {}
    assert row["unbooked_fills"] == [] and row["reconcile_mismatch"] is False


# ---------------------------------------------------------------------------
# report: the falsifier one-legged count comes from the RECONCILED row
# ---------------------------------------------------------------------------
def test_report_gate_table_counts_reconciled_one_legged_without_wing_batch_sets():
    """The 02:00Z row shape: armed/realised, TWO reconciled one-legged contracts but ZERO wing_batch_sets
    (no wings ever taken). The pre-gate-E batch-sum read 0; the reconciled field makes it 2."""
    from service.v33.report import build_falsifier_gate_table

    def _row(ol):
        return {"roster": "DegeneracyV3_3", "close_time": "2026-10-03T02:00:00Z", "mode": "armed",
                "armed": True, "dry_sim": False, "wing_batch_sets": [], "rung_fills": [],
                "one_legged_contracts": ol, "ladder": {}}
    gt = build_falsifier_gate_table([_row(2)])
    assert gt["one_legged"] == 2                               # reconciled, not the (empty) batch sum
    # one more than the pin (<= 2) is an immediate KILL, driven by the reconciled count.
    gt3 = build_falsifier_gate_table([_row(3)])
    assert gt3["one_legged"] == 3 and gt3["verdict"].startswith("KILL")


def test_report_window_surfaces_unbooked_orphan_and_alarms():
    from service.v33.report import build_v33_report

    row = {"roster": "DegeneracyV3_3", "close_time": "2026-10-03T02:00:00Z",
           "effective_mode": "armed", "armed": True, "dry_sim": False, "ladder": {"rungs_filled": 0},
           "unbooked_fills": [{"coid": "a"}, {"coid": "b"}], "reconcile_mismatch": True,
           "one_legged_contracts": 2, "alarms": 10,
           "alarms_breakdown": {"total": 10, "executor": 9, "driver": 1},
           "rung_fills": [{"orphan": True, "count": 1}, {"orphan": False, "count": 1}]}
    rep = build_v33_report([row])
    w = rep["windows"][0]
    assert w["unbooked_fills"] == 2 and w["reconcile_mismatch"] is True
    assert w["orphan_rung_fills"] == 1 and w["alarms"] == 10
    t = rep["totals"]
    assert t["unbooked_fills"] == 2 and t["orphan_rung_fills"] == 1
    assert t["reconcile_mismatch_windows"] == 1 and t["alarms"] == 10


# ---------------------------------------------------------------------------
# the rebuild CLI tool: reads a .jsonl, refuses the SEAL window
# ---------------------------------------------------------------------------
def _load_rebuild_tool():
    import importlib.util
    repo = os.path.dirname(os.path.dirname(_HERE))
    path = os.path.join(repo, "tools", "rebuild_v33_row.py")
    spec = importlib.util.spec_from_file_location("rebuild_v33_row", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_rebuild_cli_tool_rebuilds_from_a_tmp_jsonl(tmp_path):
    mod = _load_rebuild_tool()
    jpath = tmp_path / "20261003T020000Z.jsonl"           # plain .jsonl (NOT the gz operational journal)
    with open(jpath, "w", encoding="utf-8") as f:
        for r in _incident_journal_records():
            f.write(json.dumps(r) + "\n")
    out = mod.rebuild(str(jpath))
    assert out["lots_filled"] == 2 and out["one_legged"] is True and out["alarms"] == 10
    assert out["close_time"] == "2026-10-03T02:00:00Z"


def test_rebuild_cli_tool_refuses_the_seal_window(tmp_path):
    import pytest
    mod = _load_rebuild_tool()
    jpath = tmp_path / "20260810T120000Z.jsonl"           # 2026-08-10 is inside the SEAL window
    jpath.write_text("", encoding="utf-8")
    with pytest.raises(SystemExit):
        mod.rebuild(str(jpath))


def test_rebuild_cli_close_iso_from_filename():
    mod = _load_rebuild_tool()
    assert mod.close_iso_from_filename("20261003T020000Z.jsonl.gz") == "2026-10-03T02:00:00Z"
    assert mod.close_iso_from_filename("20261003T020000Z.jsonl") == "2026-10-03T02:00:00Z"
    assert mod.close_iso_from_filename("not_a_journal.txt") is None
