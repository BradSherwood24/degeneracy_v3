"""Census end-to-end on synthetic corpora: OK row, G<$0.01 exclusion (A2.2),
NaN hard-fail (A2.4), degenerate-predicate."""
from decimal import Decimal

import pytest

from census import (EPSILON, IntegrityError, _cand, build_census, hole_G,
                    is_degenerate)


def _row_at(rows, t_iso):
    return next(r for r in rows if r["close_time"] == t_iso)


def test_ok_pin_row(corpus):
    root, meta = corpus()               # default: A>K, G=50, print 60200 -> PIN
    rows, receipt = build_census([meta["D"]], data_root=root)
    r = _row_at(rows, meta["T_iso"])
    assert r["status"] == "OK"
    assert r["orientation"] == "A_above_K"
    assert r["pin_escape"] == "PIN"
    assert r["H_result"] == "no" and r["L_result"] == "yes"
    assert r["G"] == "50.00"
    # C_base = yes_ask(0.30)+fee + no_ask(1-0.55=0.45)+fee
    assert Decimal(r["C_base"]) == Decimal("0.30") + Decimal("0.45") \
        + Decimal("0.0147") + Decimal("0.0174")
    assert r["payoff_base"].startswith("-")   # PIN loses C
    assert receipt["ok_pins"] >= 1


def test_G_below_epsilon_excluded(corpus):
    root, meta = corpus(k_offset=0.0)   # K == A -> G = 0.00
    rows, receipt = build_census([meta["D"]], data_root=root)
    r = _row_at(rows, meta["T_iso"])
    assert r["status"] == "EXCL_G_LT_EPS"
    assert "EXCL_G_LT_EPS" in receipt["exclusion_inventory"]


def test_degenerate_predicate():
    assert is_degenerate(100.00, 100.00) is True      # exact equality
    assert is_degenerate(100.00, 100.005) is True     # rounds to 0.00 < 0.01
    assert is_degenerate(100.00, 100.01) is False     # G = 0.01, not < epsilon
    assert hole_G(100.00, 100.01) == Decimal("0.01")
    assert EPSILON == Decimal("0.01")


def test_nan_candle_hard_fails(corpus):
    root, meta = corpus(ya_close="NaN")   # H-leg yes_ask close is NaN
    with pytest.raises(IntegrityError):
        build_census([meta["D"]], data_root=root)


def test_cand_rejects_nan_string():
    with pytest.raises(IntegrityError):
        _cand("NaN")
    assert _cand("") is None
    assert _cand(None) is None
    assert _cand("0.30") == Decimal("0.30")


def test_receipt_has_shas_and_constants(corpus):
    root, meta = corpus()
    rows, receipt = build_census([meta["D"]], data_root=root)
    assert receipt["constants"]["sigma_anchors"] == 9
    assert receipt["constants"]["epsilon"] == "0.01"
    assert len(receipt["input_file_sha256"]) == 4   # 2 series x 2 kinds, 1 day


def test_A3_1_strikeless_anchor_excl_no_anchor(corpus):
    # A3.1: the T-market is the strike-less "up/down" product (no floor_strike). The hour
    # must route to EXCL_NO_ANCHOR with a receipt, never crash with KeyError.
    root, meta = corpus(anchor_strikeless=True)
    rows, receipt = build_census([meta["D"]], data_root=root)
    r = _row_at(rows, meta["T_iso"])
    assert r["status"] == "EXCL_NO_ANCHOR"
    assert "EXCL_NO_ANCHOR" in receipt["exclusion_inventory"]


def test_A3_3_missing_worst_stays_OK_with_blank_worst(corpus):
    # A3.3: a BASE-quoted hour whose WORST candle fields are absent stays OK; C_worst and
    # payoff_worst are blank (fidelity-limited); the receipt counts such rows.
    root, meta = corpus(worst_missing=True)
    rows, receipt = build_census([meta["D"]], data_root=root)
    r = _row_at(rows, meta["T_iso"])
    assert r["status"] == "OK"
    assert r["C_base"] != "" and r["payoff_base"] != ""
    assert r["C_worst"] == "" and r["payoff_worst"] == ""
    assert receipt["ok_rows_worst_blank"] >= 1


def test_A3_5_empty_expiration_value_excl_ev_missing(corpus):
    # A3.5: an EMPTY expiration_value on a leg -> EXCL_EV_MISSING (fail-closed), not a crash.
    root, meta = corpus(ev_l="")
    rows, receipt = build_census([meta["D"]], data_root=root)
    r = _row_at(rows, meta["T_iso"])
    assert r["status"] == "EXCL_EV_MISSING"
    assert "EXCL_EV_MISSING" in receipt["exclusion_inventory"]


def test_A3_5_unequal_expiration_value_hard_fails(corpus):
    # A3.5: a present-but-UNEQUAL print across legs (Decimal compare) remains a hard fail.
    root, meta = corpus(ev_l="60201.00")   # 15M print is 60200.00
    with pytest.raises(IntegrityError):
        build_census([meta["D"]], data_root=root)


def test_A3_5_decimal_equal_but_string_different_is_ok(corpus):
    # A3.5: equality is compared as Decimal, so "60200.00" == "60200.0000" does NOT fail.
    root, meta = corpus(ev_l="60200.0000")
    rows, receipt = build_census([meta["D"]], data_root=root)
    r = _row_at(rows, meta["T_iso"])
    assert r["status"] == "OK"
