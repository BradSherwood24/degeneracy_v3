"""Guard tests for test-isolation of the LIVE lever/guard files (spec item 2).

These assert the machinery in conftest actually holds: the WindowService lever-default constants are
repointed under tmp for every test, a service built WITHOUT explicit lever paths reads corridor /
shakedown from tmp (never the operator's box lever) and derives its stops-guard dir under tmp, and
the builtins.open tripwire fires the instant a test touches the real ops/strategy.txt or ops/mode.txt.
If any of these regress, the whole suite silently starts depending on the operator's live box lever.
"""

from __future__ import annotations

import os

import pytest

import service.run_window as rw
from service.run_window import (
    DEFAULT_MODE_TXT,
    DEFAULT_STRATEGY_TXT,
    WindowService,
    resolve_mode,
    resolve_strategy,
)
from tests.conftest import LIVE_MODE_TXT, LIVE_STRATEGY_TXT, _is_live_lever

CLOSE = "2026-08-30T22:00:00Z"  # forward (non-sealed) UTC close


def _norm(p) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(p)))


def test_lever_default_constants_are_redirected_under_tmp(tmp_path):
    # The autouse _isolate_levers fixture must have repointed both module constants away from the
    # live ops/ files and into this test's tmp dir.
    assert _norm(rw.DEFAULT_STRATEGY_TXT) != LIVE_STRATEGY_TXT
    assert _norm(rw.DEFAULT_MODE_TXT) != LIVE_MODE_TXT
    assert _norm(rw.DEFAULT_STRATEGY_TXT).startswith(_norm(tmp_path))
    assert _norm(rw.DEFAULT_MODE_TXT).startswith(_norm(tmp_path))
    # The imported names track the module attribute the fixture monkeypatched.
    assert DEFAULT_STRATEGY_TXT != rw.DEFAULT_STRATEGY_TXT or _norm(DEFAULT_STRATEGY_TXT) != LIVE_STRATEGY_TXT


def test_default_resolution_reads_corridor_and_shakedown_from_tmp():
    # resolve_* over the (redirected) defaults yields the ships-with corridor + absent-mode shakedown,
    # regardless of what the operator's live box lever currently says.
    strategy, valid = resolve_strategy(None, rw.DEFAULT_STRATEGY_TXT)
    assert (strategy, valid) == ("corridor", True)
    assert resolve_mode(None, rw.DEFAULT_MODE_TXT) == "shakedown"


def test_service_without_lever_paths_resolves_under_tmp(tmp_path):
    # A WindowService constructed with NO explicit lever paths must resolve them (and the derived
    # stops-guard/ops dir) under tmp — never the live ops/ files.
    svc = WindowService(close_time=CLOSE, cli_mode=None)
    assert _norm(svc.strategy_txt_path) != LIVE_STRATEGY_TXT
    assert _norm(svc.mode_txt_path) != LIVE_MODE_TXT
    assert _norm(svc.strategy_txt_path).startswith(_norm(tmp_path))
    assert _norm(svc.mode_txt_path).startswith(_norm(tmp_path))
    assert _norm(svc._ops_dir).startswith(_norm(tmp_path))
    assert _norm(svc._day_guard_path).startswith(_norm(tmp_path))
    # And the resolved strategy is corridor (fail-safe), not the live box.
    strategy, valid = resolve_strategy(None, svc.strategy_txt_path)
    assert (strategy, valid) == ("corridor", True)


def test_explicit_paths_still_honored(tmp_path):
    # Passing explicit lever paths must override the tmp defaults (tests that manage their own levers
    # keep working unchanged).
    mine_strat = tmp_path / "explicit_strategy.txt"
    mine_strat.write_text("box\n", encoding="utf-8")
    mine_mode = tmp_path / "sub" / "explicit_mode.txt"
    svc = WindowService(
        close_time=CLOSE,
        cli_mode=None,
        strategy_txt_path=str(mine_strat),
        mode_txt_path=str(mine_mode),
    )
    assert _norm(svc.strategy_txt_path) == _norm(mine_strat)
    assert _norm(svc.mode_txt_path) == _norm(mine_mode)
    assert _norm(svc._ops_dir) == _norm(tmp_path / "sub")


def test_tripwire_fires_on_live_strategy_path():
    # Opening the real ops/strategy.txt (by its live absolute path) must raise, whether or not the
    # file physically exists on this box.
    with pytest.raises(AssertionError, match="LIVE lever file"):
        open(LIVE_STRATEGY_TXT, "r", encoding="utf-8")


def test_tripwire_fires_on_live_mode_path():
    with pytest.raises(AssertionError, match="LIVE lever file"):
        open(LIVE_MODE_TXT, "r", encoding="utf-8")


def test_is_live_lever_ignores_non_paths_and_tmp(tmp_path):
    # The tripwire predicate must not choke on fds / non-path objects, and must pass tmp paths.
    assert _is_live_lever(3) is False
    assert _is_live_lever(None) is False
    assert _is_live_lever(str(tmp_path / "strategy.txt")) is False
    assert _is_live_lever(LIVE_STRATEGY_TXT) is True
