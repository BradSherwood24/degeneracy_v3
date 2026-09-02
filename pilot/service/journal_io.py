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

    Sequence: stream the raw bytes into ``<path>.gz.tmp`` (gzip), flush + close (writes the gzip
    trailer), atomically rename the temp to ``<path>.gz``, then remove the raw file. A crash at any
    point leaves EITHER the intact raw journal (temp orphaned, cleaned on the exception path) OR the
    finished ``.gz`` — never a half-written ``.gz`` in place of the raw. Returns ``(raw_bytes,
    gz_bytes)``. Raises on any failure (the caller records it per-file and keeps going).
    """
    tmp = path + ".gz.tmp"
    final = path + ".gz"
    raw_bytes = os.path.getsize(path)
    try:
        with open(path, "rb") as src, gzip.open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst, _COPY_CHUNK)
            dst.flush()
        os.replace(tmp, final)
    except BaseException:
        # Leave the raw journal untouched; clean up any partial temp so it can't accumulate.
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise
    gz_bytes = os.path.getsize(final)
    os.remove(path)
    return raw_bytes, gz_bytes


def rotate_closed_journals(
    journal_dir: str,
    *,
    exclude_basenames: set[str] | None = None,
    keep_path: str | None = None,
    min_age_s: float = DEFAULT_MIN_AGE_S,
    now: float | None = None,
) -> dict:
    """Gzip every closed raw journal in ``journal_dir`` and delete the raw file.

    A raw ``*.jsonl`` is rotated iff ALL of: it is not ``summary.jsonl``; its basename is not in
    ``exclude_basenames`` (the caller passes the CURRENT window's journal filename); its basename is
    not in the keep list at ``keep_path``; and its mtime is older than ``min_age_s`` seconds. Each
    file is compressed via :func:`_gzip_one_crash_safe`. A per-file failure (locked file, unreadable,
    etc.) is caught and RECORDED — it never aborts the sweep and never propagates. Returns a summary
    ``{"rotated": [basenames], "errors": [{"file","error"}], "bytes_saved": int, "count": int,
    "kept": [basenames], "scanned": int}``.

    Note: this returns raw ``*.jsonl`` only — ``glob("*.jsonl")`` does not match already-rotated
    ``*.jsonl.gz`` — so re-running the sweep is idempotent (nothing left to compress).
    """
    now = time.time() if now is None else now
    exclude = set(exclude_basenames or ())
    keep = read_keep_list(keep_path) if keep_path else set()

    rotated: list[str] = []
    kept: list[str] = []
    errors: list[dict] = []
    bytes_saved = 0
    scanned = 0

    for path in sorted(glob.glob(os.path.join(journal_dir, "*" + _JSONL))):
        base = os.path.basename(path)
        if base == _SUMMARY:
            continue
        scanned += 1
        if base in exclude or base in keep:
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
        try:
            raw_bytes, gz_bytes = _gzip_one_crash_safe(path)
            rotated.append(base)
            bytes_saved += raw_bytes - gz_bytes
        except BaseException as e:  # noqa: BLE001 - one bad file must not sink the sweep
            errors.append({"file": base, "error": repr(e)})

    return {
        "rotated": rotated,
        "errors": errors,
        "bytes_saved": bytes_saved,
        "count": len(rotated),
        "kept": kept,
        "scanned": scanned,
    }
