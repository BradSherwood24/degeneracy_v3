"""V3.3 ETH DRY roster: the ETH policy pin + geometry, the SAME core driving an ETH ($5 width) ladder
through a dry_sim fill, the THREE-WAY dry enforcement (entry refuses armed; no write-capable executor;
proxy read-only noted), and writable-path isolation. FAKES ONLY; no network, no proxy dial, no
sealed/holdout read. BTC byte-identity is covered by the unchanged existing suite (parse/discovery
defaults) plus the explicit default-unchanged assertions here."""

from __future__ import annotations

import gzip
import json
import os
from dataclasses import replace as dr
from decimal import Decimal

import pytest

import service.paths as P
from service.book import TopOfBook
from service.record_range import Bucket, RangeDiscovery
from service.run_v32 import FrozenExecutor, StrikeDiscovery
from service.v32.events import classify_ticker, parse_strike_ticker
from service.v33 import V33ParamsShaMismatch, V33State, load_v33_params
from service.v33 import eth as ETHMOD
from service.v33.eth import (
    ETH_RANGE_SERIES,
    ETH_ROSTER,
    ETH_STRIKE_SERIES,
    FROZEN_V33_ETH_PARAMS_SHA256,
    load_v33_eth_params,
    parse_eth_strike_ticker,
)
from service.v33.ledger import load_v33_rows
import service.run_v33_eth as E

_ONE = Decimal(1)

# --- ETH $5-width geometry fixture (verified live 2026-10-17; prices dropped to ~$1,700) ---
BK = {"KXETH-26OCT1720-B1702": (1700.0, 1704.99), "KXETH-26OCT1720-B1707": (1705.0, 1709.99)}
B_SD = "KXETH-26OCT1720-B1702"
STK_SD = "KXETHD-26OCT1720-T1699.99"   # floor 1700
STK_SU = "KXETHD-26OCT1720-T1704.99"   # floor 1705
SFM = {STK_SD: 1700, STK_SU: 1705, "KXETHD-26OCT1720-T1709.99": 1710}


def _top(bid, ask):
    yb, ya = Decimal(bid), Decimal(ask)
    return TopOfBook(yes_bid=yb, yes_bid_size=Decimal(100), yes_ask=ya, yes_ask_size=Decimal(100),
                     no_bid=_ONE - ya, no_bid_size=Decimal(100), no_ask=_ONE - yb,
                     no_ask_size=Decimal(100), suspect=False)


class J:
    def __init__(self):
        self.recs = []

    def append(self, k, o, t):
        self.recs.append((k, o))

    def kinds(self):
        return [k for k, _ in self.recs]


# ---------------------------------------------------------------------------
# ETH policy pin + geometry
# ---------------------------------------------------------------------------
def test_eth_params_load_and_sha_pin():
    p = load_v33_eth_params()
    assert p.sha256 == FROZEN_V33_ETH_PARAMS_SHA256
    assert p.bucket_width == 5
    # ETH $5 width forces the hysteresis down (validator: 2*hyst < bucket_width); proportional to BTC's 15.
    assert p.bucket_switch_hysteresis_usd == 1


def test_eth_params_strategy_knobs_identical_to_btc():
    """Margins / rungs / lots / shadow_Es / floors are UNCHANGED from BTC — only the $5-geometry constants
    differ. Guards against an accidental strategy change sneaking into the ETH copy."""
    eth = load_v33_eth_params()
    btc = load_v33_params()
    for f in ("E_min", "rungs", "lots_per_rung", "tol", "quote_start_s", "quote_end_s", "wing_margin",
              "lock_floor", "n_min", "max_sets_per_hour", "shadow_Es", "deep_obs_rungs",
              "max_contracts_per_order_hint", "print_through"):
        assert getattr(eth, f) == getattr(btc, f), f"ETH {f} drifted from BTC"
    # the ONLY intended differences:
    assert eth.bucket_width != btc.bucket_width
    assert eth.bucket_switch_hysteresis_usd != btc.bucket_switch_hysteresis_usd


def test_eth_params_tamper_fails_closed(tmp_path):
    raw = json.load(open(ETHMOD.DEFAULT_V33_ETH_PARAMS_PATH, encoding="utf-8"))
    raw["E_min"] = "0.09"   # tamper
    p = tmp_path / "v33_eth_params.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(V33ParamsShaMismatch):
        load_v33_eth_params(str(p))


# ---------------------------------------------------------------------------
# strike-ticker parsing (ETH) + BTC byte-identity
# ---------------------------------------------------------------------------
def test_eth_strike_parse_and_btc_default_unchanged():
    assert parse_eth_strike_ticker("KXETHD-26OCT1720-T1704.99") == 1705
    assert parse_eth_strike_ticker("KXETHD-26OCT1720-T1699.99") == 1700
    # ETH parser rejects a BTC ticker; the BTC default parser rejects an ETH ticker.
    assert parse_eth_strike_ticker("KXBTCD-26SEP2000-T80399.99") is None
    assert parse_strike_ticker("KXETHD-26OCT1720-T1704.99") is None
    # BTC default call is byte-identical to before.
    assert parse_strike_ticker("KXBTCD-26SEP2000-T80399.99") == 80400


def test_classify_ticker_default_path_unchanged_and_eth_map():
    bmap = {B_SD: (1700.0, 1704.99)}
    # default (no strike map): a bucket classifies; an ETH strike does NOT (BTC prefix parse rejects it).
    assert classify_ticker(B_SD, bmap) == ("bucket", 1700)
    assert classify_ticker(STK_SD, bmap) is None
    # with the ETH strike-floor map, the ETH strike classifies by lookup; the bucket still wins precedence.
    assert classify_ticker(STK_SD, bmap, SFM) == ("strike", 1700)
    assert classify_ticker(STK_SU, bmap, SFM) == ("strike", 1705)
    assert classify_ticker(B_SD, bmap, SFM) == ("bucket", 1700)


# ---------------------------------------------------------------------------
# the SAME core drives an ETH ($5-width) ladder through a dry_sim sweep
# ---------------------------------------------------------------------------
def _eth_dry_driver():
    import service.run_v33 as RUN
    p = dr(load_v33_eth_params(), freshness_max_age_s=3600.0, bucket_freshness_max_age_s=3600.0,
           strike_feed_dead_s=3600.0, wing_book_max_age_s=3600.0)
    cts = 1_000_000
    st = V33State.new("2026-10-18T00:00:00Z", cts, BK, p, shakedown=True, strike_floor_map=SFM)
    drv = RUN.V33Driver(p, st, J(), FrozenExecutor(BK), dry_sim=True, clock=lambda: 0.0)
    return p, cts, drv


def _bring_up(drv, cts):
    t0 = cts - 900 + 1
    for m, tb in ((B_SD, ("0.10", "0.11")), (STK_SU, ("0.10", "0.11")), (STK_SD, ("0.54", "0.55"))):
        drv.on_book_update(m, _top(*tb), t0)


def test_eth_core_forms_width5_ladder():
    p, cts, drv = _eth_dry_driver()
    _bring_up(drv, cts)
    # the $5 geometry: Su = Sd + bucket_width = 1700 + 5.
    assert drv.state.spot_Sd == 1700 and drv.state.spot_Su == 1705
    assert len(drv.state.ladder) == 11 and drv.state.n_top == Decimal("0.42")
    assert "would_place_rest" in drv.journal.kinds() and "place_rest" not in drv.journal.kinds()


def test_eth_core_books_dry_sim_fills_and_sends_nothing():
    p, cts, drv = _eth_dry_driver()
    _bring_up(drv, cts)
    drv.on_trade(B_SD, {"taker_side": "yes", "yes_price_dollars": "0.99", "count_fp": "1.00"}, cts - 500)
    assert drv._dry_sim_fills == 11 and drv.state.rungs_filled == 11 and drv.state.ladder == ()
    drv.on_clock_tick(cts - 400)
    kinds = set(drv.journal.kinds())
    assert "dry_sim_fill" in kinds
    for real in ("place_rest", "amend_rest", "cancel_rest", "take_wings", "retry_wing"):
        assert real not in kinds, f"ETH dry run emitted a REAL {real}"


def test_eth_dry_sim_ignores_non_bucket_and_no_side():
    p, cts, drv = _eth_dry_driver()
    _bring_up(drv, cts)
    drv.on_trade(B_SD, {"taker_side": "no", "yes_price_dollars": "0.99", "count_fp": "5.00"}, cts - 500)
    drv.on_trade(STK_SD, {"taker_side": "yes", "yes_price_dollars": "0.99", "count_fp": "5.00"}, cts - 500)
    assert drv._dry_sim_fills == 0 and drv.state.rungs_filled == 0


# ---------------------------------------------------------------------------
# DRY ENFORCEMENT (three ways)
# ---------------------------------------------------------------------------
def test_eth_effective_mode_never_armed():
    for requested in ("armed", "ARMED", " armed ", "dry", "shakedown", "garbage", "", None):
        assert E.eth_effective_mode(requested) == "dry"


def test_build_eth_executor_is_always_frozen():
    assert isinstance(E.build_eth_executor({}), FrozenExecutor)


def test_run_v33_eth_module_builds_no_write_capable_executor():
    """(b) the ETH module never REFERENCES a write-capable executor / writer / the armed builder in CODE
    (AST identifiers only — docstrings explaining the enforcement are allowed)."""
    import ast
    tree = ast.parse(open(E.__file__, encoding="utf-8").read())
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.alias):
            used.add(node.name.split(".")[-1])
            if node.asname:
                used.add(node.asname)
    for forbidden in ("V33LiveExecutor", "V33AsyncExecutor", "ProxyWriter", "build_executor_v33",
                      "AsyncOrderWriter"):
        assert forbidden not in used, f"run_v33_eth references a write-capable path: {forbidden}"


def _fake_eth_discovery(monkeypatch, close_iso):
    import service.run_v32 as R
    strike = StrikeDiscovery(close_iso, (STK_SD, STK_SU, "KXETHD-26OCT1720-T1709.99"),
                             {STK_SD: 1700, STK_SU: 1705, "KXETHD-26OCT1720-T1709.99": 1710},
                             {STK_SD: 2, STK_SU: 2, "KXETHD-26OCT1720-T1709.99": 2}, 1)
    buckets = tuple(
        Bucket(ticker=tk, floor=fc[0], cap=fc[1], event_ticker="KXETH-26OCT1720",
               open_time="", status="active", exchange_index=2)
        for tk, fc in BK.items()
    )
    rng = RangeDiscovery(close_iso, buckets, 1)
    monkeypatch.setattr(R, "discover_strike_ladder", lambda *a, **k: strike)
    monkeypatch.setattr(E, "discover_range_markets", lambda *a, **k: rng)

    async def _noop_window(*a, **k):
        return None
    monkeypatch.setattr(E.RUN, "run_v33_window", _noop_window)


def test_eth_main_runs_dry_and_refuses_armed(monkeypatch, tmp_path):
    """(a) end-to-end: --mode armed STILL runs DRY (never arms), writes a labelled ETH ledger row, and
    journals the refusal. No network (discovery + the window run-loop are faked)."""
    close_iso = "2026-10-18T00:00:00Z"
    jdir = tmp_path / "journals_v33_eth"
    jdir.mkdir()
    ledger = tmp_path / "v33_eth_ledger.jsonl"
    _fake_eth_discovery(monkeypatch, close_iso)
    rc = E.main(["--mode", "armed", "--close", close_iso,
                 "--journal-dir", str(jdir), "--ledger", str(ledger),
                 "--mode-file", str(tmp_path / "nope.txt")])
    assert rc == 0
    rows = load_v33_rows(str(ledger))
    assert len(rows) == 1
    row = rows[0]
    assert row["mode"] == "dry" and row["effective_mode"] == "dry"
    assert row["armed"] is False and row["dry_sim"] is True
    assert row["roster"] == ETH_ROSTER and row["series"] == ETH_RANGE_SERIES
    assert row["stand_down"] is False
    # the refusal reached the (gzipped) journal.
    gzs = [f for f in os.listdir(str(jdir)) if f.endswith(".jsonl.gz")]
    assert gzs, "no gzipped ETH journal written"
    with gzip.open(os.path.join(str(jdir), gzs[0]), "rt", encoding="utf-8") as f:
        kinds = [json.loads(ln)["kind"] for ln in f if ln.strip()]
    assert "armed_refused_dry_only" in kinds
    wm = None
    with gzip.open(os.path.join(str(jdir), gzs[0]), "rt", encoding="utf-8") as f:
        for ln in f:
            r = json.loads(ln)
            if r["kind"] == "window_meta":
                wm = r["obj"]
                break
    assert wm["range_series"] == ETH_RANGE_SERIES and wm["strike_series"] == ETH_STRIKE_SERIES
    assert wm["bucket_width"] == 5 and wm["dry_enforced"] is True and wm["armed_refused"] is True


def test_eth_main_stands_down_on_params_mismatch(monkeypatch, tmp_path):
    jdir = tmp_path / "journals_v33_eth"
    jdir.mkdir()
    ledger = tmp_path / "v33_eth_ledger.jsonl"

    def _boom(*a, **k):
        raise V33ParamsShaMismatch("tampered")
    monkeypatch.setattr(E, "load_v33_eth_params", _boom)
    rc = E.main(["--mode", "dry", "--close", "2026-10-18T00:00:00Z",
                 "--journal-dir", str(jdir), "--ledger", str(ledger)])
    assert rc == 0
    rows = load_v33_rows(str(ledger))
    assert len(rows) == 1 and rows[0]["stand_down"] is True
    assert "params_load_failed" in str(rows[0]["stand_down_reason"])
    assert rows[0]["roster"] == ETH_ROSTER and rows[0]["series"] == ETH_RANGE_SERIES


# ---------------------------------------------------------------------------
# writable-path isolation
# ---------------------------------------------------------------------------
def _clear(monkeypatch):
    monkeypatch.delenv(P.DATA_DIR_ENV, raising=False)


def test_eth_paths_under_checkout_by_default(monkeypatch):
    _clear(monkeypatch)
    pilot = P.pilot_dir()
    assert P.journal_dir_v33_eth() == os.path.join(pilot, "journals_v33_eth")
    assert P.log_dir_v33_eth() == os.path.join(pilot, "logs_v33_eth")
    assert P.ledger_path_v33_eth() == os.path.join(pilot, "ledger", "v33_eth_ledger.jsonl")
    assert P.mode_path_v33_eth() == os.path.join(pilot, "ops", "v33_eth_mode.txt")


def test_eth_paths_relocate_under_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv(P.DATA_DIR_ENV, str(tmp_path))
    assert P.journal_dir_v33_eth() == os.path.join(str(tmp_path), "journals_v33_eth")
    assert P.ledger_path_v33_eth() == os.path.join(str(tmp_path), "ledger", "v33_eth_ledger.jsonl")
    assert P.mode_path_v33_eth() == os.path.join(str(tmp_path), "ops", "v33_eth_mode.txt")


def test_eth_paths_distinct_from_btc_rosters(monkeypatch):
    _clear(monkeypatch)
    assert P.journal_dir_v33_eth() not in (P.journal_dir_v33(), P.journal_dir_v32())
    assert P.ledger_path_v33_eth() not in (P.ledger_path_v33(), P.ledger_path_v32())
    assert P.mode_path_v33_eth() not in (P.mode_path_v33(), P.mode_path_v32())
    # ...but they share the ledger dir + ops dir (the file names keep them apart).
    assert P.ledger_dir_v33() == P.ledger_dir_v32()
