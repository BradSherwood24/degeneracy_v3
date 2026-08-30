"""Unit tests for fetch_history's pure guards: quiet window + sealed-day filter.

Run: python -m pytest tools/tests/test_fetch_history.py   (or: python tools/tests/test_fetch_history.py)
No network — everything here is pure/injected-clock.
"""
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fetch_history as fh  # noqa: E402


def _dt(minute, second=0):
    return datetime(2026, 8, 30, 12, minute, second, tzinfo=timezone.utc)


def test_in_quiet_window_boundaries():
    assert fh.in_quiet_window(_dt(0)) is False
    assert fh.in_quiet_window(_dt(37, 59)) is False
    assert fh.in_quiet_window(_dt(38)) is True      # window opens at :38
    assert fh.in_quiet_window(_dt(50)) is True       # pilot entry window
    assert fh.in_quiet_window(_dt(59, 59)) is True
    assert fh.in_quiet_window(_dt(1)) is False


def test_seconds_until_top_of_hour():
    assert fh.seconds_until_top_of_hour(_dt(38, 0)) == 22 * 60      # :38:00 -> :00 = 22 min
    assert fh.seconds_until_top_of_hour(_dt(59, 0)) == 60
    assert fh.seconds_until_top_of_hour(_dt(59, 59)) == 1
    assert fh.seconds_until_top_of_hour(_dt(0, 0)) == 60 * 60


def test_wait_out_quiet_window_sleeps_when_inside():
    slept = []
    fh.wait_out_quiet_window(now_fn=lambda: _dt(50, 0), sleep_fn=slept.append, log_fn=lambda *a: None)
    assert len(slept) == 1
    # :50:00 -> top of hour = 600s, plus 1s cushion
    assert abs(slept[0] - 601.0) < 1e-6


def test_wait_out_quiet_window_noop_when_outside():
    slept = []
    fh.wait_out_quiet_window(now_fn=lambda: _dt(10, 0), sleep_fn=slept.append, log_fn=lambda *a: None)
    assert slept == []


def test_is_sealed():
    assert fh.is_sealed(date(2026, 8, 1)) is False
    assert fh.is_sealed(date(2026, 8, 2)) is True    # inclusive start
    assert fh.is_sealed(date(2026, 8, 10)) is True
    assert fh.is_sealed(date(2026, 8, 18)) is True    # inclusive end
    assert fh.is_sealed(date(2026, 8, 19)) is False


def test_build_days_refuses_sealed_by_default():
    days = fh.build_days(date(2026, 7, 31), date(2026, 8, 20), acknowledge_sealed=False)
    assert date(2026, 8, 1) in days
    assert date(2026, 8, 19) in days
    assert date(2026, 8, 20) in days
    for n in range(2, 19):
        assert date(2026, 8, n) not in days, f"sealed day 8/{n} leaked into fetch list"


def test_build_days_includes_sealed_when_acknowledged():
    days = fh.build_days(date(2026, 8, 1), date(2026, 8, 20), acknowledge_sealed=True)
    for n in range(1, 21):
        assert date(2026, 8, n) in days


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
