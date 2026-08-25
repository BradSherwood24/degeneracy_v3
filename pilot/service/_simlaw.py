"""Bridge to the FROZEN Rung-1 / Rung-1.5 sim law (sim/census.py, sim/gate_fit.py,
sim/tape_sim.py, sim/loader.py).

House law (PLAN "Recycling inventory"): the pilot IMPORTS the frozen law — fee, hole_G,
sigma-hat (A3.2), the quintile edges, and the census EV curve — and NEVER reimplements
them. This module is the single import choke-point: it puts the repo's ``sim`` directory on
``sys.path`` (the sim modules use bare ``import loader`` / ``import census`` etc.) and
re-exports exactly the primitives Phase 2 consumes, so every other pilot module imports the
law from ONE place and a drift in the sim's location breaks in exactly one file.

Nothing here reads any historical-data file, and nothing sealed is touched: building the EV
curve reads only ``sim/out/census_train.csv`` (a TRAIN artifact) and verifies its sha against
the frozen ``tape_sim.CENSUS_TRAIN_SHA256`` before use.
"""

from __future__ import annotations

import os
import sys

# Repo root = .../degeneracy_v3 ; this file is .../degeneracy_v3/pilot/service/_simlaw.py
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SIM_DIR = os.path.join(_REPO_ROOT, "sim")

if _SIM_DIR not in sys.path:
    # Appended, not prepended: the pilot's own packages keep priority; the sim dir only needs
    # to resolve the bare `import loader/census/gate_fit/tape_sim` the sim modules perform.
    sys.path.append(_SIM_DIR)

# The frozen law. Imported by name so a rename in sim/ fails loudly here, not silently elsewhere.
import census as _census            # noqa: E402
import gate_fit as _gate_fit        # noqa: E402
import tape_sim as _tape_sim        # noqa: E402
import loader as _loader            # noqa: E402

# --- fee / hole / sigma-hat (census, A3.2) ---
fee = _census.fee
hole_G = _census.hole_G
sigma_hat = _census.sigma_hat
InsufficientTape = _census.InsufficientTape
IntegrityError = _census.IntegrityError
SIGMA_ANCHORS = _census.SIGMA_ANCHORS
ANCHOR_STEP = _census.ANCHOR_STEP

# --- quintile edges / bucket assignment (gate_fit) ---
quintile_edges = _gate_fit.quintile_edges
bucket_of = _gate_fit.bucket_of
read_ok_rows = _gate_fit.read_ok_rows

# --- EV curve + constants (tape_sim) ---
EVCurve = _tape_sim.EVCurve
WINDOW_S = _tape_sim.WINDOW_S
STALENESS_S_DEFAULT = _tape_sim.STALENESS_S_DEFAULT
CENSUS_TRAIN_SHA256 = _tape_sim.CENSUS_TRAIN_SHA256
TAPE_FIELDNAMES = _tape_sim.TAPE_FIELDNAMES

# --- loader helpers (epoch parsing only; never a sealed read from the pilot) ---
close_epoch = _loader.close_epoch

# Canonical location of the frozen TRAIN census artifact used for the EV curve + edges.
DEFAULT_CENSUS_CSV = os.path.join(_SIM_DIR, "out", "census_train.csv")


def load_ev_curve(census_csv: str = DEFAULT_CENSUS_CSV) -> "EVCurve":
    """Build the sha-verified census EV curve (refuses on a census sha mismatch — the sim law).

    Returns a ``tape_sim.EVCurve`` exposing ``.edges`` (the 4 G/sigma quintile boundaries),
    ``.pin_rate`` (per-quintile train pin rate), ``.fair`` / ``.fair_for(direction, q)``, and
    ``.assign(gos)`` — all reproduced from the frozen artifact exactly as the tape sim does.
    """
    return EVCurve.from_census(census_csv)
