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
        "effective_mode": "armed",
        "close_time": f"2026-09-{day:02d}T{hour:02d}:00:00Z",
        "realized_lock": lock,
        "one_legged": one_legged,
        "realized_unsettled": True,
        "shadow": shadow,
        "replaces": replaces,
        "strike_lag_seconds": slag,
        "bucket_lag_seconds": blag,
    }


def _armed_no_fill_row(day: int, hour: int) -> dict:
    """An armed window that RAN but whose rest never filled (no completed set, no one-legged). It counts
    toward armed_windows (effective_mode armed) but not toward fills_total/n."""
    return {
        "armed": True,
        "effective_mode": "armed",
        "close_time": f"2026-09-{day:02d}T{hour:02d}:00:00Z",
        "realized_lock": None,
        "one_legged": False,
        "realized_unsettled": False,
        "shadow": {},
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
    # clarified denominator: armed_windows / 24 (30 armed windows -> 30/24 armed days)
    assert sb["armed_windows"] == 30 and sb["armed_days"] == Decimal(30) / Decimal(24)
    assert sb["mean_lock_c"] == Decimal(9)
    # 30 rest fills over 30 armed windows = 30 / (30/24) = 24.0 sets/day (>= the 2.0 pin)
    assert sb["fill_rate_per_day"] == Decimal(30) / (Decimal(30) / Decimal(24))
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
    # Clarified denominator = armed_windows / 24. 30 rest fills spread across 400 armed windows =
    # 30 / (400/24) = 1.8 sets/day < the 2.0 pin. Locks/gap/legged all fine, so the ONLY miss is the
    # fill-rate gate. The calendar span does not matter now -- only the count of armed windows the
    # pilot ran (the 370 armed windows whose rest never filled dilute the rate honestly).
    rows: list[dict] = []
    made = 0
    day, hour = 1, 0
    while made < 400:
        rows.append(_set_row(day, hour, "0.09", "0.10") if made < 30
                    else _armed_no_fill_row(day, hour))
        made += 1
        hour += 1
        if hour == 24:
            hour, day = 0, day + 1
    sb = build_falsifier_scoreboard(rows)
    assert sb["n"] == 30 and sb["armed_windows"] == 400
    assert sb["armed_days"] == Decimal(400) / Decimal(24)
    assert sb["fill_rate_per_day"] == Decimal(30) / (Decimal(400) / Decimal(24))  # 1.8/day
    assert sb["fill_rate_per_day"] < Decimal("2.0")
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
    # a dry/shakedown row (armed False, effective_mode not armed) never counts toward the live
    # scoreboard -- not as a completed set, a rest fill, or an armed window
    rows = [_set_row(1, 16, "0.09", "0.10")]
    rows[0]["armed"] = False
    rows[0]["effective_mode"] = "dry"
    sb = build_falsifier_scoreboard(rows)
    assert sb["n"] == 0 and sb["fills_total"] == 0
    assert sb["armed_windows"] == 0 and sb["armed_days"] == Decimal(0)
    assert sb["fill_rate_per_day"] is None


def test_scoreboard_armed_days_is_windows_over_24():
    # Registered clarification 2026-09-15: "armed evaluation days" = armed_windows / 24, NOT distinct
    # calendar days. 7 armed windows across TWO UTC dates (3 on the 14th, 4 on the 15th) with a single
    # completed set -> armed_windows 7, armed_days 7/24, fill rate 1 / (7/24) = 24/7 = 3.43/day.
    rows = [_set_row(14, 16, "0.09", "0.10")]                       # the one completed set (a fill)
    rows += [_armed_no_fill_row(14, h) for h in (17, 18)]           # 2 more armed windows, no fill
    rows += [_armed_no_fill_row(15, h) for h in (16, 17, 18, 19)]   # 4 armed windows next UTC day
    sb = build_falsifier_scoreboard(rows)
    assert sb["armed_windows"] == 7
    assert sb["armed_days"] == Decimal(7) / Decimal(24)
    assert sb["n"] == 1 and sb["fills_total"] == 1
    assert sb["n_days"] == 2                                        # two distinct calendar dates
    assert sb["fill_rate_per_day"] == Decimal(1) / (Decimal(7) / Decimal(24))  # 24/7 = 3.428.../day
    # n=1 < 30 so the verdict is still pending; the denominator clarification does not change that
    assert sb["verdict"].startswith("n<")


def test_scoreboard_surfaces_shadow_fills_outside_window():
    # Reviewer nit (2026-09-15 ~18:10Z / PR #54): the additive ledger counter
    # ``shadow_fills_outside_window`` (would-be shadow fills SUPPRESSED by the T-15..T-5 window gate)
    # is summed across rows and surfaced on the scoreboard tail. Older rows lack the key -> counted 0.
    from service.v32.report import _render_scoreboard
    rows = _thirty_sets("0.09", "0.10")
    rows[0]["shadow_fills_outside_window"] = 2
    rows[1]["shadow_fills_outside_window"] = 1
    # rows[2:] carry NO key (older-row shape) -> must contribute 0, not raise.
    sb = build_falsifier_scoreboard(rows)
    assert sb["shadow_fills_outside_window"] == 3
    line = [l for l in _render_scoreboard(sb) if "shadow fills outside window" in l]
    assert line and "= 3" in line[0]


def test_scoreboard_shadow_fills_outside_window_defaults_zero():
    # A ledger with no row carrying the counter (pre-PR#54 rows) reports 0, never a KeyError.
    sb = build_falsifier_scoreboard(_thirty_sets("0.09", "0.10"))
    assert sb["shadow_fills_outside_window"] == 0
