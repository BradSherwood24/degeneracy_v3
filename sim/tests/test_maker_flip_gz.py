"""maker_flip discovery must be gz-aware post-rotation: find both raw and rotated journals, dedup by
stem (prefer raw), and strip window names regardless of .jsonl / .jsonl.gz. tmp dirs only."""

from __future__ import annotations

import gzip
import os

from replay.maker_flip import _find_journals, _window_name


def _touch(path):
    with open(path, "w", encoding="utf-8") as f:
        f.write("{}\n")


def _touch_gz(path):
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write("{}\n")


def test_window_name_handles_both_suffixes():
    assert _window_name("/j/20260826T040000Z.jsonl") == "20260826T040000Z"
    assert _window_name("/j/20260826T040000Z.jsonl.gz") == "20260826T040000Z"


def test_find_journals_discovers_gz_and_prefers_raw(tmp_path):
    d = str(tmp_path)
    _touch(os.path.join(d, "20260826T040000Z.jsonl"))          # raw only
    _touch_gz(os.path.join(d, "20260826T050000Z.jsonl.gz"))    # rotated only
    _touch(os.path.join(d, "20260826T060000Z.jsonl"))          # both -> raw wins
    _touch_gz(os.path.join(d, "20260826T060000Z.jsonl.gz"))

    got = [os.path.basename(p) for p in _find_journals(d)]
    assert got == [
        "20260826T040000Z.jsonl",
        "20260826T050000Z.jsonl.gz",
        "20260826T060000Z.jsonl",
    ]
