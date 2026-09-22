"""test_paths.py -- DV3_DATA_DIR / DV3_PROXY_BASE resolution (Phase H, V3.3).

The contract: with the env UNSET every writable path is byte-identical to the historic
_PILOT_DIR-relative literal (behaviour-neutral for the live V3.2); with DV3_DATA_DIR SET every
WRITABLE path moves under it while read-only inputs (falsifier, keep-list) stay in the checkout.
"""

from __future__ import annotations

import os

from service import paths


def _clear_env(monkeypatch):
    monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
    monkeypatch.delenv(paths.PROXY_BASE_ENV, raising=False)


def test_unset_matches_historic_literals(monkeypatch):
    _clear_env(monkeypatch)
    pilot = paths.pilot_dir()
    # These RHS expressions are the exact literals run_v32 / ledger used before Phase H.
    assert paths.journal_dir_v32() == os.path.join(pilot, "journals_v32")
    assert paths.log_dir_v32() == os.path.join(pilot, "logs_v32")
    assert paths.ledger_dir_v32() == os.path.join(pilot, "ledger")
    assert paths.ledger_path_v32() == os.path.join(pilot, "ledger", "v32_ledger.jsonl")
    assert paths.ops_dir_v32() == os.path.join(pilot, "ops")
    assert paths.mode_path_v32() == os.path.join(pilot, "ops", "v32_mode.txt")
    assert paths.supervisor_log_path() == os.path.join(pilot, "logs_v32", "supervisor.out")
    assert paths.data_dir() is None


def test_run_v32_and_ledger_constants_unchanged_when_unset(monkeypatch):
    _clear_env(monkeypatch)
    pilot = paths.pilot_dir()
    import service.run_v32 as r
    from service.v32 import ledger as L

    assert r.DEFAULT_JOURNAL_DIR == os.path.join(pilot, "journals_v32")
    assert r.DEFAULT_LOG_DIR == os.path.join(pilot, "logs_v32")
    assert r.DEFAULT_MODE_PATH == os.path.join(pilot, "ops", "v32_mode.txt")
    assert r.DEFAULT_FALSIFIER_PATH == os.path.join(pilot, "ceremony", "v32_falsifier.md")
    assert L.DEFAULT_V32_LEDGER_DIR == os.path.join(pilot, "ledger")
    assert L.DEFAULT_V32_LEDGER_PATH == os.path.join(pilot, "ledger", "v32_ledger.jsonl")


def test_set_relocates_writable_paths(monkeypatch, tmp_path):
    data = str(tmp_path / "dv3")
    monkeypatch.setenv(paths.DATA_DIR_ENV, data)
    assert paths.data_dir() == data
    assert paths.journal_dir_v32() == os.path.join(data, "journals_v32")
    assert paths.log_dir_v32() == os.path.join(data, "logs_v32")
    assert paths.ledger_dir_v32() == os.path.join(data, "ledger")
    assert paths.ledger_path_v32() == os.path.join(data, "ledger", "v32_ledger.jsonl")
    assert paths.ops_dir_v32() == os.path.join(data, "ops")
    assert paths.mode_path_v32() == os.path.join(data, "ops", "v32_mode.txt")
    assert paths.supervisor_log_path() == os.path.join(data, "logs_v32", "supervisor.out")


def test_read_only_inputs_stay_in_checkout_even_when_data_dir_set(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "dv3"))
    pilot = paths.pilot_dir()
    # Read-only inputs never move: the rotation keep-list and the falsifier stay with the code.
    assert paths.journal_keep_path() == os.path.join(pilot, "ops", "journal_keep.txt")
    assert paths.falsifier_path_v32() == os.path.join(pilot, "ceremony", "v32_falsifier.md")
    assert paths.checkout_ops_dir() == os.path.join(pilot, "ops")


def test_blank_data_dir_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv(paths.DATA_DIR_ENV, "   ")
    assert paths.data_dir() is None
    assert paths.journal_dir_v32() == os.path.join(paths.pilot_dir(), "journals_v32")


def test_proxy_base_default_unset(monkeypatch):
    _clear_env(monkeypatch)
    assert paths.default_proxy_base() == "http://127.0.0.1:8642"


def test_proxy_base_from_env(monkeypatch):
    monkeypatch.setenv(paths.PROXY_BASE_ENV, "http://degeneracy-proxy-abc:8642")
    assert paths.default_proxy_base() == "http://degeneracy-proxy-abc:8642"


def test_proxy_base_blank_falls_back(monkeypatch):
    monkeypatch.setenv(paths.PROXY_BASE_ENV, "  ")
    assert paths.default_proxy_base() == "http://127.0.0.1:8642"
