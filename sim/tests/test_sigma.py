"""sigma-hat: 9 anchors -> 8 diffs, contiguous tape, cross-day backward; <9 raises;
sigma==0 handled by census (A2.1/A2.4)."""
import datetime as _dt
import statistics

import pytest

from census import ANCHOR_STEP, SIGMA_ANCHORS, InsufficientTape, sigma_hat


def _ep(y, mo, d, h, mi):
    return int(_dt.datetime(y, mo, d, h, mi, tzinfo=_dt.timezone.utc).timestamp())


def test_sigma_uses_nine_contiguous_anchors():
    T = _ep(2026, 7, 1, 3, 0)
    vals = [60000.0, 60010.0, 60025.0, 60045.0, 60070.0,
            60100.0, 60135.0, 60175.0, 60220.0]  # oldest..newest (T)
    tape = {T - (8 - k) * ANCHOR_STEP: vals[k] for k in range(9)}
    # anchors newest..oldest as sigma_hat reads them: T, T-900, ...
    ordered = [tape[T - k * ANCHOR_STEP] for k in range(9)]
    diffs = [ordered[i] - ordered[i + 1] for i in range(8)]
    assert sigma_hat(tape, T) == pytest.approx(statistics.stdev(diffs))
    assert SIGMA_ANCHORS == 9


def test_sigma_missing_any_of_nine_raises():
    T = _ep(2026, 7, 1, 3, 0)
    tape = {T - k * ANCHOR_STEP: 60000.0 + k for k in range(9)}
    del tape[T - 4 * ANCHOR_STEP]        # punch a hole
    with pytest.raises(InsufficientTape):
        sigma_hat(tape, T)


def test_sigma_stitches_backward_across_utc_midnight():
    # T = 01:00 UTC on 2026-07-02; the 9 anchors reach back into 2026-07-01 (prior day).
    T = _ep(2026, 7, 2, 1, 0)
    tape = {T - k * ANCHOR_STEP: 60000.0 + (k * k) for k in range(9)}
    # oldest anchor is at 2026-07-01 22:45 UTC -> genuinely cross-day
    oldest_ep = T - 8 * ANCHOR_STEP
    assert _dt.datetime.fromtimestamp(oldest_ep, tz=_dt.timezone.utc).day == 1
    assert sigma_hat(tape, T) > 0.0


def test_sigma_zero_when_flat_tape():
    T = _ep(2026, 7, 1, 3, 0)
    tape = {T - k * ANCHOR_STEP: 60000.0 for k in range(9)}   # all identical -> diffs 0
    assert sigma_hat(tape, T) == 0.0
