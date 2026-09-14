"""sim.v32_replay — the V3.2 REPLAY LAB.

Re-runs the V3.2 strategy's pricing rules and the forward sim's pricing assumptions over the
millisecond journals ``run_v32`` records every hour (``pilot/journals_v32/*.jsonl.gz``), and reports
optimistic / base / pessimistic performance estimates that firm up as journals accumulate.

House law honored here:
  * READ-ONLY over the journals; never opens the ``.jsonl`` a live window is still writing (only
    ``*.jsonl.gz``). Never reads ``.env`` / ``*.pem`` / ``sim/out/sealed_eval`` / ``pilot/journals``
    (the v1.1 journals) / ``pilot/ledger``.
  * REFUSES any journal whose close date falls in the sealed / holdout ranges (2026-08-02..18 SEAL,
    2026-08-20..29 range holdout) unless an explicit one-shot acknowledge flag is set — and this tool
    NEVER sets that flag. See ``frames.assert_not_sealed``.
  * Money math is the pinned law: ``service.v32.core.solve_n`` / ``wing_cost`` / ``lock_value`` and
    ``service._simlaw.fee`` are imported, never retyped.

Path bootstrap: the pilot ``service`` package lives under ``<repo>/pilot``; put it on sys.path so a
plain ``python -m sim.v32_replay.lab`` (run from the repo root) resolves ``service.*`` exactly as the
existing sim tests do (``sim/tests/test_replay_book.py``).
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))          # <repo>/sim/v32_replay -> <repo>
_PILOT = os.path.join(_REPO_ROOT, "pilot")
if os.path.isdir(_PILOT) and _PILOT not in sys.path:
    # Appended so the repo's own top-level packages keep priority; this only needs to resolve the
    # pilot ``service`` package for the imported money law + BookMirror.
    sys.path.append(_PILOT)

REPO_ROOT = _REPO_ROOT
PILOT_DIR = _PILOT

__all__ = ["REPO_ROOT", "PILOT_DIR"]
