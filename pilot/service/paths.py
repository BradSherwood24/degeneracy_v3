"""paths.py -- writable-path + proxy-base resolution for the host-shaped runtime.

Phase H (V3.3). ``DV3_DATA_DIR`` (a Render persistent disk, or a local data dir), when set,
relocates every WRITABLE runtime path -- journals, logs, ledger, and the writable ops files
(the mode file and the day-guard/stops JSON) -- under ``$DV3_DATA_DIR/<same relative name>``.
When ``DV3_DATA_DIR`` is UNSET (or blank), every path returned here is byte-identical to the
historic ``_PILOT_DIR``-relative default, so the live V3.2 is behaviour-neutral.

Read-only INPUTS never move: ``policy/v32_params.json``, ``ceremony/v32_falsifier.md`` and the
rotation keep-list ``ops/journal_keep.txt`` ship WITH the code and are never written at runtime,
so they always resolve in the code checkout regardless of ``DV3_DATA_DIR``.

Mode-file convention (documented in ``ops/V33_RUNBOOK.md``): when ``DV3_DATA_DIR`` is set the mode
file ``v32_mode.txt`` lives in the DATA DIR (Brad copies it once at cutover), full stop -- there is
NO fall-back read of the checkout copy. This keeps the single source of truth on the disk that the
supervisor (and, later, the Render worker) owns.

``DV3_PROXY_BASE`` supplies the default proxy base URL when no ``--proxy-base`` flag is given; unset
-> ``http://127.0.0.1:8642`` exactly as today. House law is unchanged: all Kalshi access still goes
through the proxy; this module only chooses which proxy URL / which writable directory, never a key.
"""

from __future__ import annotations

import os

# The pilot/ directory of THIS checkout: .../pilot/service/paths.py -> .../pilot
_PILOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DATA_DIR_ENV = "DV3_DATA_DIR"
PROXY_BASE_ENV = "DV3_PROXY_BASE"

# The historic default proxy base -- unchanged from proxy_auth.DEFAULT_PROXY_BASE. Kept as a literal
# here so the env default can be resolved without importing the proxy module (avoids any import cycle).
DEFAULT_PROXY_BASE = "http://127.0.0.1:8642"


def pilot_dir() -> str:
    """The code checkout's ``pilot/`` directory (never moves)."""
    return _PILOT_DIR


def data_dir() -> str | None:
    """The configured data dir (``$DV3_DATA_DIR``), or ``None`` when unset/blank.

    A blank or whitespace-only value is treated as unset so an empty env var can never silently
    redirect writes to the process CWD.
    """
    raw = os.environ.get(DATA_DIR_ENV)
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


def _writable_base() -> str:
    """Root under which WRITABLE dirs resolve: the data dir if set, else the pilot checkout."""
    return data_dir() or _PILOT_DIR


# --- writable locations (relocate under DV3_DATA_DIR when set) ---


def journal_dir_v32() -> str:
    return os.path.join(_writable_base(), "journals_v32")


def log_dir_v32() -> str:
    return os.path.join(_writable_base(), "logs_v32")


def ledger_dir_v32() -> str:
    return os.path.join(_writable_base(), "ledger")


def ledger_path_v32() -> str:
    return os.path.join(ledger_dir_v32(), "v32_ledger.jsonl")


def ops_dir_v32() -> str:
    """Writable ops dir: the day-guard/stops JSON, and the mode file when DV3_DATA_DIR is set."""
    return os.path.join(_writable_base(), "ops")


def mode_path_v32() -> str:
    return os.path.join(ops_dir_v32(), "v32_mode.txt")


def supervisor_log_path() -> str:
    """The supervisor's own structured (JSON-per-window) log -- lives with the other logs."""
    return os.path.join(log_dir_v32(), "supervisor.out")


# --- read-only inputs (always in the code checkout) ---


def checkout_ops_dir() -> str:
    """The code-checkout ops dir. Read-only INPUTS (journal_keep.txt) live here regardless of env."""
    return os.path.join(_PILOT_DIR, "ops")


def journal_keep_path() -> str:
    """The rotation keep-list -- a read-only input, always in the checkout."""
    return os.path.join(checkout_ops_dir(), "journal_keep.txt")


def falsifier_path_v32() -> str:
    """The V3.2 falsifier -- a read-only input (S5 reads it), always in the checkout."""
    return os.path.join(_PILOT_DIR, "ceremony", "v32_falsifier.md")


# --- proxy base ---


def default_proxy_base() -> str:
    """The proxy base URL to use when no ``--proxy-base`` flag is given.

    ``$DV3_PROXY_BASE`` when set/non-blank, else ``http://127.0.0.1:8642`` (today's default).
    """
    raw = os.environ.get(PROXY_BASE_ENV)
    if raw is None:
        return DEFAULT_PROXY_BASE
    raw = raw.strip()
    return raw or DEFAULT_PROXY_BASE
