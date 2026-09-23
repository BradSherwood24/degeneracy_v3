"""V3.3 window process (L2): mode resolution (missing -> dry, never armed by default; armed requires
the file), the params-sha fail-closed stand-down, and the DRY ladder-fill SIMULATION replayed on the
2026-09-20T04:00Z golden fixture -- 11 dry_sim fills with the study locks, comparable ledger row.
FAKES ONLY; no network, no proxy, no sealed/holdout read."""

from __future__ import annotations

import json
import os
from dataclasses import replace as dr
from decimal import Decimal

import pytest

from service.book import TopOfBook
from service.v33 import V33State, load_v33_params
from service.v33.core import lock_value
from service.v33.ledger import compute_ladder_money_math, load_v33_rows
from service.run_v32 import FrozenExecutor
import service.run_v33 as RUN

_HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(_HERE, "fixtures", "v33", "golden_20260920T040000Z.json")
_ONE = Decimal(1)
_CENT = Decimal("0.01")
BK = {"KXBTC-RANGE-B80400": (80400.0, 80499.99), "KXBTC-RANGE-B80500": (80500.0, 80599.99)}
B_SD = "KXBTC-RANGE-B80400"
STK_SD = "KXBTCD-26SEP2000-T80399.99"
STK_SU = "KXBTCD-26SEP2000-T80499.99"
SWEEP_W = Decimal("1.4737")


class J:
    def __init__(self):
        self.recs = []

    def append(self, k, o, t):
        self.recs.append((k, o))

    def kinds(self):
        return [k for k, _ in self.recs]


def _top(bid, ask):
    yb, ya = Decimal(bid), Decimal(ask)
    return TopOfBook(yes_bid=yb, yes_bid_size=Decimal(100), yes_ask=ya, yes_ask_size=Decimal(100),
                     no_bid=_ONE - ya, no_bid_size=Decimal(100), no_ask=_ONE - yb,
                     no_ask_size=Decimal(100), suspect=False)


def _load_fixture():
    if not os.path.exists(FIXTURE):
        pytest.skip("v33 golden fixture absent")
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# mode resolution
# ---------------------------------------------------------------------------
def test_mode_missing_file_defaults_to_dry():
    assert RUN.resolve_v33_mode(None, "/no/such/v33_mode.txt") == "dry"


def test_mode_unknown_content_defaults_to_dry(tmp_path):
    p = tmp_path / "v33_mode.txt"
    p.write_text("garbage\n", encoding="utf-8")
    assert RUN.resolve_v33_mode(None, str(p)) == "dry"


def test_mode_armed_requires_the_file(tmp_path):
    p = tmp_path / "v33_mode.txt"
    p.write_text("armed\n", encoding="utf-8")
    assert RUN.resolve_v33_mode(None, str(p)) == "armed"


def test_mode_cli_overrides_file(tmp_path):
    p = tmp_path / "v33_mode.txt"
    p.write_text("armed\n", encoding="utf-8")
    assert RUN.resolve_v33_mode("dry", str(p)) == "dry"


# ---------------------------------------------------------------------------
# params-sha fail-closed stand-down (no network reached)
# ---------------------------------------------------------------------------
def test_params_sha_mismatch_stands_down(monkeypatch, tmp_path):
    from service.v33 import V33ParamsShaMismatch
    jdir = tmp_path / "journals_v33"
    jdir.mkdir()
    ledger = tmp_path / "v33_ledger.jsonl"

    def _boom(*a, **k):
        raise V33ParamsShaMismatch("tampered")

    monkeypatch.setattr(RUN, "load_v33_params", _boom)
    rc = RUN.main(["--mode", "dry", "--close", "2026-09-20T04:00:00Z",
                   "--journal-dir", str(jdir), "--ledger", str(ledger)])
    assert rc == 0
    rows = load_v33_rows(str(ledger))
    assert len(rows) == 1 and rows[0]["stand_down"] is True
    assert "params_load_failed" in str(rows[0]["stand_down_reason"])
    assert rows[0]["roster"] == "DegeneracyV3_3"


# ---------------------------------------------------------------------------
# the DRY ladder-fill simulation (golden replay through the real V33Driver)
# ---------------------------------------------------------------------------
def _dry_driver():
    fix = _load_fixture()
    p = dr(load_v33_params(), tol=Decimal("0.01"), deb_ms=0,
           freshness_max_age_s=3600.0, bucket_freshness_max_age_s=3600.0)
    cts = fix["close_epoch"]
    st = V33State.new(fix["close_time"], cts, BK, p, shakedown=True)  # DRY -> WOULD_* twins
    drv = RUN.V33Driver(p, st, J(), FrozenExecutor(BK), dry_sim=True, clock=lambda: 0.0)
    return fix, p, cts, drv


def _bring_up(drv, cts):
    t0 = cts - 900 + 1
    for m, tb in ((B_SD, ("0.10", "0.11")), (STK_SU, ("0.10", "0.11")), (STK_SD, ("0.54", "0.55"))):
        drv.on_book_update(m, _top(*tb), t0)


def test_dry_run_places_full_ladder_via_frozen_acks():
    fix, p, cts, drv = _dry_driver()
    _bring_up(drv, cts)
    assert len(drv.state.ladder) == 11 and drv.state.n_top == Decimal("0.45")
    # nothing REAL was sent: only WOULD_* + no place_rest/take_wings/amend_rest in the journal so far.
    assert "place_rest" not in drv.journal.kinds()
    assert "would_place_rest" in drv.journal.kinds()


def test_dry_run_full_sweep_books_11_dry_sim_fills_with_study_locks():
    fix, p, cts, drv = _dry_driver()
    _bring_up(drv, cts)
    for dt, yp_s, _cnt in sorted(fix["prints"], key=lambda r: r[0]):
        drv.on_trade(B_SD, {"taker_side": "yes", "yes_price_dollars": yp_s, "count_fp": "1.00"},
                     cts + float(dt))
    assert drv._dry_sim_fills == 11 and drv.state.rungs_filled == 11 and drv.state.ladder == ()
    drv.on_clock_tick(cts - 400)  # flush the coalesce groups + take (synthetic) wings
    m = compute_ladder_money_math(drv.state, dry_sim=True)
    assert m["dry_sim"] is True and len(m["rung_fills"]) == 11
    # solved per-rung locks match the study for the 10 shared rungs, deeper on the distinct 11th.
    locks = {Decimal(r["price"]): Decimal(r["lock_solved"]) for r in m["rung_fills"]}
    study = fix["study_rungs"]
    for E in range(5, 15):
        n = Decimal(str(study[str(E)]["n"]))
        assert locks[n] == Decimal(str(study[str(E)]["lock"]))
    assert locks[Decimal("0.45")] == Decimal("0.0589")
    assert locks[Decimal("0.35")] == Decimal("0.1603")   # the documented deeper 11th rung
    # two coalesced wing batches (3 + 8), 11 lots taken.
    assert sorted(int(b["fill_count"]) for b in m["wing_batch_sets"]) == [3, 8]


def test_dry_run_sends_nothing_only_would_and_sim_records():
    fix, p, cts, drv = _dry_driver()
    _bring_up(drv, cts)
    for dt, yp_s, _cnt in sorted(fix["prints"], key=lambda r: r[0]):
        drv.on_trade(B_SD, {"taker_side": "yes", "yes_price_dollars": yp_s, "count_fp": "1.00"},
                     cts + float(dt))
    drv.on_clock_tick(cts - 400)
    kinds = set(drv.journal.kinds())
    # DRY: only WOULD_* + dry_sim_fill; NEVER a real place/amend/cancel/take.
    assert "dry_sim_fill" in kinds
    for real in ("place_rest", "amend_rest", "cancel_rest", "take_wings", "retry_wing"):
        assert real not in kinds, f"dry run emitted a REAL {real}"


def test_dry_sim_ignores_no_side_taker_and_non_bucket_trades():
    fix, p, cts, drv = _dry_driver()
    _bring_up(drv, cts)
    # a NO-side taker never lifts our NO offer; a print on a strike ticker is not the ladder's bucket.
    drv.on_trade(B_SD, {"taker_side": "no", "yes_price_dollars": "0.99", "count_fp": "5.00"}, cts - 500)
    drv.on_trade(STK_SD, {"taker_side": "yes", "yes_price_dollars": "0.99", "count_fp": "5.00"}, cts - 500)
    assert drv._dry_sim_fills == 0 and drv.state.rungs_filled == 0


def test_o1_partial_sweep_does_not_latch_allotment_until_full():
    """O-1 (reviewer R4): a partially-swept ladder must NOT latch the allotment early -- the surviving
    rungs stay live until every rung fills (or max_sets). A shallow print fills 3, 8 survive, allotment
    open; a deep print fills the rest -> allotment latched."""
    fix, p, cts, drv = _dry_driver()
    _bring_up(drv, cts)
    drv.on_trade(B_SD, {"taker_side": "yes", "yes_price_dollars": "0.57", "count_fp": "1.00"}, cts - 500)
    assert drv.state.rungs_filled == 3 and len(drv.state.ladder) == 8
    assert not drv.state.rest_allotment_done   # NOT latched with 8 rungs still live
    drv.on_trade(B_SD, {"taker_side": "yes", "yes_price_dollars": "0.99", "count_fp": "1.00"}, cts - 490)
    assert drv.state.rungs_filled == 11 and drv.state.ladder == ()
    assert drv.state.rest_allotment_done


def test_close_time_flush_takes_wings_for_last_coalesce_group():
    """NIT-3: the core FLUSHES the open coalesce group and TAKES its wings by close, so no rung is left
    un-batched (un-hedged) in the money math. A fill opens a coalesce group; a clock tick past
    wing_coalesce_ms flushes it into a WingBatch and the (synthetic) wings are taken."""
    fix, p, cts, drv = _dry_driver()
    _bring_up(drv, cts)
    drv.on_trade(B_SD, {"taker_side": "yes", "yes_price_dollars": "0.55", "count_fp": "1.00"}, cts - 500)
    assert drv.state.coalesce_open is not None and drv.state.wing_batches == ()
    drv.on_clock_tick(cts - 498)   # 2 s later >> 150 ms wing_coalesce_ms
    assert drv.state.coalesce_open is None and len(drv.state.wing_batches) == 1
    m = compute_ladder_money_math(drv.state, dry_sim=True)
    assert Decimal(m["floor_booked"]) > 0    # the flushed+hedged batch books a real floor, not naked


def test_dry_convergence_rolls_via_frozen_amend():
    """A W move in dry cycles the convergence through the FrozenExecutor's synthetic amend (would_amend
    -> synth OrderAmended -> the moved order re-labels). No real order is sent."""
    fix, p, cts, drv = _dry_driver()
    _bring_up(drv, cts)
    top0 = drv.state.n_top
    # lower ya (STK_SD ask) by 1c -> lower W -> n_top rises 1c -> one rung rolls (Brad's one-order roll).
    drv.on_book_update(STK_SD, _top("0.53", "0.54"), cts - 800)
    assert drv.state.n_top == top0 + _CENT
    assert "would_amend_rest" in drv.journal.kinds()
    assert drv.state.roll_count >= 1
    # still nothing real sent.
    assert "amend_rest" not in drv.journal.kinds()
