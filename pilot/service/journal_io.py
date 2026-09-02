"""Shared journal I/O helpers: gz-transparent discovery + open, plus the :40-wake rotation.

Decision (Brad, 2026-09-02): disk is nearly full. Two changes, both here so every journal
reader shares one implementation:

  (A) READERS handle ``.jsonl`` and ``.jsonl.gz`` transparently. ``journal_paths`` discovers both
      (deduped by stem, preferring the raw ``.jsonl`` when both exist), excluding ``summary.jsonl``;
      ``open_journal`` returns a text-mode handle regardless of compression.

  (B) The hourly wake COMPRESSES closed journals. ``rotate_closed_journals`` gzips every closed raw
      journal (older than a threshold, not the current window, not summary, not keep-listed) via a
      crash-safe temp-file + rename + delete sequence.

The armed WRITE path is untouched: the current window's journal is still written raw ``.jsonl`` by
``Journal.flush`` (service/journal.py); rotation only ever acts on OLD, already-closed journals.
"""

from __future__ import annotations

import glob
import gzip
import os
import shutil
import time
from typing import IO

_JSONL = ".jsonl"
_JSONL_GZ = ".jsonl.gz"
_SUMMARY = "summary.jsonl"
_SUMMARY_GZ = "summary.jsonl.gz"

# Copy buffer for streaming a large raw journal into gzip without materializing it (~300 MB files).
_COPY_CHUNK = 1 << 20  # 1 MiB

# A closed journal must be at least this old (by mtime) before rotation touches it — a belt-and-
# suspenders guard so a journal still being flushed is never compressed out from under the writer.
DEFAULT_MIN_AGE_S = 30 * 60  # 30 minutes

# gzip level: measured on a 300 MB journal fixture, level 9 = 23.9 s -> 34 MB vs level 6 = 4.8 s ->
# 36 MB (~5x faster for ~5% larger). The wake sits in the pre-execute critical path, so speed wins.
GZIP_COMPRESSLEVEL = 6

# Per-wake rotation is BOUNDED so a large backlog (first deploy after arming, or recovery after an
# outage) can never overrun the window: at most DEFAULT_MAX_FILES are compressed, and no NEW file is
# started once DEFAULT_MAX_SECONDS of wall time has elapsed. Leftover files simply wait for the next
# wake and the backlog drains over several hours. At level 6 (~5 s/file) three files is ~15 s, well
# inside the ~5 min the anchor-resolve sleep absorbs before the armed poll deadline.
DEFAULT_MAX_FILES = 3
DEFAULT_MAX_SECONDS = 60.0


def _best_effort_remove(path: str) -> bool:
    """Delete ``path``, swallowing an OSError (e.g. a transient Windows sharing lock). Returns
    whether the file is gone."""
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def journal_paths(journal_dir: str) -> list[str]:
    """Sorted journal paths in ``journal_dir`` matching ``*.jsonl`` and ``*.jsonl.gz``.

    Deduped by stem (the filename minus its ``.jsonl`` / ``.jsonl.gz`` suffix): if BOTH a raw and a
    gzipped journal exist for the same stem, the raw ``.jsonl`` wins (it is authoritative and cheaper
    to read; the ``.gz`` is a not-yet-cleaned-up rotation artifact). ``summary.jsonl`` /
    ``summary.jsonl.gz`` are always excluded. Sorted by path so fixed-width ``YYYYMMDDThhmmssZ`` stems
    order chronologically, matching the previous ``sorted(glob(*.jsonl))`` behavior for the raw case.
    """
    chosen: dict[str, str] = {}
    # gz first, then raw overwrites for the same stem (prefer raw).
    for path in glob.glob(os.path.join(journal_dir, "*" + _JSONL_GZ)):
        base = os.path.basename(path)
        if base == _SUMMARY_GZ:
            continue
        chosen.setdefault(base[: -len(_JSONL_GZ)], path)
    for path in glob.glob(os.path.join(journal_dir, "*" + _JSONL)):
        base = os.path.basename(path)
        if base == _SUMMARY:
            continue
        # glob("*.jsonl") never matches "*.jsonl.gz" (it ends in .gz), so raw and gz are disjoint sets.
        chosen[base[: -len(_JSONL)]] = path
    return sorted(chosen.values())


def open_journal(path: str) -> IO[str]:
    """Open a journal for TEXT reading, transparently handling gzip.

    ``.gz`` -> ``gzip.open(path, "rt", encoding="utf-8")``; anything else -> ``open(path, "r",
    encoding="utf-8")``. The returned object yields ``str`` lines either way, so the fast keep-kinds
    line scanner and the ``json.loads`` readers work identically over compressed and raw journals.
    """
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, "r", encoding="utf-8")


def read_keep_list(keep_path: str) -> set[str]:
    """Read ``journal_keep.txt`` (one filename per line) into a set. Missing file -> empty set.

    Blank lines are ignored; each surviving line is stripped. A read error is swallowed (fail to an
    empty keep list) so a broken keep file can never crash the wake — worst case a keep-listed
    journal gets rotated, never the reverse.
    """
    if not keep_path or not os.path.exists(keep_path):
        return set()
    out: set[str] = set()
    try:
        with open(keep_path, "r", encoding="utf-8") as f:
            for line in f:
                name = line.strip()
                if name and not name.startswith("#"):  # allow blank lines and # comments
                    out.add(name)
    except OSError:
        return set()
    return out


def _gzip_one_crash_safe(path: str) -> tuple[int, int]:
    """Compress ``path`` -> ``path + ".gz"`` crash-safely, then delete the raw file.

    Sequence: stream the raw bytes into ``<path>.gz.tmp`` (gzip, level 6), flush + close (writes the
    gzip trailer), atomically rename the temp to ``<path>.gz``, then best-effort remove the raw file.
    A crash before the rename leaves the intact raw journal (temp orphaned, cleaned on the exception
    path); a crash after it leaves the finished ``.gz`` — never a half-written ``.gz`` in place of the
    raw. If the final raw-remove fails (a Windows sharing lock held AFTER the ``.gz`` is safely in
    place), raw+gz is left — an acceptable, consistent state: ``journal_paths`` prefers the raw for
    reading, and the next wake sees the ``.gz`` already exists, skips re-gzipping, and clears the raw
    once the lock lifts. Returns ``(raw_bytes, gz_bytes)``. Raises only if the compression itself
    fails (the caller records it per-file and keeps going).
    """
    tmp = path + ".gz.tmp"
    final = path + ".gz"
    raw_bytes = os.path.getsize(path)
    try:
        with open(path, "rb") as src, gzip.open(tmp, "wb", compresslevel=GZIP_COMPRESSLEVEL) as dst:
            shutil.copyfileobj(src, dst, _COPY_CHUNK)
            dst.flush()
        os.replace(tmp, final)
    except BaseException:
        # Leave the raw journal untouched; clean up any partial temp so it can't accumulate.
        # (BaseException so a Ctrl+C mid-compress still cleans the temp before propagating.)
        _best_effort_remove(tmp)
        raise
    gz_bytes = os.path.getsize(final)
    _best_effort_remove(path)  # a lock here leaves raw+gz (consistent; next wake finishes the job)
    return raw_bytes, gz_bytes


def rotate_closed_journals(
    journal_dir: str,
    *,
    exclude_basenames: set[str] | None = None,
    keep_path: str | None = None,
    min_age_s: float = DEFAULT_MIN_AGE_S,
    now: float | None = None,
    max_files: int = DEFAULT_MAX_FILES,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    monotonic=time.monotonic,
) -> dict:
    """Gzip closed raw journals in ``journal_dir`` (bounded per call) and delete each raw file.

    A raw ``*.jsonl`` is ELIGIBLE iff ALL of: it is not ``summary.jsonl``; its basename is not in
    ``exclude_basenames`` (the caller passes the CURRENT window's journal filename); its basename is
    not in the keep list at ``keep_path``; and its mtime is older than ``min_age_s`` seconds.

    Work is BOUNDED so a large backlog can never overrun the window: at most ``max_files`` eligible
    files are compressed, and no NEW file is STARTED once ``monotonic()`` shows more than
    ``max_seconds`` elapsed. Any further eligible files are DEFERRED to the next wake (counted, not
    processed). Each compressed file goes through :func:`_gzip_one_crash_safe`. A per-file
    compression failure (unreadable, locked) is caught and RECORDED — it never aborts the sweep and
    never propagates (``KeyboardInterrupt`` / ``SystemExit`` DO propagate).

    Idempotency / leftover-raw: ``glob("*.jsonl")`` never matches ``*.jsonl.gz``, so a fully rotated
    file is never revisited. If a prior wake produced the ``.gz`` but could not delete the raw (a
    transient lock), the raw is seen again here; because its ``.gz`` already exists we do NOT waste
    time re-compressing — we just retry the best-effort raw delete and move on.

    Returns ``{"rotated": [basenames], "errors": [{"file","error"}], "bytes_saved": int,
    "count": int, "kept": [basenames], "deferred": int, "scanned": int, "stopped_early": bool}``.
    """
    now = time.time() if now is None else now
    exclude = set(exclude_basenames or ())
    keep = read_keep_list(keep_path) if keep_path else set()
    started = monotonic()

    rotated: list[str] = []
    kept: list[str] = []
    errors: list[dict] = []
    bytes_saved = 0
    scanned = 0
    deferred = 0

    for path in sorted(glob.glob(os.path.join(journal_dir, "*" + _JSONL))):
        base = os.path.basename(path)
        if base == _SUMMARY:
            continue
        scanned += 1
        if base in exclude or base in keep:
            kept.append(base)
            continue
        if os.path.exists(path + ".gz"):
            # A prior wake compressed this but could not delete the raw (leftover lock). The .gz is
            # authoritative-enough; do NOT re-gzip — just retry the raw delete (best effort).
            _best_effort_remove(path)
            kept.append(base)
            continue
        try:
            age = now - os.path.getmtime(path)
        except OSError as e:
            errors.append({"file": base, "error": repr(e)})
            continue
        if age < min_age_s:
            kept.append(base)
            continue
        # BOUND: once a cap is hit, stop STARTING new files — defer the rest to the next wake.
        if len(rotated) >= max_files or (monotonic() - started) >= max_seconds:
            deferred += 1
            continue
        try:
            raw_bytes, gz_bytes = _gzip_one_crash_safe(path)
            rotated.append(base)
            bytes_saved += raw_bytes - gz_bytes
        except Exception as e:  # noqa: BLE001 - one bad file must not sink the sweep (Ctrl+C propagates)
            errors.append({"file": base, "error": repr(e)})

    return {
        "rotated": rotated,
        "errors": errors,
        "bytes_saved": bytes_saved,
        "count": len(rotated),
        "kept": kept,
        "deferred": deferred,
        "scanned": scanned,
        "stopped_early": deferred > 0,
    }
