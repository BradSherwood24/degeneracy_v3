"""Tests for tools/daily_replay.py (the daily incremental replay + falsifier tripwire) and the lab's
new --only / --files-from subset option. All offline, no engine run (the lab subprocess is never
invoked here -- the heavy path is exercised only via pure helpers / monkeypatch)."""

from __future__ import annotations

import gzip
import json
import os
import sys
from datetime import date

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_REPO_ROOT, os.path.join(_REPO_ROOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import daily_replay as dr  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _write_gz(path: str, text: str = "x\n") -> None:
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(text)


def _fake_journal_dir(tmp_path, names):
    d = tmp_path / "journals"
    d.mkdir()
    for name, text in names.items():
        _write_gz(str(d / name), text)
    return str(d)


def _fill(close, lock_c, *, model="lag", E="0.10", tol="0.02", deb=5000, trade_ts=1.0,
          n="0.62", W="1.28"):
    return {"close": close, "model": model, "E": E, "tol": tol, "deb": deb,
            "trade_ts": trade_ts, "n": n, "W_completion": W, "lock_c": lock_c}


# ---------------------------------------------------------------------------
# filename <-> close
# ---------------------------------------------------------------------------
def test_close_iso_from_filename():
    assert dr.close_iso_from_filename("20260914T170000Z.jsonl.gz") == "2026-09-14T17:00:00Z"
    assert dr.close_iso_from_filename("20260914T170000Z.jsonl") == "2026-09-14T17:00:00Z"
    assert dr.close_iso_from_filename("not_a_journal.jsonl.gz") is None
    assert dr.close_iso_from_filename("2026091T170000Z.jsonl.gz") is None  # too short


# ---------------------------------------------------------------------------
# manifest incremental selection + max-windows guard
# ---------------------------------------------------------------------------
def test_select_new_incremental_and_size_change(tmp_path):
    jdir = _fake_journal_dir(tmp_path, {
        "20260925T170000Z.jsonl.gz": "a\n",
        "20260925T180000Z.jsonl.gz": "bb\n",
        "20260925T190000Z.jsonl.gz": "ccc\n",
    })
    manifest = {"files": {}}
    to_run, deferred = dr.select_new(jdir, manifest, max_windows=60)
    assert to_run == sorted(os.listdir(jdir))  # all new, oldest first
    assert deferred == 0

    # mark the first two done -> only the third is new
    dr.mark_done(manifest, jdir, to_run[:2])
    to_run2, _ = dr.select_new(jdir, manifest, max_windows=60)
    assert to_run2 == ["20260925T190000Z.jsonl.gz"]

    # a size change re-selects a "done" file (re-rotated / grew)
    _write_gz(os.path.join(jdir, "20260925T170000Z.jsonl.gz"), "a-longer-content\n")
    to_run3, _ = dr.select_new(jdir, manifest, max_windows=60)
    assert "20260925T170000Z.jsonl.gz" in to_run3

    # ignores the live .jsonl (only *.jsonl.gz discovered)
    open(os.path.join(jdir, "20260925T200000Z.jsonl"), "w").close()
    assert "20260925T200000Z.jsonl" not in dr.list_gz_journals(jdir)


def test_max_windows_guard(tmp_path):
    jdir = _fake_journal_dir(tmp_path, {f"2026092{i}T170000Z.jsonl.gz": "x\n" for i in range(1, 6)})
    to_run, deferred = dr.select_new(jdir, {"files": {}}, max_windows=2)
    assert len(to_run) == 2
    assert deferred == 3
    assert to_run == sorted(os.listdir(jdir))[:2]  # oldest two first


def test_full_ignores_manifest(tmp_path):
    jdir = _fake_journal_dir(tmp_path, {"20260925T170000Z.jsonl.gz": "x\n"})
    manifest = {"files": {}}
    dr.mark_done(manifest, jdir, ["20260925T170000Z.jsonl.gz"])
    assert dr.select_new(jdir, manifest, 60, full=False)[0] == []
    assert dr.select_new(jdir, manifest, 60, full=True)[0] == ["20260925T170000Z.jsonl.gz"]


# ---------------------------------------------------------------------------
# merge / dedupe
# ---------------------------------------------------------------------------
def test_merge_fills_dedupe(tmp_path):
    dst = str(tmp_path / "fills_all.jsonl")
    src1 = str(tmp_path / "s1.jsonl")
    src2 = str(tmp_path / "s2.jsonl")
    dr.write_jsonl(src1, [_fill("2026-09-25T17:00:00Z", 5.0, trade_ts=1.0),
                          _fill("2026-09-25T17:00:00Z", 6.0, trade_ts=2.0)])
    # src2 repeats the trade_ts=1.0 fill (dup) and adds one new
    dr.write_jsonl(src2, [_fill("2026-09-25T17:00:00Z", 5.0, trade_ts=1.0),
                          _fill("2026-09-25T18:00:00Z", 7.0, trade_ts=3.0)])
    total, added, dup = dr.merge_fills(dst, src1)
    assert (total, added, dup) == (2, 2, 0)
    total, added, dup = dr.merge_fills(dst, src2)
    assert added == 1 and dup == 1 and total == 3
    # a full re-merge of the same file is entirely dup
    _, added2, dup2 = dr.merge_fills(dst, src2)
    assert added2 == 0 and dup2 == 2


def test_merge_per_window_dedupe(tmp_path):
    dst = str(tmp_path / "pw_all.jsonl")
    src = str(tmp_path / "pw.jsonl")
    dr.write_jsonl(src, [{"close_time": "2026-09-25T17:00:00Z", "total_frames": 10},
                         {"close_time": "2026-09-25T18:00:00Z", "total_frames": 20}])
    total, added, dup = dr.merge_per_window(dst, src)
    assert (total, added, dup) == (2, 2, 0)
    # re-merge -> all dup, none added
    _, added2, dup2 = dr.merge_per_window(dst, src)
    assert added2 == 0 and dup2 == 2 and dr.read_jsonl(dst).__len__() == 2


# ---------------------------------------------------------------------------
# tripwire math: OK / WATCH / TRIP
# ---------------------------------------------------------------------------
def test_tripwire_ok_healthy_regime():
    # 20 healthy fills over 7 days, mean well above +2c -> OK
    fills = []
    for i in range(20):
        d = f"2026-09-{21 + (i % 7):02d}T17:00:00Z"
        fills.append(_fill(d, 10.0, trade_ts=float(i)))
    tw = dr.compute_tripwire(fills, as_of=date(2026, 9, 27))
    assert tw["verdict"] == "OK"
    assert tw["trailing_7d"]["n"] == 20
    assert tw["trailing_7d"]["mean_c"] == 10.0
    assert dr._VERDICT_EXIT[tw["verdict"]] == 0


def test_tripwire_watch_3day_only():
    # 8 negative fills all within the last 3 days -> 3-day n=8 mean<2 (WATCH), 7-day n=8 (<15, not TRIP)
    fills = []
    for i in range(8):
        d = f"2026-09-{25 + (i % 3):02d}T17:00:00Z"  # 25,26,27
        fills.append(_fill(d, -3.0, trade_ts=float(i)))
    tw = dr.compute_tripwire(fills, as_of=date(2026, 9, 27))
    assert tw["verdict"] == "WATCH"
    assert tw["trailing_3d"]["n"] == 8
    assert tw["trailing_7d"]["n"] == 8
    assert dr._VERDICT_EXIT[tw["verdict"]] == 2


def test_tripwire_trip_7day_kill():
    # 21 fills over 7 days, negative mean -> 7-day n=21>=15 and mean<2 -> TRIP
    fills = []
    for i in range(21):
        d = f"2026-09-{21 + (i % 7):02d}T17:00:00Z"
        fills.append(_fill(d, -3.0, trade_ts=float(i)))
    tw = dr.compute_tripwire(fills, as_of=date(2026, 9, 27))
    assert tw["verdict"] == "TRIP"
    assert tw["trailing_7d"]["n"] == 21
    assert tw["trailing_7d"]["mean_c"] == -3.0
    assert dr._VERDICT_EXIT[tw["verdict"]] == 3
    # per-day rows cover exactly the last 7 calendar days
    assert [d["date"] for d in tw["per_day_last7"]] == [f"2026-09-{d:02d}" for d in range(21, 28)]


def test_tripwire_ignores_non_base_cells_and_computes_drift():
    # non-base cells must not count; wing drift = (W - (2 - E - n)) * 100
    fills = [
        _fill("2026-09-27T17:00:00Z", -3.0, n="0.62", W="1.28", trade_ts=1.0),          # base
        _fill("2026-09-27T17:00:00Z", -9.0, model="ideal", E="0.10", trade_ts=2.0),     # ignored
        _fill("2026-09-27T17:00:00Z", -9.0, model="lag", E="0.08", trade_ts=3.0),       # ignored (E)
    ]
    tw = dr.compute_tripwire(fills, as_of=date(2026, 9, 27))
    assert tw["trailing_3d"]["n"] == 1
    # drift = (1.28 - (2 - 0.10 - 0.62)) * 100 = (1.28 - 1.28) * 100 = 0.0
    assert abs(tw["trailing_3d"]["wing_drift_p90_c"]) < 1e-9


def test_percentile():
    assert dr._percentile([], 0.9) is None
    assert dr._percentile([5.0], 0.9) == 5.0
    assert dr._percentile([0.0, 10.0], 0.5) == 5.0
    assert dr._percentile([0.0, 5.0, 10.0], 0.90) == 9.0


# ---------------------------------------------------------------------------
# seed import
# ---------------------------------------------------------------------------
def test_seed_import(tmp_path, monkeypatch):
    fills_all = str(tmp_path / "fills_all.jsonl")
    pw_all = str(tmp_path / "per_window_all.jsonl")
    manifest_path = str(tmp_path / "manifest.json")
    monkeypatch.setattr(dr, "FILLS_ALL", fills_all)
    monkeypatch.setattr(dr, "PER_WINDOW_ALL", pw_all)
    monkeypatch.setattr(dr, "MANIFEST_PATH", manifest_path)

    seed = tmp_path / "seed"
    seed.mkdir()
    dr.write_jsonl(str(seed / "fills.jsonl"),
                   [_fill("2026-09-14T17:00:00Z", 10.0, trade_ts=1.0),
                    _fill("2026-09-14T18:00:00Z", 9.0, trade_ts=2.0)])
    dr.write_jsonl(str(seed / "per_window.jsonl"),
                   [{"close_time": "2026-09-14T17:00:00Z"},
                    {"close_time": "2026-09-14T18:00:00Z"}])

    # journals dir: two match seeded closes, one does not
    jdir = _fake_journal_dir(tmp_path, {
        "20260914T170000Z.jsonl.gz": "a\n",     # seeded
        "20260914T180000Z.jsonl.gz": "b\n",     # seeded
        "20260915T170000Z.jsonl.gz": "c\n",     # NEW (not in seed per_window)
    })

    summary = dr.seed_from(str(seed), jdir)
    assert summary["fills_added"] == 2
    assert summary["per_window_added"] == 2
    assert summary["journals_marked_done"] == 2

    manifest = dr.load_manifest(manifest_path)
    assert set(manifest["files"]) == {"20260914T170000Z.jsonl.gz", "20260914T180000Z.jsonl.gz"}
    # after seeding, the first scheduled run picks up only the NEW window
    to_run, _ = dr.select_new(jdir, manifest, 60)
    assert to_run == ["20260915T170000Z.jsonl.gz"]

    # re-seeding is idempotent (all dup)
    summary2 = dr.seed_from(str(seed), jdir)
    assert summary2["fills_added"] == 0 and summary2["per_window_added"] == 0


# ---------------------------------------------------------------------------
# main() smoke: dry-run and the no-new-windows path (no lab invoked)
# ---------------------------------------------------------------------------
def test_main_dry_run_lists_without_running(tmp_path, monkeypatch, capsys):
    jdir = _fake_journal_dir(tmp_path, {"20260925T170000Z.jsonl.gz": "x\n"})
    monkeypatch.setattr(dr, "MANIFEST_PATH", str(tmp_path / "manifest.json"))
    # ensure the lab is never called during a dry run
    monkeypatch.setattr(dr, "run_lab", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no run")))
    rc = dr.main(["--journals", jdir, "--dry-run"])
    assert rc == 0
    assert "DRY RUN" in capsys.readouterr().out


def test_main_no_new_windows_refreshes_tripwire(tmp_path, monkeypatch):
    jdir = str(tmp_path / "empty_journals")
    os.makedirs(jdir)
    fills_all = str(tmp_path / "fills_all.jsonl")
    dr.write_jsonl(fills_all, [_fill("2026-09-27T17:00:00Z", 10.0, trade_ts=1.0)])
    monkeypatch.setattr(dr, "MANIFEST_PATH", str(tmp_path / "manifest.json"))
    monkeypatch.setattr(dr, "FILLS_ALL", fills_all)
    monkeypatch.setattr(dr, "TRIPWIRE_TXT", str(tmp_path / "tw.txt"))
    monkeypatch.setattr(dr, "TRIPWIRE_JSON", str(tmp_path / "tw.json"))
    monkeypatch.setattr(dr, "DAILY_LOG", str(tmp_path / "log.txt"))
    monkeypatch.setattr(dr, "OPS_DIR", str(tmp_path))
    monkeypatch.setattr(dr, "run_lab", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no run")))
    rc = dr.main(["--journals", jdir, "--as-of", "2026-09-27"])
    assert rc == 0  # OK verdict
    assert os.path.exists(str(tmp_path / "tw.txt"))
    tw = json.load(open(str(tmp_path / "tw.json")))
    assert tw["verdict"] == "OK" and tw["trailing_7d"]["n"] == 1


# ---------------------------------------------------------------------------
# lab --only / --files-from option (pure helpers; no engine)
# ---------------------------------------------------------------------------
def test_lab_wanted_basenames_and_filter(tmp_path):
    from sim.v32_replay import lab

    assert lab._wanted_basenames(None, None) is None
    assert lab._wanted_basenames("a.jsonl.gz, b.jsonl.gz ", None) == {"a.jsonl.gz", "b.jsonl.gz"}

    ff = tmp_path / "list.txt"
    ff.write_text("# comment\n\nc.jsonl.gz\n/some/path/d.jsonl.gz\n", encoding="utf-8")
    assert lab._wanted_basenames(None, str(ff)) == {"c.jsonl.gz", "d.jsonl.gz"}
    # --only and --files-from combine
    assert lab._wanted_basenames("a.jsonl.gz", str(ff)) == {"a.jsonl.gz", "c.jsonl.gz", "d.jsonl.gz"}

    paths = ["/j/20260925T170000Z.jsonl.gz", "/j/20260925T180000Z.jsonl.gz",
             "/j/20260925T190000Z.jsonl.gz"]
    sel, missing = lab._filter_paths(paths, {"20260925T180000Z.jsonl.gz", "nope.jsonl.gz"})
    assert sel == ["/j/20260925T180000Z.jsonl.gz"]
    assert missing == ["nope.jsonl.gz"]
    # None -> keep all, no missing
    assert lab._filter_paths(paths, None) == (paths, [])
