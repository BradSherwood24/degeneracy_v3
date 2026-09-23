"""V3.3 writable-path routing (L2): the v33 journal/log/ledger/mode paths resolve under the checkout
by default and relocate under DV3_DATA_DIR when set, SEPARATE from the v32 equivalents; read-only
inputs (the v33 falsifier) never move."""

from __future__ import annotations

import os

import service.paths as P


def _clear(monkeypatch):
    monkeypatch.delenv(P.DATA_DIR_ENV, raising=False)


def test_v33_paths_under_checkout_by_default(monkeypatch):
    _clear(monkeypatch)
    pilot = P.pilot_dir()
    assert P.journal_dir_v33() == os.path.join(pilot, "journals_v33")
    assert P.log_dir_v33() == os.path.join(pilot, "logs_v33")
    assert P.ledger_path_v33() == os.path.join(pilot, "ledger", "v33_ledger.jsonl")
    assert P.mode_path_v33() == os.path.join(pilot, "ops", "v33_mode.txt")


def test_v33_paths_relocate_under_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv(P.DATA_DIR_ENV, str(tmp_path))
    assert P.journal_dir_v33() == os.path.join(str(tmp_path), "journals_v33")
    assert P.log_dir_v33() == os.path.join(str(tmp_path), "logs_v33")
    assert P.ledger_path_v33() == os.path.join(str(tmp_path), "ledger", "v33_ledger.jsonl")
    assert P.mode_path_v33() == os.path.join(str(tmp_path), "ops", "v33_mode.txt")


def test_v33_and_v32_paths_are_distinct(monkeypatch):
    _clear(monkeypatch)
    assert P.journal_dir_v33() != P.journal_dir_v32()
    assert P.ledger_path_v33() != P.ledger_path_v32()
    assert P.mode_path_v33() != P.mode_path_v32()
    # ...but they share the ledger dir + ops dir (the day-guard prefix keeps them apart).
    assert P.ledger_dir_v33() == P.ledger_dir_v32()
    assert P.ops_dir_v33() == P.ops_dir_v32()


def test_v33_falsifier_is_a_readonly_checkout_input(monkeypatch, tmp_path):
    # the falsifier is a read-only INPUT -> stays in the checkout even with DV3_DATA_DIR set.
    monkeypatch.setenv(P.DATA_DIR_ENV, str(tmp_path))
    assert P.falsifier_path_v33().startswith(P.pilot_dir())
    assert P.falsifier_path_v33().endswith(os.path.join("ceremony", "v33_falsifier.md"))


def test_blank_data_dir_treated_as_unset(monkeypatch):
    monkeypatch.setenv(P.DATA_DIR_ENV, "   ")
    assert P.journal_dir_v33() == os.path.join(P.pilot_dir(), "journals_v33")
