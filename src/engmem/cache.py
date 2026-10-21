"""Local, best-effort cache of per-document scoring data, keyed by a hash of the body.

Rules and their reasons: contracts/cache.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import unicodedata
from uuid import uuid4
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Generic, TypeVar

# Bump the number when the cached payload shape, the key, or the tokenizer changes; older-format
# entries are then treated as absent, not corrupt. 3: headings tolerate 0-3 leading spaces,
# changing section boundaries. 4: Unicode tokenization. 5: keyed by the body's sha256, not the
# file's `(mtime_ns, size)`. The Unicode database version rides along because NFKC, casefold and
# the token character classes all come from it (contracts/cache.md).
CACHE_FORMAT_VERSION = f"5/unicode-{unicodedata.unidata_version}"


def cache_root() -> Path:
    """Cache lives outside the store, honoring XDG_CACHE_HOME, else `~/.cache/engmem`."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "engmem"


def identity_for(path: Path) -> tuple[int, int] | None:
    """`(mtime_ns, size)` for the write-side guards, or None when the file cannot be stat()'d.
    Not the cache key: see contracts/cache.md, "Keyed by content"."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def content_key(body: str) -> str:
    """The key of the payload built from `body`: its sha256, so no other text can match it."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


V = TypeVar("V")


class KeptByPath(Generic[V]):
    """One value per document path, served while the key it was built under still matches;
    contracts/cache.md, "Kept between searches"."""

    # keyed by the path's text: a Windows path compares case-insensitively, and a document
    # renamed `Foo.md` -> `foo.md` must not be served under its old path
    def __init__(self) -> None:
        self._kept: dict[str, tuple[object, V]] = {}

    def get(self, path: Path, key: object, build: Callable[[], V]) -> V:
        kept = self._kept.get(str(path))
        if kept is not None and kept[0] == key:
            return kept[1]
        value = build()
        self._kept[str(path)] = (key, value)
        return value

    def retain(self, paths: Iterable[Path]) -> None:
        """Forgets every path not in `paths`."""
        wanted = {str(path) for path in paths}
        self._kept = {path: kept for path, kept in self._kept.items() if path in wanted}


def _entry_path(path: Path) -> tuple[Path, Path]:
    """`(entry_path, resolved_document_path)` — resolved once, so a caller recording the path
    cannot name a different document than the filename addresses."""
    # sha256 of the resolved path: fixed-length, filesystem-safe, and one entry for a symlink
    # and its target. A hard link or a case-variant name still gets its own — see
    # contracts/cache.md. Where an entry lives decides nothing about whether it is served.
    resolved = path.resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()
    return cache_root() / f"{digest}.json", resolved


def _warn(message: str) -> None:
    # stderr only: the agent reading search output never reads stderr
    # (ENGMEM-SPEC.md §4, §5), so a slow-but-correct search must not look failed.
    print(f"engmem: cache warning: {message}", file=sys.stderr)


def load(path: Path, key: str) -> object | None:
    """Cached payload for `path` if present and built from the body `key` names, else None;
    never raises."""
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

    # a plain `!=`: any value of any type that is not this body's hash is a miss, and none of
    # them can raise. Why this branch stays silent: contracts/cache.md, "Degrading, and how loudly"
    if record.get("body_sha256") != key:
        return None

    # `is None` and not `not in`: no legitimate write produces a null payload, so unlike the
    # `body_sha256` row above there is no live case sharing this branch to keep it quiet for
    if record.get("payload") is None:
        _warn(f"cache entry for {path.name} is corrupt, recomputing (missing or null payload)")
        return None

    return record["payload"]


def store(path: Path, key: str, payload: object) -> None:
    """Best-effort write-through: a write failure warns rather than failing the search."""
    entry_path, resolved = _entry_path(path)
    record = {
        "format_version": CACHE_FORMAT_VERSION,
        # never read back; it is the only way from a directory of opaque digests to the
        # document an entry belongs to. The resolved path, because that is what was hashed
        "path": str(resolved),
        "body_sha256": key,
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


# the cache root and store paths of this process's last completed prune
_last_pruned: tuple[str, frozenset[str]] | None = None


def prune_orphans(current_paths: Iterable[Path]) -> None:
    """Drops entries for documents no longer in the store, and any leaked write-temp files;
    once per set of paths in a process — contracts/cache.md, "Kept between searches"."""
    global _last_pruned
    root = cache_root()
    paths = list(current_paths)
    current = (str(root), frozenset(str(path) for path in paths))
    if current == _last_pruned:
        return
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
    _last_pruned = current
    if not entries and not temp_entries and not legacy_temp_entries:
        return

    valid = {_entry_path(p)[0] for p in paths}
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
