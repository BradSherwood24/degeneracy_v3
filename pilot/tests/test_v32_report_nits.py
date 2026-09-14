"""V3.2 reporting nits (2026-09-14). FAKES ONLY — no proxy, no socket, no sealed/holdout date.

Covers the two report fixes:
  1. The ledger row (and summary) carry the LAST QUOTED spot bucket (spot_bucket_ticker/Sd/Su), the
     last rest price + desired n, and every distinct spot bucket quoted — captured WHILE quoting by
     ``V32Driver._capture_quote`` (not read from the reset-at-close state). The T-5 end-of-quoting
     cancel is recorded as ``quote_end_cancel`` and NOT counted as a stand-down; a real stand-down
     (staleness / no-spot / alarm) sets stand_downs + stand_down_reason.
  2. The report scoreboard's data-age p99 reads ``lag_stats`` (max of per-window p99s), the per-window
     table gains a lastRest column + the Sd from the new field, and legacy rows still render.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from service.record_range import StreamJournal
from service.v32 import ActionKind, V32State, load_v32_params
from service.v32.actions import V32Action
from service.v32.ledger import build_v32_ledger_row, load_v32_rows
from service.v32.report import (
    _render,
    _render_scoreboard,
    build_falsifier_scoreboard,
    build_report,
)
import service.run_v32 as R

CLOSE = "2026-09-13T20:00:00Z"
CTS = 1789156800  # epoch of CLOSE (not a sealed/holdout date)
B_SD = "KXBTC-26SEP1316-B68200"
B_SU = "KXBTC-26SEP1316-B68300"
BUCKET_MAP = {B_SD: (68200.0, 68299.99), B_SU: (68300.0, 68399.99)}


def _params():
    return load_v32_params()


def _driver(tmp_path):
    p = _params()
    j = StreamJournal(str(tmp_path / "w.jsonl"), flush_every=1)
    j.open()
    state = V32State.new(CLOSE, CTS, BUCKET_MAP, p, shakedown=True)
    drv = R.V32Driver(p, state, j, R.FrozenExecutor(BUCKET_MAP))
    return p, j, drv


def _place(ticker: str, price: str) -> V32Action:
    return V32Action(kind=ActionKind.WOULD_PLACE_REST, ticker=ticker, side="no", action="buy",
                     count=1, price=Decimal(price), expiration_epoch=CTS - 300,
                     client_order_id="c1")


def _row_from_driver(p, drv, **over):
    """Build a ledger row the way ``_finalize`` does — reading the driver's captured attrs."""
    kwargs = dict(
        close_time=CLOSE, resolved_mode="dry", effective_mode="dry", degrade=None, params=p,
        state=drv.state, driver_counts=dict(drv.counts), executor_counts={}, ws_counts={},
        strike_count=2, strike_generations=1, bucket_count=2, bucket_generations=1,
        strike_lag_seconds=None, bucket_lag_seconds=None, journal_path=None, record_count=0,
        stand_down_reason=None, now=1.0,
        last_quoted_bucket_ticker=drv._last_quoted_bucket_ticker,
        last_quoted_Sd=drv._last_quoted_Sd, last_quoted_Su=drv._last_quoted_Su,
        last_rest_price=drv._last_rest_price, last_desired_n=drv._last_desired_n,
        spot_buckets_quoted=list(drv._spot_buckets_quoted), quote_end_cancel=drv._quote_end_cancel,
        real_stand_downs=drv._real_stand_downs, last_stand_down_reason=drv._last_stand_down_reason,
    )
    kwargs.update(over)
    return build_v32_ledger_row(**kwargs)


# ===========================================================================
# 1. ledger capture while quoting two buckets
# ===========================================================================
def test_ledger_captures_last_quoted_bucket_over_two_buckets(tmp_path):
    p, j, drv = _driver(tmp_path)
    # quote bucket 68200 (rest 0.53), then move to 68300 (rest 0.55) — the reset-at-close state has
    # neither, so only the WHILE-quoting capture can carry them.
    drv.state = replace(drv.state, spot_Sd=68200, spot_Su=68300, desired_n=Decimal("0.53"))
    drv._journal_action(_place(B_SD, "0.53"), CTS - 700)
    drv.state = replace(drv.state, spot_Sd=68300, spot_Su=68400, desired_n=Decimal("0.55"))
    drv._journal_action(_place(B_SU, "0.55"), CTS - 500)
    # T-5 end-of-quoting cancel: a STAND_DOWN("past_quote_end") that must NOT count as a stand-down
    drv._journal_action(V32Action(kind=ActionKind.STAND_DOWN, reason="past_quote_end"), CTS - 300)
    # the reset-at-close state nulls the spot (as it does live)
    drv.state = replace(drv.state, spot_Sd=None, spot_Su=None, desired_n=None)

    row = _row_from_driver(p, drv)
    assert row["spot_buckets_quoted"] == [68200, 68300]
    assert row["spot_bucket_ticker"] == B_SU  # last quoted, not None
    assert row["Sd"] == 68300 and row["Su"] == 68400
    assert row["last_rest_price"] == "0.55" and row["last_desired_n"] == "0.55"
    assert row["quote_end_cancel"] is True
    assert row["stand_downs"] == 0 and row["stand_down_reason"] is None
    assert row["stand_down"] is False


def test_real_stand_down_counted_and_reasoned(tmp_path):
    p, j, drv = _driver(tmp_path)
    drv.state = replace(drv.state, spot_Sd=68200, spot_Su=68300, desired_n=Decimal("0.50"))
    drv._journal_action(_place(B_SD, "0.50"), CTS - 700)
    # a genuine stand-down (no spot bucket), then the orderly quote-end cancel
    drv._journal_action(V32Action(kind=ActionKind.STAND_DOWN, reason="no_spot_bucket"), CTS - 400)
    drv._journal_action(V32Action(kind=ActionKind.STAND_DOWN, reason="past_quote_end"), CTS - 300)

    row = _row_from_driver(p, drv)
    assert row["stand_downs"] == 1                       # only the real one
    assert row["stand_down_reason"] == "no_spot_bucket"  # last real stand-down record
    assert row["quote_end_cancel"] is True
    assert row["stand_down"] is False                    # the window did not fully stand down


def test_legacy_row_shape_preserved_without_capture(tmp_path):
    # a caller that passes none of the new kwargs (legacy / _stand_down path) still gets a row with
    # the new fields defaulted and stand_downs from the raw action count.
    p = _params()
    row = build_v32_ledger_row(
        close_time=CLOSE, resolved_mode="dry", effective_mode="dry", degrade=None, params=p,
        state=None, driver_counts={"stand_down": 2}, executor_counts={}, ws_counts={},
        strike_count=0, strike_generations=0, bucket_count=0, bucket_generations=0,
        strike_lag_seconds=None, bucket_lag_seconds=None, journal_path=None, record_count=0,
        stand_down_reason="no buckets", now=1.0,
    )
    assert row["spot_buckets_quoted"] == [] and row["quote_end_cancel"] is False
    assert row["last_rest_price"] is None and row["last_desired_n"] is None
    assert row["stand_downs"] == 2                       # falls back to the raw action count
    assert row["stand_down_reason"] == "no buckets" and row["stand_down"] is True


def test_row_roundtrips_json(tmp_path):
    p, j, drv = _driver(tmp_path)
    drv.state = replace(drv.state, spot_Sd=68200, spot_Su=68300, desired_n=Decimal("0.53"))
    drv._journal_action(_place(B_SD, "0.53"), CTS - 700)
    row = _row_from_driver(p, drv)
    from service.v32.ledger import append_v32_ledger_row
    lp = str(tmp_path / "l.jsonl")
    append_v32_ledger_row(row, lp)
    back = load_v32_rows(lp)
    assert back[0]["spot_buckets_quoted"] == [68200] and back[0]["last_rest_price"] == "0.53"


# ===========================================================================
# 2. report: scoreboard data-age p99 from lag_stats + new table columns
# ===========================================================================
def _armed_row(hour: int, lock: str, slag_p99: float, blag_p99: float) -> dict:
    return {
        "armed": True, "close_time": f"2026-09-05T{hour:02d}:00:00Z",
        "realized_lock": lock, "one_legged": False, "realized_unsettled": True,
        "shadow": {"0.10": {"filled": True, "lock": "0.10"}}, "replaces": 40,
        # NO per-row *_lag_seconds -> the ONLY lag signal is lag_stats
        "lag_stats": {"strikes": {"mean": 0.1, "p99": slag_p99, "last": 0.1, "n": 100},
                      "buckets": {"mean": 0.2, "p99": blag_p99, "last": 0.2, "n": 100}},
    }


def test_scoreboard_reads_lag_stats_p99_max_across_windows():
    rows = [_armed_row(16, "0.09", 0.30, 0.40), _armed_row(17, "0.09", 0.90, 1.50)]
    sb = build_falsifier_scoreboard(rows)
    # max of the per-window p99s (the honest worst tail across windows)
    assert sb["strike_lag_stats_p99_s"] == Decimal("0.9")
    assert sb["bucket_lag_stats_p99_s"] == Decimal("1.5")
    # the legacy per-row lag p99 is n/a here (rows carry no *_lag_seconds), so the render must use
    # the lag_stats value, not fall through to n/a.
    assert sb["strike_lag_p99_s"] is None
    text = "\n".join(_render_scoreboard(sb))
    assert "data-age p99 (max/window)" in text
    assert "strike 0.90s" in text and "bucket 1.50s" in text


def test_scoreboard_data_age_na_only_when_lag_stats_absent():
    # a row with neither lag_stats nor *_lag_seconds -> the line is n/a (fallback exhausted)
    rows = [{"armed": True, "close_time": "2026-09-05T16:00:00Z", "realized_lock": "0.09",
             "shadow": {}, "replaces": 40}]
    sb = build_falsifier_scoreboard(rows)
    assert sb["strike_lag_stats_p99_s"] is None
    text = "\n".join(_render_scoreboard(sb))
    assert "strike n/a" in text and "bucket n/a" in text


def test_report_table_shows_sd_and_last_rest():
    rows = [{
        "close_time": CLOSE, "effective_mode": "dry", "mode": "dry",
        "spot_bucket_ticker": B_SU, "Sd": 68300, "Su": 68400, "last_rest_price": "0.55",
        "replaces": 81, "would_places": 81, "late_fills": 0, "shadow": {}, "m15_frames": 0,
        "strike_lag_seconds": None, "bucket_lag_seconds": None,
        "stand_down": False, "stand_down_reason": None, "spot_buckets_quoted": [68200, 68300],
        "quote_end_cancel": True,
    }]
    report = build_report(rows)
    w = report["windows"][0]
    assert w["Sd"] == 68300 and w["last_rest"] == "0.55"
    text = _render(report)
    assert "lastRest" in text            # new column header
    assert "Sd" in text                  # new column header
    assert "68300" in text               # Sd rendered
    assert "0.55" in text                # last rest price rendered
    assert B_SU in text                  # last-quoted bucket ticker


def test_report_table_renders_legacy_row_without_new_fields():
    # a row predating the nits fix (no Sd/last_rest_price/spot_buckets_quoted) still renders
    rows = [{
        "close_time": CLOSE, "effective_mode": "dry", "mode": "dry",
        "spot_bucket_ticker": None, "replaces": 3, "would_places": 3, "late_fills": 0,
        "shadow": {}, "m15_frames": 0, "stand_down": True, "stand_down_reason": "no_spot_bucket",
    }]
    report = build_report(rows)
    w = report["windows"][0]
    assert w["Sd"] is None and w["last_rest"] is None
    text = _render(report)
    assert "STAND DOWN" in text          # bucket placeholder for a fully stood-down window
    assert "lastRest" in text            # header present; the cell falls back to "-"
