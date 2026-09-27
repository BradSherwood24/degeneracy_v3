"""register_daily_replay.ps1 / unregister_daily_replay.ps1 DRY parse -- validates well-formedness
WITHOUT registering anything (NEVER auto-register in tests). Mirrors test_register_supervisor_tasks.py."""

from __future__ import annotations

import os
import shutil
import subprocess


def _ops_dir():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops")


_REGISTER_TOKENS = (
    "Register-ScheduledTask",
    "DegeneracyReplayDaily",
    "daily_replay.py",
    "New-ScheduledTaskTrigger -Daily",
    "10:10 UTC",
    "LogonType",
    "StartWhenAvailable",
    "ExecutionTimeLimit",
    "MultipleInstances IgnoreNew",
)


def _run_dryrun(script, extra=None):
    pwsh = shutil.which("powershell") or shutil.which("pwsh")
    if not pwsh:
        return None
    args = [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, "-DryRun"]
    if extra:
        args += extra
    return subprocess.run(args, capture_output=True, text=True, timeout=60)


def test_register_daily_replay_dryrun_is_well_formed():
    script = os.path.join(_ops_dir(), "register_daily_replay.ps1")
    assert os.path.exists(script)
    out = _run_dryrun(script)
    if out is not None:
        assert out.returncode == 0, out.stderr
        text = out.stdout
        assert "DRY RUN" in text
        for tok in _REGISTER_TOKENS:
            assert tok in text, tok
    else:  # no PowerShell available -> structural text assertion
        with open(script, "r", encoding="utf-8") as f:
            text = f.read()
        assert "DryRun" in text
        for tok in _REGISTER_TOKENS:
            assert tok in text, tok


def test_register_daily_replay_s4u_note():
    """The -LogonType S4U path prints the admin-shell note (parse-only, still a dry run)."""
    script = os.path.join(_ops_dir(), "register_daily_replay.ps1")
    out = _run_dryrun(script, extra=["-LogonType", "S4U"])
    if out is not None:
        assert out.returncode == 0, out.stderr
        assert "LogonType S4U" in out.stdout
        assert "ADMIN" in out.stdout
    else:
        with open(script, "r", encoding="utf-8") as f:
            text = f.read()
        assert "S4U" in text and "ADMIN" in text


def test_unregister_daily_replay_dryrun_is_well_formed():
    script = os.path.join(_ops_dir(), "unregister_daily_replay.ps1")
    assert os.path.exists(script)
    out = _run_dryrun(script)
    if out is not None:
        assert out.returncode == 0, out.stderr
        assert "Unregister-ScheduledTask" in out.stdout
        assert "DegeneracyReplayDaily" in out.stdout
    else:
        with open(script, "r", encoding="utf-8") as f:
            text = f.read()
        assert "Unregister-ScheduledTask" in text
        assert "DegeneracyReplayDaily" in text
