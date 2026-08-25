"""Rung 1.5 unseal runner — policy-evaluator behavior + fail-closed refusals.

All tests run on SYNTHETIC tape_points rows (temp CSVs) or on TRAIN artifacts. No
test ever touches a sealed data byte: the sealed-file checks here are existence-only
(os.path.exists metadata), and run_sealed() is never called.
"""
import csv
import json
import os

import pytest

import loader
import unseal_runner15 as U
from unseal_runner15 import PolicyEvaluator, RefuseToRun
from tape_sim import TAPE_FIELDNAMES


# ---------------------------------------------------------------------------
# Synthetic tape_points helpers
# ---------------------------------------------------------------------------
def _row(direction, close_time, t_minus_s, ev, C, quintile, payoff,
         pin=0, high_age=0.0, low_age=0.0, date="2026-06-13", hard_floor=None):
    """One tape_points row. Only the columns the evaluator reads carry meaning; the
    rest are filled with inert placeholders so the CSV is well-formed. ``hard_floor`` can
    be overridden to force a disagreement with float(C)<1.0 (F4 tests); by default it
    mirrors tape_sim (flip: '1' iff C<1.0, else '0'; strangle: blank)."""
    if hard_floor is None:
        hf = ("1" if (direction == "flip" and C < 1.0) else "0") if direction == "flip" else ""
    else:
        hf = hard_floor
    return {
        "date": date, "close_time": close_time, "direction": direction,
        "t_minus_s": f"{t_minus_s:.6f}", "fill_side": "HIGH",
        "G": "10.00", "sigma_hat": "100.000000", "g_over_sigma": "0.100000",
        "quintile": quintile,
        "high_leg_price": "0.5000", "low_leg_price": "0.5000",
        "high_leg_age_s": f"{high_age:.3f}", "low_leg_age_s": f"{low_age:.3f}",
        "C": f"{C:.4f}", "fair": "0.900000", "fair_lin": "0.900000",
        "ev": f"{ev:.6f}", "ev_lin": "0.000000",
        "hard_floor": hf,
        "pin": "1" if pin else "0",
        "payoff": f"{payoff:.4f}", "dwell_s": "1.000",
    }


def _write(tmp_path, rows, name="tp.csv"):
    p = tmp_path / name
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TAPE_FIELDNAMES)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return str(p)


def _eval(tmp_path, rows, burned=()):
    path = _write(tmp_path, rows)
    ev = PolicyEvaluator(burned_close_times=burned)
    ev.feed_csv(path)
    ev.finalize()
    return ev


# ---------------------------------------------------------------------------
# Q1 race: strangle EV-5 vs sub-$1 flip, decided by t_minus_s (larger = earlier)
# ---------------------------------------------------------------------------
def test_q1_strangle_wins_when_earlier(tmp_path):
    ct = "2026-06-13T02:00:00Z"
    rows = [
        _row("strangle", ct, 800.0, ev=0.06, C=1.20, quintile=0, payoff=0.50),
        _row("flip", ct, 700.0, ev=0.00, C=0.90, quintile=0, payoff=0.30),
    ]
    ev = _eval(tmp_path, rows)
    assert len(ev.entries) == 1
    e = ev.entries[0]
    assert e["source"] == "Q1-strangle"
    assert e["payoff"] == 0.50


def test_q1_subdollar_wins_when_earlier_despite_file_order(tmp_path):
    # tape_sim emits strangle rows BEFORE flip rows in the file, but the flip here is
    # earlier in wall-clock time (larger t_minus_s) so it must win the Q1 race.
    ct = "2026-06-13T02:00:00Z"
    rows = [
        _row("strangle", ct, 600.0, ev=0.06, C=1.20, quintile=0, payoff=0.50),
        _row("flip", ct, 750.0, ev=0.00, C=0.90, quintile=0, payoff=0.30),
    ]
    ev = _eval(tmp_path, rows)
    assert len(ev.entries) == 1
    e = ev.entries[0]
    assert e["source"] == "sub$1-flip"
    assert e["payoff"] == 0.30


def test_q1_strangle_alone_enters(tmp_path):
    ct = "2026-06-13T02:00:00Z"
    rows = [_row("strangle", ct, 800.0, ev=0.07, C=1.30, quintile=0, payoff=0.4)]
    ev = _eval(tmp_path, rows)
    assert [e["source"] for e in ev.entries] == ["Q1-strangle"]


# ---------------------------------------------------------------------------
# Q2-Q4: sub-$1 flip ONLY (a qualifying strangle must not enter)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("q", [1, 2, 3])
def test_q2_q4_strangle_ignored(tmp_path, q):
    ct = "2026-06-13T02:00:00Z"
    # strangle with huge EV (would win in Q1) is NOT routed in Q2-Q4; no sub-$1 flip
    # present -> no entry at all.
    rows = [_row("strangle", ct, 800.0, ev=0.30, C=1.10, quintile=q, payoff=0.9)]
    ev = _eval(tmp_path, rows)
    assert ev.entries == []
    assert ev.eligible_windows == 1


@pytest.mark.parametrize("q", [1, 2, 3])
def test_q2_q4_subdollar_enters(tmp_path, q):
    ct = "2026-06-13T02:00:00Z"
    rows = [
        _row("strangle", ct, 800.0, ev=0.30, C=1.10, quintile=q, payoff=0.9),
        _row("flip", ct, 700.0, ev=0.00, C=0.95, quintile=q, payoff=0.2),
    ]
    ev = _eval(tmp_path, rows)
    assert [e["source"] for e in ev.entries] == ["sub$1-flip"]
    assert ev.entries[0]["payoff"] == 0.2


# ---------------------------------------------------------------------------
# Q5: flip EV-10 ONLY (a sub-$1 flip must NOT enter Q5)
# ---------------------------------------------------------------------------
def test_q5_subdollar_does_not_enter(tmp_path):
    ct = "2026-06-13T02:00:00Z"
    # flip is sub-$1 (C<1) but its EV is below 0.10 -> Q5 requires EV-10, so NO entry.
    rows = [_row("flip", ct, 700.0, ev=0.05, C=0.90, quintile=4, payoff=0.3)]
    ev = _eval(tmp_path, rows)
    assert ev.entries == []
    assert ev.eligible_windows == 1


def test_q5_flip_ev10_enters(tmp_path):
    ct = "2026-06-13T02:00:00Z"
    rows = [_row("flip", ct, 700.0, ev=0.12, C=1.05, quintile=4, payoff=0.6)]
    ev = _eval(tmp_path, rows)
    assert [e["source"] for e in ev.entries] == ["Q5-flip"]
    assert ev.entries[0]["payoff"] == 0.6


def test_q5_strangle_ignored(tmp_path):
    ct = "2026-06-13T02:00:00Z"
    # a strangle EV-5 in Q5 is not routed; only flip EV-10 can enter Q5.
    rows = [_row("strangle", ct, 800.0, ev=0.20, C=1.10, quintile=4, payoff=0.9)]
    ev = _eval(tmp_path, rows)
    assert ev.entries == []


# ---------------------------------------------------------------------------
# Freshness filter: max(both leg ages) <= 1.0 s
# ---------------------------------------------------------------------------
def test_freshness_filter_rejects_stale(tmp_path):
    ct = "2026-06-13T02:00:00Z"
    rows = [_row("flip", ct, 700.0, ev=0.00, C=0.90, quintile=0, payoff=0.3,
                 high_age=2.0, low_age=0.0)]     # one leg stale -> not fresh
    ev = _eval(tmp_path, rows)
    assert ev.entries == []
    assert ev.eligible_windows == 1


def test_freshness_boundary_one_second_ok(tmp_path):
    ct = "2026-06-13T02:00:00Z"
    rows = [_row("flip", ct, 700.0, ev=0.00, C=0.90, quintile=0, payoff=0.3,
                 high_age=1.0, low_age=1.0)]     # exactly 1.0 s -> fresh
    ev = _eval(tmp_path, rows)
    assert [e["source"] for e in ev.entries] == ["sub$1-flip"]


# ---------------------------------------------------------------------------
# Burned close_time exclusion + count
# ---------------------------------------------------------------------------
def test_burned_close_time_excluded_and_counted(tmp_path):
    burn = "2026-08-18T19:00:00Z"
    keep = "2026-06-13T02:00:00Z"
    rows = [
        _row("flip", burn, 700.0, ev=0.00, C=0.90, quintile=0, payoff=0.3),
        _row("flip", keep, 700.0, ev=0.00, C=0.90, quintile=0, payoff=0.4),
    ]
    ev = _eval(tmp_path, rows, burned={burn})
    assert ev.eligible_windows == 1          # burned window not eligible
    assert len(ev.entries) == 1
    assert ev.entries[0]["payoff"] == 0.4
    assert ev.burned_excluded == {burn}
    assert len(ev.burned_excluded) == 1


# ---------------------------------------------------------------------------
# First-entry uniqueness per window
# ---------------------------------------------------------------------------
def test_one_entry_per_window_earliest_wins(tmp_path):
    ct = "2026-06-13T02:00:00Z"
    # multiple qualifying rows across both directions -> exactly ONE entry, the earliest
    # qualifying event (largest t_minus_s). Rows are chronological (descending t_minus_s)
    # within each direction, as tape_sim emits them.
    rows = [
        _row("strangle", ct, 820.0, ev=0.06, C=1.20, quintile=0, payoff=0.11),
        _row("strangle", ct, 810.0, ev=0.09, C=1.20, quintile=0, payoff=0.12),
        _row("flip", ct, 815.0, ev=0.00, C=0.80, quintile=0, payoff=0.21),
        _row("flip", ct, 805.0, ev=0.00, C=0.70, quintile=0, payoff=0.22),
    ]
    ev = _eval(tmp_path, rows)
    assert len(ev.entries) == 1
    # strangle 820 is the largest t_minus_s among all qualifying events.
    assert ev.entries[0]["source"] == "Q1-strangle"
    assert ev.entries[0]["payoff"] == 0.11


def test_two_windows_two_entries(tmp_path):
    ct1 = "2026-06-13T02:00:00Z"
    ct2 = "2026-06-13T03:00:00Z"
    rows = [
        _row("flip", ct1, 700.0, ev=0.00, C=0.90, quintile=1, payoff=0.1),
        _row("flip", ct2, 700.0, ev=0.00, C=0.95, quintile=2, payoff=0.2),
    ]
    ev = _eval(tmp_path, rows)
    assert ev.eligible_windows == 2
    assert len(ev.entries) == 2


def test_non_contiguous_window_raises(tmp_path):
    ct1 = "2026-06-13T02:00:00Z"
    ct2 = "2026-06-13T03:00:00Z"
    rows = [
        _row("flip", ct1, 700.0, ev=0.00, C=0.90, quintile=1, payoff=0.1),
        _row("flip", ct2, 700.0, ev=0.00, C=0.95, quintile=2, payoff=0.2),
        _row("flip", ct1, 600.0, ev=0.00, C=0.90, quintile=1, payoff=0.1),  # ct1 recurs
    ]
    with pytest.raises(U.PolicyError, match="non-contiguous"):
        _eval(tmp_path, rows)


# ---------------------------------------------------------------------------
# Fail-closed refusals (never touch sealed data)
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _restore_paths():
    saved = (U.FALSIFIER_MD, U.CENSUS_CSV, U.DRYRUN_JSON, U.RESULT_JSON,
             U.STARTED_MARKER, U.SEALED_EVAL_DIR, U.FULL60_CHUNKS_DIR, U.FULL60_TAPE)
    yield
    (U.FALSIFIER_MD, U.CENSUS_CSV, U.DRYRUN_JSON, U.RESULT_JSON,
     U.STARTED_MARKER, U.SEALED_EVAL_DIR, U.FULL60_CHUNKS_DIR, U.FULL60_TAPE) = saved


def _valid_cert():
    return {
        "STATUS": "TRAIN DRY-RUN CERTIFICATE",
        "eligible_windows": U.REFERENCE["eligible_windows"],
        "entered": U.REFERENCE["entered"],
        "pins_among_entered": U.REFERENCE["pins_among_entered"],
        "pooled_mean_payoff_cents_2dp": U.REFERENCE["pooled_mean_payoff_cents_2dp"],
        "by_source": dict(U.REFERENCE["by_source"]),
        "sub_dollar_by_quintile": dict(U.REFERENCE["sub_dollar_by_quintile"]),
        "policy_params_sha256": U.POLICY_PARAMS_SHA256,
    }


def _frozen_falsifier(tmp_path):
    p = tmp_path / "falsifier.md"
    p.write_text("STATUS: FROZEN\nclauses...\n", encoding="utf-8")
    return str(p)


def test_refuses_when_falsifier_not_frozen(tmp_path):
    p = tmp_path / "falsifier.md"
    p.write_text("STATUS: DRAFT — NOT FROZEN\nclauses...\n", encoding="utf-8")
    U.FALSIFIER_MD = str(p)
    with pytest.raises(RefuseToRun, match="not FROZEN"):
        U.preflight_sealed()


def test_refuses_when_falsifier_missing(tmp_path):
    U.FALSIFIER_MD = str(tmp_path / "nope.md")
    with pytest.raises(RefuseToRun, match="falsifier missing"):
        U.preflight_sealed()


def test_refuses_when_census_sha_mismatch(tmp_path):
    U.FALSIFIER_MD = _frozen_falsifier(tmp_path)
    bad = tmp_path / "census_train.csv"
    bad.write_text("not the real census\n", encoding="utf-8")
    U.CENSUS_CSV = str(bad)
    with pytest.raises(RefuseToRun, match="sha256"):
        U.preflight_sealed()


def test_refuses_when_dryrun_certificate_missing(tmp_path):
    U.DRYRUN_JSON = str(tmp_path / "no_cert.json")
    with pytest.raises(RefuseToRun, match="certificate missing"):
        U._assert_dryrun_certificate()


def test_refuses_when_dryrun_numbers_mismatch(tmp_path):
    cert = _valid_cert()
    cert["entered"] = 600            # off by one
    p = tmp_path / "cert.json"
    p.write_text(json.dumps(cert), encoding="utf-8")
    U.DRYRUN_JSON = str(p)
    with pytest.raises(RefuseToRun, match="does not match"):
        U._assert_dryrun_certificate()


def test_refuses_when_dryrun_policy_sha_mismatch(tmp_path):
    cert = _valid_cert()
    cert["policy_params_sha256"] = "deadbeef" * 8
    p = tmp_path / "cert.json"
    p.write_text(json.dumps(cert), encoding="utf-8")
    U.DRYRUN_JSON = str(p)
    with pytest.raises(RefuseToRun, match="different policy"):
        U._assert_dryrun_certificate()


def test_valid_dryrun_certificate_accepted(tmp_path):
    p = tmp_path / "cert.json"
    p.write_text(json.dumps(_valid_cert()), encoding="utf-8")
    U.DRYRUN_JSON = str(p)
    out = U._assert_dryrun_certificate()          # no raise
    assert out["entered"] == U.REFERENCE["entered"]


def test_refuses_when_result_json_already_exists(tmp_path):
    # Stage a FULLY-passing preflight (frozen falsifier copy, real train census, valid
    # dry-run cert), then assert the pre-existing result JSON trips the one-shot guard.
    # This reads NO sealed byte: sealed-file checks are existence-only and run_sealed()
    # is never called.
    U.FALSIFIER_MD = _frozen_falsifier(tmp_path)
    # real train census satisfies the sha pin
    assert loader.sha256_file(U.CENSUS_CSV) == U.CENSUS_TRAIN_SHA256
    cert = tmp_path / "cert.json"
    cert.write_text(json.dumps(_valid_cert()), encoding="utf-8")
    U.DRYRUN_JSON = str(cert)
    result = tmp_path / "unseal15_result.json"
    result.write_text("{}", encoding="utf-8")
    U.RESULT_JSON = str(result)
    with pytest.raises(RefuseToRun, match="already exists"):
        U.preflight_sealed()


def test_preflight_passes_reads_no_sealed_byte(tmp_path):
    # With every gate satisfied and no result file, preflight returns the frozen text
    # WITHOUT reading a sealed byte (it only checks file existence).
    U.FALSIFIER_MD = _frozen_falsifier(tmp_path)
    assert loader.sha256_file(U.CENSUS_CSV) == U.CENSUS_TRAIN_SHA256
    cert = tmp_path / "cert.json"
    cert.write_text(json.dumps(_valid_cert()), encoding="utf-8")
    U.DRYRUN_JSON = str(cert)
    U.RESULT_JSON = str(tmp_path / "does_not_exist_result.json")
    text = U.preflight_sealed()
    assert loader.falsifier_is_frozen(text)


def test_main_requires_a_mode():
    with pytest.raises(RefuseToRun, match="dry-run-train"):
        U.main([])


# ---------------------------------------------------------------------------
# Policy fingerprint stability (guards against silent policy drift)
# ---------------------------------------------------------------------------
def test_policy_sha_matches_frozen_params():
    import hashlib
    expect = hashlib.sha256(
        json.dumps(U.POLICY_PARAMS, sort_keys=True).encode("utf-8")).hexdigest()
    assert U.POLICY_PARAMS_SHA256 == expect


# ---------------------------------------------------------------------------
# F4 — hard_floor is the authoritative sub-$1 indicator; disagreement is tracked
# ---------------------------------------------------------------------------
def test_f4_hardfloor_disagreement_raises_in_strict_mode(tmp_path):
    ct = "2026-06-13T02:00:00Z"
    # hard_floor='1' but C=1.0000 (float(C)<1.0 is False) -> disagreement.
    rows = [_row("flip", ct, 700.0, ev=0.00, C=1.0000, quintile=0, payoff=0.3,
                 hard_floor="1")]
    path = _write(tmp_path, rows)
    ev = PolicyEvaluator(strict_hardfloor=True)
    with pytest.raises(U.PolicyError, match="sub-\\$1 disagreement"):
        ev.feed_csv(path)


def test_f4_hardfloor_authoritative_and_counted_non_strict(tmp_path):
    ct = "2026-06-13T02:00:00Z"
    # hard_floor='1' but C=1.0000: non-strict uses hard_floor (enters as sub-$1) and counts
    # the divergence.
    rows = [_row("flip", ct, 700.0, ev=0.00, C=1.0000, quintile=0, payoff=0.3,
                 hard_floor="1")]
    ev = _eval(tmp_path, rows)                       # default strict_hardfloor=False
    assert [e["source"] for e in ev.entries] == ["sub$1-flip"]
    assert ev.hardfloor_floatc_divergences == 1


def test_f4_hardfloor_zero_blocks_subdollar_despite_low_float_C(tmp_path):
    ct = "2026-06-13T02:00:00Z"
    # hard_floor='0' but C=0.9990 (float<1.0): authoritative says NOT sub-$1 -> no entry.
    rows = [_row("flip", ct, 700.0, ev=0.00, C=0.9990, quintile=0, payoff=0.3,
                 hard_floor="0")]
    ev = _eval(tmp_path, rows)
    assert ev.entries == []
    assert ev.hardfloor_floatc_divergences == 1


# ---------------------------------------------------------------------------
# F1 — authoritative eligible count (train receipts; sealed tape truth)
# ---------------------------------------------------------------------------
def _write_chunk_receipts(tmp_path, eligible_per_chunk):
    d = tmp_path / "chunks"
    for i, n in enumerate(eligible_per_chunk):
        cd = d / f"c{i}"
        cd.mkdir(parents=True)
        (cd / "tape_receipt.json").write_text(
            json.dumps({"n_eligible_windows": n}), encoding="utf-8")
    return str(d)


def test_f1_train_eligible_summed_from_chunk_receipts(tmp_path):
    chunks = _write_chunk_receipts(tmp_path, [158, 158, 161, 161, 161, 159, 161, 23])
    assert U._train_eligible_from_receipts(chunks) == 1142


def test_f1_train_eligible_none_when_receipts_absent(tmp_path):
    assert U._train_eligible_from_receipts(str(tmp_path / "nope")) is None


def test_f1_dryrun_raises_when_authoritative_disagrees_with_csv(tmp_path):
    # Small synthetic train tape: 2 windows with events -> CSV eligible = 2. A chunk receipt
    # claiming 5 eligible must trip the authoritative-vs-CSV consistency check.
    rows = [
        _row("flip", "2026-06-13T02:00:00Z", 700.0, ev=0.0, C=0.9, quintile=1, payoff=0.1),
        _row("flip", "2026-06-13T03:00:00Z", 700.0, ev=0.0, C=0.9, quintile=2, payoff=0.2),
    ]
    tape = _write(tmp_path, rows, name="mini_tape.csv")
    U.FULL60_CHUNKS_DIR = _write_chunk_receipts(tmp_path, [5])
    with pytest.raises(RefuseToRun, match="authoritative eligible"):
        U.run_dry_run_train(tape_path=tape, out_path=str(tmp_path / "cert.json"))


def test_f1_sealed_eligible_from_tape_truth_minus_burned_seen(tmp_path):
    # Two normal entered windows + one burned window seen (has events). tape truth says 10
    # eligible windows across the days (so 7 non-burned eligible are event-less).
    burn = "2026-08-18T19:00:00Z"
    rows = [
        _row("flip", "2026-08-05T02:00:00Z", 700.0, ev=0.0, C=0.9, quintile=1,
             payoff=0.10, date="2026-08-05"),
        _row("flip", "2026-08-05T03:00:00Z", 700.0, ev=0.0, C=0.9, quintile=2,
             payoff=0.20, date="2026-08-05"),
        _row("flip", burn, 700.0, ev=0.0, C=0.9, quintile=0, payoff=0.99,
             date="2026-08-18"),
    ]
    ev = _eval(tmp_path, rows, burned={burn})
    assert ev.eligible_windows == 2               # non-burned windows with events
    assert len(ev.burned_excluded) == 1
    res = U._sealed_aggregates(ev, eligible_from_tape=10)
    assert res["eligible_windows_tape_truth_all"] == 10
    assert res["burned_close_times_seen_count"] == 1
    assert res["eligible_windows"] == 10 - 1                    # minus burned-seen
    assert res["eligible_windows_event_less_estimate"] == 9 - 2  # eligible - with-events
    assert res["entries"] == 2
    assert abs(res["entry_rate"] - 2 / 9) < 1e-12
    # F5: configured vs seen both reported
    assert res["burned_close_times_configured_count"] == 2
    assert res["burned_close_times_seen_count"] == 1


def test_f1_sealed_impossible_accounting_raises(tmp_path):
    rows = [
        _row("flip", "2026-08-05T02:00:00Z", 700.0, ev=0.0, C=0.9, quintile=1,
             payoff=0.10, date="2026-08-05"),
        _row("flip", "2026-08-05T03:00:00Z", 700.0, ev=0.0, C=0.9, quintile=2,
             payoff=0.20, date="2026-08-05"),
    ]
    ev = _eval(tmp_path, rows)
    # tape truth (1) < windows-with-events (2) is an impossible accounting error.
    with pytest.raises(U.PolicyError, match="accounting error"):
        U._sealed_aggregates(ev, eligible_from_tape=1)


# ---------------------------------------------------------------------------
# F2 — one-shot start marker (crash-safe)
# ---------------------------------------------------------------------------
def _stage_passing_preflight(tmp_path):
    """Stage a fully-passing sealed preflight WITHOUT any sealed read (existence checks
    only). Returns nothing; sets the module paths."""
    U.FALSIFIER_MD = _frozen_falsifier(tmp_path)
    assert loader.sha256_file(U.CENSUS_CSV) == U.CENSUS_TRAIN_SHA256   # real train census
    cert = tmp_path / "cert.json"
    cert.write_text(json.dumps(_valid_cert()), encoding="utf-8")
    U.DRYRUN_JSON = str(cert)
    U.STARTED_MARKER = str(tmp_path / "STARTED.marker")
    U.RESULT_JSON = str(tmp_path / "result.json")
    U.SEALED_EVAL_DIR = str(tmp_path / "sealed_eval")


def test_f2_refuses_when_start_marker_exists(tmp_path):
    _stage_passing_preflight(tmp_path)
    with open(U.STARTED_MARKER, "w", encoding="utf-8") as f:
        f.write("{}")
    with pytest.raises(RefuseToRun, match="start marker"):
        U.preflight_sealed()


def test_f2_marker_written_before_first_sealed_open(tmp_path, monkeypatch):
    # Monkeypatch tape_sim.run to record whether the marker exists at the moment of the
    # FIRST (would-be) sealed open, then abort — so NO sealed byte is ever read.
    _stage_passing_preflight(tmp_path)
    seen = {"marker_at_first_call": None, "n_calls": 0, "ack": None}

    def fake_run(dates, out_dir, **kw):
        seen["n_calls"] += 1
        if seen["marker_at_first_call"] is None:
            seen["marker_at_first_call"] = os.path.exists(U.STARTED_MARKER)
            seen["ack"] = kw.get("acknowledge_sealed_read")
        raise RuntimeError("halt before any sealed read")

    monkeypatch.setattr(U._tape_sim, "run", fake_run)
    with pytest.raises(RuntimeError, match="halt before any sealed read"):
        U.run_sealed()
    assert seen["n_calls"] == 1
    assert seen["marker_at_first_call"] is True     # marker existed BEFORE the sealed open
    # (sanity: the runner does intend to acknowledge the sealed read on the real call)
    assert seen["ack"] is True
    # and a second run is now blocked by the marker the aborted run left behind
    with pytest.raises(RefuseToRun, match="start marker"):
        U.preflight_sealed()
