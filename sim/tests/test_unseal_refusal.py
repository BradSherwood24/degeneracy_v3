"""Unseal runner fail-closed preflight: refuses without a frozen falsifier / gate /
census artifacts, on a census<->gate sha mismatch (A3.9), and main refuses without Brad's
explicit-go flag. Freeze is line-based (A3.6). NEVER runs the read."""
import json
import os

import pytest

import loader
import unseal_runner as U
from unseal_runner import RefuseToRun


def _stage(tmp_path, *, gate=True, census=True, gate_sha=None, falsifier_text=None):
    """Point the runner's paths at a temp dir; create only the requested artifacts.

    When both gate and census are present, gate.json records the census file's sha256 so
    the A3.9 binding check passes (override with gate_sha to force a mismatch)."""
    census_csv = tmp_path / "census_train.csv"
    if census:
        census_csv.write_text("x\n", encoding="utf-8")
        (tmp_path / "census_receipt.json").write_text("{}", encoding="utf-8")
    U.CENSUS_CSV = str(census_csv)
    U.CENSUS_RECEIPT = str(tmp_path / "census_receipt.json")
    if gate:
        sha = gate_sha if gate_sha is not None else (
            loader.sha256_file(str(census_csv)) if census else "")
        p = tmp_path / "gate.json"
        p.write_text(json.dumps({"quintile_edges_gos": [1, 2, 3, 4],
                                 "gate_buckets": [0, 1],
                                 "census_csv_sha256": sha}), encoding="utf-8")
        U.GATE_JSON = str(p)
    else:
        U.GATE_JSON = str(tmp_path / "no_gate.json")
    if falsifier_text is not None:
        f = tmp_path / "falsifier.md"
        f.write_text(falsifier_text, encoding="utf-8")
        U.FALSIFIER_MD = str(f)
    else:
        U.FALSIFIER_MD = str(tmp_path / "no_falsifier.md")


@pytest.fixture(autouse=True)
def _restore():
    saved = (U.GATE_JSON, U.CENSUS_CSV, U.CENSUS_RECEIPT, U.FALSIFIER_MD)
    yield
    U.GATE_JSON, U.CENSUS_CSV, U.CENSUS_RECEIPT, U.FALSIFIER_MD = saved


def test_refuses_when_gate_missing(tmp_path):
    _stage(tmp_path, gate=False, falsifier_text="FROZEN")
    with pytest.raises(RefuseToRun, match="gate"):
        U.preflight()


def test_refuses_when_census_missing(tmp_path):
    _stage(tmp_path, census=False, falsifier_text="FROZEN")
    with pytest.raises(RefuseToRun, match="census"):
        U.preflight()


def test_refuses_when_falsifier_missing(tmp_path):
    _stage(tmp_path, falsifier_text=None)
    with pytest.raises(RefuseToRun, match="falsifier"):
        U.preflight()


def test_refuses_when_falsifier_not_frozen(tmp_path):
    # A3.6: a DRAFT STATUS line (no line == "STATUS: FROZEN") is refused.
    _stage(tmp_path, falsifier_text="STATUS: DRAFT - NOT FROZEN\nclauses...")
    with pytest.raises(RefuseToRun, match="not FROZEN"):
        U.preflight()


def test_refuses_when_no_frozen_marker(tmp_path):
    _stage(tmp_path, falsifier_text="some clauses without a status marker")
    with pytest.raises(RefuseToRun, match="FROZEN"):
        U.preflight()


def test_freeze_is_line_based_not_substring(tmp_path):
    # A3.6: prose mentioning FROZEN in a sentence must NOT freeze the file; only a
    # dedicated STATUS line does. This is the B1 deadlock guard.
    _stage(tmp_path, falsifier_text=(
        "STATUS: DRAFT — NOT FROZEN\n\n"
        "This file freezes when the STATUS line reads FROZEN.\n"))
    with pytest.raises(RefuseToRun, match="not FROZEN"):
        U.preflight()


def test_preflight_passes_when_all_present_and_frozen(tmp_path):
    _stage(tmp_path, falsifier_text="STATUS: FROZEN\nclauses...")
    text = U.preflight()
    assert loader.falsifier_is_frozen(text)


def test_preflight_refuses_on_census_sha_mismatch(tmp_path):
    # A3.9: gate.json's recorded census sha must equal the on-disk census sha.
    _stage(tmp_path, gate_sha="deadbeef" * 8, falsifier_text="STATUS: FROZEN\n")
    with pytest.raises(RefuseToRun, match="A3.9"):
        U.preflight()


def test_freeze_a_copy_of_real_repo_falsifier_passes(tmp_path):
    # A3.6 (mandated): freeze a COPY of the real repo falsifier.md and assert preflight
    # passes. The real file stays a DRAFT; only the copy is frozen.
    real = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "ceremony", "falsifier.md")
    with open(real, "r", encoding="utf-8") as fh:
        text = fh.read()
    frozen = text.replace("STATUS: DRAFT — NOT FROZEN", "STATUS: FROZEN")
    assert loader.falsifier_is_frozen(frozen)          # the copy is now frozen
    assert not loader.falsifier_is_frozen(text)        # the real file is not
    _stage(tmp_path, falsifier_text=frozen)
    out = U.preflight()
    assert loader.falsifier_is_frozen(out)


def test_main_requires_explicit_go_flag():
    with pytest.raises(RefuseToRun, match="explicit go"):
        U.main([])


def test_real_repo_falsifier_is_still_a_draft():
    # Sanity: the checked-in falsifier.md must NOT be frozen during the campaign — and
    # under the A3.6 line-based mechanism, not just contain the substring.
    ceremony = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "ceremony", "falsifier.md")
    with open(ceremony, "r", encoding="utf-8") as fh:
        text = fh.read()
    assert not loader.falsifier_is_frozen(text)
    assert "NOT FROZEN" in text
