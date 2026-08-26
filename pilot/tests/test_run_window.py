"""Tests for run_window: mode parsing, startup-order law, stand-down, crash-flush, register parse."""

from __future__ import annotations

import os
import shutil
import subprocess
from decimal import Decimal

import pytest

from service.executor import ExecutorConfig
from service.orders.envelope import SINGLE_CREATE_PATH
from service.ledger import (
    PURPOSE_ENTRY,
    PURPOSE_FLATTEN,
    Intent,
    IntentLeg,
    new_ledger,
    record_intent,
)
from service.pilot_ledger import append_entry, load_entries
from service.reconciler import ReconcileResult
import asyncio
import dataclasses

from service.record_window import GRACE_SECONDS
from service.run_window import (
    ANCHOR_POLL_TIMEOUT_S,
    _CONNECT_MARGIN_S,
    HarnessExecutor,
    LiveWindowRecorder,
    Plan,
    ThreadSafeJournal,
    WindowService,
    read_mode_file,
    resolve_mode,
)
from service.sigma_feed import QuintileOutcome, SigmaFeed
from service.wake import LadderCheck, Leg, WakeResult, close_epoch, ladder_check

CLOSE = "2026-08-21T22:00:00Z"

GOOD_HEALTH = {
    "orders_enabled": True,
    "caps": {"max_contracts_per_order": 2, "ticker_prefixes": ["KXBTC15M", "KXBTCD"],
             "daily_order_budget": 100},
}
NO_CAPS_HEALTH = {"orders_enabled": True}  # missing caps block


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeWake:
    def __init__(self, result):
        self._r = result

    def sweep(self, close):
        return self._r


class FakeSigma:
    def __init__(self, outcome):
        self._o = outcome
        self.assign_calls = []
        self.trailing_fetches = 0

    def fetch_trailing_15m(self, close, anchors=9, step=900):
        # PHASE A prefetch: returns a canned trailing tape (empty is fine — the canned outcome
        # below is what phase B actually uses).
        self.trailing_fetches += 1
        return []

    def assign(self, close, hourly, current_15m_markets=None, trailing_markets=None):
        # PHASE B compute: record what it was handed so tests can assert the polled current market
        # and the phase-A-prefetched trailing tape were both passed through.
        self.assign_calls.append(
            {"current": current_15m_markets, "trailing": trailing_markets, "hourly": hourly}
        )
        return self._o


class FakeEV:
    def fair_for(self, direction, quintile):
        return Decimal("0.50")


class FakeProxy:
    """Only used as the ws_client's proxy_auth holder in execute() paths (never dialed in tests)."""

    def rest_get(self, path, params=None):
        return {}

    def ws_connect_params(self):
        raise RuntimeError("no dial in tests")


def _leg(series, event, ticker, strike):
    return Leg(
        series=series, event_ticker=event, open_time="2026-08-21T21:00:00Z",
        close_time=CLOSE, window_seconds=900, market_tickers=(ticker,),
        floor_strikes=(strike,),
        markets=({"ticker": ticker, "floor_strike": strike, "close_time": CLOSE,
                  "open_time": "2026-08-21T21:00:00Z", "status": "active", "event_ticker": event},),
    )


def _wake(strangle_disabled=False, alarm=False):
    fifteen = _leg("KXBTC15M", "KXBTC15M-EV", "KXBTC15M-ANCHOR", 100000.0)
    hourly = _leg("KXBTCD", "KXBTCD-EV", "KXBTCD-100040", 100040.0)
    ladder = LadderCheck(expected_step=Decimal(100), observed_step=Decimal(100), uniform=True,
                         ok=not alarm, strangle_disabled=strangle_disabled, alarm=alarm,
                         reason="ok" if not alarm else "deviation")
    return WakeResult(close_time=CLOSE, fifteen_leg=fifteen, hourly_leg=hourly, ladder=ladder,
                      balance=None, affordable=None)


def _outcome(**kw):
    base = dict(close_time=CLOSE, ok=True, stand_down=False, quintile=0,
                high_ticker="KXBTCD-100040", low_ticker="KXBTC15M-ANCHOR", G=Decimal("40"),
                sigma_hat=50.0, strangle_disabled=False, reason="ok")
    base.update(kw)
    return QuintileOutcome(**base)


# A fixed clock AFTER the 15M open (21:00) so PHASE B polls immediately without a real sleep; the
# default anchor_fetcher returns the (strike-bearing) 15M leg markets so the anchor resolves at once.
_FIXED_NOW = float(close_epoch("2026-08-21T21:00:05Z"))


def _svc(tmp_path, cli_mode, *, wake=None, outcome=None, health=GOOD_HEALTH,
         positions_body=None, falsifier_path=None, window_driver=None, pairs=1,
         anchor_fetcher=None, clock=None, poll_sleep=None, balance=1000000):
    if positions_body is None:
        positions_body = {"market_positions": []}
    # S4 is balance-based (Brad 2026-08-26): armed/dry need a clean balance read at wake. Default a
    # healthy balance ($10,000 in cents); a fresh tmp_path guard snapshots it as the day baseline
    # (first wake -> no loss). Pass balance=None to exercise the fail-closed path.
    balance_get = None if balance is None else (lambda: {"balance": balance})
    w = wake if wake is not None else _wake()
    if anchor_fetcher is None:
        # PHASE B: default to a fetcher returning the strike-bearing co-settling 15M leg markets, so
        # the anchor resolves on the first poll (no real network, no real sleep).
        anchor_fetcher = (lambda: list(w.fifteen_leg.markets))
    return WindowService(
        close_time=CLOSE,
        cli_mode=cli_mode,
        pairs=pairs,
        proxy=FakeProxy(),
        falsifier_path=falsifier_path or _frozen_falsifier(tmp_path),
        mode_txt_path=os.path.join(tmp_path, "mode.txt"),  # absent -> shakedown if no cli_mode
        journal_dir=os.path.join(tmp_path, "journals"),
        ledger_path=os.path.join(tmp_path, "ledger", "pilot_ledger.jsonl"),
        wake_context=FakeWake(w),
        sigma_feed=FakeSigma(outcome if outcome is not None else _outcome()),
        health_get=(lambda: health),
        positions_reader=(lambda: positions_body),
        balance_get=balance_get,
        ev_curve=FakeEV(),
        window_driver=window_driver,
        anchor_fetcher=anchor_fetcher,
        clock=(clock if clock is not None else (lambda: _FIXED_NOW)),
        poll_sleep=(poll_sleep if poll_sleep is not None else (lambda d: None)),
    )


def _frozen_falsifier(tmp_path):
    p = os.path.join(tmp_path, "falsifier_frozen.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("# X\nSTATUS: FROZEN\n")
    return p


def _draft_falsifier(tmp_path):
    p = os.path.join(tmp_path, "falsifier_draft.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("# X\nSTATUS: DRAFT\n")
    return p


def _kinds(journal):
    return [r["kind"] for r in journal.records()]


# ---------------------------------------------------------------------------
# mode parsing
# ---------------------------------------------------------------------------
def test_resolve_mode_cli_wins():
    assert resolve_mode("armed", "/nonexistent") == "armed"


def test_resolve_mode_reads_file(tmp_path):
    p = os.path.join(tmp_path, "mode.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write(" dry \n")
    assert resolve_mode(None, p) == "dry"


def test_resolve_mode_unknown_is_shakedown_failclosed(tmp_path):
    p = os.path.join(tmp_path, "mode.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write("YOLO")
    assert resolve_mode(None, p) == "shakedown"


def test_resolve_mode_missing_file_is_shakedown(tmp_path):
    assert resolve_mode(None, os.path.join(tmp_path, "nope.txt")) == "shakedown"
    assert read_mode_file(os.path.join(tmp_path, "nope.txt")) == ""


# ---------------------------------------------------------------------------
# startup-order law
# ---------------------------------------------------------------------------
def test_armed_refused_on_draft_falsifier_degrades_to_dry(tmp_path):
    svc = _svc(tmp_path, "armed", falsifier_path=_draft_falsifier(tmp_path))
    plan = svc.prepare()
    assert plan.armed is False
    assert plan.degraded is True
    assert plan.effective_mode == "dry"
    assert plan.stand_down is False
    assert "degrade_to_dry" in _kinds(svc.journal)


def test_armed_degraded_on_missing_health_caps(tmp_path):
    svc = _svc(tmp_path, "armed", health=NO_CAPS_HEALTH,
               falsifier_path=_frozen_falsifier(tmp_path))
    plan = svc.prepare()
    assert plan.armed is False
    assert plan.degraded is True
    assert any("caps" in r for r in plan.arm_decision.reasons)


def test_armed_arms_when_frozen_health_ok_and_flat(tmp_path):
    svc = _svc(tmp_path, "armed", falsifier_path=_frozen_falsifier(tmp_path))
    plan = svc.prepare()  # PHASE A: arming + armed-stack build happen here
    assert plan.armed is True
    assert plan.degraded is False
    assert svc.executor is not None and svc.stops is not None
    assert plan.state is None  # PHASE A does NOT build the WindowState (anchor not yet available)
    # PHASE B: once the anchor resolves, the seeded state is armed (live FIRE, not shakedown).
    outcome = svc._resolve_anchor(plan)
    svc._apply_outcome(plan, outcome)
    assert plan.state is not None and plan.state.shakedown is False  # armed -> live FIRE state


def test_reconcile_first_refuses_on_inherited_position(tmp_path):
    body = {"market_positions": [{"ticker": "KXBTC15M-ANCHOR", "position": 1}]}
    svc = _svc(tmp_path, "armed", positions_body=body,
               falsifier_path=_frozen_falsifier(tmp_path))
    plan = svc.prepare()
    assert plan.stand_down is True
    assert plan.inherited and "KXBTC15M-ANCHOR" in plan.inherited
    assert "inherited_position_refusal" in _kinds(svc.journal)


def test_reconcile_first_ignores_unrelated_positions(tmp_path):
    body = {"market_positions": [{"ticker": "KXETH-SOMETHING", "position": 5}]}
    svc = _svc(tmp_path, "shakedown", positions_body=body)
    plan = svc.prepare()
    assert plan.stand_down is False


def test_wake_standdown_exits_clean(tmp_path):
    from service.wake import StandDown

    svc = _svc(tmp_path, "shakedown")
    svc._wake_context = FakeWake(StandDown(CLOSE, "no hourly leg co-settling"))
    plan = svc.prepare()
    assert plan.stand_down is True
    code = svc.execute(plan)
    assert code == 0
    rows = load_entries(svc.ledger_path)
    assert len(rows) == 1
    assert rows[0]["stand_down"] is True
    assert rows[0]["exit_code"] == 0


def test_sigma_standdown_exits_clean(tmp_path):
    # anchor resolves in PHASE B but the pairing/σ̂ yields no clean pair -> whole-window stand down.
    out = _outcome(ok=False, stand_down=True, quintile=None, high_ticker=None, low_ticker=None,
                   G=None, sigma_hat=None, strangle_disabled=True, reason="no pair")
    svc = _svc(tmp_path, "shakedown", outcome=out)
    plan = svc.prepare()
    assert plan.stand_down is False  # PHASE A is clean; the stand-down is decided in PHASE B
    assert svc.execute(plan) == 0    # PHASE B: anchor -> no pair -> stand down
    assert plan.stand_down is True and plan.stand_down_reason == "no pair"
    rows = load_entries(svc.ledger_path)
    assert rows and rows[0]["stand_down"] is True and rows[0]["exit_code"] == 0


def test_noquintile_fallback_sub_only_quintile_and_strangle_disabled(tmp_path):
    # PHASE B: anchor + pair resolve, but σ̂/quintile fail -> sub-only routing (strangle disabled).
    out = _outcome(ok=False, stand_down=False, quintile=None, strangle_disabled=True,
                   sigma_hat=None, reason="EXCL_SIGMA fallback")
    svc = _svc(tmp_path, "dry", outcome=out)
    plan = svc.prepare()
    outcome = svc._resolve_anchor(plan)
    svc._apply_outcome(plan, outcome)
    assert plan.state.strangle_disabled is True
    # sub-only routing bucket: contains sub-$1 flip, NOT the strangle
    q = plan.state.quintile
    srcs = plan.policy.quintile_routing[q]
    from service.policy import Q1_STRANGLE, SUB_DOLLAR_FLIP

    assert SUB_DOLLAR_FLIP in srcs and Q1_STRANGLE not in srcs


def test_ladder_disabled_propagates_to_state(tmp_path):
    svc = _svc(tmp_path, "shakedown", wake=_wake(strangle_disabled=True, alarm=True))
    plan = svc.prepare()
    outcome = svc._resolve_anchor(plan)  # PHASE B builds the state
    svc._apply_outcome(plan, outcome)
    assert plan.state.strangle_disabled is True


# ---------------------------------------------------------------------------
# crash-mid-window flushes the journal (F13-as-modified) + exits nonzero
# ---------------------------------------------------------------------------
def test_crash_mid_window_flushes_journal_and_exits_nonzero(tmp_path):
    def crashing_driver(recorder, deadline):
        recorder.journal.append(
            "kalshi_ws",
            {"type": "orderbook_snapshot", "msg": {"market_ticker": "KXBTC15M-ANCHOR"}},
            123.0,
        )
        raise RuntimeError("boom mid-window")

    svc = _svc(tmp_path, "shakedown", window_driver=crashing_driver)
    code = svc.run()
    assert code == 1
    # the buffered ws record + the traceback record survive in the flushed journal
    journal_path = os.path.join(tmp_path, "journals",
                                CLOSE.replace(":", "").replace("-", "") + ".jsonl")
    assert os.path.exists(journal_path)
    with open(journal_path, "r", encoding="utf-8") as f:
        text = f.read()
    assert "boom mid-window" in text
    assert "orderbook_snapshot" in text
    # ledger records exit_code 1
    rows = load_entries(svc.ledger_path)
    assert rows and rows[0]["exit_code"] == 1


def test_clean_shakedown_run_records_ledger_and_exits_zero(tmp_path):
    def noop_driver(recorder, deadline):
        return None

    svc = _svc(tmp_path, "shakedown", window_driver=noop_driver)
    code = svc.run()
    assert code == 0
    rows = load_entries(svc.ledger_path)
    assert rows and rows[0]["mode"] == "shakedown"
    assert rows[0]["stand_down"] is False
    assert rows[0]["orders_attempted"] == 0  # dry-cycle criterion: zero orders attempted


def test_dry_mode_zero_orders_attempted(tmp_path):
    def noop_driver(recorder, deadline):
        return None

    svc = _svc(tmp_path, "dry", window_driver=noop_driver,
               falsifier_path=_draft_falsifier(tmp_path))
    code = svc.run()
    assert code == 0
    rows = load_entries(svc.ledger_path)
    assert rows[0]["orders_attempted"] == 0
    assert rows[0]["mode"] == "dry"


# ---------------------------------------------------------------------------
# Finding 4: realized_delta booking — a Q1-strangle must NOT book the sub-$1 $1/pair FLOOR (an
# OPTIMISTIC win) into the S4 daily-loss cap. It books 0/unsettled at close; a sub-$1 flip keeps
# its guaranteed-floor arithmetic unchanged.
# ---------------------------------------------------------------------------
def _filled_pair_ledger(source):
    """A both-legs-filled entry ledger with a POSITIVE realized_min (the optimistic strangle-win /
    sub-$1-floor number). matched=1, net cash out ~0.82 -> realized_min ~ +0.18."""
    from service.ledger import record_response
    from service.orders.envelope import OrderResponse

    ls = new_ledger(CLOSE, source, "KXBTCD-100040", "KXBTC15M-ANCHOR")
    legs = (
        IntentLeg("KXBTCD-100040", "yes", "buy", 1, Decimal("0.40"), "cid-hi"),
        IntentLeg("KXBTC15M-ANCHOR", "yes", "buy", 1, Decimal("0.40"), "cid-lo"),
    )
    ls = record_intent(ls, Intent(CLOSE, source, PURPOSE_ENTRY, legs))
    for cid in ("cid-hi", "cid-lo"):
        ls = record_response(ls, OrderResponse(
            client_order_id=cid, order_id="o-" + cid, fill_count=Decimal(1),
            remaining_count=Decimal(0), average_fill_price=Decimal("0.40"),
            average_fee_paid=Decimal("0.01"), ts_ms=1000, error=None, no_fill=False))
    return ls


def test_strangle_realized_books_conservative_cashflow_never_the_optimistic_win(tmp_path):
    # BUG-2 repair: a strangle has NO floor, so at close it books the CONSERVATIVE cash OUTLAY
    # (proceeds - costs, here all-buys = negative), NEVER the optimistic realized_min win, and marks
    # the held legs unsettled for the settlement backfill. (Old behavior booked 0; that hid the
    # committed cost from S4.)
    svc = _svc(tmp_path, "armed", falsifier_path=_frozen_falsifier(tmp_path))
    ls = _filled_pair_ledger("Q1-strangle")
    assert ls.realized_min() > 0  # the OPTIMISTIC strangle-win number the old booking must never use
    svc.ledger_state = ls
    entry = svc._build_ledger_entry(Plan(effective_mode="armed", armed=True), None, 0, "j.jsonl", 0)
    assert Decimal(entry["realized_delta"]) == ls.realized_cashflow()  # = -0.82, conservative
    assert Decimal(entry["realized_delta"]) < 0  # a committed cost, not an assumed win
    assert Decimal(entry["realized_delta"]) < ls.realized_min()  # never the optimistic win
    assert entry["realized_unsettled"] is True
    assert entry["filled"] is True  # the pair DID fill; the settlement payoff is deferred
    # both held legs are recorded for the settlement backfill
    assert len(entry["unsettled_legs"]) == 2
    # S4 total reflects the conservative outlay (safe direction), never the +0.18 optimistic win.
    append_entry(entry, svc.ledger_path)
    from service.pilot_ledger import s4_running_loss
    assert s4_running_loss(load_entries(svc.ledger_path)) == ls.realized_cashflow()


def test_sub1_pair_still_books_its_floor_arithmetic_unchanged(tmp_path):
    svc = _svc(tmp_path, "armed", falsifier_path=_frozen_falsifier(tmp_path))
    ls = _filled_pair_ledger("sub$1-flip")
    floor = ls.realized_min()
    assert floor > 0
    svc.ledger_state = ls
    entry = svc._build_ledger_entry(Plan(effective_mode="armed", armed=True), None, 0, "j.jsonl", 0)
    # sub-$1 flip has a GUARANTEED >=$1/pair settlement -> the floor is conservative + unchanged.
    assert Decimal(entry["realized_delta"]) == floor
    assert entry["realized_unsettled"] is False


# ---------------------------------------------------------------------------
# Finding 5: a startup (prepare) crash must still leave a FLAGGED trace — a minimal startup-failed
# ledger row + a journal flush — not an ABSENT row the 24h-dry-cycle quick-scan silently misses.
# ---------------------------------------------------------------------------
def test_prepare_crash_writes_startupfailed_row_and_flushes_journal(tmp_path):
    # PHASE A crash: the armed-stack build (unwrapped in prepare()) raises before a Plan exists.
    # (The former sigma.assign crash now happens in PHASE B / execute() and is covered by the
    # execute-path crash test; finding-5's prepare() wrapper is exercised here instead.)
    svc = _svc(tmp_path, "armed", falsifier_path=_frozen_falsifier(tmp_path))

    def boom(policy):
        raise RuntimeError("startup boom in build_armed_stack")

    svc._build_armed_stack = boom
    code = svc.run()
    assert code == 1  # exits nonzero (fail-closed; no order was ever placed)
    rows = load_entries(svc.ledger_path)
    assert rows, "a startup crash must still append a ledger row (not an absent row)"
    row = rows[0]
    assert row["status"] == "startup-failed"
    assert row["exit_code"] == 1  # the quick-scan (exit_code or orders_attempted) flags it
    assert row["stand_down"] is True
    assert "startup boom" in row["error"]
    # the buffered startup journal (incl. the traceback) was flushed to disk
    journal_path = os.path.join(tmp_path, "journals",
                                CLOSE.replace(":", "").replace("-", "") + ".jsonl")
    assert os.path.exists(journal_path)
    with open(journal_path, "r", encoding="utf-8") as f:
        text = f.read()
    assert "startup boom" in text
    assert "window_start" in text  # prepare's first record survived the crash


# ---------------------------------------------------------------------------
# Phase-3-review handoff wiring: F9 caps agreement, F3 day-lock/seed, F5 S3 gate,
# F7 interlock, F8 flatten budget exemption
# ---------------------------------------------------------------------------
def test_F9_armed_refused_when_proxy_cap_exceeds_pilot_ceiling(tmp_path):
    health = {"orders_enabled": True,
              "caps": {"max_contracts_per_order": 5, "ticker_prefixes": ["KXBTC15M", "KXBTCD"],
                       "daily_order_budget": 100}}
    svc = _svc(tmp_path, "armed", health=health, falsifier_path=_frozen_falsifier(tmp_path))
    plan = svc.prepare()
    assert plan.armed is False and plan.degraded is True
    assert "ceiling" in plan.degrade_reason


def test_F9_armed_refused_when_proxy_prefixes_miss_our_series(tmp_path):
    health = {"orders_enabled": True,
              "caps": {"max_contracts_per_order": 2, "ticker_prefixes": ["KXOTHER"],
                       "daily_order_budget": 100}}
    svc = _svc(tmp_path, "armed", health=health, falsifier_path=_frozen_falsifier(tmp_path))
    plan = svc.prepare()
    assert plan.armed is False
    assert "series" in plan.degrade_reason


def _ledger_path(tmp_path):
    return os.path.join(tmp_path, "ledger", "pilot_ledger.jsonl")


def test_armed_refused_by_s4_balance_loss(tmp_path):
    # BUG-3 (S4 balance): a prior baseline in the day-guard file + a lower balance-now = a loss >=
    # cap ($3.00) -> refuse to arm (degrade to dry).
    from service.stops import DayGuard, _write_day_guard, day_guard_path
    gp = day_guard_path(str(tmp_path), "2026-08-21")
    _write_day_guard(gp, DayGuard(utc_day="2026-08-21", balance_start_cents=1000000, latched=()))
    svc = _svc(tmp_path, "armed", falsifier_path=_frozen_falsifier(tmp_path),
               balance=1000000 - 300)  # $3.00 loss, exactly the cap
    plan = svc.prepare()
    assert plan.armed is False and plan.degraded is True
    assert "S4 balance day-lock" in plan.degrade_reason


def test_armed_refused_by_latched_stop_in_day_guard(tmp_path):
    # BUG-3 (stop latching): a day-halting stop latched earlier today (persisted to the guard file)
    # refuses arming for the rest of the UTC day (falsifier: a stop halts the DAY).
    from service.stops import DayGuard, _write_day_guard, day_guard_path
    gp = day_guard_path(str(tmp_path), "2026-08-21")
    _write_day_guard(gp, DayGuard(
        utc_day="2026-08-21", balance_start_cents=1000000,
        latched=({"kind": "S2", "reason": "imbalance unrestorable", "window": "2026-08-21T20:00:00Z",
                  "ts": 1.0},)))
    svc = _svc(tmp_path, "armed", falsifier_path=_frozen_falsifier(tmp_path))
    plan = svc.prepare()
    assert plan.armed is False and plan.degraded is True
    assert "S2 latched earlier today" in plan.degrade_reason


def test_armed_refused_when_day_guard_corrupt(tmp_path):
    # A corrupt guard file fails closed: cannot confirm no latch -> refuse to arm.
    from service.stops import day_guard_path
    gp = day_guard_path(str(tmp_path), "2026-08-21")
    os.makedirs(os.path.dirname(gp), exist_ok=True)
    with open(gp, "w", encoding="utf-8") as f:
        f.write("{ this is not valid json")
    svc = _svc(tmp_path, "armed", falsifier_path=_frozen_falsifier(tmp_path))
    plan = svc.prepare()
    assert plan.armed is False and plan.degraded is True
    assert "corrupt" in plan.degrade_reason


def test_armed_refused_when_balance_read_fails(tmp_path):
    # A missing/failed balance read at wake fails closed (never arm).
    svc = _svc(tmp_path, "armed", falsifier_path=_frozen_falsifier(tmp_path), balance=None)
    plan = svc.prepare()
    assert plan.armed is False and plan.degraded is True
    assert "balance read failed" in plan.degrade_reason


def test_first_wake_snapshots_balance_start_and_arms(tmp_path):
    # BUG-3 (S4 balance): the FIRST wake of the day snapshots balance_start into the guard file and,
    # with no loss, arms. The s4_balance_check record carries first_wake + ledger_vs_balance_delta.
    from service.stops import day_guard_path, read_day_guard
    svc = _svc(tmp_path, "armed", falsifier_path=_frozen_falsifier(tmp_path), balance=1000000)
    plan = svc.prepare()
    assert plan.armed is True
    g = read_day_guard(day_guard_path(str(tmp_path), CLOSE[:10]), CLOSE[:10])
    assert g.balance_start_cents == 1000000
    checks = [r["obj"] for r in svc.journal.records() if r["kind"] == "s4_balance_check"]
    assert checks and checks[0]["first_wake"] is True
    assert "ledger_vs_balance_delta" in checks[0]


def test_open_positions_at_wake_skip_balance_compare(tmp_path):
    # Brad's ruling note (a): open positions at wake -> the balance is not clean cash, so record a
    # skip rather than compare. reconcile-first still stands the window down.
    svc = _svc(tmp_path, "armed", falsifier_path=_frozen_falsifier(tmp_path),
               positions_body={"market_positions": [{"ticker": "KXBTCD-HELD", "position": -1}]})
    plan = svc.prepare()
    assert plan.stand_down is True
    kinds = [r["kind"] for r in svc.journal.records()]
    assert "balance_check_skipped_open_positions" in kinds


def test_F3_armed_refused_by_a4_day_lock_from_ledger(tmp_path):
    lp = _ledger_path(tmp_path)
    for h in range(5):
        append_entry({"close_time": f"2026-08-21T{h:02d}:00:00Z", "realized_delta": "0",
                      "guard_trips": 1}, lp)
    svc = _svc(tmp_path, "armed", falsifier_path=_frozen_falsifier(tmp_path))
    plan = svc.prepare()
    assert plan.armed is False
    assert "A4 day-lock" in plan.degrade_reason


def test_F3_armed_seeds_stopstate_from_ledger_day_totals(tmp_path):
    lp = _ledger_path(tmp_path)
    append_entry({"close_time": "2026-08-21T20:00:00Z", "realized_delta": "-1.50", "guard_trips": 2}, lp)
    svc = _svc(tmp_path, "armed", falsifier_path=_frozen_falsifier(tmp_path))
    plan = svc.prepare()
    assert plan.armed is True  # -1.50 not at cap; 2 trips < 5
    assert svc.stops.state.daily_realized == Decimal("-1.50")
    assert svc.stops.state.guard_trips == 2


class _FakeReconTrip:
    def __init__(self):
        self.ticks = 0

    def tick(self, state, params=None):
        self.ticks += 1
        return ReconcileResult(stop="S3", mismatches=("KXBTC15M-ANCHOR",))


def _armed_svc(tmp_path):
    svc = _svc(tmp_path, "armed", falsifier_path=_frozen_falsifier(tmp_path))
    plan = svc.prepare()
    assert plan.armed is True
    svc.armed = True
    return svc


def test_F5_s3_poll_skips_while_order_in_flight(tmp_path):
    svc = _armed_svc(tmp_path)
    ls = new_ledger(CLOSE, "sub$1-flip", "KXBTCD-100040", "KXBTC15M-ANCHOR")
    leg = IntentLeg("KXBTC15M-ANCHOR", "yes", "buy", 1, Decimal("0.40"), "cid-inflight")
    ls = record_intent(ls, Intent(CLOSE, "sub$1-flip", PURPOSE_ENTRY, (leg,)))  # intent, no response
    svc.ledger_state = ls
    fake = _FakeReconTrip()
    svc.reconciler = fake
    svc._s3_poll_once()
    assert fake.ticks == 0  # F5: deferred while an order is in flight
    assert not svc.stops.state.is_stopped


def test_F5_s3_poll_trips_when_idle_and_mismatch(tmp_path):
    svc = _armed_svc(tmp_path)
    svc.ledger_state = new_ledger(CLOSE, "sub$1-flip", "KXBTCD-100040", "KXBTC15M-ANCHOR")  # no inflight
    svc.reconciler = _FakeReconTrip()
    svc._s3_poll_once()
    assert svc.stops.state.is_stopped and svc.stops.state.has("S3")
    assert svc.executor.armed is False  # the trip froze the executor


class _ReconMutatesLedgerDuringTick:
    """Simulates the TOCTOU the F5 gate must close: an entry lands AND records on the ledger during
    the poll's network read (inflight cleared by the time tick returns), while tick's mismatch was
    computed against the STALE pre-poll snapshot. The poll must DEFER, never trip a spurious S3."""

    def __init__(self, svc):
        self.svc = svc
        self.ticks = 0

    def tick(self, state, params=None):
        self.ticks += 1
        from service.orders.envelope import OrderResponse
        from service.ledger import record_response
        ls = new_ledger(CLOSE, "sub$1-flip", "KXBTCD-100040", "KXBTC15M-ANCHOR")
        leg = IntentLeg("KXBTC15M-ANCHOR", "yes", "buy", 1, Decimal("0.40"), "cid-live")
        ls = record_intent(ls, Intent(CLOSE, "sub$1-flip", PURPOSE_ENTRY, (leg,)))
        ls = record_response(ls, OrderResponse(
            client_order_id="cid-live", order_id="o1", fill_count=Decimal(1),
            remaining_count=Decimal(0), average_fill_price=Decimal("0.40"),
            average_fee_paid=Decimal("0.01"), ts_ms=1000, error=None, no_fill=False))
        self.svc.ledger_state = ls  # the ledger advanced+recorded during the poll (no inflight now)
        return ReconcileResult(stop="S3", mismatches=("KXBTC15M-ANCHOR",))


def test_F5_s3_poll_defers_when_ledger_advances_during_poll(tmp_path):
    svc = _armed_svc(tmp_path)
    svc.ledger_state = new_ledger(CLOSE, "sub$1-flip", "KXBTCD-100040", "KXBTC15M-ANCHOR")  # empty, no inflight
    svc.reconciler = _ReconMutatesLedgerDuringTick(svc)
    svc._s3_poll_once()
    # the diff raced the order path (stale snapshot) -> deferred, NOT a spurious S3 halt.
    assert not svc.stops.state.is_stopped
    assert svc.executor.armed is True


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


def test_F7_harness_executor_never_rearms_after_stop():
    ex = HarnessExecutor(ThreadSafeJournal(), ExecutorConfig(armed=True),
                         post_fn=lambda p, b: _Resp(200, {}), clock=lambda: 0.0)
    assert ex.armed is True
    ex.set_armed(False)               # simulate a stop trip
    assert ex.armed is False and ex.arm_locked is True
    ex.set_armed(True)                # attempt re-arm within the process
    assert ex.armed is False          # F7: refused


def test_F8_flatten_exempt_from_rate_budget_but_strategy_is_not():
    posted = []

    def post(path, body):
        posted.append(path)
        return _Resp(200, {"order": {"client_order_id": "x", "fill_count": "0",
                                     "remaining_count": "1"}})

    cfg = ExecutorConfig(armed=False, window_token_budget=0, tokens_per_entry=10)
    ex = HarnessExecutor(ThreadSafeJournal(), cfg, post_fn=post, clock=lambda: 0.0)
    flat = Intent("W", "sub$1-flip", PURPOSE_FLATTEN,
                  (IntentLeg("KXBTC15M-A", "no", "sell", 1, Decimal("0.40"), "c1", reduce_only=True),))
    res = ex.execute(flat, t_minus_s=10.0, stop_authorized=True)
    assert res.refused is None       # F8: NOT rate_budget_exhausted
    assert posted == [SINGLE_CREATE_PATH]  # a POST happened, via the envelope's events-path constant
    # a strategy order under the same exhausted budget IS refused
    ex.set_armed(True)
    entry = Intent("W", "sub$1-flip", PURPOSE_ENTRY,
                   (IntentLeg("KXBTC15M-A", "yes", "buy", 1, Decimal("0.40"), "c2"),))
    res2 = ex.execute(entry, t_minus_s=10.0)
    assert res2.refused == "rate_budget_exhausted"


# ---------------------------------------------------------------------------
# Connect-gate (live finding 2026-08-21): discover+arm at :40 while the 15M leg is 'initialized',
# but HOLD the WS dial until ~open_time (:45). Tested with a fake clock + fake feed (no live network).
# ---------------------------------------------------------------------------
def _wake_with_fifteen_open(open_iso: str) -> WakeResult:
    w = _wake()
    return dataclasses.replace(w, fifteen_leg=dataclasses.replace(w.fifteen_leg, open_time=open_iso))


def test_connect_gate_future_open_returns_gate(tmp_path):
    svc = _svc(tmp_path, "shakedown")
    svc.clock = lambda: float(close_epoch("2026-08-21T21:40:00Z"))  # :40 wake
    wake = _wake_with_fifteen_open("2026-08-21T21:45:00Z")          # 15M opens :45
    gate = svc._connect_gate(wake)
    assert gate == float(close_epoch("2026-08-21T21:45:00Z")) - _CONNECT_MARGIN_S


def test_connect_gate_none_when_leg_already_open(tmp_path):
    svc = _svc(tmp_path, "shakedown")
    svc.clock = lambda: float(close_epoch("2026-08-21T21:50:00Z"))  # already past :45 open
    wake = _wake_with_fifteen_open("2026-08-21T21:45:00Z")
    assert svc._connect_gate(wake) is None


def test_connect_gate_none_when_open_time_unparseable(tmp_path):
    svc = _svc(tmp_path, "shakedown")
    wake = _wake_with_fifteen_open("not-a-timestamp")
    assert svc._connect_gate(wake) is None  # fail-open to dialing (wake already vetted liveness)


class _FakeGateWS:
    """Records the clock at each dial; the first dial jumps the clock to the deadline so the driver's
    dial loop exits after one connect (a clean, connected close)."""

    def __init__(self, clock_holder, deadline):
        self._clock_holder = clock_holder
        self._deadline = deadline
        self.connect_times: list[float] = []

    async def connect(self):
        self.connect_times.append(self._clock_holder[0])
        self._clock_holder[0] = self._deadline  # end the window

    async def force_close(self):
        pass

    def data_age_seconds(self):
        return None

    def silence_seconds(self):
        return 0.0


class _FakeRecorder:
    def __init__(self, ws, clock):
        self.ws_client = ws
        self.clock = clock
        self.alarms: list[str] = []

    def mark_all_suspect(self):
        pass

    def record_alarm(self, kind, obj):
        self.alarms.append(kind)


def test_run_ws_window_holds_dial_until_connect_gate(tmp_path):
    start = float(close_epoch("2026-08-21T21:40:00Z"))          # :40 wake
    open_epoch = float(close_epoch("2026-08-21T21:45:00Z"))     # 15M opens :45
    deadline = float(close_epoch(CLOSE) + GRACE_SECONDS)
    gate = open_epoch - _CONNECT_MARGIN_S
    clock_holder = [start]
    clock = lambda: clock_holder[0]

    async def fake_sleep(d):
        clock_holder[0] += d  # a fake sleep advances the fake clock

    svc = _svc(tmp_path, "shakedown")
    svc.clock = clock
    ws = _FakeGateWS(clock_holder, deadline)
    rec = _FakeRecorder(ws, clock)

    asyncio.run(svc._run_ws_window(rec, deadline, connect_not_before=gate, sleep=fake_sleep))
    assert ws.connect_times, "the socket must eventually dial"
    assert ws.connect_times[0] >= gate           # NEVER dialed before the gate (the whole point)
    assert ws.connect_times[0] < open_epoch + 1  # dialed right around open, not far after


def test_run_ws_window_no_gate_dials_immediately(tmp_path):
    start = float(close_epoch("2026-08-21T21:50:00Z"))  # leg already open -> gate None
    deadline = float(close_epoch(CLOSE) + GRACE_SECONDS)
    clock_holder = [start]
    clock = lambda: clock_holder[0]

    async def fake_sleep(d):
        clock_holder[0] += d

    svc = _svc(tmp_path, "shakedown")
    svc.clock = clock
    ws = _FakeGateWS(clock_holder, deadline)
    rec = _FakeRecorder(ws, clock)

    asyncio.run(svc._run_ws_window(rec, deadline, connect_not_before=None, sleep=fake_sleep))
    assert ws.connect_times and ws.connect_times[0] == start  # dialed at once, no hold


def test_await_connect_gate_bounded_by_deadline(tmp_path):
    """A gate AFTER the deadline (pathological) must not park past window end: the wait is bounded by
    the deadline and returns so the driver can close cleanly."""
    start = float(close_epoch("2026-08-21T21:40:00Z"))
    deadline = start + 30.0
    gate = start + 10_000.0  # absurdly far in the future
    clock_holder = [start]
    clock = lambda: clock_holder[0]

    async def fake_sleep(d):
        clock_holder[0] += d

    svc = _svc(tmp_path, "shakedown")
    svc.clock = clock
    asyncio.run(svc._await_connect_gate(gate, deadline, fake_sleep))
    assert clock_holder[0] >= deadline and clock_holder[0] < gate  # stopped at the deadline, not the gate


# ---------------------------------------------------------------------------
# PHASE A / PHASE B split (live finding 2026-08-21): the co-settling 15M leg is 'initialized' with
# NO floor_strike at the :40 wake; the anchor strike materializes only when it flips 'active' at
# open_time (~:45:13). PHASE A (prepare) does everything anchor-INDEPENDENT + prefetches the σ̂
# trailing tape; PHASE B (execute) polls REST at leg open until the strike appears, then
# pairs/scores/quintiles. Driven with a fake clock + fake REST — no live network.
# ---------------------------------------------------------------------------
def _strikeless_15m(open_iso="2026-08-21T21:45:00Z"):
    return {"ticker": "KXBTC15M-ANCHOR", "close_time": CLOSE, "open_time": open_iso,
            "status": "initialized", "event_ticker": "KXBTC15M-EV"}  # NO floor_strike key


def _strikeful_15m(strike=77315.17, open_iso="2026-08-21T21:45:00Z"):
    return {"ticker": "KXBTC15M-ANCHOR", "floor_strike": strike, "close_time": CLOSE,
            "open_time": open_iso, "status": "active", "event_ticker": "KXBTC15M-EV"}


def test_phase_a_defers_quintile_no_quintile_record(tmp_path):
    """PHASE A journals a phase_a record and builds NO state/outcome/quintile (anchor not available)."""
    svc = _svc(tmp_path, "shakedown")
    plan = svc.prepare()
    kinds = _kinds(svc.journal)
    assert "phase_a" in kinds
    assert "quintile" not in kinds            # the quintile is NOT computed at :40
    assert plan.state is None and plan.outcome is None
    assert svc._sigma_feed.trailing_fetches == 1  # trailing σ̂ tape WAS prefetched in phase A


def test_phase_b_polls_until_strike_appears_at_open(tmp_path):
    """Strike absent at the :40 wake, appears at :45:13 -> the poll waits past open, then resolves."""
    open_ep = float(close_epoch("2026-08-21T21:45:00Z"))
    strike_ep = open_ep + 13.0                                   # observed ~:45:13
    clock_holder = [float(close_epoch("2026-08-21T21:40:00Z"))]  # :40 wake
    fetches = []

    def fetch():
        fetches.append(clock_holder[0])
        return [_strikeful_15m()] if clock_holder[0] >= strike_ep else [_strikeless_15m()]

    def poll_sleep(d):
        clock_holder[0] += d

    wake = _wake_with_fifteen_open("2026-08-21T21:45:00Z")
    svc = _svc(tmp_path, "shakedown", wake=wake, anchor_fetcher=fetch,
               clock=(lambda: clock_holder[0]), poll_sleep=poll_sleep)
    plan = svc.prepare()
    outcome = svc._resolve_anchor(plan)
    assert outcome is not None and outcome.ok            # resolved once the strike materialized
    assert clock_holder[0] >= strike_ep                  # the poll waited for the strike
    assert min(fetches) >= open_ep                       # never fetched before the leg opened
    kinds = _kinds(svc.journal)
    assert "phase_b_start" in kinds and "phase_b_anchor" in kinds and "quintile" in kinds


def test_phase_b_poll_timeout_stands_down_no_anchor(tmp_path):
    """Strike never materializes -> poll times out at open+45s -> EXCL_NO_ANCHOR stand-down, exit 0."""
    clock_holder = [float(close_epoch("2026-08-21T21:40:00Z"))]

    def poll_sleep(d):
        clock_holder[0] += d

    wake = _wake_with_fifteen_open("2026-08-21T21:45:00Z")
    svc = _svc(tmp_path, "shakedown", wake=wake, anchor_fetcher=(lambda: [_strikeless_15m()]),
               clock=(lambda: clock_holder[0]), poll_sleep=poll_sleep)
    plan = svc.prepare()
    code = svc.execute(plan)
    assert code == 0
    assert plan.stand_down is True and "EXCL_NO_ANCHOR" in plan.stand_down_reason
    assert "phase_b_timeout" in _kinds(svc.journal)
    # bounded: the poll gave up ~open+45s, NEVER parked to the window deadline (close+grace).
    assert clock_holder[0] <= float(close_epoch("2026-08-21T21:45:00Z")) + ANCHOR_POLL_TIMEOUT_S + 1
    rows = load_entries(svc.ledger_path)
    assert rows and rows[0]["stand_down"] is True and rows[0]["exit_code"] == 0


def test_phase_b_reuses_prefetched_trailing_and_polls_current(tmp_path):
    """PHASE B hands sigma.assign the POLLED current market + the phase-A-prefetched trailing tape
    (no re-fetch of the trailing tape in phase B)."""
    svc = _svc(tmp_path, "shakedown")
    plan = svc.prepare()
    polled = [_strikeful_15m(strike=100000.0, open_iso="2026-08-21T21:00:00Z")]
    svc._anchor_fetcher = lambda: polled
    svc._resolve_anchor(plan)
    call = svc._sigma_feed.assign_calls[-1]
    assert call["current"] == polled                 # the freshly-polled current market
    assert call["trailing"] == plan.trailing_tape    # the phase-A prefetch, reused (not re-fetched)
    assert svc._sigma_feed.trailing_fetches == 1      # still only the one phase-A fetch


def test_phase_b_sub_only_routing_reachable_when_sigma_fails(tmp_path):
    """Anchor + pair resolve in phase B but σ̂/quintile fail -> sub-only routing (strangle disabled),
    NOT a stand-down (the sub-$1 flip still runs)."""
    out = _outcome(ok=False, stand_down=False, quintile=None, strangle_disabled=True,
                   sigma_hat=None, reason="EXCL_SIGMA fallback")
    svc = _svc(tmp_path, "dry", outcome=out, falsifier_path=_draft_falsifier(tmp_path))
    plan = svc.prepare()
    outcome = svc._resolve_anchor(plan)
    assert outcome.stand_down is False
    svc._apply_outcome(plan, outcome)
    assert plan.state.strangle_disabled is True
    from service.policy import Q1_STRANGLE, SUB_DOLLAR_FLIP
    srcs = plan.policy.quintile_routing[plan.state.quintile]
    assert SUB_DOLLAR_FLIP in srcs and Q1_STRANGLE not in srcs


def test_phase_b_crash_writes_exit1_row_via_execute_path(tmp_path):
    """A phase-B (execute) failure is caught by execute()'s handler -> exit 1 + a ledger row (NOT the
    prepare-path startup-failed row)."""
    class _CrashSigma(FakeSigma):
        def assign(self, *a, **k):
            raise RuntimeError("boom in phase-B sigma.assign")

    svc = _svc(tmp_path, "shakedown")
    svc._sigma_feed = _CrashSigma(_outcome())
    code = svc.run()
    assert code == 1
    assert "unhandled_exception" in _kinds(svc.journal)
    rows = load_entries(svc.ledger_path)
    assert rows and rows[0]["exit_code"] == 1


# ---------------------------------------------------------------------------
# F1 (dual-generation): the phase-B pairing must pool ALL co-settling hourly generations (census
# h1_by_ct semantics), pick the global-nearest strike (even in a NON-selected generation), exclude a
# cross-generation equidistant tie, and apply the ladder-map check to the CHOSEN generation. The WS
# subscription follows the chosen market's generation.
# ---------------------------------------------------------------------------
def _hourly_gen(event: str, open_iso: str, strikes: list[float]) -> Leg:
    markets = tuple(
        {"ticker": f"{event}-{s}", "floor_strike": s, "close_time": CLOSE, "open_time": open_iso,
         "status": "active", "event_ticker": event}
        for s in strikes
    )
    return Leg(
        series="KXBTCD", event_ticker=event, open_time=open_iso, close_time=CLOSE,
        window_seconds=(close_epoch(CLOSE) - close_epoch(open_iso)),
        market_tickers=tuple(m["ticker"] for m in markets),
        floor_strikes=tuple(strikes), markets=markets,
    )


def _dual_wake(anchor: float, selected_gen: Leg, other_gen: Leg) -> WakeResult:
    """A WakeResult with TWO co-settling hourly generations. ``selected_gen`` is the smallest-window
    generation (=> ``hourly_leg`` + ``ladder``); both reach phase B via ``hourly_ladders``."""
    fifteen = _leg("KXBTC15M", "KXBTC15M-EV", "KXBTC15M-ANCHOR", anchor)
    return WakeResult(
        close_time=CLOSE, fifteen_leg=fifteen, hourly_leg=selected_gen,
        ladder=ladder_check(selected_gen), balance=None, affordable=None,
        hourly_ladders=(selected_gen, other_gen),
    )


def test_f1_pooled_pairing_finds_nearest_in_nonselected_generation(tmp_path):
    """Global-nearest strike lives in the WIDER (non-selected) generation; pooling across generations
    must pick it, and the WS subscription must follow the CHOSEN generation."""
    selected = _hourly_gen("KXBTCD-SEL", "2026-08-21T21:00:00Z", [77000.0, 77100.0, 77200.0])
    other = _hourly_gen("KXBTCD-OTHER", "2026-08-21T20:00:00Z", [77299.99, 77399.99, 77499.99])
    wake = _dual_wake(77315.17, selected, other)
    svc = _svc(tmp_path, "shakedown", wake=wake)
    svc._sigma_feed = SigmaFeed(FakeProxy(), edges=[0.5, 1.0, 1.5, 2.0])  # real pooled pairing
    plan = svc.prepare()
    outcome = svc._resolve_anchor(plan)
    assert outcome is not None and not outcome.stand_down
    # A(77315.17) > K(77299.99) -> low leg is the hourly, in the NON-selected generation.
    assert outcome.low_ticker == "KXBTCD-OTHER-77299.99"
    assert outcome.low_ticker in other.market_tickers
    assert outcome.high_ticker == "KXBTC15M-ANCHOR"
    svc._apply_outcome(plan, outcome)
    svc.max_hourly = None  # widen so the FULL chosen-generation ladder is subscribed
    subs = svc._subscription_tickers(plan.wake, plan.state)
    assert outcome.low_ticker in subs
    assert set(other.market_tickers) <= set(subs)          # subscription follows the chosen generation
    assert not (set(selected.market_tickers) & set(subs))  # the non-chosen generation is NOT subscribed


def test_f1_cross_generation_equidistant_tie_stands_down(tmp_path):
    """Two generations each hold a strike equidistant from A -> pooling reveals a cross-generation
    nearest-tie that ONE generation alone would not show -> census EXCL_NEAREST_TIE -> stand down."""
    selected = _hourly_gen("KXBTCD-SEL", "2026-08-21T21:00:00Z", [77250.0, 77150.0, 77050.0])
    other = _hourly_gen("KXBTCD-OTHER", "2026-08-21T20:00:00Z", [77350.0, 77450.0, 77550.0])
    wake = _dual_wake(77300.0, selected, other)  # 77250 and 77350 both 50 away
    svc = _svc(tmp_path, "shakedown", wake=wake)
    svc._sigma_feed = SigmaFeed(FakeProxy(), edges=[0.5, 1.0, 1.5, 2.0])
    plan = svc.prepare()
    outcome = svc._resolve_anchor(plan)
    assert outcome is not None and outcome.stand_down is True
    assert "tie" in outcome.reason.lower()
    # whole window stands down cleanly (exit 0, one ledger row).
    assert svc.execute(plan) == 0
    assert plan.stand_down is True
    rows = load_entries(svc.ledger_path)
    assert rows and rows[0]["stand_down"] is True and rows[0]["exit_code"] == 0


def test_f1_ladder_check_applies_to_chosen_generation(tmp_path):
    """The selected (smallest-window) generation is a BAD ladder, but the pooled pairing chose a
    strike in a CLEAN other generation -> the strangle is NOT disabled (the ladder-map check follows
    the market actually paired), and a `chosen_ladder` record is journalled."""
    bad_selected = _hourly_gen("KXBTCD-SEL", "2026-08-21T21:00:00Z", [77000.0, 77250.0, 77400.0])
    assert ladder_check(bad_selected).strangle_disabled is True  # non-uniform -> would disable
    clean_other = _hourly_gen("KXBTCD-OTHER", "2026-08-21T20:00:00Z", [77299.99, 77399.99, 77499.99])
    wake = _dual_wake(77315.17, bad_selected, clean_other)
    chosen_hourly = "KXBTCD-OTHER-77299.99"
    out = _outcome(ok=True, quintile=0, high_ticker="KXBTC15M-ANCHOR", low_ticker=chosen_hourly,
                   strangle_disabled=False)
    svc = _svc(tmp_path, "shakedown", wake=wake, outcome=out)
    plan = svc.prepare()
    outcome = svc._resolve_anchor(plan)
    svc._apply_outcome(plan, outcome)
    assert plan.state.strangle_disabled is False              # chosen (clean) generation governs
    assert "chosen_ladder" in _kinds(svc.journal)


def test_f1_ladder_check_disables_when_chosen_generation_bad(tmp_path):
    """Symmetric: the pooled pairing chose a strike in the BAD generation -> strangle disabled."""
    clean_selected = _hourly_gen("KXBTCD-SEL", "2026-08-21T21:00:00Z", [77200.0, 77100.0, 77000.0])
    bad_other = _hourly_gen("KXBTCD-OTHER", "2026-08-21T20:00:00Z", [77300.0, 77550.0, 77700.0])
    wake = _dual_wake(77315.17, clean_selected, bad_other)
    chosen_hourly = "KXBTCD-OTHER-77300.0"
    out = _outcome(ok=True, quintile=0, high_ticker="KXBTC15M-ANCHOR", low_ticker=chosen_hourly,
                   strangle_disabled=False)
    svc = _svc(tmp_path, "shakedown", wake=wake, outcome=out)
    plan = svc.prepare()
    outcome = svc._resolve_anchor(plan)
    svc._apply_outcome(plan, outcome)
    assert plan.state.strangle_disabled is True              # chosen (bad) generation disables


def test_f1_single_generation_pool_falls_back_unchanged(tmp_path):
    """Back-compat: a WakeResult with no retained ladders pools exactly the single selected
    generation (single-generation behavior is unchanged)."""
    w = _wake()
    assert w.hourly_ladders == ()
    assert w.hourly_pool_markets == tuple(w.hourly_leg.markets)


# ---------------------------------------------------------------------------
# F3: an unparseable/absent 15M open_time anchors the poll budget to (close - 900s) = the REAL open,
# NOT the machine clock (which times the poll out ~4 min early at the :40 wake).
# ---------------------------------------------------------------------------
def test_f3_unparseable_open_time_anchors_poll_to_close_minus_900(tmp_path):
    close_ep = float(close_epoch(CLOSE))
    real_open = close_ep - 900.0                                 # 21:45 for a 22:00 close
    strike_ep = real_open + 13.0                                 # strike materializes ~:45:13
    clock_holder = [float(close_epoch("2026-08-21T21:40:00Z"))]  # :40 wake (BEFORE the real open)
    fetches: list[float] = []

    def fetch():
        fetches.append(clock_holder[0])
        return [_strikeful_15m(strike=100000.0)] if clock_holder[0] >= strike_ep else [_strikeless_15m()]

    def poll_sleep(d):
        clock_holder[0] += d

    wake = _wake_with_fifteen_open("not-a-timestamp")           # unparseable open_time
    svc = _svc(tmp_path, "shakedown", wake=wake, anchor_fetcher=fetch,
               clock=(lambda: clock_holder[0]), poll_sleep=poll_sleep)
    plan = svc.prepare()
    outcome = svc._resolve_anchor(plan)
    assert outcome is not None                                   # did NOT time out ~4 min before open
    assert clock_holder[0] >= strike_ep                         # the poll waited for the real strike
    assert min(fetches) >= real_open                            # never polled before close-900 (real open)
    assert min(fetches) >= float(close_epoch("2026-08-21T21:40:00Z")) + 60  # not the :40 clock frame


# ---------------------------------------------------------------------------
# F5: a WS frame with NO server timestamp must NOT drive decide() (machine time driving a decision
# contradicts the fail-closed freshness law). The book is still folded; the frame is journalled and
# the decision core is skipped.
# ---------------------------------------------------------------------------
def test_f5_tsless_frame_never_reaches_decide(tmp_path):
    from service.book import BookMirror

    svc = _svc(tmp_path, "dry", falsifier_path=_draft_falsifier(tmp_path))
    plan = svc.prepare()
    outcome = svc._resolve_anchor(plan)
    svc._apply_outcome(plan, outcome)
    rec = svc._build_recorder(plan)
    assert isinstance(rec, LiveWindowRecorder)
    market = plan.state.high_ticker
    rec.books[market] = BookMirror()
    calls: list[tuple] = []
    rec.driver.on_book_update = lambda m, top, ts: calls.append((m, ts))

    rec._drive(market, {"market_ticker": market})               # NO ts_ms / ts
    assert calls == []                                          # decide() NOT driven on a ts-less frame
    assert "ws_frame_no_server_ts" in [r["kind"] for r in rec.journal.records()]

    rec._drive(market, {"market_ticker": market, "ts_ms": 1_700_000_000_000})  # WITH a server ts
    assert calls and calls[0][0] == market                     # a timestamped frame DOES drive decide()


# ---------------------------------------------------------------------------
# register script DRY parse (no registration; validates well-formedness)
# ---------------------------------------------------------------------------
def _ops_dir():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops")


def test_register_script_dryrun_is_well_formed():
    script = os.path.join(_ops_dir(), "register_task.ps1")
    assert os.path.exists(script)
    pwsh = shutil.which("powershell") or shutil.which("pwsh")
    if pwsh:
        out = subprocess.run(
            [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, "-DryRun"],
            capture_output=True, text=True, timeout=60,
        )
        assert out.returncode == 0, out.stderr
        text = out.stdout
        assert "DRY RUN" in text
        assert "Register-ScheduledTask" in text
        assert "run_window" in text
        assert "New-ScheduledTaskTrigger" in text
        # F2-ops: the overlap guard is pinned in the registered settings (single-instance).
        assert "MultipleInstances IgnoreNew" in text
    else:  # no PowerShell available -> structural text assertion
        with open(script, "r", encoding="utf-8") as f:
            text = f.read()
        assert "Register-ScheduledTask" in text
        assert "run_window" in text
        assert "New-ScheduledTaskTrigger" in text
        assert "DryRun" in text
        assert "MultipleInstances IgnoreNew" in text


def test_unregister_script_dryrun_is_well_formed():
    script = os.path.join(_ops_dir(), "unregister_task.ps1")
    assert os.path.exists(script)
    pwsh = shutil.which("powershell") or shutil.which("pwsh")
    if pwsh:
        out = subprocess.run(
            [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, "-DryRun"],
            capture_output=True, text=True, timeout=60,
        )
        assert out.returncode == 0, out.stderr
        assert "Unregister-ScheduledTask" in out.stdout
    else:
        with open(script, "r", encoding="utf-8") as f:
            assert "Unregister-ScheduledTask" in f.read()
