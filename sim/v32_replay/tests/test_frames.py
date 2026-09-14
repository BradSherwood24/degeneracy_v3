"""Journal reading + sealed-date refusal (no network; the existing head fixture only)."""

from __future__ import annotations

import gzip
import json
import os

import pytest

from conftest import HEAD_FIXTURE
from sim.v32_replay.frames import (
    FRAME_DELTA, FRAME_SNAPSHOT, FRAME_TRADE, SealedDateRefusal, assert_not_sealed,
    discover_journals, iter_frames, read_window_meta,
)


def test_read_window_meta():
    meta = read_window_meta(HEAD_FIXTURE)
    assert meta.close_time == "2026-09-14T17:00:00Z"
    assert meta.close_epoch == 1789405200   # epoch of 2026-09-14T17:00:00Z
    assert len(meta.bucket_map) >= 180
    assert meta.range_series == "KXBTC"
    assert meta.strike_series == "KXBTCD"
    # every bucket floor/cap is a float pair
    for tk, (fl, cap) in meta.bucket_map.items():
        assert isinstance(fl, float) and isinstance(cap, float)


def test_iter_frames_shapes():
    kinds = {FRAME_SNAPSHOT: 0, FRAME_DELTA: 0, FRAME_TRADE: 0}
    n = 0
    for fr in iter_frames(HEAD_FIXTURE):
        n += 1
        assert fr.market
        kinds[fr.kind] = kinds.get(fr.kind, 0) + 1
        if fr.kind == FRAME_SNAPSHOT:
            assert fr.server_ts is None       # snapshots carry no ts (fold-only)
        else:
            assert fr.server_ts is not None
    assert n > 9000
    assert kinds[FRAME_DELTA] > kinds[FRAME_SNAPSHOT]
    assert kinds[FRAME_TRADE] > 0


def test_sealed_refusal():
    assert_not_sealed("2026-09-14T17:00:00Z")            # not sealed -> no raise
    with pytest.raises(SealedDateRefusal):
        assert_not_sealed("2026-08-10T17:00:00Z")        # SEAL range
    with pytest.raises(SealedDateRefusal):
        assert_not_sealed("2026-08-25T17:00:00Z")        # range holdout
    # acknowledge would pass, but the lab never sets it
    assert_not_sealed("2026-08-25T17:00:00Z", acknowledge=True)


def test_read_window_meta_refuses_sealed(tmp_path):
    p = os.path.join(tmp_path, "20260825T170000Z.jsonl.gz")
    with gzip.open(p, "wt", encoding="utf-8") as f:
        f.write(json.dumps({"idx": 0, "kind": "window_meta", "local_ts": 0.0,
                            "obj": {"close_time": "2026-08-25T17:00:00Z", "buckets": []}}) + "\n")
    with pytest.raises(SealedDateRefusal):
        read_window_meta(p)


def test_discover_journals_only_gz_and_since(tmp_path):
    for name in ("20260914T170000Z.jsonl.gz", "20260914T180000Z.jsonl.gz",
                 "20260913T170000Z.jsonl.gz", "20260914T190000Z.jsonl"):  # the .jsonl is live-in-progress
        open(os.path.join(tmp_path, name), "w").close()
    got = [os.path.basename(p) for p in discover_journals(str(tmp_path))]
    assert got == ["20260913T170000Z.jsonl.gz", "20260914T170000Z.jsonl.gz",
                   "20260914T180000Z.jsonl.gz"]
    assert all(g.endswith(".jsonl.gz") for g in got)     # never the live .jsonl
    got2 = [os.path.basename(p) for p in discover_journals(str(tmp_path), since="2026-09-14")]
    assert "20260913T170000Z.jsonl.gz" not in got2
