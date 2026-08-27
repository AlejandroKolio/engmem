"""Local, best-effort cache of per-document scoring data, keyed by file identity.

Rules and their reasons: contracts/cache.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from uuid import uuid4
from collections.abc import Iterable
from pathlib import Path

# Bump when the cached payload shape changes; older-format entries are then
# treated as absent, not corrupt.
CACHE_FORMAT_VERSION = 2  # 2: split subsections inherit their parent's canonical role


def cache_root() -> Path:
    """Cache lives outside the store, honoring XDG_CACHE_HOME, else `~/.cache/engmem`."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "engmem"


def identity_for(path: Path) -> tuple[int, int] | None:
    """`(mtime_ns, size)` as a staleness key, or None when the file cannot be stat()'d."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _entry_path(path: Path) -> tuple[Path, Path]:
    """`(entry_path, resolved_document_path)` — resolved once, so a caller recording the path
    cannot name a different document than the filename addresses."""
    # sha256 of the resolved path: fixed-length, filesystem-safe, and one entry for a symlink
    # and its target. A hard link or a case-variant name still gets its own — see
    # contracts/cache.md.
    resolved = path.resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()
    return cache_root() / f"{digest}.json", resolved


def _warn(message: str) -> None:
    # stderr only: the agent reading search output never reads stderr
    # (ENGMEM-SPEC.md §4, §5), so a slow-but-correct search must not look failed.
    print(f"engmem: cache warning: {message}", file=sys.stderr)


def load(path: Path, identity: tuple[int, int] | None) -> object | None:
    """Cached payload for `path` if present and current, else None; never raises."""
    if identity is None:
        return None

    entry_path, _ = _entry_path(path)
    try:
        raw = entry_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        _warn(f"cannot read cache entry for {path.name}, recomputing ({exc})")
        return None
    except ValueError as exc:
        # bytes that are not UTF-8: a `UnicodeDecodeError` is a ValueError, not an OSError,
        # so it walked past the handler above and out of a function that never raises
        _warn(f"cache entry for {path.name} is not text, recomputing ({exc})")
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

    # `store` writes `list(identity)`; anything else is damage. A scalar is the shape that
    # makes `tuple()` raise, which `load` must never do — a `str` or `dict` would merely
    # mis-compare. Why this branch stays silent: contracts/cache.md, "Degrading, and how loudly"
    key = record.get("key")
    if not isinstance(key, list) or tuple(key) != tuple(identity):
        return None

    # `is None` and not `not in`: no legitimate write produces a null payload, so unlike the
    # `key` row above there is no live case sharing this branch to keep it quiet for
    if record.get("payload") is None:
        _warn(f"cache entry for {path.name} is corrupt, recomputing (missing or null payload)")
        return None

    return record["payload"]


def store(path: Path, identity: tuple[int, int] | None, payload: object) -> None:
    """Best-effort write-through: a write failure warns rather than failing the search."""
    if identity is None:
        return

    entry_path, resolved = _entry_path(path)
    record = {
        "format_version": CACHE_FORMAT_VERSION,
        # never read back; it is the only way from a directory of opaque digests to the
        # document an entry belongs to. The resolved path, because that is what was hashed
        "path": str(resolved),
        "key": list(identity),
        "payload": payload,
    }
    # write-then-rename so a concurrent reader never sees a half-written file. A uuid, not
    # `os.getpid()`: the pid separates processes but not threads, and two threads sharing the
    # name would interleave into one entry. Not hidden like `staging.py`'s, which needs that
    # to stay out of a document scan; the temp file is visible to `prune_orphans`, by design.
    tmp_path = entry_path.with_name(f"{entry_path.name}.{uuid4().hex}.tmp")
    try:
        entry_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")
        tmp_path.replace(entry_path)
    except OSError as exc:
        _discard(tmp_path)
        _warn(f"cannot write cache entry for {path.name}, continuing without it ({exc})")
    except BaseException:
        # an asynchronous interruption landing between `write_text` creating the file and
        # `replace` completing — `KeyboardInterrupt` (Ctrl-C) lands here, and this arm discards
        # the file promptly. A signal that kills the process outright — `SIGTERM`, `SIGKILL`,
        # power loss — raises nothing here to catch; `prune_orphans` is the backstop for that.
        _discard(tmp_path)
        raise


def _discard(tmp_path: Path) -> None:
    try:
        tmp_path.unlink()
    except OSError:
        pass  # never written, or the same fault that blocked the write


def prune_orphans(current_paths: Iterable[Path]) -> None:
    """Drops entries for documents no longer in the store, and any leaked write-temp files."""
    root = cache_root()
    try:
        entries = list(root.glob("*.json"))
        temp_entries = list(root.glob("*.json.*.tmp"))
        # the pre-uuid pid-named form, `{digest}.json.tmp{pid}` — this code no longer writes
        # it, so a match is usually pre-upgrade residue. Not categorically, though:
        # `cache_root()` has no version and no store component, and during a mixed-version
        # transition (a pinned older install, another venv) a concurrently running `store` still
        # on the pid-named scheme can own one — the same benign race the loop below accepts for
        # `temp_entries`. The two naming schemes never share a filename: a legacy name ends in
        # the pid's decimal digits, not `.tmp`, so it never matches `*.json.*.tmp`; a uuid name
        # never matches `*.json.tmp*` either, because its hex segment — the uuid after `.json.`,
        # not the sha256 digest before it — can never start with "tmp" (hex digits are
        # `0-9a-f`). See contracts/cache.md, "Writing".
        legacy_temp_entries = list(root.glob("*.json.tmp*"))
    except OSError:
        return
    if not entries and not temp_entries and not legacy_temp_entries:
        return

    valid = {_entry_path(p)[0] for p in current_paths}
    for entry_path in entries:
        if entry_path not in valid:
            try:
                entry_path.unlink()
            except OSError:
                pass

    # A temp file's name does not address a document, so it cannot be compared against
    # `valid` the way an entry is: presence under this name is the whole criterion, with no age
    # or ownership check. That also matches a temp file another `store` call is still writing,
    # between `write_text` and `replace` — its `replace` then raises `FileNotFoundError`, an
    # `OSError` that call's own `except OSError` arm turns into one lost entry and one warning,
    # not a crash. Accepted: it is the same entry loss already accepted where two stores share a
    # cache root and prune each other's entries (contracts/cache.md).
    for tmp_path in (*temp_entries, *legacy_temp_entries):
        try:
            tmp_path.unlink()
        except OSError:
            pass
