"""Gate fit primitives + determinism (commission section 8, seed 26)."""
import pytest

import gate_fit as G


def test_quantile_linear_type7():
    xs = [0.0, 1.0, 2.0, 3.0, 4.0]
    assert G.quantile_linear(xs, 0.0) == 0.0
    assert G.quantile_linear(xs, 1.0) == 4.0
    assert G.quantile_linear(xs, 0.5) == 2.0
    assert G.quantile_linear(xs, 0.25) == 1.0


def test_bucket_of_ties_land_upper():
    edges = [1.0, 2.0, 3.0, 4.0]
    assert G.bucket_of(0.5, edges) == 0
    assert G.bucket_of(1.0, edges) == 1     # tie on edge -> upper bucket
    assert G.bucket_of(2.5, edges) == 2
    assert G.bucket_of(4.0, edges) == 4
    assert G.bucket_of(9.9, edges) == 4


def _rows(pins_by_day):
    """pins_by_day: {day: (n_escape, n_pin)}; C_base fixed 0.60 -> escape +0.40, pin -0.60."""
    out = []
    g = 0.1
    for day, (ne, npin) in pins_by_day.items():
        for _ in range(ne):
            out.append({"date": day, "close_time": f"{day}#e", "gos": g,
                        "C_base": 0.60, "C_worst": 0.80, "pin": False,
                        "payoff_base": 0.40, "payoff_worst": 0.20})
            g += 0.01
        for _ in range(npin):
            out.append({"date": day, "close_time": f"{day}#p", "gos": g,
                        "C_base": 0.60, "C_worst": 0.80, "pin": True,
                        "payoff_base": -0.60, "payoff_worst": -0.80})
            g += 0.01
    return out


def test_ev_equals_one_minus_pin_minus_meanC():
    rows = _rows({"d1": (8, 2), "d2": (8, 2)})
    st = G.bucket_stats(rows)
    pin_rate = st["pin_rate"]
    meanC = st["mean_C_base"]
    assert abs(st["EV_base"] - ((1 - pin_rate) - meanC)) < 1e-12


def test_bootstrap_is_deterministic_seed26():
    rows = _rows({"d1": (7, 3), "d2": (6, 4), "d3": (8, 2)})
    a = G.bootstrap_ci(rows, "payoff_base")
    b = G.bootstrap_ci(rows, "payoff_base")
    assert a == b                       # fresh Random(26) each call -> identical
    assert a[0] is not None and a[0] <= a[1]


def test_gate_nonempty_when_all_positive():
    fit = G.fit_gate(_rows({d: (9, 1) for d in ("d1", "d2", "d3", "d4", "d5")}))
    assert not fit["gate_empty"]
    assert fit["gate_buckets"][0] == 0
    assert fit["descriptive_ci_notice"].startswith("The train gate's day-clustered CI")


def test_gate_empty_when_all_negative():
    # every hour a pin -> EV strongly negative -> no low-end bucket clears
    fit = G.fit_gate(_rows({d: (0, 10) for d in ("d1", "d2", "d3", "d4", "d5")}))
    assert fit["gate_empty"]
    assert fit["g_star"] is None


def test_A3_8_degenerate_quintiles_hard_fail():
    # A3.8: heavy G/sigma ties collapse the interior edges -> hard fail with a receipt,
    # never a silent play-everything gate (reviewer B5's 18@1.0 + 2@5.0 case).
    rows = []
    for i in range(18):
        rows.append({"date": "d1", "close_time": f"a{i}", "gos": 1.0, "C_base": 0.6,
                     "C_worst": 0.8, "pin": False, "payoff_base": 0.4, "payoff_worst": 0.2})
    for i in range(2):
        rows.append({"date": "d2", "close_time": f"b{i}", "gos": 5.0, "C_base": 0.6,
                     "C_worst": 0.8, "pin": False, "payoff_base": 0.4, "payoff_worst": 0.2})
    with pytest.raises(G.DegenerateQuintiles) as ei:
        G.fit_gate(rows)
    assert ei.value.receipt["empty_buckets"] or ei.value.receipt[
        "tied_or_nonincreasing_edge_pairs"]


def test_A3_3_blank_worst_row_stays_and_base_unaffected():
    # A3.3: an OK row with a blank WORST value contributes to BASE stats and is retained;
    # WORST stats simply exclude it. No crash on None.
    rows = _rows({"d1": (8, 2), "d2": (8, 2)})
    rows[0]["C_worst"] = None
    rows[0]["payoff_worst"] = None
    st = G.bucket_stats(rows)
    assert st["n"] == len(rows)                 # BASE keeps every row
    assert st["n_worst"] == len(rows) - 1       # WORST excludes the blank one
    assert st["EV_base"] is not None
    lb, ub, used = G.bootstrap_ci(rows, "payoff_worst")
    assert lb is not None and used > 0          # worst CI still computes


def _fit_with_top_bucket_lb(top_lb):
    def bucket(i, lb):
        return {"bucket": i, "n": 10, "n_days": 5, "gos_lo": 0.1 * i,
                "gos_hi": 0.1 * i + 0.09, "pin_rate": 0.3, "mean_C_base": 0.7,
                "mean_C_worst": 0.8, "EV_base": 0.01, "EV_base_ci95": [lb, 0.2],
                "EV_worst": -0.01, "EV_worst_ci95": [-0.2, 0.1]}
    return {
        "n_ok_rows": 50, "n_days": 5, "bucketing": "quintiles",
        "quintile_edges_gos": [0.1, 0.2, 0.3, 0.4],
        "buckets": [bucket(0, 0.05), bucket(1, 0.04), bucket(2, 0.03),
                    bucket(3, 0.02), bucket(4, top_lb)],
        "gate_buckets": [0, 1, 2, 3, 4], "gate_empty": False, "g_star": 0.4,
        "gos_max_in_gate": 0.49, "c_cap_observed_base": 0.75,
        "gate_top_bucket": 4, "gate_top_bucket_individual_lb_base": top_lb,
        "gate_top_bucket_lb_le_zero": (top_lb <= 0),
        "gate_pooled": {"n": 50, "n_days": 5, "pin_rate": 0.3, "mean_C_base": 0.7,
                        "mean_C_worst": 0.8, "EV_base": 0.02, "EV_worst": -0.01,
                        "EV_base_ci95": [0.005, 0.05], "EV_worst_ci95": [-0.2, 0.1],
                        "boot_draws_used_base": 10000, "boot_draws_used_worst": 10000},
        "bootstrap": {"n_draws": 10000, "seed": 26, "method": "day-clustered"},
        "descriptive_ci_notice": G.DESCRIPTIVE_CI_NOTICE,
    }


def test_A3_11_dilution_admission_flagged_when_top_bucket_bleeds():
    md = G.render_report(_fit_with_top_bucket_lb(-0.03), "census_train.csv", "sha")
    assert "DILUTION ADMISSION (A3.11)" in md


def test_A3_11_no_dilution_flag_when_top_bucket_clears():
    md = G.render_report(_fit_with_top_bucket_lb(0.02), "census_train.csv", "sha")
    assert "DILUTION ADMISSION" not in md
