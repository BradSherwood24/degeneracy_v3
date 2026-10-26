"""V3.3 ETH LIVE (armable) roster — the series-parametrized armed path (``service.run_v33_eth_live`` ->
``run_v33.main(spec=ETH_LIVE_SPEC)``), the coid/sweep/position ISOLATION that keeps the BTC and ETH
rosters from colliding on the shared account, the account-wide S4 wiring, the DRAFT ETH falsifier (S5
refuses to arm), and the BTC-UNCHANGED pins. FAKES ONLY; no network, no proxy dial, no sealed/holdout read.

BTC byte-identity is covered by the unchanged existing suite plus the explicit BTC-default assertions here.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal

import pytest

import service.run_v33 as RUN
import service.run_v33_eth_live as LIVE
import service.supervisor as SUP
from service.record_range import Bucket, RangeDiscovery
from service.run_v32 import FrozenExecutor, StrikeDiscovery
from service.v32.events import parse_strike_ticker
from service.v33 import V33State, load_v33_params
from service.v33.core import _mint_coid
from service.v33.eth import ETH_RANGE_SERIES, ETH_ROSTER, ETH_STRIKE_SERIES, load_v33_eth_params
from service.v33.executor import (
    _KXBTC_PREFIX,
    _KXETH_PREFIX,
    _V32_COID_PREFIX,
    _V33_COID_PREFIX,
    _V33E_COID_PREFIX,
    V33LiveExecutor,
    cancel_stale_open_orders,
)
from service.v33.ledger import load_v33_rows
from service.v33.series import BTC_SPEC, ETH_LIVE_SPEC
from service.v32.stops import reconcile_positions_clean
from service.v33.stops import (
    DayGuard,
    S4_DAY_LOSS,
    S1_LEGGED,
    decide_v33_arming,
    guard_has_s4_latch,
    v33_day_guard_path,
    v33_eth_day_guard_path,
)


# ===========================================================================
# coid prefix isolation — the shared-account collision guard
# ===========================================================================
def test_coid_prefixes_are_mutually_non_prefixing():
    """The whole shared-account safety rests on this: neither roster's startswith() scope matches the
    other's coids/tickers. If either of these ever becomes True the sweeps/attribution cross-contaminate."""
    assert _V33_COID_PREFIX == "v33-" and _V33E_COID_PREFIX == "v33e-"
    assert not "v33e-".startswith("v33-")
    assert not "v33-".startswith("v33e-")
    assert not "v33e-".startswith("v32-")
    assert not _KXETH_PREFIX.startswith(_KXBTC_PREFIX)
    assert not _KXBTC_PREFIX.startswith(_KXETH_PREFIX)


def test_core_mint_coid_uses_state_prefix():
    params = load_v33_params()
    st_btc = V33State.new("2026-10-18T00:00:00Z", 0, {}, params)              # default
    st_eth = V33State.new("2026-10-18T00:00:00Z", 0, {}, params, coid_prefix="v33e-")
    assert st_btc.coid_prefix == "v33-"
    assert st_eth.coid_prefix == "v33e-"
    coid_btc, _ = _mint_coid(st_btc)
    coid_eth, _ = _mint_coid(st_eth)
    assert coid_btc == "v33-2026-10-18T00:00:00Z-1"
    assert coid_eth == "v33e-2026-10-18T00:00:00Z-1"
    # and the two mints are mutually non-prefixing at the concrete-coid level too.
    assert not coid_eth.startswith(coid_btc.rsplit("-", 1)[0])


def test_executor_coid_prefix_instance_scoped():
    btc = V33LiveExecutor(writer=object(), bucket_map={}, exchange_index_by_ticker={}, journal=None,
                          close_epoch=0, quote_end_s=0, k_rungs=11, wing_cap=2)
    eth = V33LiveExecutor(writer=object(), bucket_map={}, exchange_index_by_ticker={}, journal=None,
                          close_epoch=0, quote_end_s=0, k_rungs=11, wing_cap=2,
                          coid_prefix=_V33E_COID_PREFIX)
    assert btc.COID_PREFIX == "v33-" and btc._mint_wing_coid() == "v33-wc-1"
    assert eth.COID_PREFIX == "v33e-" and eth._mint_wing_coid() == "v33e-wc-1"


# ===========================================================================
# startup stale-order sweep isolation — neither roster cancels the other's rests
# ===========================================================================
class _SweepWriter:
    """Returns a fixed resting-orders list and records every DELETE path."""

    def __init__(self, orders):
        self._orders = orders
        self.deleted: list[str] = []

    def rest_get(self, path, params=None):
        return {"orders": self._orders}

    def rest_delete(self, path):
        from service.proxy_writer import WriteResponse
        self.deleted.append(path)
        return WriteResponse(status_code=200, body={}, ok=True, error=None)


_MIXED_ORDERS = [
    {"ticker": "KXBTC-26OCT1800-B80400", "order_id": "ob1", "client_order_id": "v33-w-1"},
    {"ticker": "KXBTCD-26OCT1800-T80399.99", "order_id": "ob2", "client_order_id": "v33-w-2"},
    {"ticker": "KXETH-26OCT1800-B1702", "order_id": "oe1", "client_order_id": "v33e-w-1"},
    {"ticker": "KXETHD-26OCT1800-T1699.99", "order_id": "oe2", "client_order_id": "v33e-w-2"},
    {"ticker": "KXBTC-26OCT1800-B80400", "order_id": "ov1", "client_order_id": "v32-w-9"},
]


class _J:
    def __init__(self):
        self.recs = []

    def append(self, k, o, t):
        self.recs.append((k, o))


def test_btc_sweep_never_cancels_eth_or_v32():
    w = _SweepWriter(_MIXED_ORDERS)
    res = cancel_stale_open_orders(w, _J(), lambda: 0.0)   # BTC defaults
    assert set(w.deleted) == {"orders/ob1", "orders/ob2"} or all("ob" in d for d in w.deleted)
    assert res["found"] == 2 and res["cancelled"] == 2
    # NONE of the ETH (oe*) or V3.2 (ov1) orders were deleted.
    assert not any("oe" in d or "ov" in d for d in w.deleted)


def test_eth_sweep_never_cancels_btc_or_v32():
    w = _SweepWriter(_MIXED_ORDERS)
    res = cancel_stale_open_orders(w, _J(), lambda: 0.0,
                                   ticker_prefix=_KXETH_PREFIX, coid_prefix=_V33E_COID_PREFIX)
    assert res["found"] == 2 and res["cancelled"] == 2
    assert all("oe" in d for d in w.deleted)
    assert not any("ob" in d or "ov" in d for d in w.deleted)


# ===========================================================================
# reconcile-first position scope
# ===========================================================================
def test_reconcile_positions_prefix_scopes_to_series():
    btc_pos = {"market_positions": [{"ticker": "KXBTC-X", "position": 3}]}
    eth_pos = {"market_positions": [{"ticker": "KXETH-X", "position": 5}]}
    # BTC default sees the BTC position (dirty) but NOT the ETH one.
    assert reconcile_positions_clean(btc_pos)[0] is False
    assert reconcile_positions_clean(eth_pos)[0] is True
    # ETH prefix sees the ETH position (dirty) but NOT the BTC one.
    assert reconcile_positions_clean(eth_pos, prefix="KXETH")[0] is False
    assert reconcile_positions_clean(btc_pos, prefix="KXETH")[0] is True


# ===========================================================================
# account-wide S4 — cross-latch + the pending-credit sibling read
# ===========================================================================
def _guard(latched):
    return DayGuard(utc_day="2026-10-18", latched=tuple(latched), corrupt=False, exists=True)


def test_guard_has_s4_latch():
    assert guard_has_s4_latch(_guard([{"kind": S4_DAY_LOSS}])) is True
    assert guard_has_s4_latch(_guard([{"kind": S1_LEGGED}])) is False
    assert guard_has_s4_latch(_guard([])) is False


def test_eth_guard_path_is_separate():
    btc = v33_day_guard_path("/ops", "2026-10-18")
    eth = v33_eth_day_guard_path("/ops", "2026-10-18")
    assert btc.endswith("v33_stops_2026-10-18.json")
    assert eth.endswith("v33_eth_stops_2026-10-18.json")
    assert btc != eth


def _arm_kwargs(**over):
    base = dict(
        resolved_mode="armed", falsifier_path="/frozen", health={"orders_enabled": True,
        "caps": {"max_contracts_per_order": 2, "ticker_prefixes": ["KX"]},
        "orders_remaining_today": 100000}, positions={"market_positions": []}, params_verified=True,
        lots_per_rung=1, day_guard=_guard([]), s4=None, k_rungs=11, rung_lots=(1,) * 11)
    base.update(over)
    return base


def test_cross_s4_latched_degrades(monkeypatch):
    # make the falsifier appear frozen so ONLY the cross-S4 reason remains.
    monkeypatch.setattr("service.v33.stops.falsifier_is_frozen", lambda p: True)
    armed = decide_v33_arming(**_arm_kwargs(cross_s4_latched=False))
    assert armed.armed is True
    degraded = decide_v33_arming(**_arm_kwargs(cross_s4_latched=True))
    assert degraded.armed is False
    assert any("account-wide S4" in r for r in degraded.reasons)


def test_eth_position_prefix_in_arming(monkeypatch):
    monkeypatch.setattr("service.v33.stops.falsifier_is_frozen", lambda p: True)
    # an inherited KXBTC position must NOT block the ETH roster (position_prefix=KXETH).
    pos = {"market_positions": [{"ticker": "KXBTC-X", "position": 2}]}
    out = decide_v33_arming(**_arm_kwargs(positions=pos, position_prefix="KXETH"))
    assert out.armed is True
    # the SAME position blocks the BTC roster (default prefix).
    out_btc = decide_v33_arming(**_arm_kwargs(positions=pos))
    assert out_btc.armed is False


def test_account_pending_credit_adds_sibling(tmp_path):
    btc_ledger = tmp_path / "v33_ledger.jsonl"
    eth_ledger = tmp_path / "v33_eth_ledger.jsonl"
    day = "2026-10-18"
    # an ETH armed unsettled held set owes settlement credit; it must widen the BTC band.
    eth_row = {"roster": ETH_ROSTER, "series": "KXETH", "close_time": f"{day}T00:00:00Z",
               "armed": True, "dry_sim": False, "realized_unsettled": True,
               "wing_batch_sets": [{"held_legs": 2, "fill_count": 1}]}
    eth_ledger.write_text(json.dumps(eth_row) + "\n", encoding="utf-8")
    btc_ledger.write_text("", encoding="utf-8")

    # BTC spec with its sibling = the ETH ledger we just wrote.
    import dataclasses
    spec = dataclasses.replace(BTC_SPEC, sibling_ledger_path_fn=lambda: str(eth_ledger))
    pess, opt = RUN._account_pending_credit(str(btc_ledger), spec, day)
    assert opt > Decimal(0)   # the ETH held set contributes upside credit to the BTC account-wide band
    # with NO sibling ledger, the BTC band is its own (empty here) — byte-identical to pre-account-wide.
    spec_none = dataclasses.replace(BTC_SPEC, sibling_ledger_path_fn=lambda: str(tmp_path / "absent.jsonl"))
    assert RUN._account_pending_credit(str(btc_ledger), spec_none, day) == (Decimal(0), Decimal(0))


def test_sibling_s4_latched_reads_other_guard(tmp_path, monkeypatch):
    import dataclasses
    from service.stops import record_latched_stop
    day = "2026-10-18"
    ops = tmp_path
    # write an S4 latch into the ETH guard; the BTC roster must see it via its sibling resolver.
    eth_guard = v33_eth_day_guard_path(str(ops), day)
    record_latched_stop(eth_guard, day, S4_DAY_LOSS, "cap breached", f"{day}T00:00:00Z", 0.0)
    spec = dataclasses.replace(BTC_SPEC, sibling_guard_ops_dir_fn=lambda: str(ops))
    assert RUN._sibling_s4_latched(day, spec) is True
    # absent sibling guard -> False (fail-safe; BTC byte-identical).
    spec_absent = dataclasses.replace(BTC_SPEC, sibling_guard_ops_dir_fn=lambda: str(tmp_path / "nope"))
    assert RUN._sibling_s4_latched(day, spec_absent) is False


# ===========================================================================
# venue-fills ingestion isolation (shared account /portfolio/fills)
# ===========================================================================
def test_venue_fills_filtered_to_our_order_ids():
    """A shared-account /portfolio/fills containing the OTHER series' fills is NOT ingested — only our
    window's known order ids survive the filter (fail-closed on an empty known set)."""

    class _Proxy:
        def rest_get(self, path, params=None):
            return {"fills": [
                {"order_id": "ours1", "client_order_id": "v33e-w-1", "count": 1, "price": 90,
                 "ticker": "KXETH-X"},
                {"order_id": "theirs1", "client_order_id": "v33-w-1", "count": 3, "price": 90,
                 "ticker": "KXBTC-X"},                      # the BTC roster's fill on the shared account
                {"order_id": "foreign", "client_order_id": "x", "count": 9, "ticker": "KXETH-X"},
            ]}

    class _Exec:
        _by_order_id = {"ours1": "v33e-w-1"}                # only OUR window's acked order

    class _Driver:
        executor = _Exec()

    out = RUN._fetch_venue_fills_v33(_Proxy(), "2026-10-18T00:00:00Z", _Driver(), _J(), lambda: 0.0)
    assert out is not None
    ids = {f["order_id"] for f in out}
    assert ids == {"ours1"}            # neither the BTC fill nor the foreign fill was ingested


# ===========================================================================
# the spec: BTC unchanged pins + ETH values
# ===========================================================================
def test_btc_spec_unchanged_pins():
    assert BTC_SPEC.roster == "DegeneracyV3_3"
    assert BTC_SPEC.ledger_series is None               # BTC rows carry NO series key (byte-identity)
    assert BTC_SPEC.range_series == "KXBTC" and BTC_SPEC.strike_series == "KXBTCD"
    assert BTC_SPEC.coid_prefix == "v33-"
    assert BTC_SPEC.sweep_ticker_prefix == "KXBTC" and BTC_SPEC.position_prefix == "KXBTC"
    assert BTC_SPEC.needs_strike_floor_map is False
    assert BTC_SPEC.has_m15 is True
    assert BTC_SPEC.load_params is None                 # BTC uses run_v33's literal load_v33_params()
    assert BTC_SPEC.strike_parse is parse_strike_ticker
    assert BTC_SPEC.log_tag == "V33"


def test_eth_spec_values():
    assert ETH_LIVE_SPEC.roster == ETH_ROSTER
    assert ETH_LIVE_SPEC.ledger_series == ETH_RANGE_SERIES
    assert ETH_LIVE_SPEC.range_series == "KXETH" and ETH_LIVE_SPEC.strike_series == "KXETHD"
    assert ETH_LIVE_SPEC.coid_prefix == "v33e-"
    assert ETH_LIVE_SPEC.sweep_ticker_prefix == "KXETH" and ETH_LIVE_SPEC.position_prefix == "KXETH"
    assert ETH_LIVE_SPEC.needs_strike_floor_map is True
    assert ETH_LIVE_SPEC.has_m15 is False and ETH_LIVE_SPEC.m15_series is None
    assert ETH_LIVE_SPEC.load_params is load_v33_eth_params
    assert ETH_LIVE_SPEC.log_tag == "V33-ETH"


# ===========================================================================
# the ETH falsifier is a DRAFT -> S5 refuses to arm the ETH roster
# ===========================================================================
def test_eth_falsifier_is_draft_not_frozen():
    from service.stops import falsifier_is_frozen
    path = ETH_LIVE_SPEC.falsifier_path_fn()
    assert os.path.exists(path), "the ETH falsifier draft must be shipped"
    assert falsifier_is_frozen(path) is False, "the ETH falsifier must NOT be frozen (agent cannot arm ETH)"
    # no standalone line may be exactly the frozen marker.
    with open(path, encoding="utf-8") as f:
        assert not any(ln.strip() == "STATUS: FROZEN" for ln in f)


def test_eth_arming_refused_on_draft_falsifier():
    """S5 with the REAL ETH falsifier path (a draft) refuses to arm regardless of healthy caps."""
    out = decide_v33_arming(**_arm_kwargs(falsifier_path=ETH_LIVE_SPEC.falsifier_path_fn(),
                                          position_prefix="KXETH"))
    assert out.armed is False
    assert any("falsifier" in r.lower() for r in out.reasons)


# ===========================================================================
# run_v33_eth_live entry point dispatches to the shared spine with the ETH spec
# ===========================================================================
def test_eth_live_entry_calls_shared_main_with_eth_spec(monkeypatch):
    seen = {}

    def _fake_main(argv=None, *, spec=None):
        seen["argv"] = argv
        seen["spec"] = spec
        return 7

    monkeypatch.setattr(LIVE, "_run_v33_main", _fake_main)
    rc = LIVE.main(["--close", "2026-10-18T00:00:00Z"])
    assert rc == 7
    assert seen["spec"] is ETH_LIVE_SPEC
    assert seen["argv"] == ["--close", "2026-10-18T00:00:00Z"]


# ===========================================================================
# end-to-end DRY ETH-live window via the SHARED run_v33.main(spec=ETH_LIVE_SPEC)
# ===========================================================================
_BK = {"KXETH-26OCT1800-B1702": (1700.0, 1704.99), "KXETH-26OCT1800-B1707": (1705.0, 1709.99)}
_STK = ("KXETHD-26OCT1800-T1699.99", "KXETHD-26OCT1800-T1704.99", "KXETHD-26OCT1800-T1709.99")
_SFM = {_STK[0]: 1700, _STK[1]: 1705, _STK[2]: 1710}


def _fake_eth_live_discovery(monkeypatch, close_iso):
    import service.run_v32 as R
    strike = StrikeDiscovery(close_iso, _STK, _SFM, {t: 2 for t in _STK}, 1)
    buckets = tuple(Bucket(ticker=tk, floor=fc[0], cap=fc[1], event_ticker="KXETH-26OCT1800",
                           open_time="", status="active", exchange_index=2)
                    for tk, fc in _BK.items())
    rng = RangeDiscovery(close_iso, buckets, 1)
    monkeypatch.setattr(R, "discover_strike_ladder", lambda *a, **k: strike)
    monkeypatch.setattr(RUN, "discover_range_markets", lambda *a, **k: rng)

    async def _noop_window(*a, **k):
        return None
    monkeypatch.setattr(RUN, "run_v33_window", _noop_window)


def test_eth_live_dry_window_writes_labelled_row_and_threads_spec(monkeypatch, tmp_path):
    """A DRY ETH-live window runs through the SHARED run_v33 spine, writes a KXETH/ETH-roster row, and
    threads the ETH coid prefix + ETH strike-floor map + ETH params into V33State.new."""
    close_iso = "2026-10-18T00:00:00Z"
    jdir = tmp_path / "journals_v33_eth"
    jdir.mkdir()
    ledger = tmp_path / "v33_eth_ledger.jsonl"
    _fake_eth_live_discovery(monkeypatch, close_iso)

    captured = {}
    orig_new = V33State.new.__func__

    def _capturing_new(cls, *a, **k):
        captured.update(k)
        return orig_new(cls, *a, **k)
    monkeypatch.setattr(V33State, "new", classmethod(_capturing_new))

    rc = LIVE.main(["--mode", "dry", "--close", close_iso,
                    "--journal-dir", str(jdir), "--ledger", str(ledger),
                    "--mode-file", str(tmp_path / "nope.txt")])
    assert rc == 0
    # the ETH spec threaded through to the core state construction.
    assert captured.get("coid_prefix") == "v33e-"
    assert captured.get("strike_floor_map") == _SFM          # ETH feeds the floor map (BTC passes None)
    rows = load_v33_rows(str(ledger))
    assert len(rows) == 1
    row = rows[0]
    assert row["roster"] == ETH_ROSTER and row["series"] == ETH_RANGE_SERIES
    assert row["mode"] == "dry" and row["armed"] is False and row["dry_sim"] is True
    assert row["params_sha"] == load_v33_eth_params().sha256


def test_btc_dry_window_threads_btc_defaults(monkeypatch, tmp_path):
    """The SAME shared main with the DEFAULT (BTC) spec threads the BTC coid prefix + NO strike-floor map
    (BTC byte-identity: V33State.new receives coid_prefix='v33-' and strike_floor_map=None)."""
    import service.run_v32 as R
    close_iso = "2026-10-18T00:00:00Z"
    jdir = tmp_path / "journals_v33"
    jdir.mkdir()
    ledger = tmp_path / "v33_ledger.jsonl"
    strike = StrikeDiscovery(close_iso, ("KXBTCD-26OCT1800-T80399.99", "KXBTCD-26OCT1800-T80499.99"),
                             {"KXBTCD-26OCT1800-T80399.99": 80400, "KXBTCD-26OCT1800-T80499.99": 80500},
                             {"KXBTCD-26OCT1800-T80399.99": 2, "KXBTCD-26OCT1800-T80499.99": 2}, 1)
    bk = {"KXBTC-26OCT1800-B80400": (80400.0, 80499.99), "KXBTC-26OCT1800-B80500": (80500.0, 80599.99)}
    buckets = tuple(Bucket(ticker=tk, floor=fc[0], cap=fc[1], event_ticker="KXBTC-26OCT1800",
                           open_time="", status="active", exchange_index=2) for tk, fc in bk.items())
    monkeypatch.setattr(R, "discover_strike_ladder", lambda *a, **k: strike)
    monkeypatch.setattr(RUN, "discover_range_markets", lambda *a, **k: RangeDiscovery(close_iso, buckets, 1))
    monkeypatch.setattr(RUN.R, "discover_co_settling_15m_safe",
                        lambda *a, **k: (type("D", (), {"tickers": ()})(), None))

    async def _noop_window(*a, **k):
        return None
    monkeypatch.setattr(RUN, "run_v33_window", _noop_window)

    captured = {}
    orig_new = V33State.new.__func__

    def _capturing_new(cls, *a, **k):
        captured.update(k)
        return orig_new(cls, *a, **k)
    monkeypatch.setattr(V33State, "new", classmethod(_capturing_new))

    rc = RUN.main(["--mode", "dry", "--close", close_iso, "--journal-dir", str(jdir),
                   "--ledger", str(ledger), "--mode-file", str(tmp_path / "nope.txt")])
    assert rc == 0
    assert captured.get("coid_prefix") == "v33-"
    assert captured.get("strike_floor_map") is None        # BTC passes no floor map (byte-identical)
    rows = load_v33_rows(str(ledger))
    assert len(rows) == 1 and "series" not in rows[0]        # BTC row carries NO series key
    assert rows[0]["roster"] == "DegeneracyV3_3"


# ===========================================================================
# supervisor: roster map + per-roster sweep binding for v33_eth
# ===========================================================================
def test_supervisor_roster_module_map():
    assert SUP._ROSTER_MODULE["v33_eth"] == "service.run_v33_eth_live"
    assert SUP._ROSTER_MODULE["v33"] == "service.run_v33"
    assert SUP._ROSTER_MODULE["v32"] == "service.run_v32"


def test_roster_journal_dir_selection():
    from service.paths import journal_dir_v33, journal_dir_v33_eth, journal_dir_v32
    assert SUP._roster_journal_dir("v33") == journal_dir_v33()
    assert SUP._roster_journal_dir("v33_eth") == journal_dir_v33_eth()
    assert SUP._roster_journal_dir("v32") == journal_dir_v32()


def test_eth_boot_sweep_reads_eth_mode_and_scopes_to_eth(monkeypatch, tmp_path):
    """The v33_eth boot sweep reads v33_eth_mode.txt and, when armed, scopes the cancel to KXETH/v33e-."""
    mode_file = tmp_path / "v33_eth_mode.txt"
    mode_file.write_text("armed\n", encoding="utf-8")
    monkeypatch.setattr("service.paths.mode_path_v33_eth", lambda: str(mode_file))
    captured = {}

    def _fake_v33_sweep(writer, journal, clock, *, ticker_prefix=None, coid_prefix=None):
        captured["ticker_prefix"] = ticker_prefix
        captured["coid_prefix"] = coid_prefix
        return {"found": 0, "cancelled": 0, "errors": 0, "skipped_foreign": 0}

    monkeypatch.setattr("service.v33.executor.cancel_stale_open_orders", _fake_v33_sweep)
    # a ProxyWriter that never dials (we only check the sweep binding, cancel returns empty).
    monkeypatch.setattr("service.proxy_writer.ProxyWriter", lambda *a, **k: object())
    monkeypatch.setattr(SUP, "_proxy_ready", lambda base, timeout=2.0: True)
    res = SUP._default_boot_sweep("http://127.0.0.1:0", dry_sweep=False, clock=lambda: 0.0,
                                  log=lambda m: None, roster="v33_eth")
    assert captured["ticker_prefix"] == _KXETH_PREFIX
    assert captured["coid_prefix"] == _V33E_COID_PREFIX
    assert res["mode"] == "armed"


# ===========================================================================
# MultiRosterSupervisor: co-host BTC + ETH in one process
# ===========================================================================
import datetime as _dt
import signal as _signal

from service.supervisor import MultiRosterSupervisor


def _epoch(iso):
    return _dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=_dt.timezone.utc).timestamp()


class _Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class _FakeChild:
    def __init__(self, codes, pid=4321, on_wait=None):
        self._codes = list(codes)
        self.pid = pid
        self._on_wait = on_wait
        self.forwarded = []
        self.killed = False

    def wait(self, timeout):
        if self._on_wait is not None:
            self._on_wait()
        return self._codes.pop(0) if self._codes else 0

    def forward_signal(self, signum):
        self.forwarded.append(signum)

    def kill(self):
        self.killed = True


def _multi_fakes(**over):
    base = dict(
        proxy_base="http://fake:8642",
        rosters=["v33", "v33_eth"],
        sweep=lambda base_url, roster: {"roster": roster, "skipped": True, "found": 0},
        rotate=lambda jd: {"count": 0, "dir": jd},
        install_signals=False,
        log_path=os.devnull,
    )
    base.update(over)
    return base


def test_multi_spawns_both_rosters_btc_first():
    spawned = []
    events = []

    def spawn(roster, args):
        spawned.append(roster)
        return _FakeChild([0], pid=100 + len(spawned))

    sup = MultiRosterSupervisor(**_multi_fakes(
        once=True, run_now=True, spawn=spawn, on_event=events.append,
        clock=lambda: _epoch("2026-10-18T00:45:00Z")))
    assert sup.run() == 0
    assert spawned == ["v33", "v33_eth"]                       # BTC first, in order
    windows = [e for e in events if e["event"] == "window"]
    assert {w["roster"] for w in windows} == {"v33", "v33_eth"}
    assert all(w["status"] == "exited" for w in windows)
    boots = [e for e in events if e["event"] == "boot_sweep"]
    assert {b["roster"] for b in boots} == {"v33", "v33_eth"}  # each roster swept at boot


def test_multi_eth_crash_does_not_affect_btc():
    """An ETH child that exits non-zero / early still yields a BTC window event (independent children)."""
    events = []

    def spawn(roster, args):
        return _FakeChild([3]) if roster == "v33_eth" else _FakeChild([0])

    sup = MultiRosterSupervisor(**_multi_fakes(
        once=True, run_now=True, spawn=spawn, on_event=events.append,
        clock=lambda: _epoch("2026-10-18T00:45:00Z")))
    assert sup.run() == 0
    windows = {w["roster"]: w for w in events if w["event"] == "window"}
    assert windows["v33"]["exit_code"] == 0 and windows["v33"]["status"] == "exited"
    assert windows["v33_eth"]["exit_code"] == 3 and windows["v33_eth"]["status"] == "exited"


def test_multi_sigterm_forwards_to_all_live_children():
    holder = {}
    kids = []

    def spawn(roster, args):
        c = _FakeChild([None, 0], on_wait=lambda: holder["sup"].request_stop())
        kids.append(c)
        return c

    sup = MultiRosterSupervisor(**_multi_fakes(
        once=True, run_now=True, spawn=spawn, grace_s=0.0, child_poll_s=0.0,
        clock=lambda: _epoch("2026-10-18T00:45:00Z")))
    holder["sup"] = sup
    sup.run()
    assert len(kids) == 2
    for c in kids:
        assert c.forwarded == [_signal.SIGTERM]               # the signal reached BOTH children


def test_multi_sigterm_grace_is_one_shared_deadline_not_per_child():
    """Two hung children on SIGTERM share ONE grace deadline: the second child gets only what is left of
    it, so total shutdown stays under Render's 300 s cap (grace per child in series would be 2 x 240 s)."""
    clk = _Clock(_epoch("2026-10-18T00:45:00Z"))
    holder = {}
    waits = []

    class _Hung(_FakeChild):
        def wait(self, timeout):
            waits.append(timeout)
            if not holder["sup"]._stop_requested:
                holder["sup"].request_stop()
                return None
            clk.t += timeout                                  # the hung child burns its whole timeout
            return None

    sup = MultiRosterSupervisor(**_multi_fakes(
        once=True, run_now=True, spawn=lambda r, a: _Hung([]), grace_s=240.0, child_poll_s=0.0,
        clock=clk))
    holder["sup"] = sup
    start = clk.t
    sup.run()
    assert clk.t - start <= 240.0 + 1e-6                      # whole shutdown within ONE grace
    assert waits[-1] == 0.0                                   # the second child got only the remainder


def test_multi_per_child_watchdog_kills_only_overrunning_child():
    clk = _Clock(_epoch("2026-10-18T00:45:00Z"))
    kids = {}

    def spawn(roster, args):
        if roster == "v33_eth":
            # ETH hangs: never exits, and the clock has jumped past close+grace by the time it is polled.
            def on_wait():
                clk.t = _epoch("2026-10-18T01:00:00Z") + 10_000
            c = _FakeChild([None, None, None], on_wait=on_wait)
        else:
            c = _FakeChild([0])                               # BTC exits promptly
        kids[roster] = c
        return c

    events = []
    sup = MultiRosterSupervisor(**_multi_fakes(
        once=True, run_now=True, spawn=spawn, clock=clk, on_event=events.append))
    assert sup.run() == 0
    windows = {w["roster"]: w for w in events if w["event"] == "window"}
    assert windows["v33"]["status"] == "exited"               # BTC untouched
    assert windows["v33_eth"]["status"] == "watchdog_killed"  # only ETH killed
    assert kids["v33_eth"].killed is True
    assert kids["v33"].killed is False


def test_multi_unknown_rosters_filtered_and_deduped():
    sup = MultiRosterSupervisor(**_multi_fakes(rosters=["v33", "bogus", "v33", "v33_eth"]))
    assert sup._rosters == ["v33", "v33_eth"]
