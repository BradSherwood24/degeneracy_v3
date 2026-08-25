"""Fee goldens as MANDATORY literals (commission section 5, 2026-08-18 fills)."""
from decimal import Decimal

from census import fee


def test_fee_goldens_literal():
    assert fee(Decimal("0.57")) == Decimal("0.0172")
    assert fee(Decimal("0.46")) == Decimal("0.0174")
    assert fee(Decimal("0.24")) == Decimal("0.0128")
    assert fee(Decimal("0.11")) == Decimal("0.0069")


def test_fee_accepts_float_and_str():
    assert fee(0.57) == Decimal("0.0172")
    assert fee("0.46") == Decimal("0.0174")


def test_fee_symmetric_and_ceils():
    # p and (1-p) give identical p(1-p); ceiling to 4 dp.
    assert fee(Decimal("0.57")) == fee(Decimal("0.43"))
    assert fee(Decimal("0.50")) == Decimal("0.0175")  # 0.07*0.25=0.0175 exact -> 0.0175
