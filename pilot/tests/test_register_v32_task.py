"""register_v32_task.ps1 / unregister_v32_task.ps1 DRY parse — validates well-formedness WITHOUT
registering anything (NEVER auto-register in tests). Mirrors tests/test_run_window.py's register
test for the box task."""

from __future__ import annotations

import os
import shutil
import subprocess


def _ops_dir():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops")


def test_register_v32_script_dryrun_is_well_formed():
    script = os.path.join(_ops_dir(), "register_v32_task.ps1")
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
        assert "Register-ScheduledTask" in text
        assert "run_v32" in text
        assert "New-ScheduledTaskTrigger" in text
        assert "DegeneracyV3_2" in text
        assert "MultipleInstances IgnoreNew" in text
    else:  # no PowerShell available -> structural text assertion
        with open(script, "r", encoding="utf-8") as f:
            text = f.read()
        assert "Register-ScheduledTask" in text
        assert "run_v32" in text
        assert "New-ScheduledTaskTrigger" in text
        assert "DryRun" in text
        assert "DegeneracyV3_2" in text
        assert "MultipleInstances IgnoreNew" in text


def test_unregister_v32_script_dryrun_is_well_formed():
    script = os.path.join(_ops_dir(), "unregister_v32_task.ps1")
    assert os.path.exists(script)
    pwsh = shutil.which("powershell") or shutil.which("pwsh")
    if pwsh:
        out = subprocess.run(
            [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, "-DryRun"],
            capture_output=True, text=True, timeout=60,
        )
        assert out.returncode == 0, out.stderr
        assert "Unregister-ScheduledTask" in out.stdout
        assert "DegeneracyV3_2" in out.stdout
    else:
        with open(script, "r", encoding="utf-8") as f:
            assert "Unregister-ScheduledTask" in f.read()
