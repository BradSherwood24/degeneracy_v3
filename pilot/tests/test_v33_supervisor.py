"""V3.3 supervisor --roster hook (L2): the minimal, tested wiring so the Phase-H supervisor can drive
the V3.3 dry ladder (run_v33 + v33-* boot sweep + v33_mode.txt) alongside V3.2, with the DEFAULT
(v32) behaviour byte-identical."""

from __future__ import annotations

import os

import service.supervisor as S
from service.supervisor import Supervisor, _default_boot_sweep, _default_spawn, _read_mode_safe


class FakeChild:
    def __init__(self, codes=(0,), pid=4321):
        self._codes = list(codes)
        self.pid = pid

    def wait(self, timeout):
        return self._codes.pop(0) if self._codes else 0

    def forward_signal(self, signum):
        pass

    def kill(self):
        pass


def _fakes(**over):
    base = dict(proxy_base="http://fake:8642",
                sweep=lambda base_url: {"skipped": True, "found": 0},
                rotate=lambda jd: {"count": 0, "dir": jd},
                install_signals=False, log_path=os.devnull)
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# roster propagation
# ---------------------------------------------------------------------------
def test_roster_mapping():
    assert S._ROSTER_MODULE["v32"] == "service.run_v32"
    assert S._ROSTER_MODULE["v33"] == "service.run_v33"


def test_v33_roster_in_boot_and_window_events():
    events = []
    sup = Supervisor(**_fakes(once=True, run_now=True, roster="v33",
                              spawn=lambda a: FakeChild([0]), on_event=events.append))
    assert sup.run() == 0
    boot = next(e for e in events if e["event"] == "boot")
    window = next(e for e in events if e["event"] == "window")
    assert boot["roster"] == "v33" and window["roster"] == "v33"


def test_default_roster_is_v32():
    events = []
    sup = Supervisor(**_fakes(once=True, run_now=True,
                              spawn=lambda a: FakeChild([0]), on_event=events.append))
    assert sup.run() == 0
    boot = next(e for e in events if e["event"] == "boot")
    assert boot["roster"] == "v32"


def test_unknown_roster_falls_back_to_v32():
    sup = Supervisor(**_fakes(once=True, run_now=True, roster="nonsense",
                              spawn=lambda a: FakeChild([0])))
    assert sup._roster == "v32"


# ---------------------------------------------------------------------------
# default spawn selects the roster's module
# ---------------------------------------------------------------------------
def test_default_spawn_launches_run_v33_module(monkeypatch):
    captured = {}

    class _P:
        pid = 1

    def _popen(cmd, **kw):
        captured["cmd"] = cmd
        return _P()

    monkeypatch.setattr(S.subprocess, "Popen", _popen)
    _default_spawn(["--mode", "dry"], "service.run_v33")
    assert captured["cmd"][2] == "service.run_v33" and captured["cmd"][1] == "-m"


def test_default_spawn_default_module_is_run_v32(monkeypatch):
    captured = {}

    class _P:
        pid = 1

    monkeypatch.setattr(S.subprocess, "Popen", lambda cmd, **kw: captured.setdefault("cmd", cmd) or _P())
    _default_spawn(["--mode", "dry"])
    assert captured["cmd"][2] == "service.run_v32"


# ---------------------------------------------------------------------------
# v33 boot sweep reads the v33 mode file and skips when not armed
# ---------------------------------------------------------------------------
def test_read_mode_safe_v33_missing_is_shakedown(monkeypatch, tmp_path):
    import service.paths as P
    monkeypatch.setattr(P, "mode_path_v33", lambda: str(tmp_path / "nope.txt"))
    assert _read_mode_safe("v33") == "shakedown"


def test_read_mode_safe_v33_reads_the_v33_file(monkeypatch, tmp_path):
    import service.paths as P
    mp = tmp_path / "v33_mode.txt"
    mp.write_text("dry\n", encoding="utf-8")
    monkeypatch.setattr(P, "mode_path_v33", lambda: str(mp))
    assert _read_mode_safe("v33") == "dry"


def test_v33_boot_sweep_skips_in_dry(monkeypatch, tmp_path):
    import service.paths as P
    mp = tmp_path / "v33_mode.txt"
    mp.write_text("dry\n", encoding="utf-8")
    monkeypatch.setattr(P, "mode_path_v33", lambda: str(mp))
    res = _default_boot_sweep("http://fake:8642", dry_sweep=False, clock=lambda: 0.0,
                              log=lambda _m: None, roster="v33")
    assert res.get("skipped") is True and res.get("roster") == "v33"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def test_cli_parses_roster_v33():
    args = S.build_parser().parse_args(["--roster", "v33", "--once"])
    assert args.roster == "v33"


def test_v33_supervisor_default_log_path_is_logs_v33():
    # no log_path override -> the roster picks logs_v33/supervisor.out.
    sup = Supervisor(proxy_base="http://fake:8642", roster="v33", install_signals=False,
                     sweep=lambda b: {}, rotate=lambda jd: {})
    assert "logs_v33" in sup._log_path
    sup32 = Supervisor(proxy_base="http://fake:8642", roster="v32", install_signals=False,
                       sweep=lambda b: {}, rotate=lambda jd: {})
    assert "logs_v32" in sup32._log_path
