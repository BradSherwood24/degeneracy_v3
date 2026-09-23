"""V3.3 report (L2): the per-window LADDER block, the roll-integrity ratio, and the SIDE-BY-SIDE block
vs the V3.2 ledger (Brad's watch-and-compare). Pure over synthetic rows; no network, no file needed."""

from __future__ import annotations

from decimal import Decimal

from service.v33.report import build_side_by_side, build_v33_report, main


def _v33_row(ct, rungs=11, contracts=11, lock="0.7790", rolls=4, single=4, dry_sim=True):
    return {
        "roster": "DegeneracyV3_3", "close_time": ct, "mode": "dry", "effective_mode": "dry",
        "dry_sim": dry_sim, "spot_bucket_ticker": "KXBTC-X",
        "synth_counts": {"dry_sim_fill": rungs},
        "ladder": {"rungs_filled": rungs, "contracts": contracts, "shallowest_margin_c": "5.00",
                   "deepest_margin_c": "15.00", "ladder_lock": lock, "roll_count": rolls,
                   "roll_single_order_count": single,
                   "roll_single_order_ratio": (str(Decimal(single) / Decimal(rolls)) if rolls else None)},
    }


def _v32_row(ct, lock="0.11", contracts=2):
    return {
        "roster": "DegeneracyV3_2", "close_time": ct, "mode": "armed", "effective_mode": "armed",
        "lots_filled": contracts,
        "wing_batch_sets": [{"index": 0, "fill_count": contracts, "realized_lock": lock,
                             "completed": True}],
    }


def test_report_totals_fold():
    rows = [_v33_row("2026-09-20T04:00:00Z"), _v33_row("2026-09-20T05:00:00Z", rungs=3, contracts=3,
                                                        lock="0.20", rolls=2, single=2)]
    rep = build_v33_report(rows)
    t = rep["totals"]
    assert t["windows"] == 2 and t["rungs_filled"] == 14 and t["contracts"] == 14
    assert t["ladder_lock"] == Decimal("0.7790") + Decimal("0.20")
    assert t["dry_sim_fills"] == 14
    # roll integrity: 6 single of 6 rolls -> 1.0.
    assert t["roll_single_order_ratio"] == Decimal(1)


def test_report_skips_backfill_rows():
    rows = [_v33_row("2026-09-20T04:00:00Z"),
            {"mode": "backfill", "close_time": "2026-09-20T04:00:00Z", "backfill_of": "x"}]
    rep = build_v33_report(rows)
    assert rep["totals"]["windows"] == 1 and len(rep["windows"]) == 1


def test_side_by_side_matches_hours_and_totals():
    ct = "2026-09-20T04:00:00Z"
    v33 = [_v33_row(ct, lock="0.7790")]
    v32 = [_v32_row(ct, lock="0.11", contracts=2)]   # v32 total set lock = 0.11 * 2 = 0.22
    sxs = build_side_by_side(v33, v32)
    assert len(sxs["rows"]) == 1
    row = sxs["rows"][0]
    assert row["v33_lock"] == Decimal("0.7790") and row["v32_lock"] == Decimal("0.22")
    assert row["v33_dry_sim"] is True and row["v33_rungs"] == 11
    t = sxs["totals"]
    assert t["v32_lock"] == Decimal("0.22") and t["v33_lock"] == Decimal("0.7790")
    assert t["delta"] == Decimal("0.7790") - Decimal("0.22")


def test_side_by_side_only_matched_hours():
    v33 = [_v33_row("2026-09-20T04:00:00Z"), _v33_row("2026-09-20T05:00:00Z")]
    v32 = [_v32_row("2026-09-20T04:00:00Z")]   # only 04:00 matches
    sxs = build_side_by_side(v33, v32)
    assert sxs["totals"]["windows"] == 1 and sxs["rows"][0]["close_time"] == "2026-09-20T04:00:00Z"


def test_report_render_table_has_ladder_and_side_by_side(capsys, tmp_path):
    from service.v33.ledger import append_v33_ledger_row
    from service.v32.ledger import append_v32_ledger_row
    v33_ledger = tmp_path / "v33.jsonl"
    v32_ledger = tmp_path / "v32.jsonl"
    append_v33_ledger_row(_v33_row("2026-09-20T04:00:00Z"), str(v33_ledger))
    append_v32_ledger_row(_v32_row("2026-09-20T04:00:00Z"), str(v32_ledger))
    rc = main(["--ledger", str(v33_ledger), "--v32-ledger", str(v32_ledger)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "SIDE-BY-SIDE" in out and "ladderLk" in out and "roll-integrity" in out


def test_report_cli_json(capsys, tmp_path):
    import json
    from service.v33.ledger import append_v33_ledger_row
    from service.v32.ledger import append_v32_ledger_row
    v33_ledger = tmp_path / "v33.jsonl"
    v32_ledger = tmp_path / "v32.jsonl"
    append_v33_ledger_row(_v33_row("2026-09-20T04:00:00Z"), str(v33_ledger))
    append_v32_ledger_row(_v32_row("2026-09-20T04:00:00Z"), str(v32_ledger))
    rc = main(["--ledger", str(v33_ledger), "--v32-ledger", str(v32_ledger), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["side_by_side"]["totals"]["windows"] == 1
    assert out["report"]["totals"]["rungs_filled"] == 11
