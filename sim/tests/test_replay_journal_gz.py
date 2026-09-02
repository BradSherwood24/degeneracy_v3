"""The sim replayer's journal loader (replay/journal.py) must read a gzipped journal identically
to the raw one — the pilot's :40 wake compresses closed journals to <name>.jsonl.gz (2026-09-02),
and the replayer reads only pilot/journals/. tmp dirs only; no sealed reads."""

from __future__ import annotations

import gzip
import json
import os

from replay.journal import iter_ws, read_window_header

_H = "KXBTCD-26AUG2605-T76499.99"
_L = "KXBTC15M-26AUG260500-00"


def _records(close_time):
    return [
        {"idx": 0, "kind": "window_start", "local_ts": 1.0, "obj": {"close_time": close_time}},
        {"idx": 1, "kind": "quintile", "local_ts": 2.0,
         "obj": {"ok": True, "reason": None, "high_ticker": _H, "low_ticker": _L}},
        {"idx": 2, "kind": "kalshi_ws", "local_ts": 3.0, "obj": {"type": "trade", "px": 1}},
        {"idx": 3, "kind": "kalshi_ws", "local_ts": 4.0, "obj": {"type": "book", "px": 2}},
    ]


def _write_raw(path, recs):
    with open(path, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


def _write_gz(path, recs):
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


def test_iter_ws_and_header_gz_matches_raw(tmp_path):
    recs = _records("2026-08-26T05:00:00Z")
    raw = os.path.join(str(tmp_path), "20260826T050000Z.jsonl")
    gz = os.path.join(str(tmp_path), "20260826T050000Z.jsonl.gz")
    _write_raw(raw, recs)
    _write_gz(gz, recs)

    ws_raw = list(iter_ws(raw))
    ws_gz = list(iter_ws(gz))
    assert ws_gz == ws_raw
    assert [o["type"] for o in ws_gz] == ["trade", "book"]

    h_raw = read_window_header(raw)
    h_gz = read_window_header(gz)
    assert h_gz.ok and h_raw.ok
    assert (h_gz.high_ticker, h_gz.low_ticker, h_gz.leg_source) == (_H, _L, "quintile")
    assert (h_gz.high_ticker, h_gz.low_ticker) == (h_raw.high_ticker, h_raw.low_ticker)
    assert h_gz.close_time == h_raw.close_time == "2026-08-26T05:00:00Z"
