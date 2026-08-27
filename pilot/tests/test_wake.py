"""WakeContext: expected/observed step, leg discovery fixtures (normal, missing-hourly stand-down,
21:00 $250, Friday 21:00 $500, unexpected step, dual-generation), active checks, and the sweep shell.

All fixtures use FORWARD (unsealed) UTC dates. No historical-data files are read here."""

from __future__ import annotations

from decimal import Decimal

from service.wake import (
    STEP_2100,
    STEP_2100_FRIDAY,
    STEP_DEFAULT,
    Leg,
    StandDown,
    WakeContext,
    WakeResult,
    discover_legs,
    expected_step,
    ladder_check,
    live_hourly_ladders,
    observed_step,
)


# 2026-08-20 = Thursday; 2026-08-21 = Friday (verified in build).
def mkt(event, ticker, close, open_, strike, status="active"):
    return {
        "event_ticker": event,
        "ticker": ticker,
        "close_time": close,
        "open_time": open_,
        "floor_strike": strike,
        "status": status,
    }


def hourly_ladder(event, close, open_, base, step, n, status="active"):
    return [
        mkt(event, f"{event}-T{base + i * step:.2f}", close, open_, round(base + i * step, 2), status)
        for i in range(n)
    ]


def fifteen(event, ticker, close, open_, status="active"):
    return [mkt(event, ticker, close, open_, 60000.0, status)]


NOW = 1700000000.0  # far before any 2026 close -> "future" for active checks


# === expected_step ===


def test_expected_step_default_hour() -> None:
    assert expected_step("2026-08-20T15:00:00Z") == STEP_DEFAULT == Decimal(100)


def test_expected_step_2100_non_friday() -> None:
    assert expected_step("2026-08-20T21:00:00Z") == STEP_2100 == Decimal(250)  # Thursday


def test_expected_step_2100_friday() -> None:
    assert expected_step("2026-08-21T21:00:00Z") == STEP_2100_FRIDAY == Decimal(500)  # Friday


def test_expected_step_handles_offset_spelling() -> None:
    assert expected_step("2026-08-21T21:00:00+00:00") == Decimal(500)


# === observed_step ===


def test_observed_step_uniform() -> None:
    step, uniform = observed_step([73299.99, 73199.99, 73399.99])
    assert step == Decimal("100.00") and uniform is True


def test_observed_step_non_uniform() -> None:
    step, uniform = observed_step([100.0, 200.0, 350.0])
    assert step == Decimal("100.00") and uniform is False


def test_observed_step_too_few() -> None:
    assert observed_step([100.0]) == (None, False)
    assert observed_step([]) == (None, False)


# === discovery: normal hour ===


def test_discover_normal_hour() -> None:
    close = "2026-08-20T15:00:00Z"
    m15 = fifteen("KXBTC15M-26AUG201500", "KXBTC15M-26AUG201500-00", close, "2026-08-20T14:45:00Z")
    mh = hourly_ladder("KXBTCD-26AUG2015", close, "2026-08-20T14:00:00Z", 60000.0, 100.0, 20)
    res = discover_legs(m15, mh, close, NOW)
    assert not isinstance(res, StandDown)
    f, h = res
    assert f.series == "KXBTC15M" and f.primary_ticker == "KXBTC15M-26AUG201500-00"
    assert h.series == "KXBTCD" and len(h.market_tickers) == 20
    lc = ladder_check(h)
    assert lc.ok and lc.observed_step == Decimal("100.00") and not lc.strangle_disabled


# === discovery: 06-09 UTC missing hourly -> stand down ===


def test_missing_hourly_stands_down() -> None:
    close = "2026-08-20T08:00:00Z"
    m15 = fifteen("KXBTC15M-26AUG200800", "KXBTC15M-26AUG200800-00", close, "2026-08-20T07:45:00Z")
    res = discover_legs(m15, [], close, NOW)
    assert isinstance(res, StandDown)
    assert "hourly" in res.reason


def test_missing_fifteen_stands_down() -> None:
    close = "2026-08-20T15:00:00Z"
    mh = hourly_ladder("KXBTCD-26AUG2015", close, "2026-08-20T14:00:00Z", 60000.0, 100.0, 5)
    res = discover_legs([], mh, close, NOW)
    assert isinstance(res, StandDown)
    assert "15-minute" in res.reason


# === discovery: 21:00 special steps ===


def test_2100_thursday_250_ok() -> None:
    close = "2026-08-20T21:00:00Z"  # Thursday
    m15 = fifteen("KXBTC15M-26AUG202100", "KXBTC15M-26AUG202100-00", close, "2026-08-20T20:45:00Z")
    mh = hourly_ladder("KXBTCD-26AUG2021", close, "2026-08-20T20:00:00Z", 60000.0, 250.0, 10)
    f, h = discover_legs(m15, mh, close, NOW)
    lc = ladder_check(h)
    assert lc.ok and lc.observed_step == Decimal("250.00")


def test_2100_friday_500_ok() -> None:
    close = "2026-08-21T21:00:00Z"  # Friday
    m15 = fifteen("KXBTC15M-26AUG212100", "KXBTC15M-26AUG212100-00", close, "2026-08-21T20:45:00Z")
    mh = hourly_ladder("KXBTCD-26AUG2121", close, "2026-08-21T20:00:00Z", 60000.0, 500.0, 10)
    f, h = discover_legs(m15, mh, close, NOW)
    lc = ladder_check(h)
    assert lc.ok and lc.observed_step == Decimal("500.00")


# === ladder-map deviation -> alarm + strangle_disabled ===


def test_unexpected_step_disables_strangle() -> None:
    close = "2026-08-20T15:00:00Z"  # expects $100
    m15 = fifteen("KXBTC15M-26AUG201500", "KXBTC15M-26AUG201500-00", close, "2026-08-20T14:45:00Z")
    mh = hourly_ladder("KXBTCD-26AUG2015", close, "2026-08-20T14:00:00Z", 60000.0, 250.0, 10)
    f, h = discover_legs(m15, mh, close, NOW)
    lc = ladder_check(h)
    assert lc.ok is False
    assert lc.alarm is True
    assert lc.strangle_disabled is True
    assert lc.observed_step == Decimal("250.00") and lc.expected_step == Decimal("100")


def test_non_uniform_ladder_disables_strangle() -> None:
    close = "2026-08-20T15:00:00Z"
    mh = [
        mkt("KXBTCD-26AUG2015", "KXBTCD-26AUG2015-T60000.00", close, "2026-08-20T14:00:00Z", 60000.0),
        mkt("KXBTCD-26AUG2015", "KXBTCD-26AUG2015-T60100.00", close, "2026-08-20T14:00:00Z", 60100.0),
        mkt("KXBTCD-26AUG2015", "KXBTCD-26AUG2015-T60350.00", close, "2026-08-20T14:00:00Z", 60350.0),
    ]
    lc = ladder_check(discover_legs(
        fifteen("KXBTC15M-26AUG201500", "KXBTC15M-26AUG201500-00", close, "2026-08-20T14:45:00Z"),
        mh, close, NOW)[1])
    assert lc.strangle_disabled and lc.alarm and lc.uniform is False


def test_single_strike_ladder_fails_closed() -> None:
    close = "2026-08-20T15:00:00Z"
    mh = hourly_ladder("KXBTCD-26AUG2015", close, "2026-08-20T14:00:00Z", 60000.0, 100.0, 1)
    lc = ladder_check(discover_legs(
        fifteen("KXBTC15M-26AUG201500", "KXBTC15M-26AUG201500-00", close, "2026-08-20T14:45:00Z"),
        mh, close, NOW)[1])
    assert lc.observed_step is None and lc.strangle_disabled and lc.alarm


# === dual-generation (the 2026-07-31 21:00 shape) ===


def test_dual_generation_selects_smallest_window() -> None:
    """Two hourly ladders co-settle at 21:00 Friday: a WIDE-window $500 generation (opened a week
    early) and a NARROW-window $250 generation (opened the day before). Smallest-window selects the
    narrow $250 ladder; on a Friday the map expects $500, so the paired ladder deviates -> strangle
    stands down (sub-$1 continues). This pins 'validate the ladder the specific market pairs'."""
    close = "2026-08-21T21:00:00Z"  # Friday -> expected 500
    wide = hourly_ladder("KXBTCD-26AUG2121", close, "2026-08-14T20:00:00Z", 52000.0, 500.0, 50)
    narrow = hourly_ladder("KXBTCD-26AUG2121", close, "2026-08-20T11:30:00Z", 50000.0, 250.0, 10)
    m15 = fifteen("KXBTC15M-26AUG212100", "KXBTC15M-26AUG212100-00", close, "2026-08-21T20:45:00Z")
    f, h = discover_legs(m15, wide + narrow, close, NOW)
    # selected the narrow (smaller window) generation
    assert h.open_time == "2026-08-20T11:30:00Z"
    assert len(h.market_tickers) == 10
    lc = ladder_check(h)
    assert lc.observed_step == Decimal("250.00")
    assert lc.expected_step == Decimal("500")
    assert lc.strangle_disabled is True and lc.alarm is True


def test_live_hourly_ladders_pools_all_generations() -> None:
    """F1: live_hourly_ladders returns EVERY live co-settling hourly generation (census h1_by_ct
    pooling), and the selected smallest-window generation is among them."""
    close = "2026-08-20T15:00:00Z"  # expects 100
    wide = hourly_ladder("KXBTCD-WIDE", close, "2026-08-13T14:00:00Z", 50000.0, 500.0, 4)
    narrow = hourly_ladder("KXBTCD-NARROW", close, "2026-08-20T14:00:00Z", 60000.0, 100.0, 3)
    ladders = live_hourly_ladders(wide + narrow, close, NOW)
    assert len(ladders) == 2
    opens = {lad.open_time for lad in ladders}
    assert opens == {"2026-08-13T14:00:00Z", "2026-08-20T14:00:00Z"}
    # the pooled strike set is the UNION of both generations (4 + 3 markets)
    pooled = [m for lad in ladders for m in lad.markets]
    assert len(pooled) == 7
    # the selected (smallest-window) generation is present in the pool
    _, sel = discover_legs(
        fifteen("KXBTC15M-26AUG201500", "KXBTC15M-26AUG201500-00", close, "2026-08-20T14:45:00Z"),
        wide + narrow, close, NOW,
    )
    assert any(lad.open_time == sel.open_time for lad in ladders)


def test_live_hourly_ladders_drops_all_dead_generation() -> None:
    """A generation whose markets are ALL dead is not a pairing candidate (fail-closed)."""
    close = "2026-08-20T15:00:00Z"
    live = hourly_ladder("KXBTCD-LIVE", close, "2026-08-20T14:00:00Z", 60000.0, 100.0, 3)
    dead = hourly_ladder("KXBTCD-DEAD", close, "2026-08-13T14:00:00Z", 50000.0, 500.0, 4,
                         status="settled")
    ladders = live_hourly_ladders(live + dead, close, NOW)
    assert len(ladders) == 1 and ladders[0].event_ticker == "KXBTCD-LIVE"


def test_dual_generation_matching_step_ok() -> None:
    """If the smallest-window generation's step matches the map, it is OK (dual listings tolerated,
    not automatically fatal)."""
    close = "2026-08-20T15:00:00Z"  # expects 100
    wide = hourly_ladder("KXBTCD-26AUG2015", close, "2026-08-13T14:00:00Z", 50000.0, 500.0, 40)
    narrow = hourly_ladder("KXBTCD-26AUG2015", close, "2026-08-20T14:00:00Z", 60000.0, 100.0, 20)
    m15 = fifteen("KXBTC15M-26AUG201500", "KXBTC15M-26AUG201500-00", close, "2026-08-20T14:45:00Z")
    f, h = discover_legs(m15, wide + narrow, close, NOW)
    assert h.open_time == "2026-08-20T14:00:00Z"
    assert ladder_check(h).ok is True


def test_tie_break_prefers_denser_ladder() -> None:
    """Equal window duration -> the denser ladder (more strikes) wins the tie."""
    close = "2026-08-20T15:00:00Z"
    a = hourly_ladder("KXBTCD-A", close, "2026-08-20T14:00:00Z", 60000.0, 100.0, 5)
    b = hourly_ladder("KXBTCD-B", close, "2026-08-20T14:00:00Z", 60000.0, 100.0, 12)
    m15 = fifteen("KXBTC15M-26AUG201500", "KXBTC15M-26AUG201500-00", close, "2026-08-20T14:45:00Z")
    _, h = discover_legs(m15, a + b, close, NOW)
    assert h.event_ticker == "KXBTCD-B" and len(h.market_tickers) == 12


# === active checks ===


def test_closed_market_stands_down() -> None:
    close = "2026-08-20T15:00:00Z"
    now_after = 1_800_000_000.0  # after the close
    m15 = fifteen("KXBTC15M-26AUG201500", "KXBTC15M-26AUG201500-00", close, "2026-08-20T14:45:00Z")
    mh = hourly_ladder("KXBTCD-26AUG2015", close, "2026-08-20T14:00:00Z", 60000.0, 100.0, 5)
    res = discover_legs(m15, mh, close, now_after)
    assert isinstance(res, StandDown)


def test_all_dead_status_stands_down() -> None:
    # UPDATED (live finding 2026-08-21): selection is status-agnostic; an ALL-dead ladder is still
    # refused (fail-closed deny-list). Reason text changed "not active" -> "not live" to match the
    # new selection semantics (the wrong assumption this test previously encoded was the word only —
    # "finalized" IS dead and must still stand down, which it does).
    close = "2026-08-20T15:00:00Z"
    m15 = fifteen("KXBTC15M-26AUG201500", "KXBTC15M-26AUG201500-00", close, "2026-08-20T14:45:00Z", status="finalized")
    mh = hourly_ladder("KXBTCD-26AUG2015", close, "2026-08-20T14:00:00Z", 60000.0, 100.0, 5, status="finalized")
    res = discover_legs(m15, mh, close, NOW)
    assert isinstance(res, StandDown)
    assert "not live" in res.reason


# === STATUS-AGNOSTIC SELECTION (live finding 2026-08-21): the exact live :40-wake scenario ===


def test_discover_selects_initialized_15m_leg_at_wake() -> None:
    """THE live scenario, fixture mirroring the real payload: at the :40 wake the co-settling 15M leg
    is status 'initialized' (open_time :45, close_time :00), the hourly leg is 'active'. Selection is
    status-agnostic -> both legs selected (NOT a stand-down). Ticker naming: names use ET local time
    (KXBTC15M-26AUG211200-00 closes 16:00Z), but selection keys on close_time epoch, not the name."""
    close = "2026-08-21T16:00:00Z"
    # open_time = :45 (window start), close_time = :00; status "initialized" as observed pre-open.
    m15 = fifteen("KXBTC15M-26AUG211200", "KXBTC15M-26AUG211200-00", close,
                  "2026-08-21T15:45:00Z", status="initialized")
    mh = hourly_ladder("KXBTCD-26AUG2116", close, "2026-08-21T15:00:00Z", 60000.0, 100.0, 20,
                       status="active")
    res = discover_legs(m15, mh, close, NOW)
    assert not isinstance(res, StandDown), getattr(res, "reason", None)
    f, h = res
    assert f.primary_ticker == "KXBTC15M-26AUG211200-00"
    assert f.open_time == "2026-08-21T15:45:00Z"  # WakeContext surfaces each leg's open_time
    assert h.series == "KXBTCD" and len(h.market_tickers) == 20
    assert ladder_check(h).ok  # a normal $100 hourly still passes the ladder map


def test_discover_selects_initialized_both_legs() -> None:
    """Even both legs 'initialized' (a window discovered well before either flips active) selects."""
    close = "2026-08-21T16:00:00Z"
    m15 = fifteen("KXBTC15M-26AUG211200", "KXBTC15M-26AUG211200-00", close,
                  "2026-08-21T15:45:00Z", status="initialized")
    mh = hourly_ladder("KXBTCD-26AUG2116", close, "2026-08-21T15:00:00Z", 60000.0, 100.0, 8,
                       status="initialized")
    res = discover_legs(m15, mh, close, NOW)
    assert not isinstance(res, StandDown)


def test_discover_selects_when_status_missing() -> None:
    """A market with no status field at all is NOT dead -> selectable (regardless-of-status)."""
    close = "2026-08-21T16:00:00Z"
    m15 = fifteen("KXBTC15M-26AUG211200", "KXBTC15M-26AUG211200-00", close, "2026-08-21T15:45:00Z")
    for m in m15:
        del m["status"]
    mh = hourly_ladder("KXBTCD-26AUG2116", close, "2026-08-21T15:00:00Z", 60000.0, 100.0, 5)
    for m in mh:
        del m["status"]
    assert not isinstance(discover_legs(m15, mh, close, NOW), StandDown)


def test_each_dead_status_refused() -> None:
    """Each clearly-dead status, when it covers the WHOLE 15M leg, is refused as a missing leg."""
    close = "2026-08-21T16:00:00Z"
    for dead in ("settled", "finalized", "closed", "determined"):
        m15 = fifteen("KXBTC15M-26AUG211200", "KXBTC15M-26AUG211200-00", close,
                      "2026-08-21T15:45:00Z", status=dead)
        mh = hourly_ladder("KXBTCD-26AUG2116", close, "2026-08-21T15:00:00Z", 60000.0, 100.0, 5,
                           status="active")
        res = discover_legs(m15, mh, close, NOW)
        assert isinstance(res, StandDown), f"{dead} 15M leg should stand down"
        assert "15-minute leg not live" in res.reason


def test_dead_wing_tolerated_if_any_market_live() -> None:
    """A ladder with a dead wing but >=1 live market is still selected (early-settled wings)."""
    close = "2026-08-21T16:00:00Z"
    m15 = fifteen("KXBTC15M-26AUG211200", "KXBTC15M-26AUG211200-00", close,
                  "2026-08-21T15:45:00Z", status="initialized")
    mh = hourly_ladder("KXBTCD-26AUG2116", close, "2026-08-21T15:00:00Z", 60000.0, 100.0, 5,
                       status="active")
    mh[0]["status"] = "settled"  # one dead wing
    res = discover_legs(m15, mh, close, NOW)
    assert not isinstance(res, StandDown)


# === subscribe_tickers ===


def test_subscribe_tickers_and_limit() -> None:
    close = "2026-08-20T15:00:00Z"
    m15 = fifteen("KXBTC15M-26AUG201500", "KXBTC15M-26AUG201500-00", close, "2026-08-20T14:45:00Z")
    mh = hourly_ladder("KXBTCD-26AUG2015", close, "2026-08-20T14:00:00Z", 60000.0, 100.0, 20)
    f, h = discover_legs(m15, mh, close, NOW)
    r = WakeResult(close, f, h, ladder_check(h))
    assert r.subscribe_tickers()[0] == "KXBTC15M-26AUG201500-00"
    assert len(r.subscribe_tickers()) == 21  # 1 + 20
    assert len(r.subscribe_tickers(hourly_limit=3)) == 4  # 1 + 3


# === exchange_index_by_ticker (sharding route map) ===


def test_exchange_index_by_ticker_maps_both_legs_and_fails_closed():
    from service.wake import coerce_exchange_index

    close = "2026-08-26T20:00:00Z"
    # Real tonight record shape (2026-08-27 incident): crypto markets carry exchange_index 2.
    m15 = fifteen("KXBTC15M-26AUG262000", "KXBTC15M-26AUG262000-00", close, "2026-08-26T19:45:00Z")
    m15[0]["exchange_index"] = 2
    mh = hourly_ladder("KXBTCD-26AUG2620", close, "2026-08-26T19:00:00Z", 79000.0, 100.0, 4)
    for m in mh:
        m["exchange_index"] = 2
    # one ladder market whose record LACKS the field -> None (fail closed: run_window refuses it)
    del mh[1]["exchange_index"]
    f, h = discover_legs(m15, mh, close, NOW)
    r = WakeResult(close, f, h, ladder_check(h))
    xmap = r.exchange_index_by_ticker
    assert xmap["KXBTC15M-26AUG262000-00"] == 2
    assert xmap[mh[0]["ticker"]] == 2
    assert xmap[mh[1]["ticker"]] is None          # missing field -> None (never routed to shard 0)
    # coercion is fail-closed on junk / bools / non-integral floats
    assert coerce_exchange_index("2") == 2 and coerce_exchange_index(0) == 0
    assert coerce_exchange_index(None) is None and coerce_exchange_index("x") is None
    assert coerce_exchange_index(True) is None and coerce_exchange_index(2.5) is None


# === sweep shell (fake proxy) ===


class FakeProxy:
    def __init__(self, markets_by_series, balance=None, raise_balance=False):
        self._m = markets_by_series
        self._balance = balance
        self._raise_balance = raise_balance
        self.calls = []

    def rest_get(self, path, params=None):
        self.calls.append((path, params))
        if path == "/portfolio/balance":
            if self._raise_balance:
                raise RuntimeError("balance boom")
            return self._balance
        series = params["series_ticker"]
        return {"markets": self._m.get(series, []), "cursor": ""}


def test_sweep_returns_wakeresult() -> None:
    close = "2026-08-20T15:00:00Z"
    m15 = fifteen("KXBTC15M-26AUG201500", "KXBTC15M-26AUG201500-00", close, "2026-08-20T14:45:00Z")
    mh = hourly_ladder("KXBTCD-26AUG2015", close, "2026-08-20T14:00:00Z", 60000.0, 100.0, 8)
    proxy = FakeProxy({"KXBTC15M": m15, "KXBTCD": mh}, balance={"balance": 5000})
    wc = WakeContext(proxy, clock=lambda: NOW)  # type: ignore[arg-type]
    res = wc.sweep(close)
    assert isinstance(res, WakeResult)
    assert res.ladder.ok and res.balance == {"balance": 5000}
    assert res.affordable is None  # no gate wired in Phase 1


def test_sweep_sends_no_status_param_and_selects_initialized_leg() -> None:
    """End-to-end via the sweep shell: the live :40-wake scenario (initialized 15M leg) must resolve
    to a WakeResult, and the /markets query must carry NO `status` param (a status=open filter would
    hide the 'initialized' leg -> stand down every window)."""
    close = "2026-08-21T16:00:00Z"
    m15 = fifteen("KXBTC15M-26AUG211200", "KXBTC15M-26AUG211200-00", close,
                  "2026-08-21T15:45:00Z", status="initialized")
    mh = hourly_ladder("KXBTCD-26AUG2116", close, "2026-08-21T15:00:00Z", 60000.0, 100.0, 8,
                       status="active")
    proxy = FakeProxy({"KXBTC15M": m15, "KXBTCD": mh}, balance={"balance": 5000})
    wc = WakeContext(proxy, clock=lambda: NOW)  # type: ignore[arg-type]
    res = wc.sweep(close)
    assert isinstance(res, WakeResult) and res.fifteen_leg.open_time == "2026-08-21T15:45:00Z"
    market_calls = [p for path, p in proxy.calls if path == "/markets" and p is not None]
    assert market_calls, "the sweep must query /markets"
    assert all("status" not in p for p in market_calls), "no status filter on /markets"


def test_sweep_stands_down_when_hourly_missing() -> None:
    close = "2026-08-20T08:00:00Z"
    m15 = fifteen("KXBTC15M-26AUG200800", "KXBTC15M-26AUG200800-00", close, "2026-08-20T07:45:00Z")
    proxy = FakeProxy({"KXBTC15M": m15, "KXBTCD": []})
    wc = WakeContext(proxy, clock=lambda: NOW)  # type: ignore[arg-type]
    assert isinstance(wc.sweep(close), StandDown)


def test_sweep_affordability_gate_called() -> None:
    close = "2026-08-20T15:00:00Z"
    m15 = fifteen("KXBTC15M-26AUG201500", "KXBTC15M-26AUG201500-00", close, "2026-08-20T14:45:00Z")
    mh = hourly_ladder("KXBTCD-26AUG2015", close, "2026-08-20T14:00:00Z", 60000.0, 100.0, 8)
    proxy = FakeProxy({"KXBTC15M": m15, "KXBTCD": mh}, balance={"balance": 100})
    seen = {}

    def gate(balance, result):
        seen["balance"] = balance
        seen["close"] = result.close_time
        return balance["balance"] >= 50

    wc = WakeContext(proxy, clock=lambda: NOW, affordability_gate=gate)  # type: ignore[arg-type]
    res = wc.sweep(close)
    assert res.affordable is True
    assert seen == {"balance": {"balance": 100}, "close": close}


def test_sweep_gate_exception_fails_closed() -> None:
    close = "2026-08-20T15:00:00Z"
    m15 = fifteen("KXBTC15M-26AUG201500", "KXBTC15M-26AUG201500-00", close, "2026-08-20T14:45:00Z")
    mh = hourly_ladder("KXBTCD-26AUG2015", close, "2026-08-20T14:00:00Z", 60000.0, 100.0, 8)
    proxy = FakeProxy({"KXBTC15M": m15, "KXBTCD": mh}, balance={"balance": 100})

    def gate(balance, result):
        raise RuntimeError("gate boom")

    wc = WakeContext(proxy, clock=lambda: NOW, affordability_gate=gate)  # type: ignore[arg-type]
    assert wc.sweep(close).affordable is False  # fail closed


def test_sweep_balance_failure_is_tolerated() -> None:
    close = "2026-08-20T15:00:00Z"
    m15 = fifteen("KXBTC15M-26AUG201500", "KXBTC15M-26AUG201500-00", close, "2026-08-20T14:45:00Z")
    mh = hourly_ladder("KXBTCD-26AUG2015", close, "2026-08-20T14:00:00Z", 60000.0, 100.0, 8)
    proxy = FakeProxy({"KXBTC15M": m15, "KXBTCD": mh}, raise_balance=True)
    wc = WakeContext(proxy, clock=lambda: NOW)  # type: ignore[arg-type]
    res = wc.sweep(close)
    assert isinstance(res, WakeResult) and res.balance is None
