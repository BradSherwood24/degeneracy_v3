"""Rung 1 loader — day-file JSONL loading, day assignment (A2.7), dedupe with
byte-identity assertion, and THE SEAL.

House law: the seal is enforced in code, not comments. The sealed UTC days
2026-08-02..2026-08-18 are HARDCODED below (not configurable). Every loader entry
point takes ``acknowledge_sealed_read: bool = False`` and raises :class:`SealError`
on any access to a sealed day unless True. Only ``sim/unseal_runner.py`` passes True.

Day assignment (A2.7): parse ``close_time`` (ISO string) -> epoch -1s -> UTC date.
A market closing exactly 00:00:00 UTC belongs to the PRIOR day.
"""

from __future__ import annotations

import datetime as _dt
import gzip
import hashlib
import os
from typing import Dict, Iterable, List, Tuple

# ---------------------------------------------------------------------------
# THE SEAL — hardcoded, not configurable.
# ---------------------------------------------------------------------------
SEALED_DATES = (
    "2026-08-02", "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06",
    "2026-08-07", "2026-08-08", "2026-08-09", "2026-08-10", "2026-08-11",
    "2026-08-12", "2026-08-13", "2026-08-14", "2026-08-15", "2026-08-16",
    "2026-08-17", "2026-08-18",
)
_SEALED = frozenset(SEALED_DATES)

# TRAIN half (52 days), for reference / guards.
TRAIN_START = "2026-06-11"
TRAIN_END = "2026-08-01"

_SERIES = ("15-minute", "1-hour")
_KINDS = ("markets", "candles", "trades")

# Repo root = parent of the directory holding this file (sim/ -> repo root).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_ROOT = os.path.join(_ROOT, "historical-data")

# A3.6/A3.7: the freeze marker lives on a dedicated STATUS line. Defense-in-depth (A3.7)
# has the LOADER itself assert this file is FROZEN before opening any sealed byte, on top
# of unseal_runner's own preflight. Overridable so tests can point at a frozen copy.
FALSIFIER_MD = os.path.join(_ROOT, "sim", "ceremony", "falsifier.md")
FROZEN_STATUS_LINE = "STATUS: FROZEN"


class SealError(Exception):
    """Raised on any attempt to read a sealed UTC day without acknowledgement."""


class IntegrityError(Exception):
    """Raised on any fail-closed data integrity violation (byte-identity, etc.)."""


def is_sealed(date_str: str) -> bool:
    return date_str in _SEALED


def data_path(series: str, kind: str, date_str: str, data_root: str = _DATA_ROOT) -> str:
    if series not in _SERIES:
        raise ValueError(f"unknown series {series!r}")
    if kind not in _KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    # trades are gzip JSONL (.jsonl.gz); markets/candles are plain JSONL (.jsonl).
    ext = ".jsonl.gz" if kind == "trades" else ".jsonl"
    return os.path.join(data_root, series, kind, date_str + ext)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def close_epoch(close_time: str) -> int:
    """ISO close_time -> integer UTC epoch seconds."""
    s = close_time.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = _dt.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return int(dt.timestamp())


def assign_day(close_time: str) -> str:
    """A2.7 day assignment: (close_epoch - 1s) -> UTC date string YYYY-MM-DD.

    A market closing exactly at 00:00:00 UTC belongs to the PRIOR day.
    """
    ep = close_epoch(close_time) - 1
    return _dt.datetime.fromtimestamp(ep, tz=_dt.timezone.utc).strftime("%Y-%m-%d")


def _norm_line(line: str) -> str:
    # Byte-identity is compared on line content with trailing EOL/whitespace
    # stripped so a lone "\r\n" vs "\n" is not a false mismatch.
    return line.rstrip("\r\n")


def falsifier_is_frozen(text: str) -> bool:
    """A3.6: freeze state is keyed on a dedicated structured line — a line that strips to
    exactly ``STATUS: FROZEN`` — parsed as a line, not a whole-file substring (so prose
    that merely mentions FROZEN / NOT FROZEN cannot flip the state)."""
    return any(line.strip() == FROZEN_STATUS_LINE for line in text.splitlines())


def _assert_falsifier_frozen() -> None:
    """A3.7: the loader itself refuses a sealed read unless falsifier.md is FROZEN."""
    if not os.path.exists(FALSIFIER_MD):
        raise SealError(
            f"SEAL: falsifier missing ({FALSIFIER_MD}); refusing sealed read (A3.7 "
            f"defense-in-depth)."
        )
    with open(FALSIFIER_MD, "r", encoding="utf-8") as f:
        text = f.read()
    if not falsifier_is_frozen(text):
        raise SealError(
            "SEAL: falsifier.md is not FROZEN (no line reads exactly 'STATUS: FROZEN'); "
            "the loader refuses the sealed read (A3.7 defense-in-depth)."
        )


def _guard_seal(date_str: str, acknowledge_sealed_read: bool) -> None:
    if date_str in _SEALED:
        if not acknowledge_sealed_read:
            raise SealError(
                f"SEAL: refused access to sealed UTC day {date_str} "
                f"(acknowledge_sealed_read=False). Only sim/unseal_runner.py may pass True."
            )
        # A3.7: even with acknowledgement, the falsifier must be FROZEN first.
        _assert_falsifier_frozen()


def load_raw_lines(series: str, kind: str, date_str: str,
                   acknowledge_sealed_read: bool = False,
                   data_root: str = _DATA_ROOT) -> Tuple[List[str], str]:
    """Return (raw json lines, sha256) for one day-file. Seal fires BEFORE any read."""
    _guard_seal(date_str, acknowledge_sealed_read)
    path = data_path(series, kind, date_str, data_root=data_root)
    sha = sha256_file(path)
    out: List[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                out.append(_norm_line(line))
    return out, sha


import json as _json


def load_markets(series: str, dates: Iterable[str],
                 acknowledge_sealed_read: bool = False,
                 data_root: str = _DATA_ROOT):
    """Load market records across ``dates``.

    Returns (records, shas) where:
      * records: list of dict, deduped by ticker across files with a byte-identical
        assertion (differing copies -> IntegrityError). Each record carries
        ``_assigned_day`` (A2.7) and ``_raw`` (the source line).
      * shas: dict {path: sha256} for every file consumed.
    """
    by_ticker: Dict[str, str] = {}
    records: Dict[str, dict] = {}
    shas: Dict[str, str] = {}
    for d in dates:
        lines, sha = load_raw_lines(series, "markets", d,
                                    acknowledge_sealed_read=acknowledge_sealed_read,
                                    data_root=data_root)
        shas[data_path(series, "markets", d, data_root=data_root)] = sha
        for ln in lines:
            obj = _json.loads(ln)
            tk = obj["ticker"]
            if tk in by_ticker:
                if by_ticker[tk] != ln:
                    raise IntegrityError(
                        f"byte-identity violation: ticker {tk} differs across files"
                    )
                continue
            by_ticker[tk] = ln
            obj["_assigned_day"] = assign_day(obj["close_time"])
            obj["_raw"] = ln
            records[tk] = obj
    return list(records.values()), shas


def load_candles(series: str, dates: Iterable[str],
                 acknowledge_sealed_read: bool = False,
                 data_root: str = _DATA_ROOT):
    """Load candle records across ``dates``, keyed by ticker, deduped byte-identically.

    Returns (by_ticker, shas).
    """
    raw_by_ticker: Dict[str, str] = {}
    out: Dict[str, dict] = {}
    shas: Dict[str, str] = {}
    for d in dates:
        lines, sha = load_raw_lines(series, "candles", d,
                                    acknowledge_sealed_read=acknowledge_sealed_read,
                                    data_root=data_root)
        shas[data_path(series, "candles", d, data_root=data_root)] = sha
        for ln in lines:
            obj = _json.loads(ln)
            tk = obj["ticker"]
            if tk in raw_by_ticker:
                if raw_by_ticker[tk] != ln:
                    raise IntegrityError(
                        f"byte-identity violation: candle ticker {tk} differs across files"
                    )
                continue
            raw_by_ticker[tk] = ln
            out[tk] = obj
    return out, shas


def parse_created_epoch(created_time: str) -> float:
    """created_time (ISO, VARIABLE-width fractional seconds, trailing 'Z') -> epoch float.

    A15.1: the API's microsecond field is variable-width (e.g. ``.774115``, ``.77``, or
    absent), so ``created_time`` STRING order does NOT track chronology — it inverts. All
    trade ordering must go through this parsed-epoch key, never the raw string.
    """
    s = created_time.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = _dt.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.timestamp()


def load_trades(series: str, dates: Iterable[str],
                acknowledge_sealed_read: bool = False,
                data_root: str = _DATA_ROOT):
    """Load the executed-trades tape for ``series`` across ``dates`` (Rung 1.5).

    Each day-file is gzip JSONL, one line per market: ``{"ticker":..., "trades":[...]}``.
    Raw trades are newest-first; this returns them **sorted ASCENDING by PARSED epoch of
    created_time** (A15.1 — NOT by the created_time string, whose variable-width fractional
    seconds invert chronology). Returns (by_ticker, shas):

      * by_ticker: ``{ticker: [trade dict, ...]}`` — trades chronological. If a ticker
        recurs across day-files (boundary duplicate), trades are merged and de-duplicated
        by ``trade_id`` (fail-open union, not a byte-identity assert: trade lists are large
        API payloads, and any dropped/added duplicate would be an honest superset).
      * shas: ``{path: sha256}`` for every file consumed.

    The SEAL fires BEFORE any read (reuses ``_guard_seal`` — the same guard markets/candles
    use), so a sealed date raises :class:`SealError` unless acknowledged.
    """
    by_ticker: Dict[str, Dict[str, dict]] = {}   # ticker -> {trade_id: trade}
    shas: Dict[str, str] = {}
    for d in dates:
        _guard_seal(d, acknowledge_sealed_read)
        path = data_path(series, "trades", d, data_root=data_root)
        shas[path] = sha256_file(path)
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                obj = _json.loads(line)
                tk = obj["ticker"]
                bucket = by_ticker.setdefault(tk, {})
                for tr in obj.get("trades", []):
                    tid = tr["trade_id"]
                    if tid not in bucket:
                        bucket[tid] = tr
    out: Dict[str, List[dict]] = {}
    for tk, bucket in by_ticker.items():
        trades = list(bucket.values())
        trades.sort(key=lambda t: parse_created_epoch(t["created_time"]))   # A15.1: parsed epoch
        out[tk] = trades
    return out, shas
