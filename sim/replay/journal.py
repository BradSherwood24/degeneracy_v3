"""Streaming journal reader + per-window header extraction.

A pilot journal line is ``{"idx", "kind", "local_ts" (float epoch s, RECEIPT clock —
drifts, never used for engine ordering), "obj"}``. ``iter_ws`` streams the ``kalshi_ws``
records (the book/trade tape). ``read_window_header`` does a cheap scan for the metadata
the backtest needs: close_time and the two leg tickers (H=high, L=low).

Leg resolution order (spec):
  1. ``quintile`` record's ``high_ticker`` / ``low_ticker`` when BOTH are present (even
     ``ok=false`` — the sub-$1 fallback still carries valid tickers).
  2. else the ``fire`` / ``would_fire`` legs: the NO-side leg ticker is H, the YES-side
     leg ticker is L.
If neither yields both tickers, the window is SKIPPED (reason recorded).
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from typing import Iterator, Optional


def iter_ws(path: str) -> Iterator[dict]:
    """Yield the ``obj`` of each ``kalshi_ws`` record, augmented with ``_idx`` and
    ``_local_ts`` from the envelope. Streams line-by-line (journals are ~130 MB)."""
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            if '"kalshi_ws"' not in line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("kind") != "kalshi_ws":
                continue
            obj = rec.get("obj")
            if not isinstance(obj, dict):
                continue
            obj["_idx"] = rec.get("idx")
            obj["_local_ts"] = rec.get("local_ts")
            yield obj


def resolve_close_epoch(iso: str) -> float:
    """Parse an ISO close_time (e.g. ``2026-08-24T05:00:00Z``) to epoch seconds (UTC)."""
    s = iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return _dt.datetime.fromisoformat(s).timestamp()


@dataclass
class WindowHeader:
    path: str
    close_time: Optional[str] = None          # ISO
    close_epoch: Optional[float] = None
    high_ticker: Optional[str] = None         # H leg — we buy NO here
    low_ticker: Optional[str] = None          # L leg — we buy YES here
    leg_source: Optional[str] = None          # "quintile" | "fire" | "would_fire"
    quintile_ok: Optional[bool] = None
    quintile_reason: Optional[str] = None
    fire_kind: Optional[str] = None           # "FIRE" | "WOULD_FIRE" | None
    fire_C: Optional[str] = None
    fire_t_minus_s: Optional[float] = None
    fire_legs: list = field(default_factory=list)
    skip: bool = False
    skip_reason: Optional[str] = None

    @property
    def ok(self) -> bool:
        return (not self.skip) and self.high_ticker is not None and self.low_ticker is not None


def read_window_header(path: str) -> WindowHeader:
    """Scan the NON-ws records of a journal to build the window header. Cheap: the ws
    tape dominates the file but the header records are few and near the top; we still scan
    the whole file so a late ``fire`` is not missed (fire can appear well into the tape)."""
    hdr = WindowHeader(path=path)
    q_high = q_low = None
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            if '"kalshi_ws"' in line:
                # Fast reject the overwhelming majority of lines without JSON-parsing them.
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            kind = rec.get("kind")
            obj = rec.get("obj") or {}
            if kind == "window_start":
                if obj.get("close_time"):
                    hdr.close_time = obj["close_time"]
            elif kind == "window_meta":
                if hdr.close_time is None and obj.get("close_time"):
                    hdr.close_time = obj["close_time"]
            elif kind == "quintile":
                hdr.quintile_ok = obj.get("ok")
                hdr.quintile_reason = obj.get("reason")
                ht, lt = obj.get("high_ticker"), obj.get("low_ticker")
                if ht and lt:
                    q_high, q_low = ht, lt
            elif kind in ("fire", "would_fire"):
                # Keep the FIRST fire/would_fire (the entry decision); later records are
                # rebalance/flatten intents, not new pairs.
                if hdr.fire_kind is None:
                    hdr.fire_kind = obj.get("kind") or kind.upper()
                    hdr.fire_C = obj.get("C")
                    hdr.fire_t_minus_s = obj.get("t_minus_s")
                    hdr.fire_legs = obj.get("legs") or []

    # Resolve legs: quintile first, then fire/would_fire.
    if q_high and q_low:
        hdr.high_ticker, hdr.low_ticker, hdr.leg_source = q_high, q_low, "quintile"
    elif hdr.fire_legs:
        no_leg = next((l for l in hdr.fire_legs if l.get("side") == "no"), None)
        yes_leg = next((l for l in hdr.fire_legs if l.get("side") == "yes"), None)
        if no_leg and yes_leg:
            hdr.high_ticker = no_leg.get("ticker")
            hdr.low_ticker = yes_leg.get("ticker")
            hdr.leg_source = hdr.fire_kind.lower() if hdr.fire_kind else "fire"

    if hdr.close_time:
        try:
            hdr.close_epoch = resolve_close_epoch(hdr.close_time)
        except Exception:
            hdr.close_epoch = None

    if not (hdr.high_ticker and hdr.low_ticker):
        hdr.skip = True
        hdr.skip_reason = "no legs (quintile missing/ok=false and no fire/would_fire legs)"
    elif hdr.close_epoch is None:
        hdr.skip = True
        hdr.skip_reason = "no parseable close_time"
    return hdr
