"""Reading and writing whole documents without disturbing their bytes, shared by
`backfill` and the MCP write tools."""

from __future__ import annotations

import errno
import hashlib
import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

_BOM = b"\xef\xbb\xbf"
_WINDOWS = os.name == "nt"
# what `os.link` raises where the filesystem has no hard links at all (FAT, exFAT, a sandbox
# that forbids them) — the only failures `commit_new` falls back on
_NO_HARD_LINKS_ERRNOS = frozenset(
    code
    for code in (
        errno.EPERM,
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
        getattr(errno, "ENOSYS", None),
    )
    if code is not None
)
_ERROR_INVALID_FUNCTION = 1
_ERROR_NOT_SUPPORTED = 50
_NEWLINE_RE = re.compile(r"\r\n|\r|\n")
_VERSION_HEX_DIGITS = 16
_LOCK_WAIT_SECONDS = 2.0
_LOCK_POLL_SECONDS = 0.01


class DocumentLockedError(Exception):
    """Raised when another writer holds a document's write lock past the wait."""

    def __init__(self, lock: Path) -> None:
        super().__init__(f"{lock.name} is held")
        self.lock = lock


def version_of(data: bytes) -> str:
    """The version a client names when it rewrites a document: a digest of its exact bytes."""
    return hashlib.sha256(data).hexdigest()[:_VERSION_HEX_DIGITS]


def read_document(path: Path) -> tuple[str, bytes]:
    """`(text, byte-order mark)`. Bytes then decode, never `read_text`: it translates line
    endings, invisibly to a caller that rebuilds the file from what it read."""
    data = path.read_bytes()
    bom = _BOM if data.startswith(_BOM) else b""
    return data[len(bom):].decode("utf-8"), bom


def newline_of(text: str) -> str | None:
    """The line ending `text` uses, from its first line break."""
    match = _NEWLINE_RE.search(text)
    return match.group(0) if match else None


def discard(tmp_path: Path) -> None:
    """Best-effort: a leftover `.tmp` is inert, and raising here would replace the reason
    cleanup is running — including a Ctrl-C."""
    try:
        tmp_path.unlink(missing_ok=True)
    except OSError:
        pass


def stage(target: Path, content: bytes) -> Path:
    """Writes `content` to a sibling temp file and returns its path, for the caller to
    `commit` or `discard`."""
    tmp_path = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    # bytes, never text: `write_text` translates line endings, which a body-preservation check
    # comparing two already-translated strings cannot see. 0600 from the start, not narrowed
    # later: the staged file holds a whole document, and it is read and parsed before any
    # chmod could run
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as staged:
            staged.write(content)
            # the rename is atomic, but only over content that reached the disk
            staged.flush()
            os.fsync(staged.fileno())
    except BaseException:
        # the caller cannot discard a path it was never handed — a full disk would otherwise
        # leave one partial `.tmp` per document, and another on every re-run
        discard(tmp_path)
        raise
    return tmp_path


def commit(tmp_path: Path, target: Path) -> None:
    """Replaces `target`, restoring the mode and owner of whatever was there."""
    try:
        before = target.stat()
    except FileNotFoundError:
        before = None  # a new document keeps the staged 0600 — see contracts/backfill.md
    if before is not None:
        os.chmod(tmp_path, before.st_mode & 0o7777)
        try:
            os.chown(tmp_path, before.st_uid, before.st_gid)
        except (OSError, AttributeError):
            pass  # only root can give a file away; elsewhere there is nothing to restore
    os.replace(tmp_path, target)  # atomic on POSIX — never a half-written document


def commit_new(tmp_path: Path, target: Path) -> None:
    """Publishes the staged file as `target` only if no entry by that name exists, else raises
    `FileExistsError` with `tmp_path` left for the caller to discard. See contracts/mcp-server.md,
    "Creating a document: exclusive, not checked"."""
    try:
        # atomic and exclusive in one step: fails on any existing entry, a dangling symlink
        # included, and the name appears already holding the whole fsynced document
        os.link(tmp_path, target)
    except FileExistsError:
        raise
    except OSError as exc:
        # only "no hard links here": the lock excludes other lock holders, not a creator whose
        # link works, so falling back on a transient error would let it replace that creator
        if not _hard_links_unavailable(exc):
            raise
        _commit_new_under_lock(tmp_path, target)
        return
    discard(tmp_path)  # the document is published; a leftover second name is inert


def _hard_links_unavailable(exc: OSError) -> bool:
    if _WINDOWS and getattr(exc, "winerror", None) in (
        _ERROR_INVALID_FUNCTION,
        _ERROR_NOT_SUPPORTED,
    ):
        return True
    return exc.errno in _NO_HARD_LINKS_ERRNOS


def _take_lock(lock: Path) -> bool:
    """`mkdir` is exclusive on every filesystem; False when another holder has `lock`."""
    try:
        os.mkdir(lock)
    except (FileExistsError, PermissionError) as exc:
        # on Windows a lock another writer just removed can still be pending delete, and
        # `mkdir` then reports access denied for what is a busy lock
        if isinstance(exc, PermissionError) and not _WINDOWS:
            raise
        return False
    return True


def _release_lock(lock: Path) -> None:
    # best-effort, like `discard`: raising here would report a write that happened as one that
    # did not. A lock left behind fails closed, refusing writes to that one document by name
    try:
        os.rmdir(lock)
    except OSError:
        pass


@contextmanager
def document_lock(target: Path) -> Iterator[None]:
    """Holds `target`'s write lock for the read-check-replace inside it, waiting briefly for
    another holder. See contracts/mcp-server.md, "Rewriting a document: the version read"."""
    lock = target.with_name(f".{target.name}.write-lock")
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    while not _take_lock(lock):
        if time.monotonic() >= deadline:
            raise DocumentLockedError(lock)
        time.sleep(_LOCK_POLL_SECONDS)
    try:
        yield
    finally:
        _release_lock(lock)


def _commit_new_under_lock(tmp_path: Path, target: Path) -> None:
    """For a filesystem without hard links (FAT, exFAT): `mkdir` is exclusive everywhere, so it
    serialises the check and the rename between engmem creators."""
    lock = target.with_name(f".{target.name}.create-lock")
    if not _take_lock(lock):
        raise FileExistsError(
            errno.EEXIST,
            f"another create of {target.name} is in progress, or one was interrupted "
            f"(remove {lock.name} if no engmem process is running)",
            str(target),
        )
    try:
        if os.path.lexists(target):
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(target))
        os.replace(tmp_path, target)
    finally:
        _release_lock(lock)
