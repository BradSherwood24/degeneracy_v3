"""Journal — in-memory event buffer for one window, flushed to JSONL at window close.

Brad's ruling (PLAN F13-as-modified): NO disk I/O in the hot path. Every raw WS envelope, order
intent/response, imbalance event, and alarm is appended to an in-memory buffer during the window;
`flush(path)` writes the whole buffer to JSONL once, at window close (and on clean shutdown /
Ctrl+C). ACCEPTED, DOCUMENTED COST: a crash mid-window loses that window's replay record — never a
position (exchange truth via the Reconciler is authoritative), but that window cannot be diagnosed
offline.

Each record carries a monotonically-increasing local `idx` (assigned on append, gap-free, starting
at 0) so replay and the paired report can order events deterministically regardless of wall-clock
ties. `local_ts` is the caller-supplied receive time (the WS recorder tap stamps it from the
injected clock) — kept separate from `idx` because two frames can share a wall-clock reading but
never an index.

Serialization is deterministic: `json.dumps(sort_keys=True)`, one record per line, in idx order.
Decimals are not expected in raw WS envelopes (those are wire JSON); any Decimal that reaches the
journal (e.g. a derived value in an alarm payload) is serialized via a `default` that renders it as
a string, so a round-trip is lossless and never silently becomes a float.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from decimal import Decimal
from typing import Any


def _json_default(obj: object) -> str:
    """Deterministic, lossless fallback for non-JSON-native values (Decimal -> its string)."""
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


class Journal:
    """In-memory append-only buffer. Not thread-safe by construction — the window's WS loop and the
    (future) executor run on separate threads, so if both ever append concurrently the caller must
    serialize; Phase 1 appends only from the single WS/asyncio thread."""

    def __init__(self) -> None:
        self._records: list[dict[str, Any]] = []

    def append(self, kind: str, obj: Any, local_ts: float) -> int:
        """Buffer one record and return its assigned index.

        `kind` is the record class ("kalshi_ws", "order_intent", "order_response",
        "imbalance", "alarm", "window_meta", ...). `obj` is the payload (must be
        JSON-serializable, Decimals allowed). `local_ts` is the caller's receive/emit time.
        """
        idx = len(self._records)
        self._records.append({"idx": idx, "kind": kind, "local_ts": local_ts, "obj": obj})
        return idx

    def __len__(self) -> int:
        return len(self._records)

    def iter_records(self) -> Iterator[dict[str, Any]]:
        """Yield buffered records in idx order (the replay reader path)."""
        yield from self._records

    def records(self) -> list[dict[str, Any]]:
        """A shallow copy of the buffered records in idx order."""
        return list(self._records)

    def flush(self, path: str) -> int:
        """Write the whole buffer to `path` as JSONL (one record per line, idx order) and return
        the record count written. Creates parent directories if needed. Writes atomically-ish via a
        temp file + os.replace so a partial write can never leave a truncated journal in place."""
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for rec in self._records:
                f.write(json.dumps(rec, sort_keys=True, default=_json_default))
                f.write("\n")
        os.replace(tmp, path)
        return len(self._records)


def load_journal(path: str) -> Journal:
    """Read a flushed JSONL journal back into a Journal (replay entry point).

    Re-appends each record's payload under a fresh, gap-free index. Assumes the file was written by
    `Journal.flush` (records already in idx order); it does not trust the stored idx blindly — it
    re-derives the index from position so a hand-edited or concatenated file still yields a
    contiguous 0..n-1 sequence.
    """
    j = Journal()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            j.append(rec["kind"], rec["obj"], rec.get("local_ts", 0.0))
    return j
