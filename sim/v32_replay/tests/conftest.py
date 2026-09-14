"""Put the repo root on sys.path so ``import sim.v32_replay...`` resolves when pytest is invoked from
anywhere (the package __init__ then adds ``pilot`` for ``service.*``)."""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))   # tests -> v32_replay -> sim -> repo
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

FIXTURE_DIR = os.path.join(_REPO_ROOT, "pilot", "tests", "fixtures", "v32")
HEAD_FIXTURE = os.path.join(FIXTURE_DIR, "live_window_20260914T170000Z_head.jsonl.gz")
