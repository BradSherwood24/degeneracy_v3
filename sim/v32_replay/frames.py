"""frames.py — read V3.2 journals and reconstruct the raw frame stream (read-only).

The journal record shape (``service.record_range.StreamJournal``):
    {"idx", "kind", "local_ts", "obj"}
with ``kind == "kalshi_ws"`` carrying ``obj.type`` in {orderbook_snapshot, orderbook_delta, trade}
and ``obj.msg`` the wire payload (BookMirror-shaped for books; trade fields for trades). Record 0 is
``window_meta`` carrying the bucket map (ticker -> floor/cap) and the strike/bucket/15M series.

We read ONLY these raw frames plus ``window_meta`` (ignore our own decision records), exactly as the
task requires: the lab reconstructs everything from the feed, not from what the live core decided.

SEALED / HOLDOUT REFUSAL: a journal whose close date is in a sealed range is refused unless an explicit
one-shot acknowledge is passed. This tool never passes it.
"""

from __future__ import annotations

import glob
import gzip
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator, Mapping

# Forbidden date ranges (house law): the SEAL (latest 17 UTC days) and the range holdout. A journal
# whose UTC close date falls in either is refused unless explicitly acknowledged (never by this tool).
SEALED_RANGES: tuple[tuple[str, str], ...] = (
    ("2026-08-02", "2026-08-18"),   # SEAL.md holdout
    ("2026-08-20", "2026-08-29"),   # range holdout (MEMORY range-holdout)
)


class SealedDateRefusal(Exception):
    """Raised when a journal's close date lies in a sealed/holdout range and was not acknowledged."""


def _parse_server_ts(msg: Mapping) -> float | None:
    """Epoch seconds from a wire payload: ``ts_ms`` (epoch-ms) preferred, else ``ts`` (numeric epoch
    or ISO-8601). Mirrors ``service.ws_client._parse_server_ts`` so the reconstructed clock matches the
    live one. Snapshots carry no ts (they fold the book but never drive a decision, exactly as the live
    recorder skips ts-less frames)."""
    ts_ms = msg.get("ts_ms")
    if ts_ms is not None:
        try:
            return float(ts_ms) / 1000.0
        except (TypeError, ValueError):
            return None
    ts = msg.get("ts")
    if ts is None:
        return None
    try:
        return float(ts)
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return None


def close_epoch_of(close_time: str) -> int:
    """Epoch seconds of an ISO close time (``2026-09-14T17:00:00Z``)."""
    dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def assert_not_sealed(close_time: str, *, acknowledge: bool = False) -> None:
    """Refuse a close date inside any sealed/holdout range unless acknowledged (never by this tool)."""
    day = close_time[:10]
    for lo, hi in SEALED_RANGES:
        if lo <= day <= hi:
            if acknowledge:
                return
            raise SealedDateRefusal(
                f"journal close date {day} is in sealed/holdout range [{lo}, {hi}]; refusing to read "
                f"it (the replay lab never sets the acknowledge flag)"
            )


@dataclass(frozen=True)
class WindowMeta:
    """Provenance from record 0 (``window_meta``)."""

    close_time: str
    close_epoch: int
    bucket_map: dict[str, tuple[float, float]]     # ticker -> (floor, cap)
    strike_series: str
    range_series: str
    m15_series: str
    m15_tickers: tuple[str, ...]
    resolved_mode: str
    effective_mode: str
    params_sha: str | None
    bucket_count: int
    strike_count: int


def _open_text(path: str):
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, "r", encoding="utf-8")


def read_window_meta(path: str, *, acknowledge_sealed: bool = False) -> WindowMeta:
    """Read record 0 (``window_meta``) without streaming the whole file. Refuses sealed dates."""
    with _open_text(path) as f:
        for line in f:
            rec = json.loads(line)
            if rec.get("kind") == "window_meta":
                o = rec["obj"]
                close = o["close_time"]
                assert_not_sealed(close, acknowledge=acknowledge_sealed)
                bmap = {
                    b["ticker"]: (float(b["floor"]), float(b["cap"]))
                    for b in o.get("buckets", [])
                    if b.get("floor") is not None and b.get("cap") is not None
                }
                return WindowMeta(
                    close_time=close,
                    close_epoch=close_epoch_of(close),
                    bucket_map=bmap,
                    strike_series=o.get("strike_series", "KXBTCD"),
                    range_series=o.get("range_series", "KXBTC"),
                    m15_series=o.get("m15_series", "KXBTC15M"),
                    m15_tickers=tuple(o.get("m15_tickers", []) or []),
                    resolved_mode=o.get("resolved_mode", ""),
                    effective_mode=o.get("effective_mode", ""),
                    params_sha=o.get("params_sha"),
                    bucket_count=int(o.get("bucket_count", len(bmap))),
                    strike_count=int(o.get("strike_count", 0)),
                )
            # window_meta is record 0; if the first record isn't it, keep scanning a few lines.
    raise ValueError(f"no window_meta record found in {path}")


# Frame kinds the reconstruction consumes.
FRAME_SNAPSHOT = "orderbook_snapshot"
FRAME_DELTA = "orderbook_delta"
FRAME_TRADE = "trade"


@dataclass
class RawFrame:
    """One reconstructed WS frame: kind (snapshot/delta/trade), market ticker, msg payload, and the
    server ts (None for snapshots, which fold the book but do not drive a decision)."""

    kind: str
    market: str
    msg: dict
    server_ts: float | None


def iter_frames(path: str, *, acknowledge_sealed: bool = False) -> Iterator[RawFrame]:
    """Stream the raw kalshi_ws frames of one journal in record order (= live arrival order).

    ONE record at a time (memory-light). Skips our own decision records and every non-ws record. The
    caller must have validated the close date is not sealed (``read_window_meta`` does so); this
    re-checks the meta line defensively."""
    with _open_text(path) as f:
        for line in f:
            # Cheap prefilter: only kalshi_ws / window_meta records are relevant.
            rec = json.loads(line)
            kind = rec.get("kind")
            if kind == "window_meta":
                assert_not_sealed(rec["obj"]["close_time"], acknowledge=acknowledge_sealed)
                continue
            if kind != "kalshi_ws":
                continue
            obj = rec.get("obj") or {}
            ftype = obj.get("type")
            if ftype not in (FRAME_SNAPSHOT, FRAME_DELTA, FRAME_TRADE):
                continue
            msg = obj.get("msg") or {}
            market = msg.get("market_ticker")
            if not market:
                continue
            yield RawFrame(kind=ftype, market=market, msg=msg, server_ts=_parse_server_ts(msg))


def discover_journals(journals_dir: str, since: str | None = None) -> list[str]:
    """Sorted ``*.jsonl.gz`` in ``journals_dir`` (NEVER the live ``.jsonl`` still being written),
    optionally filtered to close dates >= ``since`` (YYYY-MM-DD). Filenames are ``<YYYYMMDD>T<HHMMSS>Z``."""
    out: list[str] = []
    for p in sorted(glob.glob(os.path.join(journals_dir, "*.jsonl.gz"))):
        base = os.path.basename(p)
        day = None
        if len(base) >= 8 and base[:8].isdigit():
            day = f"{base[:4]}-{base[4:6]}-{base[6:8]}"
        if since is not None and day is not None and day < since:
            continue
        out.append(p)
    return out
