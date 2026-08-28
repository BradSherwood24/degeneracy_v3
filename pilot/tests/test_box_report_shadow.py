"""Tests for the PRE-REGISTERED SHADOW RULE (skip implied_pin < 0.80) in service/box_report.py.

The rule is observational only — it drives no decision. These tests cover the pure function
``shadow_implied_rule`` over a hand-built window list (kept / skipped / unsettled / missing
implied_pin / one-legged-excluded), the boundary (implied_pin == 0.80 is KEPT), and the text render
(block title + the ``[shadow-skip]`` window tag). All deterministic, no I/O, no network."""

from __future__ import annotations

from decimal import Decimal

from service import box_report as br
from test_box_report import backfill_row, fire_journal, ledger_box_row, _selection


# ---------------------------------------------------------------------------
# Minimal hand-built windows for the pure function (only the fields it reads)
# ---------------------------------------------------------------------------
def _w(fill_class, implied_pin, outcome, realized, *, close_time="2026-08-28T05:00:00Z",
       realized_settled=True):
    return {
        "close_time": close_time,
        "fill_class": fill_class,
        "implied_pin": None if implied_pin is None else Decimal(implied_pin),
        "outcome": outcome,
        "realized": None if realized is None else Decimal(realized),
        "realized_settled": realized_settled,
    }


def _windows():
    return [
        _w("both", "0.88", "pinned", "0.14", close_time="2026-08-28T05:00:00Z"),        # kept
        _w("both", "0.80", "pinned", "0.10", close_time="2026-08-28T06:00:00Z"),        # kept (boundary)
        _w("both", "0.75", "not_pinned", "-0.86", close_time="2026-08-28T07:00:00Z"),   # skipped, miss
        _w("both", "0.70", "unsettled", None, close_time="2026-08-28T08:00:00Z",
           realized_settled=False),                                                     # skipped, unsettled
        _w("both", None, "pinned", "0.10", close_time="2026-08-28T09:00:00Z"),          # unknown
        _w("one-legged", "0.60", "flattened", "-0.40", close_time="2026-08-28T10:00:00Z"),  # excluded
        _w("none", "0.50", "n/a", None, close_time="2026-08-28T11:00:00Z"),             # excluded
    ]


# ---------------------------------------------------------------------------
# (a) pure-function tests
# ---------------------------------------------------------------------------
def test_shadow_partition_counts_exclude_non_two_leg():
    sr = br.shadow_implied_rule(_windows())
    # only fill_class == "both" enters (5 of the 7 windows)
    assert sr["all"]["n"] == 5
    assert sr["kept"]["n"] == 2
    assert sr["skipped"]["n"] == 2
    assert sr["unknown"]["n"] == 1


def test_shadow_kept_stats_settled_only():
    sr = br.shadow_implied_rule(_windows())
    kept = sr["kept"]
    assert kept["n_settled"] == 2
    assert kept["pins"] == 2 and kept["misses"] == 0 and kept["unsettled"] == 0
    assert kept["realized_sum"] == Decimal("0.24")          # 0.14 + 0.10
    assert kept["mean_realized_per_fill"] == Decimal("0.12")
    assert kept["pin_rate"] == Decimal("1")


def test_shadow_skipped_stats_settled_only_ignores_unsettled():
    sr = br.shadow_implied_rule(_windows())
    skip = sr["skipped"]
    assert skip["n"] == 2
    assert skip["n_settled"] == 1                            # the unsettled skip is excluded from PnL
    assert skip["pins"] == 0 and skip["misses"] == 1 and skip["unsettled"] == 1
    assert skip["realized_sum"] == Decimal("-0.86")
    assert skip["mean_realized_per_fill"] == Decimal("-0.86")
    assert skip["pin_rate"] == Decimal("0")


def test_shadow_unknown_group_holds_missing_implied_pin():
    sr = br.shadow_implied_rule(_windows())
    assert sr["unknown"]["n"] == 1
    assert sr["unknown"]["pins"] == 1
    assert sr["unknown"]["realized_sum"] == Decimal("0.10")


def test_shadow_all_is_two_leg_total_and_share():
    sr = br.shadow_implied_rule(_windows())
    assert sr["all"]["n"] == 5
    assert sr["all"]["realized_sum"] == Decimal("0.24") + Decimal("-0.86") + Decimal("0.10")
    # all = 3 pins + 1 miss + 1 unsettled -> pin_rate over SETTLED only (review nit #1)
    assert sr["all"]["n_settled"] == 4 and sr["all"]["pin_rate"] == Decimal("0.75")
    assert sr["skipped_share"] == Decimal(2) / Decimal(5)   # 2 skipped of 5 two-leg fills
    assert sr["min_implied_pin"] == Decimal("0.80")
    assert sr["registered"] == "2026-08-28"


def test_shadow_skipped_windows_list():
    sr = br.shadow_implied_rule(_windows())
    sw = sr["skipped_windows"]
    assert len(sw) == 2
    cts = [t[0] for t in sw]
    assert "2026-08-28T07:00:00Z" in cts and "2026-08-28T08:00:00Z" in cts
    # tuple shape: (close_time, implied_pin, outcome, realized)
    row = next(t for t in sw if t[0] == "2026-08-28T07:00:00Z")
    assert row[1] == Decimal("0.75") and row[2] == "not_pinned" and row[3] == Decimal("-0.86")


def test_shadow_empty_windows_all_none():
    sr = br.shadow_implied_rule([])
    assert sr["all"]["n"] == 0
    assert sr["skipped_share"] is None
    assert sr["kept"]["mean_realized_per_fill"] is None
    assert sr["kept"]["pin_rate"] is None
    assert sr["skipped_windows"] == []


# ---------------------------------------------------------------------------
# (c) boundary: implied_pin exactly 0.80 is KEPT (not skipped)
# ---------------------------------------------------------------------------
def test_shadow_boundary_080_is_kept():
    windows = [_w("both", "0.80", "pinned", "0.10")]
    sr = br.shadow_implied_rule(windows)
    assert sr["kept"]["n"] == 1
    assert sr["skipped"]["n"] == 0
    # just below the pin is skipped
    windows2 = [_w("both", "0.7999", "not_pinned", "-0.86")]
    sr2 = br.shadow_implied_rule(windows2)
    assert sr2["kept"]["n"] == 0
    assert sr2["skipped"]["n"] == 1


# ---------------------------------------------------------------------------
# aggregate wiring: the block rides inside aggregate_block
# ---------------------------------------------------------------------------
def test_shadow_wired_into_aggregate_block():
    # aggregate_block needs full window dicts (R1-R4 read slippage etc.), so build a real report.
    ct = "2026-08-28T07:00:00Z"
    j = fire_journal(ct, selection=_selection(implied_pin="0.75"))
    led = [ledger_box_row(ct, realized_delta="-0.86"),
           backfill_row(ct, settlement_payoff="1.00", realized_delta="0.00")]
    rep = br.build_report([("x", j)], led, {})
    agg = rep["cumulative"]["aggregates"]
    assert "shadow_implied_rule" in agg
    sr = agg["shadow_implied_rule"]
    assert sr["all"]["n"] == 1 and sr["skipped"]["n"] == 1 and sr["kept"]["n"] == 0


# ---------------------------------------------------------------------------
# (b) render tests: block title + [shadow-skip] tag
# ---------------------------------------------------------------------------
def test_render_shadow_block_title_and_skip_tag():
    ct = "2026-08-28T07:00:00Z"
    # implied_pin 0.75 -> below the 0.80 pin -> tagged and counted as skipped
    j = fire_journal(ct, selection=_selection(implied_pin="0.75"))
    led = [ledger_box_row(ct, realized_delta="-0.86"),
           backfill_row(ct, settlement_payoff="1.00", realized_delta="0.00")]
    rep = br.build_report([("x", j)], led, {})
    w = rep["days"][ct[:10]]["fires"][0]
    assert w["fill_class"] == "both" and w["outcome"] == "not_pinned"
    text = br.render_text(rep)
    assert "SHADOW RULE (pre-registered 2026-08-28, observational): skip implied_pin < 0.80" in text
    assert "[shadow-skip]" in text


def test_render_no_skip_tag_above_pin():
    ct = "2026-08-28T05:00:00Z"
    j = fire_journal(ct, selection=_selection(implied_pin="0.88"))
    led = [ledger_box_row(ct, realized_delta="-0.86"),
           backfill_row(ct, settlement_payoff="2.00", realized_delta="1.00")]
    rep = br.build_report([("x", j)], led, {})
    text = br.render_text(rep)
    # the block title still renders, but no window carries the skip tag
    assert "SHADOW RULE (pre-registered 2026-08-28, observational)" in text
    assert "[shadow-skip]" not in text


# ---------------------------------------------------------------------------
# JSON serialization: Decimals -> strings, tuples -> lists (via _jsonify)
# ---------------------------------------------------------------------------
def test_shadow_jsonify_serializes_like_the_rest():
    sr = br.shadow_implied_rule(_windows())
    js = br._jsonify(sr)
    assert js["min_implied_pin"] == "0.80"
    assert js["kept"]["realized_sum"] == "0.24"
    assert isinstance(js["skipped_windows"], list)
    assert isinstance(js["skipped_windows"][0], list)       # tuple -> list
    assert js["skipped_windows"][0][3] == "-0.86" or js["skipped_windows"][0][3] is None
