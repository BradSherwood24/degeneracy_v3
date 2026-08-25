"""Pin/escape truth table (A2.3), including EXACT-BOUNDARY prints in BOTH orientations.

We buy YES on the H-line market, NO on the L-line market.
  PIN     <=> H:no  AND L:yes   (both legs lose; print inside the corridor)
  ESCAPE  <=> exactly one leg pays
  H:yes AND L:no (above H and below L) is IMPOSSIBLE -> hard fail.
"""
import pytest

from census import IntegrityError, classify_outcome, recompute_result


def test_truth_table_all_four_combos():
    assert classify_outcome("no", "yes") == "PIN"       # both lose
    assert classify_outcome("yes", "yes") == "ESCAPE"   # YES-on-H pays
    assert classify_outcome("no", "no") == "ESCAPE"     # NO-on-L pays
    with pytest.raises(IntegrityError):
        classify_outcome("yes", "no")                   # impossible


def test_recompute_result_semantics():
    # 15M greater_or_equal: yes iff print >= strike
    assert recompute_result("greater_or_equal", "100.00", 100.00) == "yes"
    assert recompute_result("greater_or_equal", "99.99", 100.00) == "no"
    # 1H greater: yes iff print > strike
    assert recompute_result("greater", "100.00", 100.00) == "no"
    assert recompute_result("greater", "100.01", 100.00) == "yes"


def test_exact_boundary_orientation_A_above_K():
    # A>K: H = 15M(>=) at A, L = 1H(>) at K, with A=100.00, K=50.00.
    A, K = 100.00, 50.00
    # print exactly on H (=A): H(>=)=yes, L(>): 100>50 => yes  -> ESCAPE
    h = recompute_result("greater_or_equal", "100.00", A)
    l = recompute_result("greater", "100.00", K)
    assert (h, l) == ("yes", "yes")
    assert classify_outcome(h, l) == "ESCAPE"
    # print exactly on L (=K): H(>=): 50>=100 => no, L(>): 50>50 => no -> ESCAPE
    h = recompute_result("greater_or_equal", "50.00", A)
    l = recompute_result("greater", "50.00", K)
    assert (h, l) == ("no", "no")
    assert classify_outcome(h, l) == "ESCAPE"


def test_exact_boundary_orientation_K_above_A():
    # K>A: H = 1H(>) at K, L = 15M(>=) at A, with K=100.00, A=50.00.
    K, A = 100.00, 50.00
    # print exactly on H (=K): H(>): 100>100 => no, L(>=): 100>=50 => yes -> PIN
    h = recompute_result("greater", "100.00", K)
    l = recompute_result("greater_or_equal", "100.00", A)
    assert (h, l) == ("no", "yes")
    assert classify_outcome(h, l) == "PIN"
    # print exactly on L (=A): H(>): 50>100 => no, L(>=): 50>=50 => yes -> PIN
    h = recompute_result("greater", "50.00", K)
    l = recompute_result("greater_or_equal", "50.00", A)
    assert (h, l) == ("no", "yes")
    assert classify_outcome(h, l) == "PIN"


def test_unexpected_strike_type_hard_fails():
    with pytest.raises(IntegrityError):
        recompute_result("less_than", "1.0", 1.0)
