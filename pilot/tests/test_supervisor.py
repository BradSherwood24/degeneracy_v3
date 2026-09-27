"""test_supervisor.py -- the host-shaped wake loop (Phase H, V3.3).

Every side effect is injected: the loop is tested without real time, real processes (except the
explicit sys.executable stub-child test) or real OS signals. The POSIX-only real-signal assertions
are skipped on Windows with a reason.
"""

from __future__ import annotations

import datetime as _dt
import math
import os
import signal
import subprocess
import sys

import pytest

from service import paths, supervisor
from service.supervisor import (
    PopenChild,
    Supervisor,
    _boot_sweep_wait_ready,
    in_launch_band,
    next_forty,
)


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


# ===========================================================================
# Per-window watchdog (Finding 2)
# ===========================================================================
def test_child_watchdog_kills_hung_child_and_continues():
    """A child still running past window close + watchdog grace is forwarded/killed, logged, and the
    supervisor moves on (here --once -> returns 0 after the one killed window)."""
    clk = _Clock(_epoch("2026-09-22T00:45:00Z"))

    def on_wait(_n):
        # every wait sees the clock already far past close(01:00) + grace(120s)
        clk.t = _epoch("2026-09-22T01:00:00Z") + 10_000

    child = FakeChild([None, None, None], on_wait=on_wait)  # never exits on its own
    events = []
    sup = Supervisor(**_quiet_fakes(
        once=True, run_now=True, clock=clk,
        spawn=lambda args: child,
        on_event=events.append,
    ))
    rc = sup.run()
    assert rc == 0
    assert child.forwarded == [signal.SIGTERM]
    assert child.killed is True
    kinds = [e["event"] for e in events]
    assert "child_watchdog_killed" in kinds
    window = [e for e in events if e["event"] == "window"][0]
    assert window["status"] == "watchdog_killed"
    assert window["signaled"] is False


def test_healthy_child_never_trips_watchdog():
    """A child that exits promptly reports status 'exited' and no watchdog kill."""
    events = []
    sup = Supervisor(**_quiet_fakes(
        once=True, run_now=True,
        clock=lambda: _epoch("2026-09-22T00:45:00Z"),
        spawn=lambda args: FakeChild([0]),
        on_event=events.append,
    ))
    assert sup.run() == 0
    window = [e for e in events if e["event"] == "window"][0]
    assert window["status"] == "exited"
    assert "child_watchdog_killed" not in [e["event"] for e in events]


# ===========================================================================
# Boot-sweep proxy-readiness retry (Finding 3)
# ===========================================================================
def test_boot_sweep_wait_ready_stops_when_ready():
    logs, sleeps = [], []
    seq = iter([False, False, True])
    ready = _boot_sweep_wait_ready(
        "http://p:8642", max_attempts=4, retry_interval_s=2.0,
        ready_fn=lambda _b: next(seq), sleep=sleeps.append, log=logs.append,
    )
    assert ready is True
    assert len(logs) == 3            # one log line per attempt, until ready
    assert sleeps == [2.0, 2.0]      # slept between the two not-ready attempts, not after success


def test_boot_sweep_wait_ready_gives_up_after_max_attempts():
    logs, sleeps = [], []
    ready = _boot_sweep_wait_ready(
        "http://p:8642", max_attempts=3, retry_interval_s=1.5,
        ready_fn=lambda _b: False, sleep=sleeps.append, log=logs.append,
    )
    assert ready is False
    assert len(logs) == 3            # one per attempt
    assert sleeps == [1.5, 1.5]      # no sleep after the final attempt


def test_boot_sweep_skips_readiness_and_proxy_when_not_armed(monkeypatch):
    """dry/shakedown (and not --dry-sweep): a logged no-op -- readiness never probed, proxy untouched."""
    monkeypatch.setattr(supervisor, "_read_mode_safe", lambda: "dry")
    probed = []
    logs = []
    result = supervisor._default_boot_sweep(
        "http://p:8642", dry_sweep=False, clock=lambda: 0.0, log=logs.append,
        ready_fn=lambda b: probed.append(b) or True, sleep=lambda s: None,
    )
    assert result["skipped"] is True and result["mode"] == "dry"
    assert probed == []              # no readiness probe when the sweep touches no proxy


# ===========================================================================
# One-run-per-close guard (respawn hotfix)
# ===========================================================================
# The bug: after a child returns EARLY (an instant stand-down at a no-bucket close, a crash, or a
# watchdog kill), the loop -- still inside the [:40, :60) launch band -- respawned another child for the
# SAME close on every pass until :60. Measured hundreds-to-thousands of spawns/hour at the 21:00Z close
# (each doing proxy discovery GETs and appending a duplicate stand-down ledger row). The fix: one run
# per close; a subsequent in-band pass for a close already run sleeps to the next hour's :40.
def test_fast_standdown_in_band_runs_once_then_sleeps_close_already_run():
    """(a) A child that exits in ~0.6 s while still inside the band -> exactly ONE spawn, then a sleep
    event to the next :40 with reason 'close_already_run' (NOT a respawn for the same close)."""
    clk = _Clock(_epoch("2026-09-22T00:45:00Z"))  # in band, close = 01:00
    spawns = []

    def spawn(args):
        spawns.append(clk.t)

        def on_wait(_n):
            clk.t += 0.6  # instant stand-down: child returns 0 after ~0.6 s

        return FakeChild([0], on_wait=on_wait)

    events = []
    sup = Supervisor(**_quiet_fakes(
        once=False, run_now=True, clock=clk, spawn=spawn, on_event=events.append,
    ))

    def fake_sleep(_secs):
        # the close_already_run sleep-to-next-:40; advance the clock and stop so the test terminates.
        clk.t = _epoch("2026-09-22T01:40:00Z")
        sup.request_stop(signal.SIGTERM)
        return True

    sup._sleep = fake_sleep
    assert sup.run() == 0
    assert len(spawns) == 1  # ONE spawn for close 01:00, not a respawn storm
    car = [e for e in events if e["event"] == "sleep" and e.get("reason") == "close_already_run"]
    assert len(car) == 1
    assert car[0]["close"] == "2026-09-22T01:00:00Z"
    assert car[0]["wake"] == "2026-09-22T01:40:00Z"
    assert [e["event"] for e in events].count("window") == 1


def test_boot_inside_band_runs_current_close_once():
    """(b) Booting inside the band (no --now) runs exactly one window for the current close, no
    close_already_run guard trip (last_close_run starts None -> a genuine late first wake still runs)."""
    now = _epoch("2026-09-22T00:45:00Z")
    events = []
    sup = Supervisor(**_quiet_fakes(
        once=True, run_now=False, clock=lambda: now,
        spawn=lambda args: FakeChild([0]),
        on_event=events.append,
    ))
    assert sup.run() == 0
    kinds = [e["event"] for e in events]
    assert kinds.count("window") == 1
    assert not any(e.get("reason") == "close_already_run" for e in events if e["event"] == "sleep")


def test_watchdog_killed_child_not_rerun_for_same_close():
    """(c) A watchdog-killed child gets ONE run for its close; the loop moves on to the next :40 and
    never respawns for the killed close."""
    clk = _Clock(_epoch("2026-09-22T00:45:00Z"))  # boot in band, close = 01:00
    spawns = []

    def on_wait(_n):
        # every wait sees the clock past close(01:00) + watchdog grace(120 s) -> watchdog fires.
        clk.t = _epoch("2026-09-22T01:02:00Z")

    def spawn(args):
        spawns.append(clk.t)
        return FakeChild([None, None, None], on_wait=on_wait)  # never exits on its own

    events = []
    sup = Supervisor(**_quiet_fakes(
        once=False, run_now=True, clock=clk, spawn=spawn, on_event=events.append,
    ))

    def fake_sleep(_secs):
        # the post-window sleep-to-next-:40; stop so the test terminates.
        sup.request_stop(signal.SIGTERM)
        return True

    sup._sleep = fake_sleep
    assert sup.run() == 0
    assert len(spawns) == 1  # the killed window ran once; not respawned for close 01:00
    kinds = [e["event"] for e in events]
    assert "child_watchdog_killed" in kinds
    window = [e for e in events if e["event"] == "window"][0]
    assert window["status"] == "watchdog_killed"


def test_normal_path_next_spawn_at_next_forty():
    """(d) Normal path unchanged: a child that runs to just past close, then the loop sleeps to the next
    :40 and spawns the NEXT window (no close_already_run event)."""
    clk = _Clock(_epoch("2026-09-22T00:45:00Z"))
    spawns = []

    def on_wait(_n):
        clk.t = (math.floor(clk.t / 3600.0) + 1) * 3600 + 5  # advance to this window's close + 5 s

    def spawn(args):
        spawns.append(clk.t)
        return FakeChild([0], on_wait=on_wait)

    events = []
    sup = Supervisor(**_quiet_fakes(
        once=False, run_now=True, clock=clk, spawn=spawn, on_event=events.append,
    ))
    state = {"n": 0}

    def fake_sleep(_secs):
        state["n"] += 1
        if state["n"] == 1:
            clk.t = _epoch("2026-09-22T01:40:00Z")  # wake at the next :40 -> run window 2
            return False
        sup.request_stop(signal.SIGTERM)  # stop after the second window's post-close sleep
        return True

    sup._sleep = fake_sleep
    assert sup.run() == 0
    assert len(spawns) == 2
    assert spawns[0] == _epoch("2026-09-22T00:45:00Z")
    assert spawns[1] == _epoch("2026-09-22T01:40:00Z")
    assert not any(e.get("reason") == "close_already_run" for e in events if e["event"] == "sleep")


def test_once_exits_after_one_window_with_guard():
    """(e) --once still exits after exactly one window (the guard never fires on the first run)."""
    spawns = []
    sup = Supervisor(**_quiet_fakes(
        once=True, run_now=True,
        clock=lambda: _epoch("2026-09-22T00:45:00Z"),
        spawn=lambda args: spawns.append(1) or FakeChild([0]),
    ))
    assert sup.run() == 0
    assert len(spawns) == 1


def test_large_watchdog_grace_kill_in_next_band_runs_new_close_once():
    """N2: a watchdog kill whose deadline (close + a LARGE grace) lands inside the NEXT hour's launch
    band must run the NEW close exactly once and must NOT re-run the killed close. This discriminates
    the fix: the guard keys on the close epoch, so a legitimate next-hour band entry is not suppressed
    while a same-close re-entry is. The fake clock is advanced INSIDE the child wait and the sleep."""
    clk = _Clock(_epoch("2026-09-22T00:45:00Z"))  # boot in band, close = 01:00, grace 2500s -> 01:41:40
    spawns = []

    def spawn(args):
        idx = len(spawns)
        spawns.append(clk.t)
        if idx == 0:
            def on_wait(_n):
                # past close(01:00) + grace(2500s) = 01:41:40, which is INSIDE the 02:00 window's band.
                clk.t = _epoch("2026-09-22T01:41:40Z")
            return FakeChild([None, None, None], on_wait=on_wait)  # never exits -> watchdog fires

        def on_wait2(_n):
            clk.t = _epoch("2026-09-22T02:00:05Z")  # window 2 runs to just past its close

        return FakeChild([0], on_wait=on_wait2)

    events = []
    sup = Supervisor(**_quiet_fakes(
        once=False, run_now=True, clock=clk, watchdog_grace_s=2500.0,
        spawn=spawn, on_event=events.append,
    ))

    def fake_sleep(_secs):
        sup.request_stop(signal.SIGTERM)  # stop at window 2's post-close sleep
        return True

    sup._sleep = fake_sleep
    assert sup.run() == 0
    assert len(spawns) == 2
    assert spawns[0] == _epoch("2026-09-22T00:45:00Z")   # close 01:00 (watchdog-killed)
    assert spawns[1] == _epoch("2026-09-22T01:41:40Z")   # close 02:00 -- the NEW close, not a re-run
    windows = [e for e in events if e["event"] == "window"]
    assert windows[0]["status"] == "watchdog_killed"
    assert windows[1]["status"] == "exited"
    assert "child_watchdog_killed" in [e["event"] for e in events]
    # different closes -> the one-run-per-close guard never fires
    assert not any(e.get("reason") == "close_already_run" for e in events)
