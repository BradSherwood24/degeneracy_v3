"""V3.2 stops / arming gate (Phase 3). FAKES ONLY — no network, no proxy, no key/holdout read.

Covers the S5 matrix (frozen falsifier + params sha + /health caps & budget, incl. the startswith
prefix coverage and an absent falsifier), reconcile-first, the banded S4, the S1_LEGGED day latch, the
combined ``decide_v32_arming`` gate, and that the V3.2 day-guard file is SEPARATE from the box's."""

from __future__ import annotations

import os
from decimal import Decimal

from service.stops import DayGuard, S4Decision, read_day_guard
from service.v32.stops import (
    V32_MAX_CONTRACTS_PER_ORDER,
    V32_MIN_ORDER_BUDGET_AT_ARM,
    V32_S1_LEGGED_LATCH_THRESHOLD,
    V32_S4_DAY_LOSS_CAP_DOLLARS,
    count_legged,
    decide_v32_arming,
    reconcile_positions_clean,
    record_legged_occurrence,
    v32_arming_check,
    v32_caps_agree,
    v32_day_guard_path,
    v32_latched_stop_kind,
    v32_s4_decision,
)

FROZEN = "STATUS: FROZEN\n"


def _falsifier(tmp_path, text=FROZEN):
    p = os.path.join(tmp_path, "v32_falsifier.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


def _health(*, enabled=True, maxc=2, prefixes=("KXBTC", "KXBTC15M"), remaining=4000):
    return {"orders_enabled": enabled,
            "caps": {"max_contracts_per_order": maxc, "ticker_prefixes": list(prefixes),
                     "daily_order_budget": 4000},
            "orders_remaining_today": remaining}


# ---------------------------------------------------------------------------
# caps agreement (the startswith prefix coverage the proxy uses)
# ---------------------------------------------------------------------------
def test_caps_agree_single_kxbtc_prefix_covers_both_series():
    ok, why = v32_caps_agree(_health(prefixes=("KXBTC",)), contracts=1)
    assert ok, why  # "KXBTC" covers both KXBTC- (range) and KXBTCD- (strikes) via startswith


def test_caps_reject_prefix_missing_range_series():
    ok, why = v32_caps_agree(_health(prefixes=("KXBTCD", "KXBTC15M")), contracts=1)
    # KXBTCD covers strikes but NOT a KXBTC- range ticker -> refuse
    assert not ok and "KXBTC-" in why


def test_caps_reject_max_contracts_below_params():
    ok, why = v32_caps_agree(_health(maxc=0), contracts=1)
    assert not ok and "max_contracts_per_order" in why


def test_caps_reject_max_contracts_above_ceiling():
    ok, why = v32_caps_agree(_health(maxc=V32_MAX_CONTRACTS_PER_ORDER + 1), contracts=1)
    assert not ok and "ceiling" in why


def test_caps_reject_budget_below_min():
    ok, why = v32_caps_agree(_health(remaining=V32_MIN_ORDER_BUDGET_AT_ARM - 1), contracts=1)
    assert not ok and "orders_remaining_today" in why


def test_caps_reject_orders_disabled():
    ok, why = v32_caps_agree(_health(enabled=False), contracts=1)
    assert not ok and "orders_enabled" in why


# ---------------------------------------------------------------------------
# S5 arming check (falsifier + params sha + caps)
# ---------------------------------------------------------------------------
def test_arming_ok_when_all_pass(tmp_path):
    d = v32_arming_check(_falsifier(tmp_path), _health(), params_verified=True, contracts=1)
    assert d.armed and d.reasons == ()


def test_arming_refused_when_falsifier_absent(tmp_path):
    absent = os.path.join(tmp_path, "no_such.md")
    d = v32_arming_check(absent, _health(), params_verified=True, contracts=1)
    assert not d.armed and any("FROZEN" in r for r in d.reasons)


def test_arming_refused_when_falsifier_not_frozen(tmp_path):
    p = _falsifier(tmp_path, "STATUS: DRAFT\n")
    d = v32_arming_check(p, _health(), params_verified=True, contracts=1)
    assert not d.armed and any("FROZEN" in r for r in d.reasons)


def test_arming_refused_when_params_unverified(tmp_path):
    d = v32_arming_check(_falsifier(tmp_path), _health(), params_verified=False, contracts=1)
    assert not d.armed and any("params sha" in r for r in d.reasons)


# ---------------------------------------------------------------------------
# reconcile-first
# ---------------------------------------------------------------------------
def test_reconcile_clean_when_flat():
    clean, _ = reconcile_positions_clean({"market_positions": [
        {"ticker": "KXBTC-26SEP1316-B68200", "position": 0},
        {"ticker": "KXETH-1", "position": 5},   # not ours-series, ignored
    ]})
    assert clean


def test_reconcile_refuses_inherited_kxbtc_position():
    clean, why = reconcile_positions_clean({"market_positions": [
        {"ticker": "KXBTCD-26SEP1316-T68199.99", "position": -1}]})
    assert not clean and "inherited" in why


def test_reconcile_fail_closed_on_missing_payload():
    assert reconcile_positions_clean(None)[0] is False
    assert reconcile_positions_clean({"nope": 1})[0] is False


# ---------------------------------------------------------------------------
# S1_LEGGED day latch (SEPARATE v32 day guard) + latched-kind derivation
# ---------------------------------------------------------------------------
def test_day_guard_path_is_separate_from_box():
    p = v32_day_guard_path("/ops", "2026-09-13")
    assert p.endswith("v32_stops_2026-09-13.json")
    assert "stops_2026-09-13.json" != os.path.basename(p)  # not the box's plain stops_ file


def test_s1_legged_latches_the_day_at_threshold(tmp_path):
    path = v32_day_guard_path(str(tmp_path), "2026-09-13")
    # one occurrence: hour stood down, day NOT yet latched
    n1 = record_legged_occurrence(path, "2026-09-13", "w1", "one-legged", 1.0)
    assert n1 == 1
    assert v32_latched_stop_kind(read_day_guard(path, "2026-09-13")) is None
    # second occurrence reaches the threshold -> the DAY latches S1_LEGGED
    n2 = record_legged_occurrence(path, "2026-09-13", "w2", "one-legged", 2.0)
    assert n2 == V32_S1_LEGGED_LATCH_THRESHOLD == 2
    assert v32_latched_stop_kind(read_day_guard(path, "2026-09-13")) == "S1_LEGGED"


def test_count_legged_ignores_other_kinds():
    g = DayGuard(utc_day="2026-09-13", latched=(
        {"kind": "S4"}, {"kind": "S1_LEGGED"}, {"kind": "OTHER"}))
    assert count_legged(g) == 1
    assert v32_latched_stop_kind(g) == "S4"  # S4 (day-halting) wins immediately


# ---------------------------------------------------------------------------
# The combined arm-or-degrade gate
# ---------------------------------------------------------------------------
def _clean_guard():
    return DayGuard(utc_day="2026-09-13", latched=())


def test_decide_arming_arms_when_everything_passes(tmp_path):
    out = decide_v32_arming(
        resolved_mode="armed", falsifier_path=_falsifier(tmp_path), health=_health(),
        positions={"market_positions": []}, params_verified=True, contracts=1,
        day_guard=_clean_guard(), s4=S4Decision("clear", Decimal(0), Decimal(0)),
    )
    assert out.armed and out.effective_mode == "armed" and out.degrade_reason is None


# ---------------------------------------------------------------------------
# S4 floor-netting RULING (Phase 4): v32_s4_decision consumes the (pess, opt) credit band and nets the
# guaranteed floor into BOTH bounds; only the upside separates them.
# ---------------------------------------------------------------------------
def test_s4_band_clears_when_guaranteed_floor_covers_the_dip():
    # two complete pins unsettled: $4 cash dip, but each pays $2 at EVERY settlement (band (4, 4)).
    # Netting the guaranteed floor into the pessimistic bound rescues it: loss_pess = 100-(96+4)=0.
    d = v32_s4_decision(Decimal("100.00"), Decimal("96.00"), (Decimal(4), Decimal(4)))
    assert d.kind == "clear"
    assert d.loss_pessimistic == Decimal("0.00") and d.loss_optimistic == Decimal("0.00")


def test_s4_band_latches_when_breached_even_after_best_case_credit():
    # one complete pin unsettled (band (2, 2)); cash down $5.50. Even crediting the guaranteed $2,
    # loss_opt = 100-(94.50+2) = 3.50 >= cap 3.00 -> a REAL day loss -> latch.
    assert V32_S4_DAY_LOSS_CAP_DOLLARS == Decimal("3.00")
    d = v32_s4_decision(Decimal("100.00"), Decimal("94.50"), (Decimal(2), Decimal(2)))
    assert d.kind == "latch" and d.loss_optimistic == Decimal("3.50")


def test_s4_band_pending_when_breach_turns_on_the_upside():
    # one 2-leg subset unsettled (band (1, 2)); cash down $4. The breach depends on the unsettled
    # upside: loss_pess = 100-(96+1)=3.00 (not < cap) and loss_opt = 100-(96+2)=2.00 (< cap) -> pending.
    d = v32_s4_decision(Decimal("100.00"), Decimal("96.00"), (Decimal(1), Decimal(2)))
    assert d.kind == "pending"
    assert d.loss_pessimistic == Decimal("3.00") and d.loss_optimistic == Decimal("2.00")


def test_s4_band_lone_leg_upside_not_credited_to_pessimistic():
    # a lone leg band is (0, 1): the guaranteed floor is $0, so a lone-leg dip is NOT rescued.
    # cash down $3.20; loss_pess = start-(now+0) = 3.20 >= cap and loss_opt = start-(now+1) = 2.20 < cap
    # -> pending (the $1 upside can still resolve to $0, so it never clears the pessimistic bound).
    d = v32_s4_decision(Decimal("100.00"), Decimal("96.80"), (Decimal(0), Decimal(1)))
    assert d.kind == "pending"
    assert d.loss_pessimistic == Decimal("3.20") and d.loss_optimistic == Decimal("2.20")


def test_decide_arming_stands_down_on_s4_pending_without_latch(tmp_path):
    # RULING: a pending S4 stands the window down (degrade to dry) but writes NO day-guard latch.
    out = decide_v32_arming(
        resolved_mode="armed", falsifier_path=_falsifier(tmp_path), health=_health(),
        positions={"market_positions": []}, params_verified=True, contracts=1,
        day_guard=_clean_guard(), s4=S4Decision("pending", Decimal("3.0"), Decimal("2.0")),
    )
    assert not out.armed and out.effective_mode == "dry"
    assert any("S4" in r and "pending" in r for r in out.reasons)


def test_decide_arming_degrades_on_s4_latch(tmp_path):
    out = decide_v32_arming(
        resolved_mode="armed", falsifier_path=_falsifier(tmp_path), health=_health(),
        positions={"market_positions": []}, params_verified=True, contracts=1,
        day_guard=_clean_guard(), s4=S4Decision("latch", Decimal("3.5"), Decimal("3.5")),
    )
    assert not out.armed and out.effective_mode == "dry"
    assert out.degrade_reason == "degrade_to_dry" and any("S4" in r for r in out.reasons)


def test_decide_arming_degrades_on_corrupt_guard(tmp_path):
    out = decide_v32_arming(
        resolved_mode="armed", falsifier_path=_falsifier(tmp_path), health=_health(),
        positions={"market_positions": []}, params_verified=True, contracts=1,
        day_guard=DayGuard(utc_day="2026-09-13", corrupt=True, exists=True), s4=None,
    )
    assert not out.armed and any("corrupt" in r for r in out.reasons)


def test_decide_arming_degrades_on_inherited_position(tmp_path):
    out = decide_v32_arming(
        resolved_mode="armed", falsifier_path=_falsifier(tmp_path), health=_health(),
        positions={"market_positions": [{"ticker": "KXBTC-x", "position": 1}]},
        params_verified=True, contracts=1, day_guard=_clean_guard(), s4=None,
    )
    assert not out.armed and any("reconcile" in r for r in out.reasons)


def test_decide_arming_passthrough_when_not_armed(tmp_path):
    for m in ("dry", "shakedown"):
        out = decide_v32_arming(
            resolved_mode=m, falsifier_path="x", health={}, positions=None, params_verified=False,
            contracts=1, day_guard=_clean_guard(), s4=None)
        assert out.effective_mode == m and not out.armed and out.degrade_reason is None
