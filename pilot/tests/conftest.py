"""Make `import service.*` resolve when pytest runs from anywhere in the repo, and isolate every
test from the operator's LIVE lever/guard files.

Test isolation (spec: tests must NEVER read the live lever files ops/strategy.txt, ops/mode.txt, or
the live day-guard files ops/stops_YYYY-MM-DD.json — none of those existing on the box may change a
test outcome). The autouse ``_isolate_levers`` fixture repoints the module-level lever-default
constants (``run_window.DEFAULT_STRATEGY_TXT`` / ``DEFAULT_MODE_TXT``) at throwaway tmp paths, so a
WindowService constructed WITHOUT explicit lever paths reads ``corridor`` / shakedown from tmp and
derives its stops-guard directory under tmp — never the operator's box lever. Tests that pass their
own paths are unaffected.
"""

import os
import sys

import pytest

_PILOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PILOT_DIR not in sys.path:
    sys.path.insert(0, _PILOT_DIR)

# The two LIVE lever files that no test may read (absolute, normalized for comparison).
LIVE_STRATEGY_TXT = os.path.normcase(os.path.abspath(os.path.join(_PILOT_DIR, "ops", "strategy.txt")))
LIVE_MODE_TXT = os.path.normcase(os.path.abspath(os.path.join(_PILOT_DIR, "ops", "mode.txt")))


@pytest.fixture(autouse=True)
def _isolate_levers(monkeypatch, tmp_path):
    """Point the WindowService lever-default constants at tmp files for EVERY test.

    - strategy default -> a tmp file containing ``corridor`` (the ships-with value), so a service
      built without an explicit ``strategy_txt_path`` runs the corridor core, never the live box.
    - mode default -> a tmp path that is intentionally NOT created (absent -> shakedown), whose
      parent tmp dir becomes the derived stops-guard/ops dir (so ops/stops_*.json stay under tmp).

    Because ``WindowService.__init__`` resolves ``None`` lever paths from these module globals at
    call time, this monkeypatch takes effect for constructions that omit the paths, while tests that
    pass explicit paths keep working unchanged.
    """
    try:
        import service.run_window as rw
    except Exception:
        # Suites that never import run_window (e.g. pure quintile/book tests) don't need isolation.
        return

    lever_dir = tmp_path / "_levers"
    lever_dir.mkdir(exist_ok=True)
    strat = lever_dir / "strategy.txt"
    strat.write_text("corridor\n", encoding="utf-8")
    mode = lever_dir / "mode.txt"  # deliberately absent -> shakedown; parent dir is the ops/guard dir

    monkeypatch.setattr(rw, "DEFAULT_STRATEGY_TXT", str(strat))
    monkeypatch.setattr(rw, "DEFAULT_MODE_TXT", str(mode))


def _is_live_lever(path) -> bool:
    """True iff ``path`` resolves to the live ops/strategy.txt or ops/mode.txt (any spelling)."""
    if isinstance(path, bytes):
        try:
            path = os.fsdecode(path)
        except Exception:
            return False
    if not isinstance(path, (str, os.PathLike)):
        return False  # an int fd or unknown object -> not a lever path
    try:
        norm = os.path.normcase(os.path.abspath(os.fspath(path)))
    except Exception:
        return False
    return norm in (LIVE_STRATEGY_TXT, LIVE_MODE_TXT)


@pytest.fixture(autouse=True)
def _forbid_live_lever_reads(monkeypatch):
    """Tripwire (spec item 2): fail any test that opens the real ops/strategy.txt or ops/mode.txt.

    Wraps ``builtins.open`` for the duration of every test and raises the moment either live lever
    path is opened, so an accidental default-path regression can never pass silently — it turns a
    wrong answer into a loud, attributable failure on the offending test.
    """
    import builtins

    real_open = builtins.open

    def guarded_open(file, *args, **kwargs):
        if _is_live_lever(file):
            raise AssertionError(
                f"test attempted to read the LIVE lever file {os.fspath(file)!r}; tests must use "
                f"tmp lever paths (see conftest _isolate_levers). This is a test-isolation breach."
            )
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
