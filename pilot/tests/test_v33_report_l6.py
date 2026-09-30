"""L6 (Brad 2026-09-30, pre-freeze) report tests: the S4 day-loss CAMPAIGN kill (any armed day at the
$3.00 cap -> verdict KILL regardless of n, read from the ops/v33_stops_YYYY-MM-DD.json guard files), the
verdict n dropped 30 -> 15 (n=14 pending / n=15 decided), the promotion pin at 15, and the mean-lock bar
at +4.0c (+4.5c ALIVE / +3.5c KILL). Pure over synthetic rows + a tmp ops dir for the guard file."""

from __future__ import annotations

from decimal import Decimal

import os

from service.stops import record_latched_stop
from service.v33.falsifier_pins import V33_KILL_ON_S4_DAY_LOSS, V33_PROMOTION_MIN_N
from service.v33 import report as report_mod
from service.v33.report import (
    _render_gate_table,
    _resolve_v33_guard_path,
    build_falsifier_gate_table,
)
from service.v33.stops import S4_DAY_LOSS, v33_day_guard_path


def _rf10(solved_c, realised_c):
    """A rung fill at the 10c margin (E_rung 0.10) with the given solved / realised locks (cents)."""
    d = {"rung": 5, "E_rung": "0.10", "price": "0.40", "count": 1,
         "lock_solved": str(Decimal(str(solved_c)) / 100)}
    if realised_c is not None:
        d["realized_lock"] = str(Decimal(str(realised_c)) / 100)
    return d


def _row(ct, *, n, realised_c, solved_c):
    """A realised armed window with ``n`` 10c-margin rung fills, a filled 0.10 shadow (capture at 10c
    measurable), full single-order rolls. All non-mean gates pass when realised_c > 0 and the shortfall
    solved_c - realised_c <= 3."""
    fills = [_rf10(solved_c, realised_c) for _ in range(n)]
    return {
        "roster": "DegeneracyV3_3", "close_time": ct, "mode": "armed", "effective_mode": "armed",
        "armed": True, "dry_sim": False, "spot_bucket_ticker": "KXBTC-X", "rung_fills": fills,
        "wing_batch_sets": [{"index": 0, "fill_count": n, "one_legged": False, "completed": True}],
        "ladder": {"rungs_filled": n, "contracts": n, "roll_count": 4, "roll_single_order_count": 4,
                   "ladder_lock": "0", "shallowest_margin_c": "10.00", "deepest_margin_c": "10.00",
                   "creates": n, "amends_attempted": 4, "cancels": 0},
        "shadow": {"0.10": {"filled": True, "lock": "0.10", "offer": "0.60"}},
    }


def _empty_armed_row(ct):
    """An armed window that entered nothing (stand-down / no fills): n=0, all gates n-too-small."""
    return {
        "roster": "DegeneracyV3_3", "close_time": ct, "mode": "armed", "effective_mode": "armed",
        "armed": True, "dry_sim": False, "spot_bucket_ticker": "KXBTC-X", "rung_fills": [],
        "wing_batch_sets": [],
        "ladder": {"rungs_filled": 0, "contracts": 0, "roll_count": 0, "roll_single_order_count": 0,
                   "ladder_lock": "0"},
    }


def _all_green_15(ct="2026-09-24T04:00:00Z"):
    """A single armed window with n=15 where EVERY falsifier gate passes (mean 5c, shortfall 1c)."""
    return _row(ct, n=15, realised_c=5, solved_c=6)


# ---------------------------------------------------------------------------
# (a) S4 day-loss campaign kill
# ---------------------------------------------------------------------------
def test_s4_latch_forces_kill_even_when_all_gates_green(tmp_path):
    """An S4 latch on the report's day forces KILL even at n=15 with every other gate PASS."""
    ops = str(tmp_path)
    row = _all_green_15("2026-09-24T04:00:00Z")
    # Sanity: without the guard file (ops_dir=None) the same row is ALIVE.
    gt_clean = build_falsifier_gate_table([row], None)
    assert gt_clean["verdict"] == "ALIVE-so-far"
    assert gt_clean["s4_kill_days"] == []
    # Latch S4 on 2026-09-24 -> KILL naming the day.
    record_latched_stop(v33_day_guard_path(ops, "2026-09-24"), "2026-09-24", S4_DAY_LOSS,
                        "day-loss cap breached", "2026-09-24T04:00:00Z", 1_700_000_000.0)
    gt = build_falsifier_gate_table([row], ops)
    assert gt["s4_kill_days"] == ["2026-09-24"]
    assert gt["verdict"].startswith("KILL")
    assert "S4 day-loss latched on 2026-09-24" in gt["verdict"]
    # every underlying gate still reads PASS -- the kill is the S4 latch, not a gate miss.
    assert all(g["status"] == "PASS" for g in gt["gates"]), gt["gates"]


def test_s4_latch_kills_at_n_zero(tmp_path):
    """The S4 kill fires at n=0 (a stand-down day at the cap): the campaign stops with no fills."""
    ops = str(tmp_path)
    record_latched_stop(v33_day_guard_path(ops, "2026-09-25"), "2026-09-25", S4_DAY_LOSS,
                        "day-loss cap breached", None, 1_700_000_000.0)
    gt = build_falsifier_gate_table([_empty_armed_row("2026-09-25T04:00:00Z")], ops)
    assert gt["n_rung_fills"] == 0
    assert gt["s4_kill_days"] == ["2026-09-25"]
    assert gt["verdict"].startswith("KILL") and "S4 day-loss latched on 2026-09-25" in gt["verdict"]


def test_s4_scans_all_report_days(tmp_path):
    """The day range scanned is EVERY UTC day the report covers; a latch on any one is a kill."""
    ops = str(tmp_path)
    rows = [_row("2026-09-24T04:00:00Z", n=8, realised_c=5, solved_c=6),
            _row("2026-09-26T04:00:00Z", n=7, realised_c=5, solved_c=6)]   # n=15 pooled, ALIVE clean
    assert build_falsifier_gate_table(rows, ops)["verdict"] == "ALIVE-so-far"
    # latch S4 only on the SECOND day -> still a kill, naming 09-26.
    record_latched_stop(v33_day_guard_path(ops, "2026-09-26"), "2026-09-26", S4_DAY_LOSS,
                        "cap", None, 1_700_000_000.0)
    gt = build_falsifier_gate_table(rows, ops)
    assert gt["s4_kill_days"] == ["2026-09-26"]
    assert gt["verdict"].startswith("KILL")


def test_non_s4_latch_does_not_trigger_s4_kill(tmp_path):
    """A guard latched with a DIFFERENT kind (S1_LEGGED) is NOT the S4 campaign kill."""
    ops = str(tmp_path)
    record_latched_stop(v33_day_guard_path(ops, "2026-09-24"), "2026-09-24", "S1_LEGGED",
                        "one-legged below floor", None, 1_700_000_000.0)
    gt = build_falsifier_gate_table([_all_green_15("2026-09-24T04:00:00Z")], ops)
    assert gt["s4_kill_days"] == []
    assert gt["verdict"] == "ALIVE-so-far"


def test_s4_kill_pin_is_true():
    assert V33_KILL_ON_S4_DAY_LOSS is True


# ---------------------------------------------------------------------------
# (b) verdict n dropped 30 -> 15
# ---------------------------------------------------------------------------
def test_verdict_pending_at_n_14_decided_at_n_15():
    gt14 = build_falsifier_gate_table([_row("2026-09-24T04:00:00Z", n=14, realised_c=5, solved_c=6)])
    assert gt14["n_rung_fills"] == 14
    assert gt14["verdict"].startswith("n<15 pending")
    gt15 = build_falsifier_gate_table([_row("2026-09-24T04:00:00Z", n=15, realised_c=5, solved_c=6)])
    assert gt15["n_rung_fills"] == 15
    assert gt15["verdict"] == "ALIVE-so-far"


# ---------------------------------------------------------------------------
# (c) promotion condition reads n >= 15
# ---------------------------------------------------------------------------
def test_promotion_min_n_is_15():
    assert V33_PROMOTION_MIN_N == 15


# ---------------------------------------------------------------------------
# (d) mean-lock bar at +4.0c
# ---------------------------------------------------------------------------
def test_mean_lock_bar_4c_alive_and_kill():
    # +4.5c at n=15 with everything else green -> ALIVE-so-far.
    gt_alive = build_falsifier_gate_table([_row("2026-09-24T04:00:00Z", n=15, realised_c="4.5",
                                                 solved_c=6)])
    mg = next(g for g in gt_alive["gates"] if g["gate"] == "mean true lock")
    assert mg["status"] == "PASS" and gt_alive["verdict"] == "ALIVE-so-far"
    # +3.5c (above the +2.0c early kill, below the +4.0c bar) -> the verdict mean-lock gate FAILS -> KILL.
    gt_kill = build_falsifier_gate_table([_row("2026-09-24T04:00:00Z", n=15, realised_c="3.5",
                                                solved_c=6)])
    mg2 = next(g for g in gt_kill["gates"] if g["gate"] == "mean true lock")
    assert mg2["status"] == "FAIL" and gt_kill["verdict"].startswith("KILL")
    assert "S4" not in gt_kill["verdict"]   # the kill is the mean bar, not an S4 latch


# ---------------------------------------------------------------------------
# (e) L6 REVIEW: the verdict-boundary matrix at n=15 -- ONE coherent verdict
#     +1.5c early-kill (single line, no doubled gate list), +3.0c gate-kill (a
#     miss is a kill), +4.0c EXACTLY ALIVE (>= not >), +4.5c ALIVE.
# ---------------------------------------------------------------------------
def test_verdict_matrix_1p5c_early_kill_single_line():
    """mean +1.5c at n=15: the EARLY kill (mean < +2.0c) fires and produces ONE coherent verdict line --
    it must NOT also append the 'mean true lock' gate-fail (that lives in the else branch, unreached)."""
    gt = build_falsifier_gate_table([_row("2026-09-24T04:00:00Z", n=15, realised_c="1.5", solved_c=6)])
    assert gt["n_rung_fills"] == 15
    v = gt["verdict"]
    assert v.startswith("KILL")
    assert "< +2.0c" in v                       # the early-kill reason
    assert "mean true lock" not in v            # NOT the verdict-gate name -> not two contradictory lines
    assert v.count("KILL") == 1                  # a single KILL verdict, not two joined


def test_verdict_matrix_3c_is_gate_kill():
    """mean +3.0c at n=15 (between the +2.0c early kill and the +4.0c bar): a MISS is a KILL, via the
    verdict mean-lock gate (not the early kill, not S4)."""
    gt = build_falsifier_gate_table([_row("2026-09-24T04:00:00Z", n=15, realised_c=3, solved_c=6)])
    mg = next(g for g in gt["gates"] if g["gate"] == "mean true lock")
    assert mg["status"] == "FAIL"
    v = gt["verdict"]
    assert v.startswith("KILL") and "mean true lock" in v
    assert "< +2.0c" not in v and "S4" not in v


def test_verdict_matrix_4c_exactly_alive():
    """mean EXACTLY +4.0c at n=15: PASS (the bar is >=, not >) -> ALIVE-so-far."""
    gt = build_falsifier_gate_table([_row("2026-09-24T04:00:00Z", n=15, realised_c=4, solved_c=6)])
    mg = next(g for g in gt["gates"] if g["gate"] == "mean true lock")
    assert mg["status"] == "PASS" and gt["verdict"] == "ALIVE-so-far"


def test_verdict_matrix_4p5c_alive():
    gt = build_falsifier_gate_table([_row("2026-09-24T04:00:00Z", n=15, realised_c="4.5", solved_c=6)])
    assert gt["verdict"] == "ALIVE-so-far"


# ---------------------------------------------------------------------------
# (f) L6 REVIEW: S4 latch on a day with NO ledger row is still seen (gap fix).
#     The scan is the report's [min row day, max row day] RANGE, not only days
#     that produced a ledger row -- a day that latched S4 but wrote no row
#     (process died after latching, or the row was deduped) would be invisible.
# ---------------------------------------------------------------------------
def test_s4_latch_on_rowless_day_in_range_is_seen(tmp_path):
    ops = str(tmp_path)
    rows = [_row("2026-09-24T04:00:00Z", n=8, realised_c=5, solved_c=6),
            _row("2026-09-26T04:00:00Z", n=7, realised_c=5, solved_c=6)]   # n=15 pooled, clean ALIVE
    assert build_falsifier_gate_table(rows, ops)["verdict"] == "ALIVE-so-far"
    # latch S4 on 2026-09-25 -- a day WITHIN the range that has NO ledger row.
    record_latched_stop(v33_day_guard_path(ops, "2026-09-25"), "2026-09-25", S4_DAY_LOSS,
                        "cap", None, 1_700_000_000.0)
    gt = build_falsifier_gate_table(rows, ops)
    assert gt["s4_kill_days"] == ["2026-09-25"]
    assert gt["verdict"].startswith("KILL") and "S4 day-loss latched on 2026-09-25" in gt["verdict"]


def test_s4_latch_outside_row_range_is_ignored(tmp_path):
    """A guard file OUTSIDE the report's [min,max] row-day range is not scanned (it is a different
    reporting window)."""
    ops = str(tmp_path)
    rows = [_row("2026-09-26T04:00:00Z", n=15, realised_c=5, solved_c=6)]   # range is a single day
    record_latched_stop(v33_day_guard_path(ops, "2026-09-24"), "2026-09-24", S4_DAY_LOSS,
                        "cap", None, 1_700_000_000.0)
    gt = build_falsifier_gate_table(rows, ops)
    assert gt["s4_kill_days"] == []
    assert gt["verdict"] == "ALIVE-so-far"


# ---------------------------------------------------------------------------
# (g) L6 REVIEW: a CORRUPT guard in range is SURFACED (S4 state unknown), not
#     silently dropped -- it warns and taints an otherwise-clean verdict, and
#     is NOT counted as an S4 kill.
# ---------------------------------------------------------------------------
def test_corrupt_guard_surfaces_warning_and_taints_alive(tmp_path):
    ops = str(tmp_path)
    row = _all_green_15("2026-09-24T04:00:00Z")
    # write a malformed guard file for the report's day.
    with open(v33_day_guard_path(ops, "2026-09-24"), "w", encoding="utf-8") as f:
        f.write("{ this is not valid json")
    gt = build_falsifier_gate_table([row], ops)
    assert gt["s4_corrupt_days"] == ["2026-09-24"]
    assert gt["s4_kill_days"] == []                 # a corrupt guard is NOT an S4 kill
    v = gt["verdict"]
    assert v.startswith("ALIVE-so-far")             # gates still pass ...
    assert "WARNING" in v and "CORRUPT" in v and "2026-09-24" in v   # ... but it can't read all-clear


def test_corrupt_guard_warning_rides_alongside_s4_kill(tmp_path):
    """A real S4 latch on one day AND a corrupt guard on another in range: KILL verdict carries the
    corrupt WARNING too (neither masks the other)."""
    ops = str(tmp_path)
    rows = [_row("2026-09-24T04:00:00Z", n=8, realised_c=5, solved_c=6),
            _row("2026-09-25T04:00:00Z", n=7, realised_c=5, solved_c=6)]
    record_latched_stop(v33_day_guard_path(ops, "2026-09-24"), "2026-09-24", S4_DAY_LOSS,
                        "cap", None, 1_700_000_000.0)
    with open(v33_day_guard_path(ops, "2026-09-25"), "w", encoding="utf-8") as f:
        f.write("nope")
    gt = build_falsifier_gate_table(rows, ops)
    assert gt["s4_kill_days"] == ["2026-09-24"]
    assert gt["s4_corrupt_days"] == ["2026-09-25"]
    v = gt["verdict"]
    assert v.startswith("KILL") and "S4 day-loss latched on 2026-09-24" in v
    assert "WARNING" in v and "CORRUPT" in v


# ---------------------------------------------------------------------------
# (h) L6 REVIEW regression: the live checkout-fallback path of
#     _resolve_v33_guard_path must not raise (os import). This path runs only
#     when DV3_DATA_DIR is set and the ops dir IS the resolved live ops dir --
#     which every unit test that passes an explicit tmp ops dir skips, so the
#     missing `import os` was invisible to the suite until now.
# ---------------------------------------------------------------------------
def test_resolve_guard_path_checkout_fallback_does_not_raise(tmp_path, monkeypatch):
    primary_dir = tmp_path / "data" / "ops"
    checkout_dir = tmp_path / "checkout" / "ops"
    primary_dir.mkdir(parents=True)
    checkout_dir.mkdir(parents=True)
    # the checkout has the guard file; the primary (data-dir) does NOT -> fallback returns the checkout.
    day = "2026-09-24"
    with open(os.path.join(str(checkout_dir), f"v33_stops_{day}.json"), "w", encoding="utf-8") as f:
        f.write("{}")
    monkeypatch.setattr(report_mod, "data_dir", lambda: str(tmp_path / "data"))
    monkeypatch.setattr(report_mod, "ops_dir_v33", lambda: str(primary_dir))
    monkeypatch.setattr(report_mod, "checkout_ops_dir", lambda: str(checkout_dir))
    resolved = _resolve_v33_guard_path(str(primary_dir), day)   # must not raise NameError
    assert resolved == os.path.join(str(checkout_dir), f"v33_stops_{day}.json")


# ---------------------------------------------------------------------------
# (i) L6 REVIEW: the rendered gate table surfaces both the S4 campaign-kill
#     line and the corrupt-guard WARNING line to the operator.
# ---------------------------------------------------------------------------
def test_render_gate_table_shows_s4_and_corrupt_lines(tmp_path):
    ops = str(tmp_path)
    rows = [_row("2026-09-24T04:00:00Z", n=8, realised_c=5, solved_c=6),
            _row("2026-09-25T04:00:00Z", n=7, realised_c=5, solved_c=6)]
    record_latched_stop(v33_day_guard_path(ops, "2026-09-24"), "2026-09-24", S4_DAY_LOSS,
                        "cap", None, 1_700_000_000.0)
    with open(v33_day_guard_path(ops, "2026-09-25"), "w", encoding="utf-8") as f:
        f.write("broken")
    gt = build_falsifier_gate_table(rows, ops)
    text = "\n".join(_render_gate_table(gt))
    assert "S4 day-loss latched (campaign KILL, L6): 2026-09-24" in text
    assert "WARNING: V3.3 day-guard CORRUPT" in text and "2026-09-25" in text
