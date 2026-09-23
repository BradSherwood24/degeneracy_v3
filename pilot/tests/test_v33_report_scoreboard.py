"""V3.3 report L3: the LADDER SCOREBOARD (per-margin, DRY separated from realised), the FALSIFIER GATE
TABLE (PASS/FAIL/n-too-small + kill), the capture ratio at 10c, the side-by-side entered-vs-not lists, and
the DEEP END (SO-3) aggregation. Pure over synthetic rows; no network, no file."""

from __future__ import annotations

from decimal import Decimal

from service.v33.report import (
    build_deep_end,
    build_falsifier_gate_table,
    build_ladder_scoreboard,
    build_side_by_side,
    build_v33_report,
    main,
)


def _rf(margin_c, solved_c, realised_c, count=1):
    d = {"rung": margin_c - 5, "E_rung": str(Decimal(margin_c) / 100), "price": "0.40",
         "count": count, "lock_solved": str(Decimal(solved_c) / 100)}
    if realised_c is not None:
        d["realized_lock"] = str(Decimal(realised_c) / 100)
    return d


def _sweep_row(ct, *, realised_c=8, solved_c=10, dry_sim=False, shadow_10="0.10", rolls=4, single=4):
    """A full-sweep realised (or dry) row: 11 rung fills at margins 5..15, all same solved/realised."""
    fills = [_rf(m, solved_c, realised_c) for m in range(5, 16)]
    row = {
        "roster": "DegeneracyV3_3", "close_time": ct, "mode": "armed" if not dry_sim else "dry",
        "effective_mode": "armed" if not dry_sim else "dry", "armed": not dry_sim, "dry_sim": dry_sim,
        "spot_bucket_ticker": "KXBTC-X", "rung_fills": fills,
        "wing_batch_sets": [{"index": 0, "fill_count": 11, "one_legged": False, "completed": True}],
        "ladder": {"rungs_filled": 11, "contracts": 11, "roll_count": rolls,
                   "roll_single_order_count": single, "ladder_lock": "0.88",
                   "shallowest_margin_c": "5.00", "deepest_margin_c": "15.00",
                   "creates": 11, "amends_attempted": rolls, "cancels": 0},
    }
    if shadow_10 is not None:
        row["shadow"] = {"0.10": {"filled": True, "lock": shadow_10, "offer": "0.60"}}
    return row


# ---------------------------------------------------------------------------
# scoreboard
# ---------------------------------------------------------------------------
def test_scoreboard_separates_dry_from_realised():
    rows = [_sweep_row("2026-09-24T04:00:00Z", realised_c=8, dry_sim=False),
            _sweep_row("2026-09-24T05:00:00Z", realised_c=9, dry_sim=True)]
    sb = build_ladder_scoreboard(rows)
    # realised section has 11 margins with n_completed 1 each; dry section too, but SEPARATE
    assert sb["realised"]["pooled"]["contracts"] == 11
    assert sb["dry"]["pooled"]["contracts"] == 11
    # realised mean is 8c, dry mean is 9c -- never pooled together
    assert sb["realised"]["pooled"]["mean_realised_lock_c"] == Decimal(8)
    assert sb["dry"]["pooled"]["mean_realised_lock_c"] == Decimal(9)
    # per-margin: margin 10 present in realised with solved 10c, realised 8c -> shortfall 2c
    m10 = next(m for m in sb["realised"]["margins"] if m["margin_c"] == 10)
    assert m10["mean_solved_c"] == Decimal(10) and m10["mean_realised_c"] == Decimal(8)
    assert m10["shortfall_c"] == Decimal(2) and m10["pct_positive"] == Decimal(100)


def test_capture_ratio_at_10c():
    # two armed+bucket rows: one has both a shadow 0.10 fill AND a 10c completed rung (all sweeps do),
    # one has a shadow 0.10 fill but NO 10c fill -> capture = 1/2.
    r1 = _sweep_row("2026-09-24T04:00:00Z", shadow_10="0.10")
    r2 = _sweep_row("2026-09-24T05:00:00Z", shadow_10="0.10")
    r2["rung_fills"] = [rf for rf in r2["rung_fills"] if int(Decimal(rf["E_rung"]) * 100) != 10]
    sb = build_ladder_scoreboard([r1, r2])
    cap = sb["capture_10c"]
    assert cap["shadow"] == 2 and cap["live"] == 1 and cap["ratio"] == Decimal(1) / Decimal(2)


# ---------------------------------------------------------------------------
# gate table
# ---------------------------------------------------------------------------
def test_gate_table_n_too_small_pending():
    gt = build_falsifier_gate_table([_sweep_row("2026-09-24T04:00:00Z")])   # n=11 < 30
    assert gt["n_rung_fills"] == 11
    assert gt["verdict"].startswith("n<30 pending")
    mean_gate = next(g for g in gt["gates"] if g["gate"] == "mean true lock")
    assert mean_gate["status"] == "n-too-small"


def test_gate_table_alive_at_n_over_30():
    rows = [_sweep_row(f"2026-09-24T{h:02d}:00:00Z", realised_c=8, solved_c=10) for h in range(3)]  # 33
    gt = build_falsifier_gate_table(rows)
    assert gt["n_rung_fills"] == 33
    assert gt["mean_lock_c"] == Decimal(8)
    # every gate PASS: mean 8>=6, shortfall 2<=3, %pos 100>=80, capture 1.0>=0.5, one-legged 0<=2, roll 1.0
    assert all(g["status"] == "PASS" for g in gt["gates"]), gt["gates"]
    assert gt["verdict"] == "ALIVE-so-far"


def test_gate_table_kill_low_mean_at_n_over_15():
    rows = [_sweep_row(f"2026-09-24T{h:02d}:00:00Z", realised_c=1, solved_c=10) for h in range(2)]  # 22
    gt = build_falsifier_gate_table(rows)
    assert gt["n_rung_fills"] == 22 and gt["mean_lock_c"] == Decimal(1)
    assert gt["verdict"].startswith("KILL") and "mean lock" in gt["verdict"]


def test_gate_table_kill_on_one_legged():
    r = _sweep_row("2026-09-24T04:00:00Z", realised_c=8)
    r["wing_batch_sets"] = [{"index": 0, "fill_count": 3, "one_legged": True, "completed": False}]
    gt = build_falsifier_gate_table([r])
    assert gt["one_legged"] == 3
    assert gt["verdict"].startswith("KILL") and "one-legged" in gt["verdict"]


def test_gate_table_fail_on_wide_shortfall_at_n_over_30():
    rows = [_sweep_row(f"2026-09-24T{h:02d}:00:00Z", realised_c=3, solved_c=10) for h in range(3)]  # 33
    gt = build_falsifier_gate_table(rows)                       # shortfall 7c > 3c AND mean 3c < 6c
    assert gt["n_rung_fills"] == 33
    assert gt["verdict"].startswith("KILL")
    sf = next(g for g in gt["gates"] if g["gate"].startswith("per-rung shortfall"))
    assert sf["status"] == "FAIL" and sf["value"] == Decimal(7)


# ---------------------------------------------------------------------------
# side-by-side entered-vs-not
# ---------------------------------------------------------------------------
def _v32_row(ct, filled=True, lock="0.11"):
    row = {"roster": "DegeneracyV3_2", "close_time": ct, "mode": "armed", "effective_mode": "armed"}
    if filled:
        row["lots_filled"] = 2
        row["wing_batch_sets"] = [{"index": 0, "fill_count": 2, "realized_lock": lock, "completed": True}]
    else:
        row["lots_filled"] = 0
    return row


def test_side_by_side_entered_lists():
    ct1 = "2026-09-24T04:00:00Z"   # both enter
    ct2 = "2026-09-24T05:00:00Z"   # v33 enters, v32 does not
    ct3 = "2026-09-24T06:00:00Z"   # v32 enters, v33 does not
    v33 = [_sweep_row(ct1), _sweep_row(ct2),
           {"roster": "DegeneracyV3_3", "close_time": ct3, "mode": "dry", "effective_mode": "dry",
            "dry_sim": True, "ladder": {"contracts": 0, "rungs_filled": 0, "ladder_lock": "0",
                                        "roll_count": 0, "roll_single_order_count": 0}}]
    v32 = [_v32_row(ct1, filled=True), _v32_row(ct2, filled=False), _v32_row(ct3, filled=True)]
    sxs = build_side_by_side(v33, v32)
    assert sxs["v33_entered_v32_did_not"] == [ct2]
    assert sxs["v32_entered_v33_did_not"] == [ct3]
    assert sxs["totals"]["windows"] == 3
    assert len(sxs["per_day"]) == 1 and sxs["per_day"][0]["day"] == "2026-09-24"


# ---------------------------------------------------------------------------
# deep end (SO-3) aggregation
# ---------------------------------------------------------------------------
def test_deep_end_aggregation():
    def deep(reached_margins, absorb):
        return {"margins_c": list(range(16, 26)), "deepest_yes_print": "0.98", "prints_observed": 5,
                "reached_count": len(reached_margins),
                "rungs": [{"margin_c": m, "reached": m in reached_margins,
                           "first": ({"lock_solved": "0.16"} if m in reached_margins else None),
                           "absorption_lots": str(absorb if m in reached_margins else 0),
                           "prints_through": (2 if m in reached_margins else 0)}
                          for m in range(16, 26)]}
    rows = [{"close_time": "2026-09-24T04:00:00Z", "mode": "dry", "deep_obs": deep([16, 17], 5)},
            {"close_time": "2026-09-24T05:00:00Z", "mode": "dry", "deep_obs": deep([16], 3)}]
    de = build_deep_end(rows)
    assert de["windows_with_deep_obs"] == 2
    m16 = next(m for m in de["margins"] if m["margin_c"] == 16)
    assert m16["reached_windows"] == 2 and m16["absorption_lots"] == Decimal(8)  # 5 + 3
    assert m16["prints_through"] == 4 and m16["mean_ideal_lock_c"] == Decimal(16)
    m17 = next(m for m in de["margins"] if m["margin_c"] == 17)
    assert m17["reached_windows"] == 1


# ---------------------------------------------------------------------------
# capture ratio exclusions + gate n-too-small when no shadow
# ---------------------------------------------------------------------------
def test_capture_excludes_below_n_min_shadow_fill():
    r = _sweep_row("2026-09-24T04:00:00Z", shadow_10="0.03")   # 3c lock, but below n_min offer
    r["shadow"]["0.10"]["offer"] = "0.97"                       # n = 1-0.97 = 0.03 < n_min 0.05
    sb = build_ladder_scoreboard([r])
    assert sb["capture_10c"]["shadow"] == 0 and sb["capture_10c"]["ratio"] is None


def test_gate_capture_none_fails_closed_at_n_over_30():
    """F1 (fail-closed): at n >= 30 an UNMEASURABLE capture (no valid shadow availability, ratio None) is
    a FAIL, not a pass -- and the verdict is NOT ALIVE. Below n >= 30 it stays n-too-small."""
    rows = [_sweep_row(f"2026-09-24T{h:02d}:00:00Z", realised_c=8, shadow_10=None) for h in range(3)]
    gt = build_falsifier_gate_table(rows)                       # n=33, no shadow
    cap_gate = next(g for g in gt["gates"] if g["gate"].startswith("capture ratio"))
    assert cap_gate["status"] == "FAIL" and cap_gate["value"] is None
    assert gt["verdict"] != "ALIVE-so-far" and gt["verdict"].startswith("KILL")


def test_gate_capture_none_is_n_too_small_below_min_n():
    gt = build_falsifier_gate_table([_sweep_row("2026-09-24T04:00:00Z", realised_c=8, shadow_10=None)])
    cap_gate = next(g for g in gt["gates"] if g["gate"].startswith("capture ratio"))
    assert gt["n_rung_fills"] == 11                              # < 30
    assert cap_gate["status"] == "n-too-small"                  # below MIN_N: unmeasured, not yet a FAIL


def test_scoreboard_dry_only_leaves_realised_empty():
    rows = [_sweep_row("2026-09-24T04:00:00Z", dry_sim=True)]
    sb = build_ladder_scoreboard(rows)
    assert sb["realised"]["margins"] == [] and sb["realised"]["pooled"]["contracts"] == 0
    assert sb["dry"]["pooled"]["contracts"] == 11


def test_side_by_side_per_day_and_totals():
    rows32 = [_v32_row("2026-09-24T04:00:00Z"), _v32_row("2026-09-25T04:00:00Z")]
    rows33 = [_sweep_row("2026-09-24T04:00:00Z"), _sweep_row("2026-09-25T04:00:00Z")]
    sxs = build_side_by_side(rows33, rows32)
    assert len(sxs["per_day"]) == 2
    assert sxs["totals"]["delta"] == sxs["totals"]["v33_lock"] - sxs["totals"]["v32_lock"]


# ---------------------------------------------------------------------------
# render smoke (the new blocks appear; the CLI does not crash)
# ---------------------------------------------------------------------------
def test_full_report_has_all_blocks(capsys, tmp_path):
    from service.v33.ledger import append_v33_ledger_row
    from service.v32.ledger import append_v32_ledger_row
    v33_ledger = tmp_path / "v33.jsonl"
    v32_ledger = tmp_path / "v32.jsonl"
    row = _sweep_row("2026-09-24T04:00:00Z")
    row["deep_obs"] = {"margins_c": list(range(16, 26)), "reached_count": 2,
                       "rungs": [{"margin_c": 16, "reached": True, "first": {"lock_solved": "0.16"},
                                  "absorption_lots": "12", "prints_through": 3}]}
    append_v33_ledger_row(row, str(v33_ledger))
    append_v32_ledger_row(_v32_row("2026-09-24T04:00:00Z"), str(v32_ledger))
    rc = main(["--ledger", str(v33_ledger), "--v32-ledger", str(v32_ledger)])
    out = capsys.readouterr().out
    assert rc == 0
    for block in ("LADDER SCOREBOARD", "FALSIFIER GATE TABLE", "DEEP END (SO-3", "SIDE-BY-SIDE",
                  "capture ratio @ 10c", "REALISED", "DRY (dry_sim"):
        assert block in out, block


def test_build_v33_report_includes_new_sections():
    rep = build_v33_report([_sweep_row("2026-09-24T04:00:00Z")])
    assert "scoreboard" in rep and "gate_table" in rep and "deep_end" in rep
    assert rep["gate_table"]["verdict"].startswith("n<30 pending")


# ---------------------------------------------------------------------------
# gate boundaries + finer gate behaviour
# ---------------------------------------------------------------------------
def test_gate_mean_lock_boundary_pass_and_fail():
    # mean exactly +6.0c at n>=30 -> PASS; +5c -> FAIL (mean-lock gate is >= not >)
    at6 = [_sweep_row(f"2026-09-24T{h:02d}:00:00Z", realised_c=6, solved_c=8) for h in range(3)]
    gt6 = build_falsifier_gate_table(at6)
    mg = next(g for g in gt6["gates"] if g["gate"] == "mean true lock")
    assert mg["status"] == "PASS" and gt6["verdict"] == "ALIVE-so-far"
    at5 = [_sweep_row(f"2026-09-24T{h:02d}:00:00Z", realised_c=5, solved_c=6) for h in range(3)]
    gt5 = build_falsifier_gate_table(at5)
    mg5 = next(g for g in gt5["gates"] if g["gate"] == "mean true lock")
    assert mg5["status"] == "FAIL" and gt5["verdict"].startswith("KILL")


def test_shortfall_gate_ignores_rungs_with_few_fills():
    # a single sweep row: each margin has exactly ONE fill (< 3), so the shortfall gate is n-too-small
    # even though every rung's shortfall is 2c -- the gate only judges rungs with >= 3 fills.
    gt = build_falsifier_gate_table([_sweep_row("2026-09-24T04:00:00Z", realised_c=8, solved_c=10)])
    sf = next(g for g in gt["gates"] if g["gate"].startswith("per-rung shortfall"))
    assert sf["status"] == "n-too-small"


def test_pct_positive_fails_when_some_negative():
    # 33 fills at realised -3c -> %positive 0 and mean -3c: KILL (mean < +2 at n>=15 fires first)
    rows = [_sweep_row(f"2026-09-24T{h:02d}:00:00Z", realised_c=-3, solved_c=1) for h in range(3)]
    gt = build_falsifier_gate_table(rows)
    assert gt["pct_positive"] == Decimal(0)
    assert gt["verdict"].startswith("KILL")


def test_one_legged_counts_contracts_across_batches():
    r = _sweep_row("2026-09-24T04:00:00Z")
    r["wing_batch_sets"] = [{"index": 0, "fill_count": 2, "one_legged": True, "completed": False},
                            {"index": 1, "fill_count": 1, "one_legged": True, "completed": False}]
    gt = build_falsifier_gate_table([r])
    assert gt["one_legged"] == 3   # 2 + 1 across both one-legged batches


def test_scoreboard_realised_per_day():
    rows = [_sweep_row("2026-09-24T04:00:00Z"), _sweep_row("2026-09-25T04:00:00Z")]
    sb = build_ladder_scoreboard(rows)
    per_day = sb["realised"]["per_day"]
    assert [d["day"] for d in per_day] == ["2026-09-24", "2026-09-25"]
    assert all(d["contracts"] == 11 for d in per_day)
