"""Rung 1.5 tape-sim tests: fee/C composition, honest-fills side selection, staleness
expiry, EV bucket assignment, census-sha refusal, sealed-date refusal, and newest-first
trade sorting. Pure-function coverage plus a seal integration check."""
import gzip
import json
import os
from decimal import Decimal

import pytest

import loader
import tape_sim as TS
from census import fee
from loader import SealError


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _iso(ep: float) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(ep, tz=dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


def _yes_trade(ep, price, side="yes"):
    return {"created_time": _iso(ep), "taker_outcome_side": side,
            "yes_price_dollars": f"{price:.4f}", "no_price_dollars": f"{1-price:.4f}",
            "trade_id": f"y{ep}", "ticker": "H"}


def _no_trade(ep, price, side="no"):
    return {"created_time": _iso(ep), "taker_outcome_side": side,
            "no_price_dollars": f"{price:.4f}", "yes_price_dollars": f"{1-price:.4f}",
            "trade_id": f"n{ep}", "ticker": "L"}


T = 1_000_000  # arbitrary epoch anchor for the pure walk tests


# ---------------------------------------------------------------------------
# fee / C composition
# ---------------------------------------------------------------------------
def test_cost_C_uses_audited_fee_per_leg():
    # C = yes + fee(yes) + no + fee(no); fee is the audited census.fee.
    yes = Decimal("0.57"); no = Decimal("0.46")
    expected = yes + fee(yes) + no + fee(no)
    assert TS.cost_C(yes, no) == expected
    # golden fee literals still hold through cost_C
    assert fee(Decimal("0.24")) == Decimal("0.0128")


def test_fair_linear_law():
    # fair_lin = 1 - 0.006*G
    assert TS.fair_linear(50.0) == Decimal("1") - Decimal("0.006") * Decimal("50")
    assert TS.fair_linear(0.0) == Decimal("1")


# ---------------------------------------------------------------------------
# honest-fills side selection
# ---------------------------------------------------------------------------
def test_side_fills_filters_by_taker_outcome_side():
    trades = [_yes_trade(T - 100, 0.40, side="yes"),
              _yes_trade(T - 90, 0.99, side="no")]   # a "no" taker on the H market
    yes_fills = TS._side_fills(trades, "yes", "yes_price_dollars")
    assert len(yes_fills) == 1
    assert yes_fills[0][1] == Decimal("0.4000")      # never the 0.99 no-side price


def test_no_fill_never_sets_yes_leg_price():
    # H-market tape carries a "no" taker with a bogus yes_price; it must NOT price the
    # YES leg. The only YES price a moment may show is from a taker_outcome_side=="yes".
    yes_trades = [_yes_trade(T - 300, 0.99, side="no"),   # bogus; wrong side
                  _yes_trade(T - 200, 0.40, side="yes")]  # legitimate YES fill
    no_trades = [_no_trade(T - 195, 0.30, side="no")]
    events, _ = TS.walk_window(T, yes_trades, no_trades,
                               fair_bucket=Decimal("0.90"), G=50.0, pin=False,
                               quintile=1, staleness_s=60)
    assert events, "expected at least one both-live event"
    assert all(e["yes_price"] == Decimal("0.4000") for e in events)
    assert all(e["yes_price"] != Decimal("0.9900") for e in events)


# ---------------------------------------------------------------------------
# staleness expiry
# ---------------------------------------------------------------------------
def test_staleness_blocks_event_when_other_leg_stale():
    # NO fill at T-100, YES fill at T-10: at the YES fill the NO leg is 90s old (> 60s) so
    # NOT both-live -> no event.
    yes_trades = [_yes_trade(T - 10, 0.40, side="yes")]
    no_trades = [_no_trade(T - 100, 0.30, side="no")]
    events, summ = TS.walk_window(T, yes_trades, no_trades, Decimal("0.90"), 50.0,
                                  False, 1, staleness_s=60)
    assert events == []
    assert summ["n_events"] == 0


def test_staleness_allows_event_within_window():
    # NO fill at T-30, YES fill at T-10: NO leg is 20s old (<= 60) -> both-live event.
    yes_trades = [_yes_trade(T - 10, 0.40, side="yes")]
    no_trades = [_no_trade(T - 30, 0.30, side="no")]
    events, summ = TS.walk_window(T, yes_trades, no_trades, Decimal("0.90"), 50.0,
                                  False, 1, staleness_s=60)
    assert summ["n_events"] == 1
    e = events[0]
    assert e["yes_price"] == Decimal("0.4000")
    assert e["no_price"] == Decimal("0.3000")
    assert e["no_age_s"] == pytest.approx(20.0, abs=1e-6)


def test_staleness_boundary_exactly_at_limit_is_live():
    # exactly staleness_s old counts as live (<=).
    yes_trades = [_yes_trade(T - 10, 0.40, side="yes")]
    no_trades = [_no_trade(T - 70, 0.30, side="no")]   # 60s old at the YES fill
    events, summ = TS.walk_window(T, yes_trades, no_trades, Decimal("0.90"), 50.0,
                                  False, 1, staleness_s=60)
    assert summ["n_events"] == 1


# ---------------------------------------------------------------------------
# EV bucket assignment + EV values
# ---------------------------------------------------------------------------
def test_bucket_assignment_and_edge_tie_upper():
    curve = TS.EVCurve(edges=[0.1, 0.2, 0.3, 0.4],
                       pin_rate=[0.02, 0.09, 0.16, 0.17, 0.33],
                       bucket_n=[1, 1, 1, 1, 1], census_sha="x")
    assert curve.assign(0.05) == 0
    assert curve.assign(0.15) == 1
    assert curve.assign(0.45) == 4
    assert curve.assign(0.10) == 1     # tie on an edge -> upper bucket (bisect_right)
    assert curve.fair[0] == Decimal("1") - Decimal("0.02")


def test_ev_bucket_value_is_fair_minus_C():
    # single both-live event; ev_bucket must equal fair_bucket - C exactly.
    yes_trades = [_yes_trade(T - 20, 0.40, side="yes")]
    no_trades = [_no_trade(T - 15, 0.30, side="no")]
    fair = Decimal("0.90")
    events, _ = TS.walk_window(T, yes_trades, no_trades, fair, 50.0, False, 1,
                               staleness_s=60)
    e = events[0]
    assert e["ev_bucket"] == fair - e["C"]
    assert e["ev_lin"] == TS.fair_linear(50.0) - e["C"]


# ---------------------------------------------------------------------------
# census sha refusal (EV curve)
# ---------------------------------------------------------------------------
def test_evcurve_refuses_on_sha_mismatch(tmp_path):
    bogus = tmp_path / "census_train.csv"
    bogus.write_text("date,g_over_sigma,pin_escape,status\n", encoding="utf-8")
    with pytest.raises(Exception) as ei:
        TS.EVCurve.from_census(str(bogus))
    assert "sha mismatch" in str(ei.value)


def test_evcurve_accepts_real_census():
    census = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "out", "census_train.csv")
    curve = TS.EVCurve.from_census(census)
    assert curve.census_sha == TS.CENSUS_TRAIN_SHA256
    assert len(curve.pin_rate) == 5
    # reproduces gate.json bucket-0 pin rate
    assert curve.pin_rate[0] == pytest.approx(0.02145922746781116, abs=1e-12)


# ---------------------------------------------------------------------------
# sealed-date refusal
# ---------------------------------------------------------------------------
def test_load_trades_refuses_sealed_date():
    # A sealed UTC day must be refused BEFORE any read, with acknowledge=False.
    with pytest.raises(SealError):
        loader.load_trades("15-minute", ["2026-08-05"])


def test_load_trades_refuses_every_sealed_day():
    for d in loader.SEALED_DATES:
        with pytest.raises(SealError):
            loader.load_trades("1-hour", [d])


def test_run_refuses_sealed_date(tmp_path):
    # end-to-end: run() must refuse a sealed eval date (never sets acknowledge).
    with pytest.raises(SealError):
        TS.run(["2026-08-10"], str(tmp_path))


# ---------------------------------------------------------------------------
# newest-first trade sorting handled
# ---------------------------------------------------------------------------
def test_load_trades_sorts_newest_first_input_ascending(tmp_path):
    # Build a synthetic gz day-file with trades stored NEWEST-FIRST (as the API delivers)
    # and assert load_trades returns them ASCENDING by created_time.
    root = tmp_path / "root"
    d = "2026-06-13"
    p = root / "15-minute" / "trades" / f"{d}.jsonl.gz"
    os.makedirs(os.path.dirname(p), exist_ok=True)
    newest_first = [_yes_trade(T - 5, 0.40), _yes_trade(T - 50, 0.41),
                    _yes_trade(T - 300, 0.42)]
    assert [t["created_time"] for t in newest_first] == sorted(
        (t["created_time"] for t in newest_first), reverse=True)
    with gzip.open(p, "wt", encoding="utf-8") as f:
        f.write(json.dumps({"ticker": "H", "trades": newest_first}) + "\n")
    by_ticker, shas = loader.load_trades("15-minute", [d], data_root=str(root))
    times = [t["created_time"] for t in by_ticker["H"]]
    assert times == sorted(times)          # ascending
    assert len(shas) == 1


def test_load_trades_dedupes_by_trade_id(tmp_path):
    root = tmp_path / "root"
    for d in ("2026-06-13", "2026-06-14"):
        p = root / "1-hour" / "trades" / f"{d}.jsonl.gz"
        os.makedirs(os.path.dirname(p), exist_ok=True)
        # same trade_id appears in both files (boundary duplicate)
        with gzip.open(p, "wt", encoding="utf-8") as f:
            f.write(json.dumps({"ticker": "L",
                                "trades": [_no_trade(T - 100, 0.30)]}) + "\n")
    by_ticker, _ = loader.load_trades("1-hour", ["2026-06-13", "2026-06-14"],
                                      data_root=str(root))
    assert len(by_ticker["L"]) == 1        # deduped by trade_id


# ---------------------------------------------------------------------------
# leg reconstruction cross-check
# ---------------------------------------------------------------------------
def test_legs_for_row_matches_orientation():
    row = {"close_time": "2026-06-13T07:00:00Z", "anchor_A": "63647.18",
           "threshold_K": "63599.99", "orientation": "A_above_K"}
    m15_by_ct = {"2026-06-13T07:00:00Z": {"ticker": "M15", "floor_strike": 63647.18}}
    h1_by_ct = {"2026-06-13T07:00:00Z": [
        {"ticker": "H1a", "floor_strike": 63599.99},
        {"ticker": "H1b", "floor_strike": 63500.00}]}
    hs, ht, ls, lt = TS.legs_for_row(row, m15_by_ct, h1_by_ct)
    # A>K: 15M is the H (YES) leg, 1H is the L (NO) leg
    assert (hs, ht) == ("15-minute", "M15")
    assert (ls, lt) == ("1-hour", "H1a")


def test_legs_for_row_hard_fails_on_anchor_drift():
    row = {"close_time": "2026-06-13T07:00:00Z", "anchor_A": "63647.18",
           "threshold_K": "63599.99", "orientation": "A_above_K"}
    m15_by_ct = {"2026-06-13T07:00:00Z": {"ticker": "M15", "floor_strike": 60000.00}}
    h1_by_ct = {"2026-06-13T07:00:00Z": [{"ticker": "H1a", "floor_strike": 63599.99}]}
    with pytest.raises(Exception):
        TS.legs_for_row(row, m15_by_ct, h1_by_ct)


# ===========================================================================
# A15.1 — variable-width microsecond sorting (the loader-contract fix)
# ===========================================================================
def _trade_ct(ct, tid):
    return {"created_time": ct, "taker_outcome_side": "yes",
            "yes_price_dollars": "0.5000", "no_price_dollars": "0.5000",
            "trade_id": tid, "ticker": "H"}


def test_load_trades_variable_microsecond_width_sorts_chronologically(tmp_path):
    # A15.1: created_time fractional seconds are VARIABLE-WIDTH; string order inverts
    # chronology (a no-fraction "…:01Z" sorts AFTER "…:01.25Z" as a string because 'Z' > '.')
    # so load_trades must sort by PARSED EPOCH. Includes a no-fraction timestamp.
    root = tmp_path / "root"
    d = "2026-06-13"
    p = root / "15-minute" / "trades" / f"{d}.jsonl.gz"
    os.makedirs(os.path.dirname(p), exist_ok=True)
    # chronological order t0<t1<t2<t3:
    cts = ["2026-06-13T12:00:00Z",       # 0.0  (no fraction)
           "2026-06-13T12:00:00.5Z",     # 0.5
           "2026-06-13T12:00:01Z",       # 1.0  (no fraction)
           "2026-06-13T12:00:01.25Z"]    # 1.25
    # feed them SCRAMBLED into the file
    scrambled = [cts[2], cts[0], cts[3], cts[1]]
    trades = [_trade_ct(ct, f"t{i}") for i, ct in enumerate(scrambled)]
    with gzip.open(p, "wt", encoding="utf-8") as f:
        f.write(json.dumps({"ticker": "H", "trades": trades}) + "\n")

    by_ticker, _ = loader.load_trades("15-minute", [d], data_root=str(root))
    loaded = [t["created_time"] for t in by_ticker["H"]]
    assert loaded == cts                                     # parsed-epoch chronological
    # prove a naive STRING sort would have produced a DIFFERENT (wrong) order
    string_sorted = sorted(t["created_time"] for t in trades)
    assert string_sorted != cts
    # specifically: the no-fraction "…:01Z" is misplaced after "…:01.25Z" by string order
    assert string_sorted.index("2026-06-13T12:00:01Z") > \
        string_sorted.index("2026-06-13T12:00:01.25Z")


def test_parse_created_epoch_handles_missing_fraction():
    a = TS.parse_ts("2026-06-13T12:00:01Z")
    b = TS.parse_ts("2026-06-13T12:00:00.5Z")
    assert b < a                                            # 0.5s < 1.0s, chronological


# ===========================================================================
# A15.4 — the FLIP direction
# ===========================================================================
def test_flip_settlement_payout_all_branches_both_orientations():
    # H/L result fields already encode orientation (H is always the higher line).
    assert TS.flip_settlement_payout("no", "yes") == Decimal(2)    # PIN -> both flip legs pay
    assert TS.flip_settlement_payout("yes", "yes") == Decimal(1)   # escape above both
    assert TS.flip_settlement_payout("no", "no") == Decimal(1)     # escape below both
    with pytest.raises(Exception):
        TS.flip_settlement_payout("yes", "no")                     # impossible ($0) -> hard fail


def test_fair_linear_flip_law():
    assert TS.fair_linear(50.0, "flip") == Decimal("1") + Decimal("0.006") * Decimal("50")
    assert TS.fair_linear(0.0, "flip") == Decimal("1")


def test_evcurve_fair_flip_is_one_plus_pin():
    curve = TS.EVCurve(edges=[0.1, 0.2, 0.3, 0.4],
                       pin_rate=[0.02, 0.09, 0.16, 0.17, 0.33],
                       bucket_n=[1, 1, 1, 1, 1], census_sha="x")
    assert curve.fair_flip[0] == Decimal("1") + Decimal("0.02")
    assert curve.fair_for("flip", 4) == Decimal("1") + Decimal("0.33")
    assert curve.fair_for("strangle", 0) == Decimal("1") - Decimal("0.02")


def _same(ts, price):
    return (float(ts), "SAME", Decimal(str(price)))


def _opp(ts, implied_bid):
    return (float(ts), "OPP", Decimal(str(implied_bid)))


def test_direction_legs_flip_mirror_side_selection():
    # flip high leg = NO-on-H (no_price); flip low leg = YES-on-L (yes_price). SAME entries
    # carry the carried buy price; OPP entries carry the implied bid.
    h_trades = [_yes_trade(T - 100, 0.55, side="yes"),   # OPP for flip high (NO leg)
                _no_trade(T - 90, 0.60, side="no")]      # SAME flip high
    l_trades = [_no_trade(T - 80, 0.30, side="no"),      # OPP for flip low (YES leg)
                _yes_trade(T - 70, 0.42, side="yes")]    # SAME flip low
    high, low = TS._direction_legs("flip", h_trades, l_trades)
    high_same = [p for _, k, p in high if k == "SAME"]
    low_same = [p for _, k, p in low if k == "SAME"]
    assert high_same == [Decimal("0.6000")]              # no_price from the NO fill
    assert low_same == [Decimal("0.4200")]               # yes_price from the YES fill
    # the opposite prints are present as OPP (refutation evidence), never as a buy price
    assert any(k == "OPP" for _, k, _ in high)
    assert any(k == "OPP" for _, k, _ in low)


def test_flip_high_leg_never_set_by_a_yes_fill():
    # honest-fills mirror: a YES taker on the H market must NOT become the flip NO leg's
    # carried buy price (it is OPP evidence only).
    h_trades = [_yes_trade(T - 200, 0.99, side="yes"),   # OPP for flip high
                _no_trade(T - 150, 0.60, side="no")]     # SAME flip high
    l_trades = [_yes_trade(T - 145, 0.40, side="yes")]   # flip low SAME
    high, _ = TS._direction_legs("flip", h_trades, l_trades)
    high_same = [p for _, k, p in high if k == "SAME"]
    assert high_same == [Decimal("0.6000")]
    assert all(p != Decimal("0.9900") for _, k, p in high if k == "SAME")


def test_flip_walk_payoff_ev_and_hardfloor():
    # one both-live flip event; verify C, fair(flip), ev, payoff, hard_floor.
    high = [_same(T - 20, "0.45")]    # NO-on-H
    low = [_same(T - 15, "0.40")]     # YES-on-L
    fair_flip = Decimal("1.10")
    fl = TS.fair_linear(50.0, "flip")
    settle = Decimal(1)                          # escape settlement for the flip
    events, summ = TS._walk_direction(T, high, low, fair_flip, fl, settle, staleness_s=60)
    assert summ["n_events"] == 1
    e = events[0]
    C = TS.cost_C(Decimal("0.45"), Decimal("0.40"))
    assert e["C"] == C
    assert e["ev"] == fair_flip - C
    assert e["ev_lin"] == fl - C
    assert e["payoff"] == settle - C             # escape -> 1 - C_flip
    assert e["hard_floor"] is True               # ~0.87 < 1.00
    assert summ["n_hardfloor"] == 1


def test_flip_pin_payoff_is_two_minus_C():
    # on a pin the flip settlement is $2, so payoff = 2 - C_flip.
    high = [_same(T - 20, "0.45")]
    low = [_same(T - 15, "0.40")]
    settle = Decimal(2)                          # pin settlement for the flip
    events, _ = TS._walk_direction(T, high, low, Decimal("1.10"),
                                   TS.fair_linear(50.0, "flip"), settle, 60)
    C = TS.cost_C(Decimal("0.45"), Decimal("0.40"))
    assert events[0]["payoff"] == Decimal(2) - C


# ===========================================================================
# A15.2 — missing trades-file pre-check (run continues)
# ===========================================================================
# ===========================================================================
# A15.8 — hard_floor CSV column blank for strangle, populated for flip
# ===========================================================================
def _sample_event(hard_floor):
    return {"t_minus_s": 123.4, "fill_side": "HIGH",
            "high_leg_price": Decimal("0.45"), "low_leg_price": Decimal("0.40"),
            "high_leg_age_s": 1.0, "low_leg_age_s": 2.0, "C": Decimal("0.8500"),
            "fair": Decimal("1.10"), "fair_lin": Decimal("1.30"),
            "ev": Decimal("0.25"), "ev_lin": Decimal("0.45"),
            "hard_floor": hard_floor, "payoff": Decimal("0.15"), "dwell_s": 0.5}


def test_csv_hard_floor_blank_for_strangle():
    r = _csv_row_helper("strangle", _sample_event(True))
    assert r["hard_floor"] == ""          # A15.8: sub-$1 strangle is NOT riskless -> blank
    r2 = _csv_row_helper("strangle", _sample_event(False))
    assert r2["hard_floor"] == ""


def test_csv_hard_floor_populated_for_flip():
    assert _csv_row_helper("flip", _sample_event(True))["hard_floor"] == "1"
    assert _csv_row_helper("flip", _sample_event(False))["hard_floor"] == "0"


def _csv_row_helper(direction, e):
    return TS._csv_event_row(direction, e, "2026-06-13", "2026-06-13T07:00:00Z",
                             50.0, 55.11, 0.9, 3, pin=False)


@pytest.fixture(scope="module")
def smoke_rows(tmp_path_factory):
    """Run the real one-day smoke (2026-06-13, both directions, A15.9 refutation ON) ONCE and
    share the parsed tape_points.csv rows across tests. Single day, non-sealed, per hard law."""
    import csv as _csv
    out = tmp_path_factory.mktemp("smoke")
    TS.run(["2026-06-13"], str(out))
    with open(os.path.join(str(out), "tape_points.csv"), newline="", encoding="utf-8") as f:
        return list(_csv.DictReader(f))


def test_smoke_csv_strangle_rows_have_blank_hard_floor_flip_do_not(smoke_rows):
    # end-to-end guard on the written CSV (A15.8).
    strangle = [r for r in smoke_rows if r["direction"] == "strangle"]
    flip = [r for r in smoke_rows if r["direction"] == "flip"]
    assert strangle and flip
    assert all(r["hard_floor"] == "" for r in strangle)          # A15.8
    assert all(r["hard_floor"] in ("0", "1") for r in flip)


# ===========================================================================
# A15.5/A15.6 — guaranteed-floor decomposition & distribution
# ===========================================================================
def test_hardfloor_policy_decomposes_guaranteed_from_realized():
    # two entered windows: one escape (settle 1), one pin (settle 2), both C=0.90.
    # guaranteed = 1 - C = 0.10 each -> total 0.20 (riskless).
    # realized: escape 1-0.90=0.10, pin 2-0.90=1.10 -> total 1.20 (pin bonus not guaranteed).
    C = Decimal("0.90")
    ws = [
        {"first_hardfloor_payoff": Decimal("1") - C, "first_hardfloor_C": C, "pin": False},
        {"first_hardfloor_payoff": Decimal("2") - C, "first_hardfloor_C": C, "pin": True},
    ]
    pol = TS._hardfloor_policy(ws)
    assert pol["n_entered"] == 2
    assert pol["guaranteed_floor_total"] == pytest.approx(0.20)
    assert pol["realized_total"] == pytest.approx(1.20)
    assert pol["n_pins"] == 1


def test_floor_histogram_flags_sub_tick():
    floors = [Decimal("0.002"), Decimal("0.008"), Decimal("0.03"), Decimal("0.30")]
    hist = dict(TS._floor_histogram(floors))
    assert hist["<$0.01 (sub-tick)"] == 2
    assert hist["$0.01-$0.05"] == 1
    assert hist[">=$0.25"] == 1
    # and the policy sub-tick count matches
    ws = [{"first_hardfloor_payoff": Decimal("1") - (Decimal("1") - g),
           "first_hardfloor_C": Decimal("1") - g, "pin": False} for g in floors]
    pol = TS._hardfloor_policy(ws)
    assert pol["n_sub_tick_floors"] == 2
    assert pol["floor_magnitudes_sorted"] == [0.002, 0.008, 0.03, 0.30]


# ===========================================================================
# A15.9 — cross-side refutation
# ===========================================================================
def test_refutation_kills_carried_price_blocks_event():
    # HIGH carried at T-40 (0.45). An OPP print at T-25 implies bid 0.50 >= 0.45 -> refutes.
    # LOW SAME fill at T-20: HIGH is refuted (not live) -> NO event.
    high = [_same(T - 40, "0.45"), _opp(T - 25, "0.50")]
    low = [_same(T - 20, "0.40")]
    events, summ = TS._walk_direction(T, high, low, Decimal("1.10"),
                                      TS.fair_linear(50.0, "flip"), Decimal(1), 60,
                                      refute=True)
    assert summ["n_events"] == 0
    # with refutation OFF (pre-A15.9) the same tape DOES produce the event
    ev_off, summ_off = TS._walk_direction(T, high, low, Decimal("1.10"),
                                          TS.fair_linear(50.0, "flip"), Decimal(1), 60,
                                          refute=False)
    assert summ_off["n_events"] == 1


def test_refutation_is_ge_not_gt_equal_price_refutes():
    # implied bid exactly == carried price refutes (>=, not >).
    high = [_same(T - 40, "0.99"), _opp(T - 30, "0.99")]      # equal -> refute
    low = [_same(T - 20, "0.40")]
    _, summ = TS._walk_direction(T, high, low, Decimal("1.10"),
                                 TS.fair_linear(50.0, "flip"), Decimal(1), 60, refute=True)
    assert summ["n_events"] == 0


def test_opposite_print_below_carried_does_not_refute():
    # implied bid 0.30 < carried 0.45 is CONSISTENT -> no refutation, event stands.
    high = [_same(T - 40, "0.45"), _opp(T - 30, "0.30")]
    low = [_same(T - 20, "0.40")]
    _, summ = TS._walk_direction(T, high, low, Decimal("1.10"),
                                 TS.fair_linear(50.0, "flip"), Decimal(1), 60, refute=True)
    assert summ["n_events"] == 1


def test_refutation_truncates_dwell():
    # HIGH & LOW both live from T-100; a refuting OPP on LOW at T-70 truncates the dwell of
    # the event opened at T-100. Without the OPP, dwell would run to staleness/next fill.
    high = [_same(T - 100, "0.45")]
    low = [_same(T - 100, "0.40"), _opp(T - 70, "0.95")]     # implied bid 0.95 >= 0.40
    events, _ = TS._walk_direction(T, high, low, Decimal("1.10"),
                                   TS.fair_linear(50.0, "flip"), Decimal(1), 60, refute=True)
    # the event(s) opened at T-100; its dwell ends at the OPP print (T-70), i.e. 30s, not 60s.
    assert events
    first = events[0]
    assert first["t_minus_s"] == 100
    assert first["dwell_s"] == pytest.approx(30.0, abs=1e-6)


def test_consistent_opp_does_not_shorten_dwell():
    # A15.10 (reviewer's proof shape): HIGH & LOW live at T-100; a CONSISTENT OPP on LOW at
    # T-70 (implied bid 0.20 < carried 0.40) must be transparent to the dwell clock. With
    # staleness 60, expiry = T-40, so the event's dwell must be 60.0, NOT 30.0.
    high = [_same(T - 100, "0.45")]
    low = [_same(T - 100, "0.40"), _opp(T - 70, "0.20")]      # implied bid 0.20 < 0.40
    events, _ = TS._walk_direction(T, high, low, Decimal("1.10"),
                                   TS.fair_linear(50.0, "flip"), Decimal(1), 60, refute=True)
    assert events
    assert events[0]["t_minus_s"] == 100
    assert events[0]["dwell_s"] == pytest.approx(60.0, abs=1e-6)


def test_refuting_opp_still_truncates_dwell_after_a159_fix():
    # A REFUTING opp (implied bid 0.95 >= carried 0.40) at T-70 truncates to 30.0 (contrast
    # with the consistent case above) — the fix must not make refuting prints transparent.
    high = [_same(T - 100, "0.45")]
    low = [_same(T - 100, "0.40"), _opp(T - 70, "0.95")]
    events, _ = TS._walk_direction(T, high, low, Decimal("1.10"),
                                   TS.fair_linear(50.0, "flip"), Decimal(1), 60, refute=True)
    assert events[0]["dwell_s"] == pytest.approx(30.0, abs=1e-6)


def test_leg_stream_hard_fails_on_non_complementary_trade():
    # A15.10 F2: yes+no must sum to 1; a violation hard-fails rather than mis-refuting.
    bad = [{"created_time": _iso(T - 10), "taker_outcome_side": "yes",
            "yes_price_dollars": "0.60", "no_price_dollars": "0.30",   # sums to 0.90
            "trade_id": "bad1", "ticker": "H"}]
    with pytest.raises(Exception):
        TS._leg_stream(bad, "yes", "yes_price_dollars", "no_price_dollars")


def test_refuted_leg_revives_only_on_next_same_side_fill():
    # HIGH refuted at T-50; a fresh HIGH SAME fill at T-30 revives it; LOW SAME at T-20 then
    # produces an event (both live again).
    high = [_same(T - 60, "0.45"), _opp(T - 50, "0.60"), _same(T - 30, "0.44")]
    low = [_same(T - 25, "0.40"), _same(T - 20, "0.40")]
    events, summ = TS._walk_direction(T, high, low, Decimal("1.10"),
                                      TS.fair_linear(50.0, "flip"), Decimal(1), 60,
                                      refute=True)
    assert summ["n_events"] >= 1
    # the revived HIGH price is 0.44 (the fresh SAME fill), never the refuted 0.45
    assert all(e["high_leg_price"] == Decimal("0.44") for e in events)


def test_same_timestamp_tiebreak_same_side_survives():
    # A co-timestamp OPP (implied bid 0.60 >= 0.45) and SAME (0.45) on HIGH: the SAME is the
    # surviving evidence -> HIGH live, event emitted with price 0.45 (confessed tie-break).
    high = [_opp(T - 20, "0.60"), _same(T - 20, "0.45")]     # same ts
    low = [_same(T - 20, "0.40")]
    events, summ = TS._walk_direction(T, high, low, Decimal("1.10"),
                                      TS.fair_linear(50.0, "flip"), Decimal(1), 60,
                                      refute=True)
    assert summ["n_events"] >= 1
    assert any(e["high_leg_price"] == Decimal("0.45") for e in events)


def test_a159_known_answer_0200_flip_hardfloor_refuted(smoke_rows):
    # Commission A15.9 known-answer: the 2026-06-13 02:00 flip "hard floor" at t_minus 81.309
    # (C=0.9982) must be ABSENT after refutation — NO-taker prints at implied yes-bid 0.99
    # refute the carried 0.99 low leg during the 38s gap, so no event is emitted there.
    offenders = [r for r in smoke_rows
                 if r["direction"] == "flip"
                 and r["close_time"] == "2026-06-13T02:00:00Z"
                 and 81.0 <= float(r["t_minus_s"]) <= 81.6]
    assert offenders == [], f"expected the refuted 02:00 flip event to be absent, got {offenders}"


def test_run_records_missing_trade_day_and_continues(tmp_path, corpus):
    # The corpus builds markets+candles for one OK hour but NO trades files. run() must
    # record the missing day and continue (not crash), producing outputs with zero events.
    root, meta = corpus()
    out = tmp_path / "out"
    agg = TS.run([meta["D"]], str(out), data_root=root)
    assert meta["D"] in agg["missing_trade_days"]
    assert (out / "tape_points.csv").exists()
    assert agg["n_eligible_windows"] >= 1
    assert agg["n_evaluation_events"] == 0        # no trades -> no events, run still finished
