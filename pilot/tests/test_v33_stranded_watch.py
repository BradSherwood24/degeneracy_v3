"""Tests for service/v33/stranded_watch.py -- the observational adverse-repricing watch (registered
2026-10-05 00:45Z). Pure, offline, synthetic ledger rows."""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from decimal import Decimal

_PILOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PILOT not in sys.path:
    sys.path.insert(0, _PILOT)

from service.v33 import stranded_watch as sw  # noqa: E402


def _fill(rung: int, lock: str | None, count: str | int = 1) -> dict:
    return {"rung": rung, "realized_lock": lock, "count": count, "price": "0.40", "n_top": "0.22"}


def _row(close: str, fills: list[dict], mode: str = "armed", dry_sim: bool = False,
         delta: str | None = "0.1") -> dict:
    return {"close_time": close, "effective_mode": mode, "dry_sim": dry_sim, "rung_fills": fills,
            "lots_filled": len(fills), "realized_delta": delta, "spot_bucket_ticker": "KXBTC-X-B1",
            "one_legged_contracts": 0}


# the 10-05 00:00Z shape: one lot split 0.06 + 0.94 at rung -19, one lot at rung -17, all negative
TONIGHT = _row("2026-10-05T00:00:00Z",
               [_fill(-19, "-0.0973", "0.06"), _fill(-19, "-0.0973", "0.94"), _fill(-17, "-0.0871")],
               delta="-0.1506")
# a stranded-but-positive window (W moved the other way)
POSITIVE_STRANDED = _row("2026-10-04T14:00:00Z",
                         [_fill(-2, "0.0622"), _fill(-1, "0.0725")] + [_fill(k, "0.12") for k in range(9)],
                         delta="1.5797")
# in-ladder only
LADDER_ONLY = _row("2026-10-04T20:00:00Z", [_fill(k, "0.13") for k in range(11)], delta="1.6421")


def test_classify_tonight_is_adverse_with_fractional_counts():
    c = sw.classify_window(TONIGHT)
    assert c is not None
    assert c["stranded"] == 3 and c["ladder"] == 0 and c["all_stranded"] is True
    assert c["depth_c"] == 19
    # 0.06*-0.0973 + 0.94*-0.0973 + -0.0871 = -0.1844
    assert Decimal(c["stranded_lock"]) == Decimal("-0.1844")
    assert c["adverse"] is True


def test_classify_positive_stranded_is_not_adverse():
    c = sw.classify_window(POSITIVE_STRANDED)
    assert c["stranded"] == 2 and c["ladder"] == 9
    assert Decimal(c["stranded_lock"]) == Decimal("0.1347")
    assert c["adverse"] is False and c["all_stranded"] is False


def test_classify_ladder_only_and_no_fills():
    c = sw.classify_window(LADDER_ONLY)
    assert c["stranded"] == 0 and c["adverse"] is False and c["depth_c"] == 0
    assert sw.classify_window(_row("2026-10-04T07:00:00Z", [])) is None


def test_missing_lock_counted_not_summed():
    c = sw.classify_window(_row("2026-10-02T02:00:00Z", [_fill(-2, None), _fill(-1, None)]))
    assert c["lock_missing"] == 2 and Decimal(c["stranded_lock"]) == 0 and c["adverse"] is False


def test_build_watch_filters_dry_and_sim_rows_and_counts_adverse():
    rows = [TONIGHT, POSITIVE_STRANDED, LADDER_ONLY,
            _row("2026-10-04T10:00:00Z", [_fill(-9, "-0.2")], mode="dry"),
            _row("2026-10-04T11:00:00Z", [_fill(-9, "-0.2")], mode="armed", dry_sim=True),
            _row("2026-10-04T07:00:00Z", [])]                      # armed, no fills
    w = sw.build_watch(rows, as_of=date(2026, 10, 5))
    assert w["armed_windows"] == 4            # 3 with fills + the empty armed one
    assert w["windows_with_fills"] == 3
    assert w["fills"] == 25 and w["stranded"] == 5
    assert w["adverse_windows"] == ["2026-10-05T00:00:00Z"]
    assert w["verdict"].startswith("WATCH: 1 adverse")
    assert Decimal(w["stranded_lock"]) == Decimal("-0.1844") + Decimal("0.1347")
    # sorted by close_time
    assert [x["close_time"] for x in w["windows"]] == sorted(x["close_time"] for x in w["windows"])


def test_build_watch_days_window():
    rows = [TONIGHT, POSITIVE_STRANDED, LADDER_ONLY]
    w = sw.build_watch(rows, days=1, as_of=date(2026, 10, 5))
    assert [x["close_time"] for x in w["windows"]] == ["2026-10-05T00:00:00Z"]
    w2 = sw.build_watch(rows, days=2, as_of=date(2026, 10, 5))
    assert w2["windows_with_fills"] == 3
    w3 = sw.build_watch([LADDER_ONLY], as_of=date(2026, 10, 5))
    assert w3["verdict"].startswith("OK:") and w3["adverse_windows"] == []


def test_render_and_write(tmp_path):
    w = sw.build_watch([TONIGHT, LADDER_ONLY], as_of=date(2026, 10, 5))
    txt = sw.render_txt(w)
    assert "WATCH: 1 adverse-repricing window(s)" in txt
    assert "2026-10-05T00:00:00Z" in txt and "ADVERSE" in txt
    assert "only REPORTS" in txt
    t, j = sw.write_outputs(w, str(tmp_path))
    assert os.path.exists(t) and os.path.exists(j)
    with open(j, encoding="utf-8") as f:
        back = json.load(f)
    assert back["adverse_windows"] == ["2026-10-05T00:00:00Z"]


def test_main_reads_ledger_and_writes(tmp_path, capsys):
    ledger = tmp_path / "v33_ledger.jsonl"
    with open(ledger, "w", encoding="utf-8") as f:
        for r in (TONIGHT, LADDER_ONLY):
            f.write(json.dumps(r) + "\n")
        f.write("not json\n")
    ops = tmp_path / "ops"
    rc = sw.main(["--ledger", str(ledger), "--ops-dir", str(ops), "--write", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["windows_with_fills"] == 2 and out["adverse_windows"] == ["2026-10-05T00:00:00Z"]
    assert (ops / sw.WATCH_TXT_NAME).exists() and (ops / sw.WATCH_JSON_NAME).exists()
    rc2 = sw.main(["--ledger", str(tmp_path / "missing.jsonl")])
    assert rc2 == 0 and "OK: no adverse" in capsys.readouterr().out
