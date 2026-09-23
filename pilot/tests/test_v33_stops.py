"""V3.3 stops / arming gate (L2): the SEPARATE v33 day-guard file, S4 $3.00 cap banding, S1_LEGGED
occurrence latch, S5 caps agreement, reconcile-first reuse, and the one arm-or-degrade decision."""

from __future__ import annotations

import os
from decimal import Decimal

from service.stops import DayGuard, read_day_guard
from service.v33.stops import (
    V33_S1_LEGGED_LATCH_THRESHOLD,
    V33_S4_DAY_LOSS_CAP_DOLLARS,
    count_legged,
    decide_v33_arming,
    record_legged_occurrence,
    reconcile_positions_clean,
    v33_arming_check,
    v33_caps_agree,
    v33_day_guard_path,
    v33_latched_stop_kind,
    v33_s4_decision,
)

DAY = "2026-09-20"


def _health(ok=True, maxc=2, remaining=8000, prefixes=("KXBTC",)):
    return {"orders_enabled": ok, "orders_remaining_today": remaining,
            "caps": {"max_contracts_per_order": maxc, "ticker_prefixes": list(prefixes)}}


def test_day_guard_path_is_v33_prefixed():
    p = v33_day_guard_path("/ops", DAY)
    assert p.endswith(os.path.join("/ops", "v33_stops_2026-09-20.json").replace("/", os.sep)) \
        or p.endswith("v33_stops_2026-09-20.json")
    assert "v33_stops_" in p and "v32_stops_" not in p


def test_caps_agree_ok_cap_two():
    # MUST-FIX-1: proxy cap 2 (wings chunk to ceil(K/2) IOC takes) is ACCEPTED at K=11.
    ok, why = v33_caps_agree(_health(maxc=2), lots_per_rung=1, k_rungs=11)
    assert ok, why


def test_caps_agree_ok_cap_eleven():
    # MUST-FIX-1: proxy cap 11 (wings go as 2 orders) is ALSO accepted (== K*lots_per_rung ceiling).
    ok, why = v33_caps_agree(_health(maxc=11), lots_per_rung=1, k_rungs=11)
    assert ok, why


def test_caps_reject_over_ceiling():
    # cap above K*lots_per_rung (11) removes the one-lot-per-rung guard -> refuse.
    ok, why = v33_caps_agree(_health(maxc=12), lots_per_rung=1, k_rungs=11)
    assert not ok and "ceiling" in why


def test_caps_reject_below_lots_per_rung():
    ok, why = v33_caps_agree(_health(maxc=0), lots_per_rung=1, k_rungs=11)
    assert not ok and "lots_per_rung" in why


def test_caps_reject_low_budget():
    ok, why = v33_caps_agree(_health(remaining=100), lots_per_rung=1, k_rungs=11)
    assert not ok and "orders_remaining_today" in why


def test_caps_reject_missing_prefix():
    ok, why = v33_caps_agree(_health(prefixes=("KXETH",)), lots_per_rung=1, k_rungs=11)
    assert not ok and "prefixes" in why


def test_s4_cap_is_three_dollars():
    assert V33_S4_DAY_LOSS_CAP_DOLLARS == Decimal("3.00")


def test_s4_latch_when_loss_exceeds_cap_under_every_resolution():
    # start 54, now 50 -> loss 4 > cap 3 with no pending credit -> latch.
    d = v33_s4_decision(Decimal("54"), Decimal("50"), (Decimal(0), Decimal(0)))
    assert d.kind == "latch"


def test_s4_clear_when_loss_within_cap():
    d = v33_s4_decision(Decimal("54"), Decimal("52.5"), (Decimal(0), Decimal(0)))
    assert d.kind == "clear"


def test_s4_pending_when_credit_straddles_cap():
    # loss 4 now, but a pending floor of $2 would bring it to $2 (< cap) at best, $4 at worst -> pending.
    d = v33_s4_decision(Decimal("54"), Decimal("50"), (Decimal(0), Decimal(2)))
    assert d.kind == "pending"


def test_s1_legged_latch_threshold(tmp_path):
    path = v33_day_guard_path(str(tmp_path), DAY)
    n1 = record_legged_occurrence(path, DAY, "w1", "legged", ts=0.0)
    assert n1 == 1
    n2 = record_legged_occurrence(path, DAY, "w2", "legged", ts=1.0)
    assert n2 == V33_S1_LEGGED_LATCH_THRESHOLD == 2
    guard = read_day_guard(path, DAY)
    assert count_legged(guard) == 2
    assert v33_latched_stop_kind(guard) == "S1_LEGGED"


def test_reconcile_clean_and_dirty():
    clean, _ = reconcile_positions_clean({"market_positions": []})
    assert clean
    dirty, detail = reconcile_positions_clean(
        {"market_positions": [{"ticker": "KXBTC-X", "position": 3}]})
    assert not dirty and "KXBTC" in detail


def test_arming_check_ok(tmp_path):
    fpath = tmp_path / "v33_falsifier.md"
    fpath.write_text("STATUS: FROZEN\n", encoding="utf-8")
    dec = v33_arming_check(str(fpath), _health(), params_verified=True, lots_per_rung=1, k_rungs=11)
    assert dec.armed and dec.reasons == ()


def test_arming_check_refuses_unfrozen_falsifier(tmp_path):
    fpath = tmp_path / "v33_falsifier.md"
    fpath.write_text("STATUS: DRAFT\n", encoding="utf-8")
    dec = v33_arming_check(str(fpath), _health(), params_verified=True, lots_per_rung=1, k_rungs=11)
    assert not dec.armed and any("STATUS" in r for r in dec.reasons)


def test_decide_arming_dry_passes_through():
    out = decide_v33_arming(resolved_mode="dry", falsifier_path="/none", health={}, positions=None,
                            params_verified=True, lots_per_rung=1, k_rungs=11, day_guard=DayGuard(utc_day=DAY))
    assert out.effective_mode == "dry" and not out.armed and out.degrade_reason is None


def test_decide_arming_all_gates_pass(tmp_path):
    fpath = tmp_path / "v33_falsifier.md"
    fpath.write_text("STATUS: FROZEN\n", encoding="utf-8")

    class _S4:
        kind = "clear"

    out = decide_v33_arming(resolved_mode="armed", falsifier_path=str(fpath), health=_health(),
                            positions={"market_positions": []}, params_verified=True, lots_per_rung=1,
                            day_guard=DayGuard(utc_day=DAY), s4=_S4(), k_rungs=11)
    assert out.armed and out.effective_mode == "armed"


def test_decide_arming_degrades_on_s4_latch(tmp_path):
    fpath = tmp_path / "v33_falsifier.md"
    fpath.write_text("STATUS: FROZEN\n", encoding="utf-8")

    class _S4:
        kind = "latch"

    out = decide_v33_arming(resolved_mode="armed", falsifier_path=str(fpath), health=_health(),
                            positions={"market_positions": []}, params_verified=True, lots_per_rung=1,
                            day_guard=DayGuard(utc_day=DAY), s4=_S4(), k_rungs=11)
    assert not out.armed and out.effective_mode == "dry" and "S4" in "; ".join(out.reasons)


def test_decide_arming_degrades_on_dirty_positions(tmp_path):
    fpath = tmp_path / "v33_falsifier.md"
    fpath.write_text("STATUS: FROZEN\n", encoding="utf-8")
    out = decide_v33_arming(resolved_mode="armed", falsifier_path=str(fpath), health=_health(),
                            positions={"market_positions": [{"ticker": "KXBTC-X", "position": 1}]},
                            params_verified=True, lots_per_rung=1, k_rungs=11, day_guard=DayGuard(utc_day=DAY))
    assert not out.armed and any("reconcile" in r for r in out.reasons)
