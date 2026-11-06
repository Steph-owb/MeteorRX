"""Output naming: persistent session sequence number and files that can never be overwritten."""
from __future__ import annotations

import json
import os
import re
import shutil
import time


def next_sequence(data_dir: str) -> int:
    """Monotonic session number kept in data\\sequence.json (survives restarts)."""
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "sequence.json")
    lock = path + ".lock"
    t0 = time.time()
    fd = None
    while fd is None:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.time() - t0 > 5:          # stale lock (crash): take it over
                try:
                    os.remove(lock)
                except OSError:
                    pass
            time.sleep(0.05)
    try:
        try:
            with open(path, "r", encoding="utf-8") as f:
                n = int(json.load(f).get("next", 1))
        except (OSError, ValueError, TypeError):
            n = 1
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"next": n + 1}, f)
        os.replace(tmp, path)
        return n
    finally:
        os.close(fd)
        try:
            os.remove(lock)
        except OSError:
            pass


def unique_path(path: str) -> str:
    """path if free, else path_2, path_3 ... (before the extension for files)."""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    if os.path.isdir(path):
        root, ext = path, ""
    k = 2
    while os.path.exists(f"{root}_{k}{ext}"):
        k += 1
    return f"{root}_{k}{ext}"


def new_session_dir(output_dir: str, data_dir: str, stamp: str, tag: str) -> tuple[str, str, int]:
    """Creates and returns (folder, file prefix, sequence) for a reception session, e.g.
    Documents\\MeteorRX\\0007_20261027_0912_METEOR-M2-4\\0007_20261027_0912_METEOR-M2-4.cadu"""
    seq = next_sequence(data_dir)
    prefix = f"{seq:04d}_{stamp}_{tag}"
    folder = unique_path(os.path.join(output_dir, prefix))
    os.makedirs(folder)
    return folder, os.path.basename(folder), seq


def open_new(path: str, mode: str = "xb"):
    """Open for writing, never truncating an existing file (falls back to a suffixed name)."""
    try:
        return open(path, mode)
    except FileExistsError:
        return open(unique_path(path), mode)


SESSION_DIR = re.compile(r"^\d{4}_\d{8}_\d{4}_")


def _session_time(name: str) -> float | None:
    """Date of a session folder taken from its own name (NNNN_YYYYMMDD_HHMM_TAG), as a UNIX time."""
    m = re.match(r"^\d{4}_(\d{8})_(\d{4})_", name)
    if not m:
        return None
    try:
        return time.mktime(time.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M"))
    except ValueError:
        return None


def purge_old_sessions(output_dir: str, days: int) -> list[str]:
    """Delete whole reception folders older than `days` days ([recording] purge_days, 0 = never).

    The age comes from the folder's own name, so copying or re-processing one does not rejuvenate it; a
    folder whose name carries no usable date is left alone. This removes decoded imagery, so it only ever
    runs when the user has set a non-zero number of days. Returns what was removed."""
    removed: list[str] = []
    if days <= 0:
        return removed
    cutoff = time.time() - days * 86400.0
    try:
        names = sorted(os.listdir(output_dir))
    except OSError:
        return removed
    for n in names:
        p = os.path.join(output_dir, n)
        if not SESSION_DIR.match(n) or not os.path.isdir(p):
            continue
        t = _session_time(n)
        if t is None or t >= cutoff:
            continue
        try:
            shutil.rmtree(p)
            removed.append(p)
        except OSError:
            continue
    return removed


def clean_empty_sessions(output_dir: str) -> list[str]:
    """Remove reception folders a pass left with nothing in them (no file at all, or only empty files),
    e.g. a pass armed on a signal that never decoded. Deliberately conservative: only folders named like a
    session, never one holding a subfolder or a single byte of data (a .cadu or a baseband recording with
    content is kept - it is exactly what a failed pass must leave for diagnosis). Returns what was removed."""
    removed: list[str] = []
    try:
        names = sorted(os.listdir(output_dir))
    except OSError:
        return removed
    for n in names:
        p = os.path.join(output_dir, n)
        if not SESSION_DIR.match(n) or not os.path.isdir(p):
            continue
        try:
            entries = list(os.scandir(p))
            if any(e.is_dir() for e in entries) or any(e.stat().st_size > 0 for e in entries):
                continue
            for e in entries:
                os.remove(e.path)
            os.rmdir(p)
            removed.append(p)
        except OSError:
            continue
    return removed
