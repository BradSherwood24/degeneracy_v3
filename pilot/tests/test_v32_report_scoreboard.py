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
             slag: float = 0.2, blag: float = 0.3, bucket: str | None = "_DEFAULT") -> dict:
    """A synthetic armed ledger row. ``lock`` None + one_legged True = a one-legged (uncompleted) set;
    a completed set carries a realized_lock (dollars, as the ledger stores it). Every quoting window
    carries a ``spot_bucket_ticker`` (needed for the capture-ratio counting, MEASUREMENT CLARIFICATION
    3); pass ``bucket=None`` to model a window that armed but never selected a bucket."""
    shadow = {"0.10": {"filled": True, "lock": shadow_lock}} if shadow_lock is not None else {}
    tick = f"KXBTC-TEST{day:02d}{hour:02d}-B00000" if bucket == "_DEFAULT" else bucket
    return {
        "armed": True,
        "effective_mode": "armed",
        "spot_bucket_ticker": tick,
        "close_time": f"2026-09-{day:02d}T{hour:02d}:00:00Z",
        "realized_lock": lock,
        "one_legged": one_legged,
        "realized_unsettled": True,
        "shadow": shadow,
        "replaces": replaces,
        "strike_lag_seconds": slag,
        "bucket_lag_seconds": blag,
    }


def _armed_no_fill_row(day: int, hour: int, shadow_lock: str | None = None,
                       bucket: str | None = "_DEFAULT", stand_down_reason: str | None = None) -> dict:
    """An armed window that RAN but whose rest never filled (no completed set, no one-legged). It counts
    toward armed_windows (effective_mode armed) but not toward fills_total/n. With ``shadow_lock`` set
    AND a bucket present it is a CAPTURE MISS (the shadow filled but the live path took nothing) --
    exactly the stood-down / no-fill windows Registration 3's capture ratio should catch."""
    shadow = {"0.10": {"filled": True, "lock": shadow_lock}} if shadow_lock is not None else {}
    tick = f"KXBTC-TEST{day:02d}{hour:02d}-B00000" if bucket == "_DEFAULT" else bucket
    row = {
        "armed": True,
        "effective_mode": "armed",
        "spot_bucket_ticker": tick,
        "close_time": f"2026-09-{day:02d}T{hour:02d}:00:00Z",
        "realized_lock": None,
        "one_legged": False,
        "realized_unsettled": False,
        "shadow": shadow,
    }
    if stand_down_reason is not None:
        row["stand_down"] = True
        row["stand_down_reason"] = stand_down_reason
    return row


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
    # 30 rest fills over 30 armed windows = 30 / (30/24) = 24.0 sets/day (info now, not a gate)
    assert sb["fill_rate_per_day"] == Decimal(30) / (Decimal(30) / Decimal(24))
    # capture ratio (Registration 3): 30 completed sets over 30 armed+bucket windows the shadow filled
    assert sb["capture_live_sets"] == 30 and sb["capture_shadow_fills"] == 30
    assert sb["capture_ratio"] == Decimal(1)
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


def test_scoreboard_low_fill_rate_no_longer_kills():
    # MEASUREMENT CLARIFICATION 3 (2026-09-19): the fill-rate gate (>= 2.0 sets/day) is SUPERSEDED as a
    # verdict gate by the capture ratio. Here 30 rest fills spread across 400 armed windows = 1.8
    # sets/day (< the old 2.0 pin) -- but the 370 no-fill windows had NO shadow fill (no pump
    # availability), so the shadow only filled the 30 windows the live path captured: capture 30/30 =
    # 100% >= 50%. A lean tape no longer kills; only failing to capture available pumps does.
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
    assert sb["fill_rate_per_day"] < Decimal("2.0")                               # below the OLD pin
    # capture ratio passes: the shadow only filled where the live path also filled
    assert sb["capture_shadow_fills"] == 30 and sb["capture_live_sets"] == 30
    assert sb["capture_ratio"] == Decimal(1)
    # fill rate is NOT in the verdict any more; every real gate passes -> ALIVE-so-far
    assert sb["verdict"] == "ALIVE-so-far"
    assert "fill rate" not in sb["verdict"]


def test_scoreboard_kill_on_low_capture_ratio():
    # 30 completed sets, all clean (mean +9c, 100% positive, gap +1c, 0 one-legged) -> every OTHER gate
    # passes. But the shadow filled 40 MORE armed+bucket windows the live path took nothing in
    # (stood-down / no-fill misses): capture = 30 / 70 = 42.8% < the 0.50 pin -> the ONLY miss is the
    # capture-ratio gate. This is exactly what Registration 3 wants the gate to catch: available pumps
    # we failed to capture (execution/design), not a lean tape.
    from service.v32.falsifier_pins import V32_CAPTURE_RATIO_MIN
    rows = _thirty_sets("0.09", "0.10")                      # 30 captured windows (shadow + live set)
    # 40 windows the shadow filled but live captured nothing (a stand-down or a no-fill)
    miss = 0
    d, h = 20, 0
    while miss < 40:
        reason = "replace_rate_alarm" if miss % 2 == 0 else None
        rows.append(_armed_no_fill_row(d, h, shadow_lock="0.10", stand_down_reason=reason))
        miss += 1
        h += 1
        if h == 24:
            h, d = 0, d + 1
    sb = build_falsifier_scoreboard(rows)
    assert sb["n"] == 30                                     # verdict is decided (n >= 30)
    assert sb["capture_live_sets"] == 30 and sb["capture_shadow_fills"] == 70
    assert sb["capture_ratio"] == Decimal(30) / Decimal(70)  # 0.428...
    assert sb["capture_ratio"] < V32_CAPTURE_RATIO_MIN
    assert sb["verdict"].startswith("KILL") and "capture ratio" in sb["verdict"]
    # the other gates did NOT trip -- capture ratio is the sole miss
    for other in ("mean lock", "%positive", "exec gap", "one-legged"):
        assert other not in sb["verdict"]


def test_capture_ratio_counts_and_exclusions():
    # The capture-ratio numerator/denominator are counted ONLY over armed windows carrying a spot
    # bucket. This fixes every case in Registration 3's definition:
    #   (a) live set + shadow fill (bucket)         -> denom +1, numer +1 (a capture)
    #   (b) shadow fill + stood-down live (bucket)  -> denom +1, numer +0 (a MISS)
    #   (c) window with neither shadow nor set       -> excluded from both
    #   (d) armed window with NO spot bucket         -> excluded from both (even if the shadow filled)
    #   (e) a dry/backfill row                       -> excluded (not armed)
    rows = [
        _set_row(1, 16, "0.09", "0.10"),                                     # (a) capture
        _armed_no_fill_row(1, 17, shadow_lock="0.10",
                           stand_down_reason="stale_or_missing_wing"),       # (b) miss
        _armed_no_fill_row(1, 18),                                           # (c) neither -> excluded
        _set_row(1, 19, "0.09", "0.10", bucket=None),                        # (d) no bucket -> excluded
    ]
    dry = _set_row(1, 20, "0.09", "0.10")                                    # (e) a dry row -> excluded
    dry["armed"] = False
    dry["effective_mode"] = "dry"
    rows.append(dry)
    sb = build_falsifier_scoreboard(rows)
    assert sb["capture_shadow_fills"] == 2      # (a) + (b) only
    assert sb["capture_live_sets"] == 1         # (a) only
    assert sb["capture_ratio"] == Decimal(1) / Decimal(2)


def test_capture_ratio_none_when_no_shadow_availability():
    # No armed+bucket window the shadow filled -> the ratio is undefined (None); at n >= 30 that is a
    # verdict fail (there is availability we cannot show we captured -- mirrors the fill_rate None case).
    rows = [_set_row(d, h, "0.09", None) for d in range(1, 11) for h in (16, 17, 18)]  # sets, no shadow
    sb = build_falsifier_scoreboard(rows)
    assert sb["n"] == 30
    assert sb["capture_shadow_fills"] == 0 and sb["capture_ratio"] is None
    assert sb["verdict"].startswith("KILL") and "capture ratio" in sb["verdict"]


def test_capture_ratio_legacy_pre62_rows_handled():
    # A pre-PR#62 legacy row (single-set scalar realized_lock, no wing_batch_sets) with a shadow fill +
    # bucket must count as one captured window -- the capture counting reuses the same _row_set_events
    # fallback as the rest of the scoreboard, so an existing ledger reads identically.
    legacy = {
        "armed": True, "effective_mode": "armed",
        "spot_bucket_ticker": "KXBTC-LEGACY-B00000",
        "close_time": "2026-09-14T23:00:00Z",
        "realized_lock": "0.10", "one_legged": False, "realized_unsettled": True,
        "shadow": {"0.10": {"filled": True, "lock": "0.11"}},
    }
    assert "wing_batch_sets" not in legacy
    sb = build_falsifier_scoreboard([legacy])
    assert sb["n"] == 1
    assert sb["capture_live_sets"] == 1 and sb["capture_shadow_fills"] == 1
    assert sb["capture_ratio"] == Decimal(1)


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


def test_scoreboard_renders_capture_ratio_line():
    # MEASUREMENT CLARIFICATION 3: the scoreboard prints the capture ratio as 'live X / shadow Y = Z%'
    # with the pin, and the fill rate stays printed but labelled info/superseded.
    from service.v32.report import _render_scoreboard
    rows = _thirty_sets("0.09", "0.10")                        # 30 captured
    rows.append(_armed_no_fill_row(20, 0, shadow_lock="0.10",  # +1 shadow-only miss -> 30/31
                                   stand_down_reason="replace_rate_alarm"))
    sb = build_falsifier_scoreboard(rows)
    text = "\n".join(_render_scoreboard(sb))
    cap = [l for l in _render_scoreboard(sb) if "capture ratio" in l][0]
    assert "live 30 / shadow 31" in cap
    assert "[pin] Registration 3" in cap
    # fill rate is still printed, but labelled superseded/info
    assert "fill rate =" in text and "superseded as a gate by Registration 3" in text


def test_scoreboard_shadow_fills_outside_window_defaults_zero():
    # A ledger with no row carrying the counter (pre-PR#54 rows) reports 0, never a KeyError.
    sb = build_falsifier_scoreboard(_thirty_sets("0.09", "0.10"))
    assert sb["shadow_fills_outside_window"] == 0


# ===========================================================================
# N1 (PR #66 review): the capture ratio is a bounded per-WINDOW fraction (numerator counts windows,
# not set events) -- a two-set window counts once and a live-set-without-shadow window never pushes the
# ratio above 1.
# ===========================================================================
def test_capture_ratio_does_not_double_count_two_sets_in_one_window():
    """A single armed+bucket window that completed TWO rest-fill sets (contracts=2 partial fills) vs one
    shadow fill must count as ONE captured window, not two -- else the ratio inflates above the true
    per-window capture and can exceed 1."""
    row = {
        "armed": True, "effective_mode": "armed",
        "spot_bucket_ticker": "KXBTC-TWOSET-B0",
        "close_time": "2026-09-20T16:00:00Z",
        "shadow": {"0.10": {"filled": True, "lock": "0.10"}},
        "wing_batch_sets": [  # two completed sets in one window (per PR #62 schema)
            {"realized_lock": "0.09", "one_legged": False},
            {"realized_lock": "0.09", "one_legged": False},
        ],
        "realized_unsettled": True,
    }
    sb = build_falsifier_scoreboard([row])
    assert sb["n"] == 2                       # both sets still count for the other gates
    assert sb["capture_shadow_fills"] == 1
    assert sb["capture_live_sets"] == 1       # per-window, not per-set
    assert sb["capture_ratio"] == Decimal(1)
    assert sb["capture_ratio"] <= Decimal(1)


def test_capture_ratio_live_set_without_shadow_never_exceeds_one():
    # a window with a completed live set but NO shadow fill adds to neither capture count (a capture
    # ratio > 1 is conceptually impossible: you cannot capture more pumps than the shadow proved).
    rows = [
        _set_row(1, 16, "0.09", "0.10"),   # shadow + set -> 1/1
        _set_row(1, 17, "0.09", None),     # live set, NO shadow fill -> excluded from both
    ]
    sb = build_falsifier_scoreboard(rows)
    assert sb["n"] == 2                     # both sets count for the other gates
    assert sb["capture_shadow_fills"] == 1 and sb["capture_live_sets"] == 1
    assert sb["capture_ratio"] == Decimal(1)


# ===========================================================================
# n_min gate (Registration 3 nit): a shadow fill whose derived n (1 - offer) is below n_min is not
# live-reachable and is excluded from the capture denominator AND the shadow lock / exec-gap stats.
# ===========================================================================
def _shadow_row(day: int, hour: int, offer: str, lock: str, has_set: bool,
                set_lock: str = "0.09") -> dict:
    """An armed+bucket window whose shadow E=0.10 filled at ``offer`` (derived n = 1 - offer), with or
    without a completed live set."""
    row = {
        "armed": True, "effective_mode": "armed",
        "spot_bucket_ticker": f"KXBTC-T{day:02d}{hour:02d}-B0",
        "close_time": f"2026-09-{day:02d}T{hour:02d}:00:00Z",
        "shadow": {"0.10": {"filled": True, "offer": offer, "lock": lock}},
        "realized_unsettled": True,
    }
    row["realized_lock"] = set_lock if has_set else None
    row["one_legged"] = False
    return row


def test_capture_ratio_excludes_below_min_shadow_fill():
    # a shadow fill at offer 0.96 (derived n = 0.04 < n_min 0.05) is NOT a live-reachable counterfactual
    # -> excluded from the denominator and the shadow lock/exec-gap stats, and surfaced as a below-min
    # suppression (from the existing row via the derived-n exclusion). The 2026-09-19 23:00Z window.
    rows = [
        _shadow_row(19, 22, offer="0.68", lock="0.10", has_set=True),   # valid capture -> 1/1
        _shadow_row(19, 23, offer="0.96", lock="0.1072", has_set=False),  # below-min -> excluded
    ]
    sb = build_falsifier_scoreboard(rows)
    assert sb["capture_shadow_fills"] == 1          # the below-min window is NOT in the denominator
    assert sb["capture_live_sets"] == 1
    assert sb["capture_ratio"] == Decimal(1)
    assert sb["shadow_fills_below_min"] == 1         # derived-from-offer exclusion, surfaced
    # shadow mean lock excludes the below-min 0.1072: only the 0.10 fill remains -> +10.0c
    assert sb["shadow_mean_lock_c"] == Decimal(10)


def test_capture_ratio_below_min_boundary_at_n_min_counts():
    # a shadow fill at offer 0.95 (derived n = 0.05 == n_min) is live-reachable (strict `<`) -> counts.
    rows = [_shadow_row(1, 16, offer="0.95", lock="0.10", has_set=True)]
    sb = build_falsifier_scoreboard(rows)
    assert sb["capture_shadow_fills"] == 1
    assert sb["capture_live_sets"] == 1
    assert sb["shadow_fills_below_min"] == 0


def test_scoreboard_counts_upstream_shadow_fills_below_min():
    # a NEW row (shadow suppressed upstream, so no filled shadow record) carries the counter directly;
    # it is summed and surfaced without a filled shadow record present.
    rows = _thirty_sets("0.09", "0.10")
    rows[0]["shadow_fills_below_min"] = 1
    rows[1]["shadow_fills_below_min"] = 2
    sb = build_falsifier_scoreboard(rows)
    assert sb["shadow_fills_below_min"] == 3
    from service.v32.report import _render_scoreboard
    line = [l for l in _render_scoreboard(sb) if "below n_min" in l]
    assert line and "= 3" in line[0]


def test_scoreboard_shadow_fills_below_min_defaults_zero():
    # a ledger whose shadow fills are all at/above n_min and whose rows carry no counter -> 0.
    sb = build_falsifier_scoreboard(_thirty_sets("0.09", "0.10"))
    assert sb["shadow_fills_below_min"] == 0
