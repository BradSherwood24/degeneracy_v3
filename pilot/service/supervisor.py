"""supervisor.py -- the long-running host-shaped runtime wrapper for the V3.2 window process.

Phase H (V3.3). This is what Render's Background Worker (and, locally, a Task Scheduler "run whether
the user is logged on or not" task) runs instead of registering one Windows task per hour. It is a
THIN wrapper: it never touches the window/strategy logic -- ``run_v32`` stays the per-window process
and is spawned as a subprocess exactly as the old scheduled task launched it.

The loop, once per UTC hour:

  1. (boot, once) run the startup cancel sweep -- cancel any stray resting ``v32-*`` KXBTC order a
     crashed/killed container left on the venue (``executor.cancel_stale_open_orders`` semantics).
     In ``dry``/no-proxy this is a no-op with a log line; ``--dry-sweep`` lists what it WOULD cancel
     without cancelling; the whole sweep is injectable for tests.
  2. sleep to the next UTC :40:00 (computed from the injected clock; DST is irrelevant -- everything
     is UTC). A late wake still inside the launch band [:40, :60) runs for the CURRENT hour; a wake
     that overshot into [:00, :40) of the next hour logs ``skipped_late`` and re-computes.
  3. rotate leftover raw journals (gzip closed ``*.jsonl`` from crashed/killed windows) -- the call
     ``run_v32`` never inherited, so orphan raw journals accumulate today.
  4. spawn ``python -m service.run_v32`` (same cwd/argv as the scheduled task; mode + paths come from
     the mode file / env, not baked-in flags), wait for it, and emit one structured JSON line
     (wake time, child pid, exit code, duration). A per-window WATCHDOG kills a child still running at
     its window close + a grace (default 120 s; run_v32's own deadline is close + 10 s) and moves on,
     so one wedged window can never take the strategy offline for hours.
  5. repeat (``--once`` runs exactly one window then exits).

SIGTERM / SIGINT / (Windows) SIGBREAK: while idle it exits 0 promptly; while a child runs it forwards
the signal to the child, waits up to ``SIGTERM_GRACE_S`` (240 s, under Render's 300 s cap), then
exits with the child's code. On Windows the child is created in its own process group so a
``CTRL_BREAK_EVENT`` reaches it; on Linux (the target host) a plain ``SIGTERM`` is forwarded.

House law: no key material is ever touched here; all Kalshi access is through the proxy base URL
(``--proxy-base`` / ``DV3_PROXY_BASE`` / the historic ``http://127.0.0.1:8642``). Writable paths all
resolve through ``service.paths`` so ``DV3_DATA_DIR`` relocates them; with the env unset every path is
byte-identical to today.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import math
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from service.paths import (
    default_proxy_base,
    journal_dir_v32,
    journal_keep_path,
    mode_path_v32,
    pilot_dir,
    supervisor_log_path,
)

logger = logging.getLogger(__name__)

# UTC seconds-into-hour of the wake (:40:00). run_v32 with no --close targets the NEXT :00, so a wake
# anywhere in [:40, :60) yields a valid window (20 min on time, shorter if late) closing at :00.
LAUNCH_SEC = 40 * 60  # 2400

# SIGTERM grace: forward the signal to the child, then wait this long before hard-killing and exiting
# with the child's code. Under Render's 300 s shutdown cap; a full window is ~22 min, so a mid-window
# deploy still risks a leaked rest until T-4 expiry (hence the :02-:33 deploy rule + the boot sweep).
SIGTERM_GRACE_S = 240.0

# How often the child-wait loop wakes to re-check the stop flag / watchdog deadline while a window runs.
CHILD_POLL_S = 1.0

# Per-window watchdog: if the child is STILL running this long after its window close (:00), the
# supervisor forwards a signal, waits the SIGTERM grace, hard-kills, logs child_watchdog_killed, and
# moves on to the next :40 -- one wedged window can never silently take the strategy offline for hours.
# run_v32's OWN deadline is close + 10 s (GRACE_SECONDS), so a healthy child always returns well before.
WATCHDOG_GRACE_S = 120.0

# Boot-sweep proxy-readiness retry: a fresh host boot may start the supervisor before the proxy is up.
BOOT_SWEEP_MAX_ATTEMPTS = 4
BOOT_SWEEP_RETRY_INTERVAL_S = 2.0


# ===========================================================================
# Pure wake arithmetic (unit-tested directly; the loop calls these)
# ===========================================================================
def next_forty(now: float) -> float:
    """The next UTC :40:00 at or after ``now`` (epoch seconds).

    ``now`` exactly at :40:00 returns ``now``. UTC throughout -> no DST arithmetic; hour/day/month/
    year roll-over is just epoch addition.
    """
    base = math.floor(now / 3600.0) * 3600
    launch = base + LAUNCH_SEC
    if now <= launch:
        return launch
    return launch + 3600


def in_launch_band(now: float) -> bool:
    """True iff ``now`` is within the launch band [:40:00, :60:00) of its own UTC hour."""
    sec = now - math.floor(now / 3600.0) * 3600
    return LAUNCH_SEC <= sec < 3600


def _iso(epoch: float) -> str:
    """Epoch seconds -> ``YYYY-MM-DDTHH:MM:SSZ`` (UTC)."""
    return _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _next_top_of_hour_iso(now: float) -> str:
    """The next :00:00 UTC strictly after ``now`` (mirrors run_v32's default --close)."""
    dt = _dt.datetime.fromtimestamp(now, tz=_dt.timezone.utc)
    top = dt.replace(minute=0, second=0, microsecond=0) + _dt.timedelta(hours=1)
    return top.strftime("%Y-%m-%dT%H:%M:%SZ")


def _next_top_of_hour_epoch(now: float) -> float:
    """The next :00:00 UTC strictly after ``now`` (epoch) -- the child's window close, for the
    watchdog deadline."""
    return (math.floor(now / 3600.0) + 1) * 3600


def _current_window_journal_basename(now: float) -> str:
    """The raw-journal basename the child WILL write this window (defensive rotation exclusion).

    Mirrors ``record_range.journal_filename(next_top_of_hour_iso(now))``. At rotation time (before the
    child is spawned) this file does not yet exist, so the exclusion is belt-and-suspenders; the
    min-age rule in ``rotate_closed_journals`` is the real guard for the current window.
    """
    close_iso = _next_top_of_hour_iso(now)
    return close_iso.replace(":", "").replace("-", "") + ".jsonl"


# ===========================================================================
# Child process adapter
# ===========================================================================
class PopenChild:
    """Adapter over ``subprocess.Popen`` giving the loop a small, injectable-shaped interface."""

    def __init__(self, popen: subprocess.Popen) -> None:
        self._p = popen

    @property
    def pid(self) -> int:
        return self._p.pid

    def wait(self, timeout: float) -> int | None:
        """Wait up to ``timeout`` s; return the exit code, or ``None`` if still running."""
        try:
            return self._p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def forward_signal(self, signum: int) -> None:
        """Forward a shutdown signal to the child. Windows -> CTRL_BREAK to the child's own process
        group (it was created with CREATE_NEW_PROCESS_GROUP); POSIX -> the received signal. Any
        failure falls back to ``terminate()`` so the child is never left running by a signalling
        error."""
        try:
            if os.name == "nt":
                self._p.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            else:
                self._p.send_signal(signum)
        except Exception as e:  # noqa: BLE001 - never let a signalling error strand the child
            logger.warning("[SUPERVISOR] forward_signal failed (%s); terminating child", e)
            try:
                self._p.terminate()
            except Exception:  # noqa: BLE001
                pass

    def kill(self) -> None:
        try:
            self._p.kill()
        except Exception:  # noqa: BLE001
            pass


def _default_spawn(child_args: list[str]) -> PopenChild:
    """Spawn ``python -m service.run_v32 <child_args>`` from the pilot dir (same as the scheduled
    task). The child inherits the environment (DV3_DATA_DIR / DV3_PROXY_BASE / mode file govern it)."""
    cmd = [sys.executable, "-m", "service.run_v32", *child_args]
    kwargs: dict[str, Any] = {"cwd": pilot_dir()}
    if os.name == "nt":
        # A new process group is required for CTRL_BREAK_EVENT to reach the child on Windows.
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    return PopenChild(subprocess.Popen(cmd, **kwargs))


# ===========================================================================
# Boot sweep (default implementation; injectable for tests)
# ===========================================================================
class _LogJournal:
    """A ``journal``-shaped sink (``append(kind, payload, ts)``) that forwards to the logger, so the
    reused ``cancel_stale_open_orders`` (which journals its progress) works without a StreamJournal."""

    def __init__(self, log: Callable[[str], None]) -> None:
        self._log = log

    def append(self, kind: str, payload: Any, ts: float) -> None:
        self._log(f"boot_sweep_journal kind={kind} payload={payload}")


class _DryDeleteWriter:
    """Wraps a real ProxyWriter but turns every DELETE into a logged no-op returning a fake success,
    so ``--dry-sweep`` reuses the exact same order-listing / filtering as the real sweep and only the
    cancels are suppressed."""

    def __init__(self, inner: Any, log: Callable[[str], None]) -> None:
        self._inner = inner
        self._log = log

    def rest_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._inner.rest_get(path, params)

    def rest_delete(self, path: str) -> Any:
        from service.proxy_writer import WriteResponse

        self._log(f"boot_sweep_would_cancel path={path}")
        return WriteResponse(status_code=200, body={}, ok=True, error=None)


def _proxy_ready(proxy_base: str, timeout: float = 2.0) -> bool:
    """A cheap boot-time readiness probe: GET ``{base}/health``; ready iff it answers < 500.

    A not-yet-started proxy (connection refused / timeout) -> not ready. Read-only, no key material.
    """
    import requests

    try:
        resp = requests.get(proxy_base.rstrip("/") + "/health", timeout=timeout)
        return int(getattr(resp, "status_code", 500)) < 500
    except Exception:  # noqa: BLE001 - any transport error means "not ready yet"
        return False


def _boot_sweep_wait_ready(
    proxy_base: str,
    *,
    max_attempts: int,
    retry_interval_s: float,
    ready_fn: Callable[[str], bool],
    sleep: Callable[[float], None],
    log: Callable[[str], None],
) -> bool:
    """Poll the proxy for readiness up to ``max_attempts`` times, ``retry_interval_s`` apart, logging
    ONE line per attempt. Returns True as soon as it is ready, else False after the last attempt.

    Boot-only (a fresh host may start the supervisor before the proxy). This wraps -- does NOT replace
    -- the bounded retry already inside ``cancel_stale_open_orders`` -> ``ProxyAuth.rest_get``.
    """
    for attempt in range(1, max_attempts + 1):
        ready = ready_fn(proxy_base)
        log(f"boot_sweep proxy readiness attempt {attempt}/{max_attempts}: ready={ready} "
            f"base={proxy_base}")
        if ready:
            return True
        if attempt < max_attempts:
            sleep(retry_interval_s)
    return False


def _default_boot_sweep(
    proxy_base: str,
    *,
    dry_sweep: bool,
    clock: Callable[[], float],
    log: Callable[[str], None],
    max_attempts: int = BOOT_SWEEP_MAX_ATTEMPTS,
    retry_interval_s: float = BOOT_SWEEP_RETRY_INTERVAL_S,
    ready_fn: Callable[[str], bool] = _proxy_ready,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Cancel stray resting ``v32-*`` KXBTC orders via the proxy (crash-recovery on boot).

    - ``--dry-sweep``: list what WOULD be cancelled (read-only), issue no DELETE. Independent of mode.
    - mode != ``armed`` (from the mode file): a no-op with a log line (nothing is placed in dry, so
      nothing of ours should be resting to cancel; if a prior armed run left rests, ``--dry-sweep`` or
      an armed boot clears them). No proxy is touched -> no readiness wait.
    - mode == ``armed``: the real sweep. Fail-closed: an unreachable proxy just cancels nothing
      (``cancel_stale_open_orders`` swallows the read error and returns zeros).

    Before any proxy-touching sweep it waits (bounded, explicit ``max_attempts`` / ``retry_interval_s``,
    one log line per attempt) for the proxy to answer ``/health`` -- a fresh host boot may start the
    supervisor before the proxy is up. It proceeds regardless once the attempts are spent (the sweep is
    already fail-closed).
    """
    from service.proxy_writer import ProxyWriter
    from service.v32.executor import cancel_stale_open_orders

    mode = _read_mode_safe()
    journal = _LogJournal(log)
    if not dry_sweep and mode != "armed":
        result = {"skipped": True, "mode": mode, "found": 0, "cancelled": 0, "errors": 0}
        log(f"boot_sweep skipped (mode={mode}, not armed)")
        return result

    ready = _boot_sweep_wait_ready(
        proxy_base, max_attempts=max_attempts, retry_interval_s=retry_interval_s,
        ready_fn=ready_fn, sleep=sleep, log=log,
    )
    if dry_sweep:
        writer: Any = _DryDeleteWriter(ProxyWriter(base_url=proxy_base), log)
        result = cancel_stale_open_orders(writer, journal, clock)
        result = {**result, "dry_sweep": True, "mode": mode, "proxy_ready": ready}
        log(f"boot_sweep dry_sweep result={result}")
        return result
    writer = ProxyWriter(base_url=proxy_base)
    result = cancel_stale_open_orders(writer, journal, clock)
    result = {**result, "mode": mode, "proxy_ready": ready}
    log(f"boot_sweep result={result}")
    return result


# Mirrors run_v32.VALID_MODES_V32 / resolve_v32_mode. Replicated (not imported) so the long-running
# supervisor need not import run_v32's asyncio/websocket stack just to read the mode at boot.
_VALID_MODES = ("shakedown", "dry", "armed")


def _read_mode_safe() -> str:
    """Read the V3.2 mode file; unknown/absent/error -> ``shakedown`` (fail-closed, never raises)."""
    try:
        with open(mode_path_v32(), "r", encoding="utf-8") as f:
            raw = f.read().strip().lower()
        return raw if raw in _VALID_MODES else "shakedown"
    except OSError:
        return "shakedown"


def _default_rotate(journal_dir: str, *, clock: Callable[[], float]) -> dict[str, Any]:
    """Gzip closed raw journals in ``journal_dir`` (the call V3.2's run_v32 never inherited).

    Uses the same bounded, crash-safe ``rotate_closed_journals`` that run_window calls at its wake:
    excludes ``summary.jsonl``, the keep-list, the current window's (not-yet-existing) journal, and
    anything younger than the 30-min min-age -- so the live window's raw journal is never touched.
    """
    from service.journal_io import (
        DEFAULT_MAX_FILES,
        DEFAULT_MAX_SECONDS,
        rotate_closed_journals,
    )

    now = clock()
    return rotate_closed_journals(
        journal_dir,
        exclude_basenames={_current_window_journal_basename(now)},
        keep_path=journal_keep_path(),
        now=now,
        max_files=DEFAULT_MAX_FILES,
        max_seconds=DEFAULT_MAX_SECONDS,
    )


# ===========================================================================
# Supervisor
# ===========================================================================
class Supervisor:
    """The wake-loop. Every side effect (clock, sleep, spawn, boot sweep, journal rotation, event
    emission) is injectable so the loop is unit-testable without real time, processes or signals."""

    def __init__(
        self,
        *,
        proxy_base: str,
        child_args: list[str] | None = None,
        once: bool = False,
        run_now: bool = False,
        dry_sweep: bool = False,
        grace_s: float = SIGTERM_GRACE_S,
        child_poll_s: float = CHILD_POLL_S,
        watchdog_grace_s: float = WATCHDOG_GRACE_S,
        boot_sweep_max_attempts: int = BOOT_SWEEP_MAX_ATTEMPTS,
        boot_sweep_retry_interval_s: float = BOOT_SWEEP_RETRY_INTERVAL_S,
        clock: Callable[[], float] = time.time,
        spawn: Callable[[list[str]], Any] | None = None,
        sweep: Callable[[str], dict[str, Any]] | None = None,
        rotate: Callable[[str], dict[str, Any]] | None = None,
        sleep: Callable[[float], bool] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        log_path: str | None = None,
        install_signals: bool = True,
    ) -> None:
        self._proxy_base = proxy_base
        self._child_args = list(child_args or [])
        self._once = once
        self._run_now = run_now
        self._dry_sweep = dry_sweep
        self._grace_s = grace_s
        self._child_poll_s = child_poll_s
        self._watchdog_grace_s = watchdog_grace_s
        self._boot_sweep_max_attempts = boot_sweep_max_attempts
        self._boot_sweep_retry_interval_s = boot_sweep_retry_interval_s
        self._clock = clock
        self._spawn = spawn or _default_spawn
        self._sweep = sweep or (
            lambda base: _default_boot_sweep(
                base, dry_sweep=self._dry_sweep, clock=self._clock, log=logger.info,
                max_attempts=self._boot_sweep_max_attempts,
                retry_interval_s=self._boot_sweep_retry_interval_s,
            )
        )
        self._rotate = rotate or (lambda jd: _default_rotate(jd, clock=self._clock))
        self._on_event = on_event
        self._log_path = log_path if log_path is not None else supervisor_log_path()

        self._install_signals = install_signals
        self._stop_event = threading.Event()
        self._stop_requested = False
        self._sig: int | None = None
        self._sleep = sleep or self._event_sleep

    # --- signals ---
    def _install_signal_handlers(self) -> None:
        def handler(signum: int, _frame: Any) -> None:
            self._stop_requested = True
            self._sig = signum
            self._stop_event.set()

        for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
            s = getattr(signal, name, None)
            if s is None:
                continue
            try:
                signal.signal(s, handler)
            except (ValueError, OSError):
                # Not the main thread (e.g. under a test runner) -> tests drive request_stop directly.
                pass

    def request_stop(self, signum: int = signal.SIGTERM) -> None:
        """Test/programmatic hook: request shutdown as if ``signum`` had arrived."""
        self._stop_requested = True
        self._sig = signum
        self._stop_event.set()

    def _event_sleep(self, seconds: float) -> bool:
        """Interruptible sleep. Returns True if a stop was requested during the wait."""
        if seconds <= 0:
            return self._stop_requested
        interrupted = self._stop_event.wait(timeout=seconds)
        return interrupted or self._stop_requested

    def _sleep_until(self, wake: float) -> bool:
        remaining = wake - self._clock()
        if remaining <= 0:
            return self._stop_requested
        return self._sleep(remaining)

    # --- emission ---
    def _emit(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, sort_keys=True)
        logger.info("[SUPERVISOR] %s", line)
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._log_path)), exist_ok=True)
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            logger.warning("[SUPERVISOR] log write failed (ignored): %s", e)
        if self._on_event is not None:
            self._on_event(record)

    # --- child wait ---
    def _reap_after_signal(self, child: Any) -> int | None:
        """Forward the current signal to the child, wait the grace, hard-kill if it overstays, and
        return the child's final exit code (or None if it never reports one)."""
        sig = self._sig if self._sig is not None else signal.SIGTERM
        child.forward_signal(sig)
        rc = child.wait(self._grace_s)
        if rc is None:
            child.kill()
            rc = child.wait(self._child_poll_s)
        return rc

    def _wait_child(self, child: Any, watchdog_deadline: float) -> tuple[int | None, str]:
        """Wait for the child. Returns ``(exit_code, status)`` where status is:
          - ``"exited"``     -- the child returned on its own;
          - ``"signaled"``   -- a shutdown signal arrived; forward + grace + kill, exit the supervisor;
          - ``"watchdog_killed"`` -- the child overran ``watchdog_deadline`` (close + watchdog grace);
            forward + grace + kill, then the supervisor CONTINUES to the next :40.
        A shutdown signal takes priority over the watchdog deadline.
        """
        while True:
            rc = child.wait(self._child_poll_s)
            if rc is not None:
                return rc, "exited"
            if self._stop_requested:
                return self._reap_after_signal(child), "signaled"
            if self._clock() >= watchdog_deadline:
                logger.warning("[SUPERVISOR] child watchdog: window overran close + %.0fs; killing "
                               "child", self._watchdog_grace_s)
                return self._reap_after_signal(child), "watchdog_killed"

    # --- main loop ---
    def run(self) -> int:
        if self._install_signals:
            self._install_signal_handlers()
        self._emit({
            "event": "boot",
            "at": _iso(self._clock()),
            "proxy_base": self._proxy_base,
            "once": self._once,
            "dry_sweep": self._dry_sweep,
        })

        try:
            sweep_result = self._sweep(self._proxy_base)
        except Exception as e:  # noqa: BLE001 - a sweep failure must never sink the supervisor
            logger.warning("[SUPERVISOR] boot sweep failed (ignored): %s", e)
            sweep_result = {"error": repr(e)}
        self._emit({"event": "boot_sweep", "result": sweep_result})

        if self._stop_requested:
            self._emit({"event": "stop_idle", "at": _iso(self._clock())})
            return 0

        first = True
        while True:
            if not (first and self._run_now):
                now = self._clock()
                if not in_launch_band(now):
                    wake = next_forty(now)
                    self._emit({"event": "sleep", "now": _iso(now), "wake": _iso(wake)})
                    if self._sleep_until(wake):
                        self._emit({"event": "stop_idle", "at": _iso(self._clock())})
                        return 0
                    now2 = self._clock()
                    if not in_launch_band(now2):
                        self._emit({"event": "skipped_late", "woke": _iso(now2)})
                        first = False
                        continue
                # else already in the launch band -> run for the current hour (on-time or late)
            first = False

            try:
                rot = self._rotate(journal_dir_v32())
            except Exception as e:  # noqa: BLE001 - rotation must never affect the window run
                logger.warning("[SUPERVISOR] journal rotation failed (ignored): %s", e)
                rot = {"error": repr(e)}

            wake_iso = _iso(self._clock())
            start = self._clock()
            watchdog_deadline = _next_top_of_hour_epoch(start) + self._watchdog_grace_s
            child = self._spawn(self._child_args)
            rc, status = self._wait_child(child, watchdog_deadline)
            duration = self._clock() - start
            self._emit({
                "event": "window",
                "wake": wake_iso,
                "pid": getattr(child, "pid", None),
                "exit_code": rc,
                "duration_s": round(duration, 3),
                "status": status,
                "signaled": status == "signaled",
                "rotated": rot.get("count") if isinstance(rot, dict) else None,
            })
            if status == "watchdog_killed":
                self._emit({"event": "child_watchdog_killed", "wake": wake_iso, "exit_code": rc})

            if status == "signaled":
                return rc if isinstance(rc, int) else 0
            if self._once:
                return 0
            # "exited" or "watchdog_killed" -> continue to the next :40


# ===========================================================================
# CLI
# ===========================================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="V3.3 host-shaped supervisor: wakes one run_v32 window per UTC :40, boot cancel "
                    "sweep, leftover-journal rotation, SIGTERM-aware. Behaviour-neutral for V3.2.",
    )
    p.add_argument("--once", action="store_true",
                   help="Run exactly one window then exit (tests / a manual run).")
    p.add_argument("--now", action="store_true",
                   help="Skip the initial sleep and run the first window immediately.")
    p.add_argument("--dry-sweep", action="store_true",
                   help="Boot sweep lists what it WOULD cancel without cancelling.")
    p.add_argument("--proxy-base", default=None,
                   help="Proxy base URL for the boot sweep (default: $DV3_PROXY_BASE else "
                        "http://127.0.0.1:8642).")
    p.add_argument("--child-args", nargs=argparse.REMAINDER, default=[],
                   help="Everything after this flag is passed verbatim to run_v32 (must be last).")
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = build_parser().parse_args(argv)
    proxy_base = args.proxy_base or default_proxy_base()
    sup = Supervisor(
        proxy_base=proxy_base,
        child_args=list(args.child_args or []),
        once=args.once,
        run_now=args.now,
        dry_sweep=args.dry_sweep,
    )
    return sup.run()


if __name__ == "__main__":
    raise SystemExit(main())
