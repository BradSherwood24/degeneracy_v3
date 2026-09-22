"""test_supervisor.py -- the host-shaped wake loop (Phase H, V3.3).

Every side effect is injected: the loop is tested without real time, real processes (except the
explicit sys.executable stub-child test) or real OS signals. The POSIX-only real-signal assertions
are skipped on Windows with a reason.
"""

from __future__ import annotations

import datetime as _dt
import os
import signal
import subprocess
import sys

import pytest

from service import paths, supervisor
from service.supervisor import PopenChild, Supervisor, in_launch_band, next_forty


def _epoch(iso: str) -> float:
    return _dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=_dt.timezone.utc).timestamp()


# ===========================================================================
# Pure wake arithmetic
# ===========================================================================
@pytest.mark.parametrize("now_iso,expected_iso", [
    ("2026-09-22T00:10:00Z", "2026-09-22T00:40:00Z"),   # before :40 -> this hour's :40
    ("2026-09-22T00:40:00Z", "2026-09-22T00:40:00Z"),   # exactly :40 -> now
    ("2026-09-22T00:40:01Z", "2026-09-22T01:40:00Z"),   # just past :40 -> next hour
    ("2026-09-22T00:55:00Z", "2026-09-22T01:40:00Z"),   # in band, past :40 -> next hour
    ("2026-09-22T23:50:00Z", "2026-09-23T00:40:00Z"),   # day boundary
    ("2026-09-30T23:55:00Z", "2026-10-01T00:40:00Z"),   # month boundary
    ("2026-12-31T23:45:00Z", "2027-01-01T00:40:00Z"),   # year boundary
])
def test_next_forty_boundaries(now_iso, expected_iso):
    assert next_forty(_epoch(now_iso)) == _epoch(expected_iso)


@pytest.mark.parametrize("iso,expected", [
    ("2026-09-22T00:40:00Z", True),    # band start
    ("2026-09-22T00:59:59Z", True),    # band end (inclusive of :59)
    ("2026-09-22T00:39:59Z", False),   # one second early
    ("2026-09-22T00:00:00Z", False),   # top of hour
    ("2026-09-22T00:41:23Z", True),    # late-wake, still in band
    ("2026-09-22T01:00:00Z", False),   # next top of hour
])
def test_in_launch_band(iso, expected):
    assert in_launch_band(_epoch(iso)) is expected


# ===========================================================================
# Fakes
# ===========================================================================
class FakeChild:
    def __init__(self, codes, pid=4321, on_wait=None):
        self._codes = list(codes)
        self.pid = pid
        self._on_wait = on_wait
        self.forwarded = []
        self.killed = False
        self.wait_calls = 0

    def wait(self, timeout):
        self.wait_calls += 1
        if self._on_wait is not None:
            self._on_wait(self.wait_calls)
        return self._codes.pop(0) if self._codes else 0

    def forward_signal(self, signum):
        self.forwarded.append(signum)

    def kill(self):
        self.killed = True


def _quiet_fakes(**over):
    """Common no-op injections so a Supervisor writes nothing and touches no proxy."""
    base = dict(
        proxy_base="http://fake:8642",
        sweep=lambda base_url: {"skipped": True, "found": 0},
        rotate=lambda jd: {"count": 0, "dir": jd},
        install_signals=False,
        log_path=os.devnull,
    )
    base.update(over)
    return base


# ===========================================================================
# Boot sweep
# ===========================================================================
def test_boot_sweep_called_once_with_resolved_proxy_base():
    calls = []
    sup = Supervisor(**_quiet_fakes(
        proxy_base="http://resolved-proxy:8642",
        once=True, run_now=True,
        sweep=lambda base_url: calls.append(base_url) or {"found": 0},
        spawn=lambda args: FakeChild([0]),
    ))
    assert sup.run() == 0
    assert calls == ["http://resolved-proxy:8642"]


def test_boot_sweep_failure_does_not_sink_supervisor():
    def boom(_base):
        raise RuntimeError("proxy unreachable")

    events = []
    sup = Supervisor(**_quiet_fakes(
        once=True, run_now=True, sweep=boom,
        spawn=lambda args: FakeChild([0]),
        on_event=events.append,
    ))
    assert sup.run() == 0
    kinds = [e["event"] for e in events]
    assert "boot_sweep" in kinds and "window" in kinds


# ===========================================================================
# One window / exit codes
# ===========================================================================
def test_once_runs_exactly_one_child_and_logs_exit_code():
    spawns = []
    events = []

    def spawn(args):
        spawns.append(args)
        return FakeChild([7], pid=1234)

    sup = Supervisor(**_quiet_fakes(
        once=True, run_now=True, spawn=spawn, on_event=events.append,
    ))
    assert sup.run() == 0
    assert len(spawns) == 1
    window = [e for e in events if e["event"] == "window"]
    assert len(window) == 1
    assert window[0]["exit_code"] == 7
    assert window[0]["pid"] == 1234
    assert window[0]["signaled"] is False


def test_once_with_real_stub_child():
    """The real PopenChild.wait path against a sys.executable stub child (exit code 3)."""
    events = []
    spawns = []

    def spawn(args):
        spawns.append(args)
        return PopenChild(subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(3)"]))

    sup = Supervisor(**_quiet_fakes(once=True, run_now=True, spawn=spawn, on_event=events.append))
    assert sup.run() == 0
    assert len(spawns) == 1
    window = [e for e in events if e["event"] == "window"][0]
    assert window["exit_code"] == 3


# ===========================================================================
# Journal rotation at wake
# ===========================================================================
def test_rotation_called_at_wake_with_journal_dir(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "dv3"))
    seen = []
    sup = Supervisor(**_quiet_fakes(
        once=True, run_now=True,
        rotate=lambda jd: seen.append(jd) or {"count": 0},
        spawn=lambda args: FakeChild([0]),
    ))
    assert sup.run() == 0
    assert seen == [paths.journal_dir_v32()]
    assert seen[0] == os.path.join(str(tmp_path / "dv3"), "journals_v32")


def test_default_rotate_skips_current_and_gzips_old(tmp_path):
    """The default rotate gzips an OLD closed raw journal but never the current window's."""
    jd = tmp_path / "journals_v32"
    jd.mkdir()
    old = jd / "20260901T120000Z.jsonl"
    old.write_text('{"a":1}\n', encoding="utf-8")
    # Age it past the 30-min min-age guard.
    old_mtime = _epoch("2026-09-01T12:05:00Z")
    os.utime(old, (old_mtime, old_mtime))
    # A journal named for the CURRENT window must be excluded even if present.
    now = _epoch("2026-09-22T00:40:00Z")
    current_name = supervisor._current_window_journal_basename(now)  # -> 20260922T010000Z.jsonl
    current = jd / current_name
    current.write_text('{"b":2}\n', encoding="utf-8")
    os.utime(current, (old_mtime, old_mtime))  # old enough by mtime, but excluded by name

    res = supervisor._default_rotate(str(jd), clock=lambda: now)
    assert old.name in res["rotated"]
    assert (jd / (old.name + ".gz")).exists()
    assert not old.exists()
    # current window untouched
    assert current.exists()
    assert current_name in res["kept"]


# ===========================================================================
# Late wake / skipped_late
# ===========================================================================
class _Clock:
    """A clock the fake sleep advances -- robust to the exact number of clock() reads the loop makes."""

    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def test_skipped_late_when_wake_overshoots_into_next_hour():
    """Sleep to :40 but wake at :05 of the next hour -> skipped_late, then run at the following :40."""
    clk = _Clock(_epoch("2026-09-22T00:10:00Z"))
    state = {"slept": 0}

    def fake_sleep(_secs):
        state["slept"] += 1
        if state["slept"] == 1:
            clk.t = _epoch("2026-09-22T01:05:00Z")  # overshoot the :40 window -> skipped_late
        else:
            clk.t = _epoch("2026-09-22T01:40:00Z")  # the following :40 -> in band -> run
        return False  # not interrupted

    events = []
    sup = Supervisor(**_quiet_fakes(
        once=True, run_now=False, clock=clk, sleep=fake_sleep,
        spawn=lambda args: FakeChild([0]),
        on_event=events.append,
    ))
    assert sup.run() == 0
    kinds = [e["event"] for e in events]
    assert kinds.count("skipped_late") == 1
    assert kinds.count("window") == 1  # exactly one window ran (the following :40)


def test_late_wake_in_band_runs_current_hour():
    """Booting at :45 (already inside the band) runs immediately, no skip."""
    now = _epoch("2026-09-22T00:45:00Z")
    events = []
    sup = Supervisor(**_quiet_fakes(
        once=True, run_now=False, clock=lambda: now,
        spawn=lambda args: FakeChild([0]),
        on_event=events.append,
    ))
    assert sup.run() == 0
    kinds = [e["event"] for e in events]
    assert "skipped_late" not in kinds
    assert "sleep" not in kinds  # already in band -> no sleep
    assert kinds.count("window") == 1


# ===========================================================================
# Signals
# ===========================================================================
def test_sigterm_idle_exits_zero_without_spawning():
    spawns = []

    def spawn(args):
        spawns.append(args)
        return FakeChild([0])

    sup = Supervisor(**_quiet_fakes(
        once=False, run_now=False,
        clock=lambda: _epoch("2026-09-22T00:10:00Z"),  # before :40 -> it will sleep
        spawn=spawn,
    ))

    def fake_sleep(_secs):
        sup.request_stop(signal.SIGTERM)
        return True  # interrupted

    sup._sleep = fake_sleep
    assert sup.run() == 0
    assert spawns == []  # never spawned a child


def test_sigterm_busy_forwards_and_waits():
    """Stop arrives while the child runs -> forward the signal, wait grace, child exits -> code 0."""
    child = FakeChild([None, 0])  # first wait: running; grace wait: exited

    sup = Supervisor(**_quiet_fakes(
        once=True, run_now=True,
        spawn=lambda args: child,
    ))
    # Trigger the stop on the child's first wait (i.e. mid-window).
    child._on_wait = lambda n: sup.request_stop(signal.SIGTERM) if n == 1 else None

    rc = sup.run()
    assert child.forwarded == [signal.SIGTERM]
    assert child.killed is False
    assert rc == 0


def test_sigterm_busy_hard_kills_after_grace():
    """Child overstays the grace -> hard kill, and the supervisor returns the child's final code."""
    child = FakeChild([None, None, 137])  # running, still running after grace, then reaped

    sup = Supervisor(**_quiet_fakes(
        once=True, run_now=True,
        grace_s=0.0,
        spawn=lambda args: child,
    ))
    child._on_wait = lambda n: sup.request_stop(signal.SIGTERM) if n == 1 else None

    rc = sup.run()
    assert child.forwarded == [signal.SIGTERM]
    assert child.killed is True
    assert rc == 137


@pytest.mark.skipif(os.name == "nt",
                    reason="POSIX-only: real SIGTERM delivery to a subprocess; Windows uses "
                           "CTRL_BREAK_EVENT which pytest cannot deliver portably here.")
def test_real_sigterm_forwarded_to_stub_child():
    """A real, long-lived stub child receives a forwarded SIGTERM and dies (POSIX only)."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    child = PopenChild(proc)
    assert child.wait(0.2) is None  # it is running
    child.forward_signal(signal.SIGTERM)
    rc = child.wait(5.0)
    assert rc is not None  # it terminated on the signal


# ===========================================================================
# Data-dir containment
# ===========================================================================
def test_supervisor_log_lands_in_data_dir_only(monkeypatch, tmp_path):
    data = tmp_path / "dv3"
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(data))
    sup = Supervisor(
        proxy_base="http://fake:8642",
        once=True, run_now=True, install_signals=False,
        sweep=lambda base_url: {"found": 0},
        rotate=lambda jd: {"count": 0},
        spawn=lambda args: FakeChild([0]),
        # NOTE: no log_path override -> it must resolve under DV3_DATA_DIR
    )
    assert sup.run() == 0
    log = paths.supervisor_log_path()
    assert os.path.abspath(log).startswith(os.path.abspath(str(data)))
    assert os.path.exists(log)
    with open(log, "r", encoding="utf-8") as f:
        body = f.read()
    assert '"event": "window"' in body
