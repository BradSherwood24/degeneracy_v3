"""Phase box-2 wiring tests: strategy lever (fail-closed), box PHASE-B state build, subscription,
event routing, box_eval throttling, FIRE->Intent, ledger slot mapping, both-filled floor + backfill
(pin-region truth table), one-leg flatten (fill / miss->retry / no bid), check_s1_box both branches,
A5 counter, settlement-backfill sweep, and box-decision golden determinism. No network in tests."""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

from service.book import TopOfBook
from service.box import (
    BUY_NO,
    BUY_YES,
    BoxState,
    load_box_policy,
)
from service.box_runner import BoxSignalDriver
from service.ledger import (
    WIDE_BOX,
    IntentLeg,
    Intent,
    PURPOSE_ENTRY,
    new_ledger,
    record_intent,
    record_response,
    settlement_payoff,
)
from service.orders.envelope import OrderResponse
from service.pilot_ledger import (
    append_entry,
    box_one_legged_rate,
    build_backfill_entry,
    load_entries,
)
from service.run_window import (
    VALID_STRATEGIES,
    WindowService,
    _parse_market_result,
    resolve_strategy,
)
from service.signal import BookUpdate, ClockTick
from service.stops import ArmDecision, arming_check, check_s1, check_s1_box
from service.wake import LadderCheck, Leg, WakeResult, close_epoch
from service import box as box_mod
from service import ledger as ledger_mod

BOX_PARAMS = load_box_policy()
CLOSE = "2026-08-21T22:00:00Z"
T = close_epoch(CLOSE)
A = Decimal("65000")
HOURLY_TICKER = "KXBTCD-64000"
M15_TICKER = "KXBTC15M-ANCHOR"


# ---------------------------------------------------------------------------
# quote helper (mirrors test_box.top)
# ---------------------------------------------------------------------------
def top(*, yes_ask=None, yes_bid=None, suspect=False, ask_size="7") -> TopOfBook:
    def d(x):
        return None if x is None else Decimal(str(x))
    ya, yb = d(yes_ask), d(yes_bid)
    sz = d(ask_size)
    return TopOfBook(
        yes_bid=yb, yes_bid_size=(None if yb is None else Decimal(1)),
        yes_ask=ya, yes_ask_size=(None if ya is None else sz),
        no_bid=(None if ya is None else Decimal(1) - ya),
        no_bid_size=(None if ya is None else Decimal(1)),
        no_ask=(None if yb is None else Decimal(1) - yb),
        no_ask_size=(None if yb is None else sz),
        suspect=suspect,
    )


# ===========================================================================
# 1) strategy lever resolution (incl. fail-closed)
# ===========================================================================
def test_strategy_cli_overrides():
    assert resolve_strategy("box", "/nonexistent") == ("box", True)
    assert resolve_strategy("corridor", "/nonexistent") == ("corridor", True)


def test_strategy_reads_file(tmp_path):
    p = os.path.join(tmp_path, "strategy.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write(" box \n")
    assert resolve_strategy(None, p) == ("box", True)


def test_strategy_unknown_fails_closed_to_corridor(tmp_path):
    p = os.path.join(tmp_path, "strategy.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write("YOLO")
    assert resolve_strategy(None, p) == ("corridor", False)


def test_strategy_missing_file_fails_closed(tmp_path):
    assert resolve_strategy(None, os.path.join(tmp_path, "nope.txt")) == ("corridor", False)


def test_shipped_default_strategy_is_corridor(tmp_path):
    # The corridor is the fail-closed SHIPPED default: with no lever value (absent file) or an
    # unknown one, resolve_strategy runs the corridor core, never the box. This intentionally does
    # NOT read the operator's live ops/strategy.txt — that file is gitignored/operator-local and a
    # test's outcome must never depend on it (test isolation; conftest tripwire enforces it).
    absent = tmp_path / "no_such_strategy.txt"
    assert resolve_strategy(None, str(absent)) == ("corridor", False)
    bad = tmp_path / "garbage.txt"
    bad.write_text("wat\n", encoding="utf-8")
    assert resolve_strategy(None, str(bad)) == ("corridor", False)
    corridor = tmp_path / "corridor.txt"
    corridor.write_text("corridor\n", encoding="utf-8")
    assert resolve_strategy(None, str(corridor)) == ("corridor", True)
    assert set(VALID_STRATEGIES) == {"corridor", "box"}


def test_invalid_strategy_runs_corridor_dry_never_armed(tmp_path):
    """An unknown strategy value fails closed: corridor core, DRY, never armed — even with --mode
    armed and a frozen falsifier."""
    from service.wake import LadderCheck as LC, Leg as _Leg, WakeResult as _WR

    def _corridor_wake():
        fifteen = _Leg("KXBTC15M", "KXBTC15M-EV", "2026-08-21T21:45:00Z", CLOSE, 900,
                       (M15_TICKER,), (65000.0,),
                       ({"ticker": M15_TICKER, "floor_strike": 65000.0, "close_time": CLOSE,
                         "open_time": "2026-08-21T21:45:00Z", "status": "active",
                         "event_ticker": "KXBTC15M-EV"},))
        hourly = _Leg("KXBTCD", "KXBTCD-EV", "2026-08-21T21:00:00Z", CLOSE, 3600,
                      (HOURLY_TICKER,), (64000.0,),
                      ({"ticker": HOURLY_TICKER, "floor_strike": 64000.0, "close_time": CLOSE,
                        "open_time": "2026-08-21T21:00:00Z", "status": "active",
                        "event_ticker": "KXBTCD-EV"},))
        ladder = LC(Decimal(100), Decimal(100), True, True, False, False, "ok")
        return _WR(close_time=CLOSE, fifteen_leg=fifteen, hourly_leg=hourly, ladder=ladder)

    svc = WindowService(
        close_time=CLOSE, cli_mode="armed", cli_strategy="nonsense",
        proxy=FakeProxy(), falsifier_path=_frozen_falsifier(tmp_path),
        mode_txt_path=os.path.join(tmp_path, "mode.txt"),
        journal_dir=os.path.join(tmp_path, "journals"),
        ledger_path=os.path.join(tmp_path, "ledger", "pilot_ledger.jsonl"),
        wake_context=FakeWake(_corridor_wake()),
        health_get=(lambda: GOOD_HEALTH), positions_reader=(lambda: {"market_positions": []}),
        balance_get=(lambda: {"balance": 1000000, "balance_dollars": "10000.00",
                              "portfolio_value": "0"}),
        window_driver=(lambda r, d: None),
        anchor_fetcher=(lambda: list(_corridor_wake().fifteen_leg.markets)),
        clock=(lambda: _FIXED_NOW), poll_sleep=(lambda d: None),
        strategy_txt_path=os.path.join(tmp_path, "strategy.txt"),  # absent -> would be corridor
    )
    plan = svc.prepare()
    assert plan.strategy == "corridor" and plan.strategy_valid is False
    assert plan.armed is False and plan.degraded is True
    kinds = [r["kind"] for r in svc.journal.records()]
    assert "strategy_invalid" in kinds and "strategy_selected" in kinds


def test_box_no_anchor_stands_down(tmp_path):
    """The 15M strike never materializes -> box stands down cleanly (EXCL_NO_ANCHOR), writes a row."""
    open_epoch = close_epoch("2026-08-21T21:45:00Z")
    late_clock = float(open_epoch + 50)  # past the poll deadline (open+45)

    def _no_strike():
        # a 15M market WITHOUT floor_strike -> _anchor_at returns (None, None)
        return [{"ticker": M15_TICKER, "close_time": CLOSE, "open_time": "2026-08-21T21:45:00Z",
                 "status": "active", "event_ticker": "KXBTC15M-EV"}]

    svc = _box_svc(tmp_path, mode="shakedown", window_driver=(lambda r, d: None))
    svc._anchor_fetcher = _no_strike
    svc.clock = lambda: late_clock
    plan = svc.prepare()
    code = svc.execute(plan)
    assert code == 0 and plan.stand_down is True
    rows = load_entries(svc.ledger_path)
    assert rows and rows[-1]["strategy"] == "box" and rows[-1]["stand_down"] is True


# ===========================================================================
# WIDE_BOX constant is single-sourced (ledger local const must match box)
# ===========================================================================
def test_wide_box_constant_matches():
    assert box_mod.WIDE_BOX == ledger_mod.WIDE_BOX == "wide-box"


# ===========================================================================
# service fakes (box + armed) — no network
# ===========================================================================
GOOD_HEALTH = {
    "orders_enabled": True,
    "caps": {"max_contracts_per_order": 2, "ticker_prefixes": ["KXBTC15M", "KXBTCD"],
             "daily_order_budget": 100},
}


class FakeWake:
    def __init__(self, result):
        self._r = result

    def sweep(self, close):
        return self._r


class FakeProxy:
    def rest_get(self, path, params=None):
        return {}


class _Resp:
    def __init__(self, body):
        self._b = body
        self.status_code = 200

    def json(self):
        return self._b


class FakePost:
    """A post_fn returning controllable fills. ``entry_fills`` maps ticker -> (fill_count, raw_price,
    fee); a missing ticker is a no-fill. ``raw_price`` is the RAW venue (YES-space) price the parse
    layer normalizes to the leg's side. ``flatten_fill`` is the single-leg flatten response."""

    def __init__(self, entry_fills, flatten_fill=None):
        self.entry_fills = entry_fills
        self.flatten_fill = flatten_fill
        self.calls = []

    def _slot(self, entry, fill):
        cid = entry.get("client_order_id")
        if fill is None:
            return {"client_order_id": cid, "order_id": None, "fill_count": "0",
                    "remaining_count": "1", "average_fill_price": None,
                    "average_fee_paid": None, "ts_ms": 123}
        fc, raw, fee = fill
        return {"client_order_id": cid, "order_id": "oid", "fill_count": str(fc),
                "remaining_count": "0", "average_fill_price": str(raw),
                "average_fee_paid": str(fee), "ts_ms": 123}

    def __call__(self, path, body):
        self.calls.append((path, body))
        if isinstance(body.get("orders"), list):
            slots = [self._slot(e, self.entry_fills.get(e["ticker"])) for e in body["orders"]]
            return _Resp({"orders": slots})
        return _Resp({"order": self._slot(body, self.flatten_fill)})


def _box_wake():
    """A wake whose 15M leg anchors at A and whose hourly ladder has a qualifying K=64000 (< A)."""
    fifteen = Leg(
        series="KXBTC15M", event_ticker="KXBTC15M-EV", open_time="2026-08-21T21:45:00Z",
        close_time=CLOSE, window_seconds=900, market_tickers=(M15_TICKER,),
        floor_strikes=(65000.0,),
        markets=({"ticker": M15_TICKER, "floor_strike": 65000.0, "close_time": CLOSE,
                  "open_time": "2026-08-21T21:45:00Z", "status": "active",
                  "event_ticker": "KXBTC15M-EV"},),
    )
    hmarkets = tuple(
        {"ticker": tk, "floor_strike": fs, "close_time": CLOSE,
         "open_time": "2026-08-21T21:00:00Z", "status": "active", "event_ticker": "KXBTCD-EV"}
        for tk, fs in (("KXBTCD-63500", 63500.0), (HOURLY_TICKER, 64000.0), ("KXBTCD-66000", 66000.0))
    )
    hourly = Leg(
        series="KXBTCD", event_ticker="KXBTCD-EV", open_time="2026-08-21T21:00:00Z",
        close_time=CLOSE, window_seconds=3600,
        market_tickers=tuple(m["ticker"] for m in hmarkets),
        floor_strikes=tuple(m["floor_strike"] for m in hmarkets), markets=hmarkets,
    )
    ladder = LadderCheck(expected_step=Decimal(100), observed_step=Decimal(500), uniform=True,
                         ok=False, strangle_disabled=True, alarm=True, reason="deviation")
    return WakeResult(close_time=CLOSE, fifteen_leg=fifteen, hourly_leg=hourly, ladder=ladder,
                      balance=None, affordable=None)


_FIXED_NOW = float(close_epoch("2026-08-21T21:45:05Z"))  # after 15M open -> poll resolves at once


def _frozen_falsifier(tmp_path, name="falsifier_frozen.md"):
    p = os.path.join(tmp_path, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write("# X\nSTATUS: FROZEN\n")
    return p


def _draft_box_falsifier(tmp_path, name="box_falsifier_draft.md"):
    p = os.path.join(tmp_path, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write("# BOX FALSIFIER\nSTATUS: DRAFT\n")
    return p


def _box_svc(tmp_path, *, mode="armed", window_driver=None, post_fn=None, wake=None,
             market_result_getter=None, balance=1000000, ledger_path=None,
             box_falsifier_path=None):
    w = wake if wake is not None else _box_wake()
    return WindowService(
        close_time=CLOSE,
        cli_mode=mode,
        cli_strategy="box",
        pairs=1,
        proxy=FakeProxy(),
        falsifier_path=_frozen_falsifier(tmp_path),
        # F2: the box arms against its OWN falsifier; default the tests to a FROZEN box falsifier so
        # the armed-path wiring tests can arm. Tests that probe the S5 gate pass their own path.
        box_falsifier_path=(box_falsifier_path
                            if box_falsifier_path is not None
                            else _frozen_falsifier(tmp_path, "box_falsifier_frozen.md")),
        mode_txt_path=os.path.join(tmp_path, "mode.txt"),
        journal_dir=os.path.join(tmp_path, "journals"),
        ledger_path=ledger_path or os.path.join(tmp_path, "ledger", "pilot_ledger.jsonl"),
        wake_context=FakeWake(w),
        health_get=(lambda: GOOD_HEALTH),
        positions_reader=(lambda: {"market_positions": []}),
        # repairs/instruments F2: parse_balance requires BOTH balance (int cents) and balance_dollars.
        balance_get=(None if balance is None
                     else (lambda: {"balance": balance,
                                    "balance_dollars": str(Decimal(balance) / 100),
                                    "portfolio_value": "0"})),
        window_driver=window_driver,
        post_fn=post_fn,
        anchor_fetcher=(lambda: list(w.fifteen_leg.markets)),
        clock=(lambda: _FIXED_NOW),
        poll_sleep=(lambda d: None),
        market_result_getter=market_result_getter,
    )


def _fire_below(recorder, *, ts=None, hourly=("0.94", "0.92"), populate_books=None):
    """Drive the recorder's box driver to fire a BELOW-case box on K=64000 (15M NO + hourly YES)."""
    ts = ts if ts is not None else (T - 300)
    if populate_books:
        for tk, tp in populate_books.items():
            recorder.books[tk] = _FakeBook(tp)
    recorder.driver.on_book_update(HOURLY_TICKER, top(yes_ask=hourly[0], yes_bid=hourly[1]), ts)
    recorder.driver.on_book_update(M15_TICKER, top(yes_ask="0.20", yes_bid="0.14"), ts)


class _FakeBook:
    def __init__(self, tp):
        self._tp = tp

    def top_of_book(self):
        return self._tp


# ===========================================================================
# 2) box PHASE-B state build + 3) subscription + 4) routing
# ===========================================================================
def test_box_phase_b_builds_state_from_wake_markets(tmp_path):
    captured = {}

    def driver(recorder, deadline):
        captured["recorder"] = recorder

    svc = _box_svc(tmp_path, mode="shakedown", window_driver=driver)
    plan = svc.prepare()
    assert plan.strategy == "box" and plan.box_policy is not None
    svc.execute(plan)
    st = svc._current_box_state
    assert isinstance(st, BoxState)
    assert st.anchor_A == A and st.m15_ticker == M15_TICKER
    assert st.T == T
    # ladder strikes are Decimal, from the chosen (selected) hourly generation's markets
    assert st.strikes == {
        "KXBTCD-63500": Decimal("63500"), HOURLY_TICKER: Decimal("64000"),
        "KXBTCD-66000": Decimal("66000"),
    }
    assert all(isinstance(v, Decimal) for v in st.strikes.values())
    # shakedown mode -> box shakedown state
    assert st.shakedown is True


def test_box_subscription_is_full_ladder_plus_15m(tmp_path):
    svc = _box_svc(tmp_path, mode="shakedown", window_driver=(lambda r, d: None))
    subs = svc._box_subscription_tickers(_box_wake(), M15_TICKER)
    assert set(subs) == {"KXBTCD-63500", HOURLY_TICKER, "KXBTCD-66000", M15_TICKER}


def test_box_routes_book_updates_for_arbitrary_ladder_tickers(tmp_path):
    st = BoxState.new(close_time=CLOSE, anchor_A=A, m15_ticker=M15_TICKER,
                      strikes={HOURLY_TICKER: Decimal("64000"), "KXBTCD-63500": Decimal("63500")},
                      shakedown=True, T=T)
    from service.journal import Journal
    drv = BoxSignalDriver(BOX_PARAMS, st, Journal(), clock=lambda: 0.0)
    # an arbitrary ladder ticker's update is folded into state (routing reaches decide_box)
    drv.on_book_update("KXBTCD-63500", top(yes_ask="0.98", yes_bid="0.96"), T - 300)
    assert "KXBTCD-63500" in drv.state.tops
    # an unsubscribed ticker is ignored by decide_box
    drv.on_book_update("SOMETHING-ELSE", top(yes_ask="0.5", yes_bid="0.4"), T - 300)
    assert "SOMETHING-ELSE" not in drv.state.tops


# ===========================================================================
# 5) box_eval throttling
# ===========================================================================
def _eval_count(journal):
    return sum(1 for r in journal.records() if r["kind"] == "box_eval")


def test_box_eval_throttled_by_change_and_heartbeat():
    from service.journal import Journal
    j = Journal()
    st = BoxState.new(close_time=CLOSE, anchor_A=A, m15_ticker=M15_TICKER,
                      strikes={HOURLY_TICKER: Decimal("64000")}, shakedown=True, T=T)
    drv = BoxSignalDriver(BOX_PARAMS, st, j, clock=lambda: 0.0)
    # first m15 + hourly at t0 -> a selection view emerges -> 1 eval (on the m15 update that first
    # yields a view; the hourly-only update before m15 yields no view).
    drv.on_book_update(HOURLY_TICKER, top(yes_ask="0.94", yes_bid="0.92"), T - 700)
    drv.on_book_update(M15_TICKER, top(yes_ask="0.20", yes_bid="0.14"), T - 700)
    assert _eval_count(j) == 1
    # same selection, 3s later, no heartbeat window elapsed -> NO new eval
    drv.on_book_update(HOURLY_TICKER, top(yes_ask="0.94", yes_bid="0.92"), T - 697)
    assert _eval_count(j) == 1
    # >10s later, same selection -> heartbeat eval
    drv.on_book_update(HOURLY_TICKER, top(yes_ask="0.94", yes_bid="0.92"), T - 685)
    assert _eval_count(j) == 2
    # selection changes (hourly ask now out of range -> NoBox reason) -> change eval
    drv.on_book_update(HOURLY_TICKER, top(yes_ask="0.80", yes_bid="0.78"), T - 684)
    assert _eval_count(j) == 3


# ===========================================================================
# 6) FIRE -> Intent (tickers/sides/limits/count) + 7) ledger slot mapping
# ===========================================================================
def test_box_fire_builds_intent_and_ledger_mapping(tmp_path):
    post = FakePost(entry_fills={HOURLY_TICKER: (1, "0.94", "0.01"),
                                 M15_TICKER: (1, "0.14", "0.01")})  # 15M NO fill: raw 0.14 -> 0.86

    def driver(recorder, deadline):
        _fire_below(recorder)

    svc = _box_svc(tmp_path, mode="armed", window_driver=driver, post_fn=post)
    plan = svc.prepare()
    assert plan.armed is True
    svc.execute(plan)
    ls = svc.ledger_state
    assert ls is not None
    # ledger slot mapping: high = hourly leg, low = 15M leg
    assert ls.high_ticker == HOURLY_TICKER and ls.low_ticker == M15_TICKER
    assert ls.high_side == BUY_YES and ls.low_side == BUY_NO
    # the entry intent: count 1 each, sides correct, limits = observed ask + 0.03 margin (capped)
    entry = next(i for i in ls.intents if i.purpose == PURPOSE_ENTRY)
    legs = {lg.ticker: lg for lg in entry.legs}
    assert legs[HOURLY_TICKER].side == BUY_YES and legs[HOURLY_TICKER].count == 1
    assert legs[HOURLY_TICKER].limit_price == Decimal("0.97")   # 0.94 + 0.03
    assert legs[M15_TICKER].side == BUY_NO and legs[M15_TICKER].count == 1
    assert legs[M15_TICKER].limit_price == Decimal("0.89")      # 0.86 + 0.03
    assert entry.source == WIDE_BOX
    # both filled -> held (net 1 each), NO flatten order was sent (only the entry batch)
    assert ls.net("high") == 1 and ls.net("low") == 1
    assert len(post.calls) == 1 and isinstance(post.calls[0][1].get("orders"), list)


# ===========================================================================
# 8) both-filled hold: realized_at_close floor + pin-region truth table + backfill
# ===========================================================================
def _box_both_filled_ledger(hourly_price="0.94", m15_no_price="0.86", fee="0.01"):
    ls = new_ledger(CLOSE, WIDE_BOX, high_ticker=HOURLY_TICKER, low_ticker=M15_TICKER)
    entry = Intent(
        window=CLOSE, source=WIDE_BOX, purpose=PURPOSE_ENTRY,
        legs=(
            IntentLeg(HOURLY_TICKER, BUY_YES, "buy", 1, Decimal("0.97"), "cid-h"),
            IntentLeg(M15_TICKER, BUY_NO, "buy", 1, Decimal("0.89"), "cid-m"),
        ),
    )
    ls = record_intent(ls, entry)
    # hourly YES fill at hourly_price (yes-space)
    ls = record_response(ls, OrderResponse(
        client_order_id="cid-h", order_id="o1", fill_count=Decimal(1), remaining_count=Decimal(0),
        average_fill_price=Decimal(hourly_price), average_fee_paid=Decimal(fee),
        raw_reported_price=Decimal(hourly_price), ts_ms=1))
    # 15M NO fill at m15_no_price (NO-space)
    ls = record_response(ls, OrderResponse(
        client_order_id="cid-m", order_id="o2", fill_count=Decimal(1), remaining_count=Decimal(0),
        average_fill_price=Decimal(m15_no_price), average_fee_paid=Decimal(fee),
        raw_reported_price=Decimal(str(Decimal(1) - Decimal(m15_no_price))), ts_ms=1))
    return ls


def test_box_realized_at_close_books_one_dollar_floor():
    ls = _box_both_filled_ledger()
    cost = (Decimal("0.94") + Decimal("0.01")) + (Decimal("0.86") + Decimal("0.01"))  # 1.82
    assert ls.matched_pairs() == 1
    assert ls.realized_at_close() == Decimal(1) - cost   # 1 - 1.82 = -0.82 (floor booked)
    # BOTH held legs are pending settlement (the +$1 pinned bonus is backfilled)
    unsettled = {(t, s) for (t, s, c) in ls.unsettled_legs()}
    assert unsettled == {(HOURLY_TICKER, BUY_YES), (M15_TICKER, BUY_NO)}


@pytest.mark.parametrize("region,hourly_res,m15_res,expect_payoff", [
    # BELOW case: 15M NO (held 'no', wins when 15M result 'no' == BTC<A), hourly YES on K<A (held
    # 'yes', wins when hourly result 'yes' == BTC>=K). Pin region [K, A). Market 'result' is the
    # YES-outcome of each market.
    ("btc<K",     "no",  "no",  Decimal(1)),  # hourly loses, 15M NO wins -> $1 floor
    ("K<=btc<A",  "yes", "no",  Decimal(2)),  # BOTH win -> $2 pinned
    ("btc>=A",    "yes", "yes", Decimal(1)),  # hourly wins, 15M NO loses -> $1 floor
])
def test_box_pin_region_truth_table_below(region, hourly_res, m15_res, expect_payoff):
    ls = _box_both_filled_ledger()
    legs = list(ls.unsettled_legs())
    results = {HOURLY_TICKER: hourly_res, M15_TICKER: m15_res}
    payoff = settlement_payoff(legs, results)
    assert payoff == expect_payoff, region
    # backfill nets the $1 floor already booked at close: pinned -> +$1, else +$0
    entry = {"close_time": CLOSE, "unsettled_legs": [{"ticker": t, "side": s, "count": c}
                                                     for (t, s, c) in legs],
             "floor_booked": "1.00"}
    bf = build_backfill_entry(entry, results, payoff, 0.0)
    assert Decimal(bf["realized_delta"]) == expect_payoff - Decimal(1)


def test_box_pin_region_truth_table_above():
    """Mirror (ABOVE case): 15M YES (wins BTC>=A), hourly NO on K>A (wins BTC<K). Pin region [A, K)."""
    ls = new_ledger(CLOSE, WIDE_BOX, high_ticker="KXBTCD-66000", low_ticker=M15_TICKER)
    entry = Intent(window=CLOSE, source=WIDE_BOX, purpose=PURPOSE_ENTRY, legs=(
        IntentLeg("KXBTCD-66000", BUY_NO, "buy", 1, Decimal("0.97"), "cid-h"),
        IntentLeg(M15_TICKER, BUY_YES, "buy", 1, Decimal("0.89"), "cid-m"),
    ))
    ls = record_intent(ls, entry)
    ls = record_response(ls, OrderResponse("cid-h", "o1", Decimal(1), Decimal(0), Decimal("0.94"),
                                           Decimal("0.01"), 1, raw_reported_price=Decimal("0.06")))
    ls = record_response(ls, OrderResponse("cid-m", "o2", Decimal(1), Decimal(0), Decimal("0.86"),
                                           Decimal("0.01"), 1, raw_reported_price=Decimal("0.86")))
    legs = list(ls.unsettled_legs())
    # btc < A: hourly NO wins (result 'no'), 15M YES loses (result 'no') -> $1
    assert settlement_payoff(legs, {"KXBTCD-66000": "no", M15_TICKER: "no"}) == Decimal(1)
    # A <= btc < K (pinned): hourly NO wins (result 'no'), 15M YES wins (result 'yes') -> $2
    assert settlement_payoff(legs, {"KXBTCD-66000": "no", M15_TICKER: "yes"}) == Decimal(2)
    # btc >= K: hourly NO loses (result 'yes'), 15M YES wins (result 'yes') -> $1
    assert settlement_payoff(legs, {"KXBTCD-66000": "yes", M15_TICKER: "yes"}) == Decimal(1)


# ===========================================================================
# 9) one-leg flatten (fill / miss->retry / no bid)
# ===========================================================================
def test_box_one_leg_flatten_fills(tmp_path):
    # only the hourly leg fills; the 15M leg is a no-fill -> flatten the hourly YES at its bid.
    post = FakePost(entry_fills={HOURLY_TICKER: (1, "0.94", "0.01")},
                    flatten_fill=(1, "0.93", "0.01"))

    def driver(recorder, deadline):
        _fire_below(recorder, populate_books={HOURLY_TICKER: top(yes_ask="0.95", yes_bid="0.93")})

    svc = _box_svc(tmp_path, mode="armed", window_driver=driver, post_fn=post)
    plan = svc.prepare()
    svc.execute(plan)
    ls = svc.ledger_state
    assert ls.net("high") == 0   # hourly flattened flat
    # F3: the ENTRY was one-legged (latched, NOT reset by the flatten); the flatten OUTCOME is
    # recorded separately and here it filled.
    assert svc._box_one_legged is True
    assert svc._box_flatten_filled is True
    # exactly two dispatches: the entry batch + one flatten single (single has no "orders" list)
    assert len(post.calls) == 2
    assert isinstance(post.calls[0][1].get("orders"), list)
    assert not isinstance(post.calls[1][1].get("orders"), list)
    # the ledger row carries both fields (A5 uses box_one_legged; box_flatten_filled is the outcome)
    row = svc._build_box_ledger_entry(plan, svc._recorder, 0, "j", 0)
    assert row["box_one_legged"] is True and row["box_flatten_filled"] is True


def test_box_one_leg_flatten_misses_then_retries_then_holds(tmp_path):
    # F4: retries are EVENT-DRIVEN, each priced at the FRESH bid of a later book frame. The flatten
    # never fills -> 3 attempts max, then A_FLATTEN_EXHAUSTED + hold naked. This asserts (a) the bid
    # CHANGES between attempts are used, (b) at most 3 attempts, (c) a 4th frame is a no-op.
    post = FakePost(entry_fills={HOURLY_TICKER: (1, "0.94", "0.01")}, flatten_fill=None)  # never fills

    def driver(recorder, deadline):
        # fire + first flatten attempt at bid 0.93
        _fire_below(recorder, populate_books={HOURLY_TICKER: top(yes_ask="0.95", yes_bid="0.93")})
        # each later frame carries a FRESH bid and a later server_ts (t_minus shrinks toward settle)
        recorder.books[HOURLY_TICKER] = _FakeBook(top(yes_ask="0.94", yes_bid="0.90"))
        recorder._on_book_event(HOURLY_TICKER, T - 299)   # attempt 2 @ 0.90
        recorder.books[HOURLY_TICKER] = _FakeBook(top(yes_ask="0.93", yes_bid="0.88"))
        recorder._on_book_event(HOURLY_TICKER, T - 298)   # attempt 3 @ 0.88 -> exhausted
        recorder.books[HOURLY_TICKER] = _FakeBook(top(yes_ask="0.92", yes_bid="0.86"))
        recorder._on_book_event(HOURLY_TICKER, T - 297)   # pending cleared -> no-op

    svc = _box_svc(tmp_path, mode="armed", window_driver=driver, post_fn=post)
    svc.execute(svc.prepare())
    ls = svc.ledger_state
    assert ls.net("high") == 1   # still held (flatten never filled)
    assert svc._box_one_legged is True         # entry quality latched
    assert svc._box_flatten_filled is False    # held naked (flatten never filled)
    assert svc._pending_flatten is None        # cleared after exhaustion
    # entry batch + exactly 3 flatten attempts (F4: 3 max, not the old 4)
    flatten_calls = [c for c in post.calls if not isinstance(c[1].get("orders"), list)]
    assert len(flatten_calls) == 3
    # each attempt priced a DIFFERENT fresh bid (a YES sell wires price == the yes_bid it saw)
    prices = [Decimal(c[1]["price"]) for c in flatten_calls]
    assert prices == [Decimal("0.93"), Decimal("0.90"), Decimal("0.88")]
    alarms = [n.kind for n in svc.stops.state.alarms]
    assert "A_FLATTEN_EXHAUSTED" in alarms
    kinds = [r["kind"] for r in svc.journal.records()]
    assert "box_flatten" in kinds


def test_box_flatten_retry_respects_no_orders_cutoff(tmp_path):
    # F4: the no-orders-to-settle cutoff (t < 1s) STILL stops flatten retries -> hold naked, no order.
    post = FakePost(entry_fills={HOURLY_TICKER: (1, "0.94", "0.01")}, flatten_fill=None)  # never fills

    def driver(recorder, deadline):
        _fire_below(recorder, populate_books={HOURLY_TICKER: top(yes_ask="0.95", yes_bid="0.93")})
        # a later frame INSIDE the settle cutoff (t_minus = 0.5s < 1s): must NOT place a retry
        recorder.books[HOURLY_TICKER] = _FakeBook(top(yes_ask="0.94", yes_bid="0.90"))
        recorder._on_book_event(HOURLY_TICKER, T - 0.5)

    svc = _box_svc(tmp_path, mode="armed", window_driver=driver, post_fn=post)
    svc.execute(svc.prepare())
    # only attempt 1 (at T-300, outside the cutoff) was dispatched; the T-0.5 frame was held
    flatten_calls = [c for c in post.calls if not isinstance(c[1].get("orders"), list)]
    assert len(flatten_calls) == 1
    assert svc._pending_flatten is None
    assert svc._box_flatten_filled is False
    assert "A_FLATTEN_EXHAUSTED" not in [n.kind for n in svc.stops.state.alarms]
    stages = [r["obj"].get("stage") for r in svc.journal.records() if r["kind"] == "box_flatten"]
    assert "cutoff_hold" in stages


def test_box_one_leg_flatten_no_bid_holds_and_alarms(tmp_path):
    post = FakePost(entry_fills={HOURLY_TICKER: (1, "0.94", "0.01")}, flatten_fill=(1, "0.93", "0.01"))

    def driver(recorder, deadline):
        _fire_below(recorder)  # do NOT populate books -> no bid available

    svc = _box_svc(tmp_path, mode="armed", window_driver=driver, post_fn=post)
    svc.execute(svc.prepare())
    ls = svc.ledger_state
    assert ls.net("high") == 1   # held (no bid to flatten against)
    assert svc._box_one_legged is True         # entry quality latched
    assert svc._box_flatten_filled is False    # held naked (no bid to flatten into)
    assert svc._pending_flatten is None
    # no flatten order was ever dispatched
    assert len(post.calls) == 1
    alarms = [n.kind for n in svc.stops.state.alarms]
    assert "A_FLATTEN_NO_BID" in alarms


# ===========================================================================
# F2) box arms against ITS OWN falsifier (box_falsifier.md), never the corridor's
# ===========================================================================
def _arming_record(svc):
    return next((r["obj"] for r in svc.journal.records() if r["kind"] == "arming"), None)


def test_box_arming_refuses_draft_box_falsifier(tmp_path):
    # box + a DRAFT box falsifier -> S5 refuses; even though the CORRIDOR falsifier (default) is FROZEN.
    draft = _draft_box_falsifier(tmp_path)
    svc = _box_svc(tmp_path, mode="armed", window_driver=(lambda r, d: None),
                   box_falsifier_path=draft)
    plan = svc.prepare()
    assert plan.armed is False and plan.degraded is True
    assert "STATUS: FROZEN" in (plan.degrade_reason or "")
    arm = _arming_record(svc)
    assert arm is not None
    assert arm["falsifier_basename"] == os.path.basename(draft)
    assert arm["strategy"] == "box"
    assert arm["armed"] is False


def test_box_arms_with_frozen_box_falsifier(tmp_path):
    # box + a FROZEN box falsifier + frozen roster sha + good health -> arms, against the BOX file.
    frozen_box = _frozen_falsifier(tmp_path, "box_falsifier_frozen.md")
    svc = _box_svc(tmp_path, mode="armed", window_driver=(lambda r, d: None),
                   box_falsifier_path=frozen_box)
    plan = svc.prepare()
    assert plan.armed is True
    arm = _arming_record(svc)
    assert arm["falsifier_basename"] == os.path.basename(frozen_box)
    assert arm["strategy"] == "box"


def test_box_never_consults_corridor_falsifier(tmp_path):
    # The corridor falsifier is FROZEN (default in _box_svc) but the box falsifier is DRAFT: the box
    # must STILL refuse — proof S5 reads box_falsifier.md for the box, not falsifier.md.
    draft = _draft_box_falsifier(tmp_path)
    svc = _box_svc(tmp_path, mode="armed", window_driver=(lambda r, d: None),
                   box_falsifier_path=draft)
    plan = svc.prepare()
    assert plan.armed is False
    # the reason names the BOX falsifier basename, never the corridor's
    assert os.path.basename(draft) in (plan.degrade_reason or "")
    assert "falsifier_frozen.md" not in (plan.degrade_reason or "")


def test_arming_check_strategy_gate_and_falsifier_selection():
    # unit level: with expected_strategy pinned, a non-"box" resolved strategy refuses even when
    # everything else is green; and without expected_strategy the strategy is ignored (corridor path).
    import tempfile
    d = tempfile.mkdtemp()
    fp = os.path.join(d, "f.md")
    open(fp, "w", encoding="utf-8").write("STATUS: FROZEN\n")
    dec_ok = arming_check(fp, GOOD_HEALTH, True, strategy="box", expected_strategy="box")
    assert isinstance(dec_ok, ArmDecision) and dec_ok.armed is True
    dec_bad = arming_check(fp, GOOD_HEALTH, True, strategy="corridor", expected_strategy="box")
    assert dec_bad.armed is False and any("strategy" in r for r in dec_bad.reasons)
    # corridor path (no expected_strategy) ignores the strategy value entirely
    dec_corr = arming_check(fp, GOOD_HEALTH, True)
    assert dec_corr.armed is True


# ===========================================================================
# 10) check_s1_box both branches + non-trip on a normal 1.85 pair + corridor S1 scoping
# ===========================================================================
def test_check_s1_box_trips_on_booked_cost_over_ceiling():
    ls = _box_both_filled_ledger(hourly_price="0.99", m15_no_price="0.99", fee="0.02")
    # cost = (0.99+0.02)+(0.99+0.02) = 2.02 > 1.99
    assert check_s1_box(ls, BOX_PARAMS.pair_cost_max) is not None


def test_check_s1_box_trips_on_units_tripwire():
    # a fill ABOVE its own limit (units/side-space corruption) trips even under the cost ceiling.
    ls = new_ledger(CLOSE, WIDE_BOX, high_ticker=HOURLY_TICKER, low_ticker=M15_TICKER)
    entry = Intent(window=CLOSE, source=WIDE_BOX, purpose=PURPOSE_ENTRY, legs=(
        IntentLeg(HOURLY_TICKER, BUY_YES, "buy", 1, Decimal("0.90"), "cid-h"),
        IntentLeg(M15_TICKER, BUY_NO, "buy", 1, Decimal("0.89"), "cid-m"),
    ))
    ls = record_intent(ls, entry)
    ls = record_response(ls, OrderResponse("cid-h", "o", Decimal(1), Decimal(0), Decimal("0.95"),
                                           Decimal("0.00"), 1, raw_reported_price=Decimal("0.95")))
    reason = check_s1_box(ls, BOX_PARAMS.pair_cost_max)
    assert reason is not None and "units tripwire" in reason


def test_check_s1_box_does_not_trip_on_normal_185_pair():
    ls = _box_both_filled_ledger(hourly_price="0.94", m15_no_price="0.88", fee="0.005")
    # cost ~ (0.945)+(0.885) = 1.83 < 1.99, fills at/below limit (0.97 / 0.89) -> no trip
    assert check_s1_box(ls, BOX_PARAMS.pair_cost_max) is None
    # and the CORRIDOR S1 (realized_min<0) must NOT apply to the box (scoped to sub-$1 flip)
    assert check_s1(ls) is None


# ===========================================================================
# 11) A5 one-legged-rate counter
# ===========================================================================
def test_box_one_legged_rate_counter():
    entries = []
    # 9 clean box fires, 1 one-legged -> 1/10 = 0.10 (NOT > 0.10)
    for i in range(9):
        entries.append({"fires": 1, "fired_source": WIDE_BOX, "box_one_legged": False,
                        "close_time": f"2026-08-25T{i:02d}:00:00Z"})
    entries.append({"fires": 1, "fired_source": WIDE_BOX, "box_one_legged": True,
                    "close_time": "2026-08-25T09:00:00Z"})
    rate, ol, total = box_one_legged_rate(entries, n=20)
    assert (ol, total) == (1, 10) and rate == Decimal("0.1")
    # a second one-legged fire -> 2/11 > 0.10
    entries.append({"fires": 1, "fired_source": WIDE_BOX, "box_one_legged": True,
                    "close_time": "2026-08-25T10:00:00Z"})
    rate2, ol2, total2 = box_one_legged_rate(entries, n=20)
    assert ol2 == 2 and total2 == 11 and rate2 > Decimal("0.10")
    # non-box + backfill rows are ignored
    entries.append({"fires": 0, "fired_source": WIDE_BOX, "box_one_legged": True})  # backfill
    entries.append({"fires": 1, "fired_source": "sub$1-flip", "box_one_legged": True})  # corridor
    rate3, ol3, total3 = box_one_legged_rate(entries, n=20)
    assert (ol3, total3) == (2, 11)


# ===========================================================================
# settlement-backfill automation (spec item 5) — mocked market results
# ===========================================================================
def test_settlement_backfill_sweep_pinned_and_idempotent(tmp_path):
    ledger_path = os.path.join(tmp_path, "ledger", "pilot_ledger.jsonl")
    # a prior box window that held both legs (pinned truth) with the $1 floor booked
    append_entry({
        "close_time": "2026-08-21T20:00:00Z", "strategy": "box", "fires": 1,
        "fired_source": WIDE_BOX, "pairs": 1, "realized_delta": "-0.82",
        "realized_unsettled": True, "floor_booked": "1.00",
        "unsettled_legs": [{"ticker": HOURLY_TICKER, "side": "yes", "count": 1},
                           {"ticker": M15_TICKER, "side": "no", "count": 1}],
    }, ledger_path)
    # market results: hourly YES won, 15M NO won -> pinned -> payoff $2, net +$1 after floor
    results = {HOURLY_TICKER: "yes", M15_TICKER: "no"}
    svc = _box_svc(tmp_path, mode="shakedown", window_driver=(lambda r, d: None),
                   ledger_path=ledger_path,
                   market_result_getter=(lambda tk: results.get(tk)))
    svc._settlement_backfill_sweep()
    rows = load_entries(ledger_path)
    bf = [r for r in rows if r.get("backfill_of") == "2026-08-21T20:00:00Z"]
    assert len(bf) == 1
    assert Decimal(bf[0]["realized_delta"]) == Decimal(1)  # +$1 pinned bonus, floor netted
    # idempotent: a second sweep adds nothing
    svc2 = _box_svc(tmp_path, mode="shakedown", window_driver=(lambda r, d: None),
                    ledger_path=ledger_path, market_result_getter=(lambda tk: results.get(tk)))
    svc2._settlement_backfill_sweep()
    rows2 = load_entries(ledger_path)
    assert len([r for r in rows2 if r.get("backfill_of") == "2026-08-21T20:00:00Z"]) == 1


def test_settlement_backfill_waits_when_unsettled(tmp_path):
    ledger_path = os.path.join(tmp_path, "ledger", "pilot_ledger.jsonl")
    append_entry({
        "close_time": "2026-08-21T20:00:00Z", "strategy": "box", "fires": 1,
        "fired_source": WIDE_BOX, "realized_delta": "-0.50", "realized_unsettled": True,
        "floor_booked": "0", "unsettled_legs": [{"ticker": HOURLY_TICKER, "side": "yes", "count": 1}],
    }, ledger_path)
    svc = _box_svc(tmp_path, mode="shakedown", window_driver=(lambda r, d: None),
                   ledger_path=ledger_path, market_result_getter=(lambda tk: None))  # not settled
    svc._settlement_backfill_sweep()
    rows = load_entries(ledger_path)
    assert not any(r.get("backfill_of") for r in rows)


def test_parse_market_result():
    assert _parse_market_result({"market": {"ticker": "X", "result": "yes"}}, "X") == "yes"
    assert _parse_market_result({"markets": [{"ticker": "X", "result": "no"}]}, "X") == "no"
    assert _parse_market_result({"markets": [{"ticker": "X", "result": ""}]}, "X") is None
    assert _parse_market_result({}, "X") is None


def test_parse_market_result_requires_exact_ticker_F1():
    # F1: a FOREIGN market's result must NEVER be attributed to our ticker (that booked a fictitious
    # win). Both shapes require an exact ticker match; a mismatch returns None -> the backfill waits.
    # list branch: requested ticker absent -> None (no markets[0] fallback)
    assert _parse_market_result(
        {"markets": [{"ticker": "KXBTCD-99999", "result": "yes"}]}, "KXBTCD-64000") is None
    # {"market": {...}} branch: unlabelled/foreign market is NOT trusted
    assert _parse_market_result({"market": {"ticker": "whatever", "result": "no"}},
                                "KXBTCD-64000") is None
    # a market with no ticker key at all -> None
    assert _parse_market_result({"market": {"result": "yes"}}, "X") is None
    # happy path still returns the exact match even when foreign markets share the list
    assert _parse_market_result(
        {"markets": [{"ticker": "KXBTCD-99999", "result": "yes"},
                     {"ticker": "KXBTCD-64000", "result": "no"}]}, "KXBTCD-64000") == "no"


# ===========================================================================
# golden determinism of the box decision over a replayed event list
# ===========================================================================
def _event_list():
    return [
        BookUpdate(HOURLY_TICKER, top(yes_ask="0.995", yes_bid="0.99"), T - 600),
        BookUpdate(M15_TICKER, top(yes_ask="0.20", yes_bid="0.14"), T - 600),  # hourly out of range
        ClockTick(T - 550),
        BookUpdate("KXBTCD-63500", top(yes_ask="0.94", yes_bid="0.92"), T - 540),  # K=63500 qualifies
        BookUpdate(M15_TICKER, top(yes_ask="0.20", yes_bid="0.14"), T - 540),      # fires here
        ClockTick(T - 500),
    ]


def _replay(events):
    from service.journal import Journal
    st = BoxState.new(close_time=CLOSE, anchor_A=A, m15_ticker=M15_TICKER,
                      strikes={HOURLY_TICKER: Decimal("64000"), "KXBTCD-63500": Decimal("63500")},
                      shakedown=True, T=T)
    drv = BoxSignalDriver(BOX_PARAMS, st, Journal(), clock=lambda: 0.0)
    for ev in events:
        if isinstance(ev, BookUpdate):
            drv.on_book_update(ev.market, ev.top, ev.server_ts)
        else:
            drv.on_clock_tick(ev.server_ts)
    return drv


def test_box_decision_is_golden_deterministic():
    a = _replay(_event_list())
    b = _replay(_event_list())
    ka = [(x.kind, x.source, x.C, x.t_minus_s) for x in a.actions]
    kb = [(x.kind, x.source, x.C, x.t_minus_s) for x in b.actions]
    assert ka == kb
    # exactly one WOULD_FIRE, on K=63500 (the only qualifying strike at T-540)
    fires = [x for x in a.actions if x.kind == "WOULD_FIRE"]
    assert len(fires) == 1
    assert a.state.fired_selection.strike_K == Decimal("63500")
    assert a.state.entered == b.state.entered is True
