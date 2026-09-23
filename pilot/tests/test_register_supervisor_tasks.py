"""register_supervisor_tasks.ps1 / unregister_supervisor_tasks.ps1 DRY parse -- validates
well-formedness WITHOUT registering anything (NEVER auto-register in tests). Mirrors
test_register_v32_task.py."""

from __future__ import annotations

import os
import shutil
import subprocess


def _ops_dir():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops")


_REGISTER_TOKENS = (
    "Register-ScheduledTask",
    "DegeneracyProxy",
    "DegeneracyV3_Supervisor",
    "service.supervisor",
    "New-ScheduledTaskTrigger -AtStartup",
    "LogonType S4U",
    "RestartCount 3",
    "AllowStartIfOnBatteries",
    "DontStopIfGoingOnBatteries",
    "ExecutionTimeLimit",
    "MultipleInstances IgnoreNew",
    "DegeneracyV3_2",  # the never-two-drivers warning
)


def test_register_supervisor_script_dryrun_is_well_formed():
    script = os.path.join(_ops_dir(), "register_supervisor_tasks.ps1")
    assert os.path.exists(script)
    pwsh = shutil.which("powershell") or shutil.which("pwsh")
    if pwsh:
        out = subprocess.run(
            [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, "-DryRun"],
            capture_output=True, text=True, timeout=60,
        )
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


def test_unregister_supervisor_script_dryrun_is_well_formed():
    script = os.path.join(_ops_dir(), "unregister_supervisor_tasks.ps1")
    assert os.path.exists(script)
    pwsh = shutil.which("powershell") or shutil.which("pwsh")
    if pwsh:
        out = subprocess.run(
            [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, "-DryRun"],
            capture_output=True, text=True, timeout=60,
        )
        assert out.returncode == 0, out.stderr
        assert "Unregister-ScheduledTask" in out.stdout
        assert "DegeneracyProxy" in out.stdout
        assert "DegeneracyV3_Supervisor" in out.stdout
    else:
        with open(script, "r", encoding="utf-8") as f:
            text = f.read()
        assert "Unregister-ScheduledTask" in text
        assert "DegeneracyProxy" in text
        assert "DegeneracyV3_Supervisor" in text
