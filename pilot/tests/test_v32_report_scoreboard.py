"""V3.2 falsifier scoreboard (Phase 4 report section). Synthetic ledger only — no network/proxy/holdout.

Verifies ``service.v32.report.build_falsifier_scoreboard`` computes the pre-registered [pin] statistics
and the verdict (ALIVE-so-far / KILL / n<30 pending) from the SAME constants the falsifier document
commits to (``service.v32.falsifier_pins``)."""

from __future__ import annotations

from decimal import Decimal

from service.v32.falsifier_pins import (
    V32_FALSIFIER_MIN_MEAN_LOCK_CENTS,
    V32_FALSIFIER_MIN_N,
)
from service.v32.report import build_falsifier_scoreboard


def _set_row(day: int, hour: int, lock: str | None, shadow_lock: str | None = None,
             one_legged: bool = False, replaces: int = 77,
             slag: float = 0.2, blag: float = 0.3) -> dict:
    """A synthetic armed ledger row. ``lock`` None + one_legged True = a one-legged (uncompleted) set;
    a completed set carries a realized_lock (dollars, as the ledger stores it)."""
    shadow = {"0.10": {"filled": True, "lock": shadow_lock}} if shadow_lock is not None else {}
    return {
        "armed": True,
        "close_time": f"2026-09-{day:02d}T{hour:02d}:00:00Z",
        "realized_lock": lock,
        "one_legged": one_legged,
        "realized_unsettled": True,
        "shadow": shadow,
        "replaces": replaces,
        "strike_lag_seconds": slag,
        "bucket_lag_seconds": blag,
    }


def _thirty_sets(lock: str, shadow_lock: str) -> list[dict]:
    """30 completed sets over 10 UTC days (3/day) -> fill rate 3.0/day >= 2.0."""
    rows: list[dict] = []
    for d in range(1, 11):
        for h in (16, 17, 18):
            rows.append(_set_row(d, h, lock, shadow_lock))
    return rows


def test_scoreboard_pending_below_min_n():
    rows = [_set_row(1, 16, "0.09", "0.10"), _set_row(1, 17, "0.05", "0.06")]
    sb = build_falsifier_scoreboard(rows)
    assert sb["n"] == 2
    assert sb["verdict"] == f"n<{V32_FALSIFIER_MIN_N} pending (n=2)"


def test_scoreboard_statistics_percentiles_and_gap():
    # three completed sets with distinct locks (still < 30 -> pending), to pin the stat math.
    rows = [_set_row(1, 16, "0.03", "0.05"), _set_row(1, 17, "0.05", "0.08"),
            _set_row(1, 18, "0.09", "0.10")]
    sb = build_falsifier_scoreboard(rows)
    assert sb["n"] == 3
    assert sb["min_lock_c"] == Decimal(3)
    assert sb["median_lock_c"] == Decimal(5)         # nearest-rank median of [3,5,9]
    assert sb["p10_lock_c"] == Decimal(3)            # nearest-rank p10 -> the min
    assert sb["mean_lock_c"] == (Decimal(3) + Decimal(5) + Decimal(9)) / Decimal(3)
    assert sb["pct_positive"] == Decimal(100)
    # exec gap = mean(shadow - live) in cents: (5-3)+(8-5)+(10-9) = 6 over 3 = 2.0c
    assert sb["exec_gap_c"] == Decimal(2)
    assert sb["shadow_mean_lock_c"] == (Decimal(5) + Decimal(8) + Decimal(10)) / Decimal(3)


def test_scoreboard_alive_when_all_pins_pass():
    sb = build_falsifier_scoreboard(_thirty_sets("0.09", "0.10"))
    assert sb["n"] == 30 and sb["one_legged"] == 0 and sb["n_days"] == 10
    assert sb["mean_lock_c"] == Decimal(9)
    assert sb["fill_rate_per_day"] == Decimal(3)
    assert sb["exec_gap_c"] == Decimal(1)
    assert sb["verdict"] == "ALIVE-so-far"


def test_scoreboard_kill_on_low_mean_lock():
    # mean lock 2c < +4.0c pin -> KILL at n=30.
    sb = build_falsifier_scoreboard(_thirty_sets("0.02", "0.03"))
    assert sb["n"] == 30
    assert sb["mean_lock_c"] < V32_FALSIFIER_MIN_MEAN_LOCK_CENTS
    assert sb["verdict"].startswith("KILL")
    assert "mean lock" in sb["verdict"]


def test_scoreboard_kill_on_execution_gap():
    # locks fine (+9c) but the shadow locks +14c -> a 5c execution gap > 3.0c pin -> KILL.
    sb = build_falsifier_scoreboard(_thirty_sets("0.09", "0.14"))
    assert sb["exec_gap_c"] == Decimal(5)
    assert sb["verdict"].startswith("KILL") and "exec gap" in sb["verdict"]


def test_scoreboard_kill_on_too_many_one_legged():
    rows = _thirty_sets("0.09", "0.10")
    # add three one-legged sets (> the pin of 2) on a fresh day
    rows += [_set_row(11, 16, None, "0.10", one_legged=True),
             _set_row(11, 17, None, "0.10", one_legged=True),
             _set_row(11, 18, None, "0.10", one_legged=True)]
    sb = build_falsifier_scoreboard(rows)
    assert sb["n"] == 30 and sb["one_legged"] == 3
    assert sb["fills_total"] == 33      # 30 completed + 3 one-legged rest fills
    assert sb["verdict"].startswith("KILL") and "one-legged" in sb["verdict"]


def test_scoreboard_kill_on_low_fill_rate():
    # 30 completed sets but spread thin: 2/day on 5 days + 1/day on 20 days = 25 armed days -> 30/25 =
    # 1.2/day < the 2.0 pin. Locks/gap/legged all fine, so the ONLY miss is the fill-rate gate.
    rows: list[dict] = []
    d = 1
    for _ in range(5):          # 5 days x 2 sets = 10
        rows += [_set_row(d, 16, "0.09", "0.10"), _set_row(d, 17, "0.09", "0.10")]
        d += 1
    for _ in range(20):         # 20 days x 1 set = 20  (total 30 over 25 days)
        rows.append(_set_row(d, 16, "0.09", "0.10"))
        d += 1
    sb = build_falsifier_scoreboard(rows)
    assert sb["n"] == 30 and sb["n_days"] == 25
    assert sb["fill_rate_per_day"] == Decimal(30) / Decimal(25)   # 1.2/day < 2.0
    assert sb["verdict"].startswith("KILL") and "fill rate" in sb["verdict"]


def test_scoreboard_kill_on_low_pct_positive():
    # 23 sets at +10c and 7 at -1c: mean = (230-7)/30 = +7.43c (>= +4.0c) but %positive = 23/30 =
    # 76.7% < 80 -> the ONLY miss is the %positive gate (mean lock still clears its bar).
    rows = [_set_row(d, h, "0.10", "0.10") for d in range(1, 9) for h in (16, 17, 18)][:23]
    rows += [_set_row(9, h, "-0.01", "0.00") for h in range(7)]   # 7 negative-lock completed sets
    sb = build_falsifier_scoreboard(rows)
    assert sb["n"] == 30
    assert sb["mean_lock_c"] >= V32_FALSIFIER_MIN_MEAN_LOCK_CENTS   # mean still clears +4.0c
    assert sb["pct_positive"] == Decimal(23) * 100 / Decimal(30)    # 76.66..% < 80
    assert sb["verdict"].startswith("KILL") and "%positive" in sb["verdict"]


def test_scoreboard_p99_data_age_per_connection():
    rows = [_set_row(1, 16, "0.09", "0.10", slag=0.2, blag=0.4),
            _set_row(1, 17, "0.09", "0.10", slag=0.9, blag=1.5)]
    sb = build_falsifier_scoreboard(rows)
    # nearest-rank p99 of a 2-element list -> the max
    assert sb["strike_lag_p99_s"] == Decimal("0.9")
    assert sb["bucket_lag_p99_s"] == Decimal("1.5")
    assert sb["replaces_per_hour_mean"] == Decimal(77)


def test_scoreboard_ignores_dry_rows():
    # a dry/shakedown row (armed False) never counts toward the live scoreboard
    rows = [_set_row(1, 16, "0.09", "0.10")]
    rows[0]["armed"] = False
    sb = build_falsifier_scoreboard(rows)
    assert sb["n"] == 0 and sb["fills_total"] == 0
