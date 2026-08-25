"""Local, best-effort cache of per-document scoring data, keyed by file identity."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

# Bump when the cached payload shape changes; older-format entries are then
# treated as absent, not corrupt.
CACHE_FORMAT_VERSION = 1


def cache_root() -> Path:
    """Cache lives outside the store — a shared git repo where a derived file
    would conflict between colleagues. Honors XDG_CACHE_HOME, else ~/.cache/engmem."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "engmem"


def identity_for(path: Path) -> tuple[int, int] | None:
    """(mtime_ns, size) as a staleness key: a changed file gets a different key,
    so there is no manual invalidation. None if the file can't be stat()'d
    (missing, or a synthetic test path) — callers treat that as a cache miss."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _entry_path(path: Path) -> Path:
    # sha256 of the resolved path: fixed-length, filesystem-safe, reuses the
    # stat() the caller already did. The stored key still must match the file's
    # (mtime_ns, size) before a hit is trusted.
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()
    return cache_root() / f"{digest}.json"


def _warn(message: str) -> None:
    # stderr only: the agent reading search output never reads stderr
    # (ENGMEM-SPEC.md §1), so a slow-but-correct search must not look failed.
    print(f"engmem: cache warning: {message}", file=sys.stderr)


def load(path: Path, identity: tuple[int, int] | None):
    """Cached payload for `path` if present and current, else None. Never
    raises — a missing, unreadable, or corrupt entry is warned about and
    treated as a miss, which the caller recomputes from the document."""
    if identity is None:
        return None

    entry_path = _entry_path(path)
    try:
        raw = entry_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        _warn(f"cannot read cache entry for {path.name}, recomputing ({exc})")
        return None

    try:
        record = json.loads(raw)
    except json.JSONDecodeError as exc:
        _warn(f"cache entry for {path.name} is corrupt, recomputing ({exc})")
        return None

    if not isinstance(record, dict):
        _warn(f"cache entry for {path.name} is corrupt, recomputing (not a JSON object)")
        return None

    if record.get("format_version") != CACHE_FORMAT_VERSION:
        return None

    key = record.get("key")
    if key is None or tuple(key) != tuple(identity):
        return None  # file changed, or entry predates this key format

    if "payload" not in record:
        _warn(f"cache entry for {path.name} is corrupt, recomputing (missing payload)")
        return None

    return record["payload"]


def store(path: Path, identity: tuple[int, int] | None, payload) -> None:
    """Best-effort write-through cache. A write failure must not fail a search
    that already has its answer, so this only warns and returns."""
    if identity is None:
        return

    entry_path = _entry_path(path)
    record = {
        "format_version": CACHE_FORMAT_VERSION,
        "path": str(path),
        "key": list(identity),
        "payload": payload,
    }
    try:
        entry_path.parent.mkdir(parents=True, exist_ok=True)
        # write-then-rename so a concurrent reader never sees a half-written file
        tmp_path = entry_path.with_name(f"{entry_path.name}.tmp{os.getpid()}")
        tmp_path.write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")
        tmp_path.replace(entry_path)
    except OSError as exc:
        _warn(f"cannot write cache entry for {path.name}, continuing without it ({exc})")


def prune_orphans(current_paths) -> None:
    """Drop cache entries for documents no longer in the store. One filename
    comparison per entry; no file content read."""
    root = cache_root()
    try:
        entries = list(root.glob("*.json"))
    except OSError:
        return
    if not entries:
        return

    valid = {_entry_path(p) for p in current_paths}
    for entry_path in entries:
        if entry_path not in valid:
            try:
                entry_path.unlink()
            except OSError:
                pass
