"""Store files read and appended without leaving the store through a link (US-22, US-23). Rules
and their reasons: contracts/runtime.md, "Reads stay inside the store" and "Logs are written
inside the store"."""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# the parts of a path that sit inside the store: `<log>.jsonl`, `sessions/<id>.md`,
# `versions/<id>/<version>.md`
LOG_DEPTH = 1
SESSIONS_DEPTH = 1
SESSION_DEPTH = 2
VERSION_DEPTH = 3

# IsReparseTagNameSurrogate: the tag names another place (symlink, junction), not this file
_NAME_SURROGATE = 0x20000000

_OPENAT = (
    os.open in os.supports_dir_fd
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
)


class OutsideStoreError(OSError):
    """A store path that passes through a link or junction; nothing behind it was read or
    written."""


def _refused(path: Path, depth: int, write: bool = False) -> OutsideStoreError:
    inside = "/".join(path.parts[-depth:])
    if write:
        return OutsideStoreError(
            f"{inside} passes through a link or junction -- engmem writes only inside the "
            "store, so nothing was written"
        )
    return OutsideStoreError(
        f"{inside} passes through a link or junction -- engmem reads only files inside the "
        "store, so nothing behind it was read"
    )


def _is_link(st: os.stat_result) -> bool:
    """A symlink, or a name-surrogate reparse point such as a junction; a cloud placeholder or
    a deduplicated file is a reparse point too, and is a file in its own place."""
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_reparse_tag", 0) & _NAME_SURROGATE)


def is_link(path: Path) -> bool:
    """`path` itself is a link or junction; False when it cannot be examined."""
    try:
        return _is_link(os.lstat(path))
    except OSError:
        return False


def _open_inside(path: Path, depth: int, flags: int, write: bool = False) -> int:
    """An fd for `path`, opened one component at a time below the store root, none followed."""
    fd = os.open(path.parents[depth - 1], os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[-depth:-1]:
            child = _open_component(path, depth, part, os.O_RDONLY | os.O_DIRECTORY, fd, write)
            os.close(fd)
            fd = child
        return _open_component(path, depth, path.name, flags, fd, write)
    finally:
        os.close(fd)


def _open_component(
    path: Path, depth: int, name: str, flags: int, dir_fd: int, write: bool
) -> int:
    try:
        # 0o666, as `open()` creates a file; `os.open` would default to 0o777
        return os.open(name, flags | os.O_NOFOLLOW, 0o666, dir_fd=dir_fd)
    except OSError as exc:
        # ELOOP on Linux and macOS, EMLINK on FreeBSD, ENOTDIR when O_DIRECTORY meets the link
        try:
            linked = stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)
        except OSError:
            linked = False
        if linked or exc.errno in (errno.ELOOP, errno.EMLINK):
            raise _refused(path, depth, write) from None
        raise


def _check_components(path: Path, depth: int, write: bool = False) -> os.stat_result:
    """Without `dir_fd` (Windows): each part below the root is checked, then opened by name.
    Returns the last part's `lstat`."""
    current = path.parents[depth - 1]
    for part in path.parts[-depth:]:
        current = current / part
        st = os.lstat(current)
        if _is_link(st):
            raise _refused(path, depth, write)
    return st


def _not_regular(path: Path, st: os.stat_result) -> OSError:
    if stat.S_ISDIR(st.st_mode):
        return IsADirectoryError(errno.EISDIR, "Is a directory", str(path))
    return OSError(errno.EINVAL, "Not a regular file", str(path))


def _read_regular(path: Path, fd: int) -> bytes:
    """The bytes behind `fd`, refused unless it is a regular file; `fd` is closed either way."""
    # checked before `fdopen`: `fdopen` on a directory raises naming the fd number, not the path,
    # and leaves the fd open, one more for every search in a long-lived server
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _not_regular(path, st)
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as f:
        return f.read()


def read_bytes(path: Path, depth: int) -> bytes:
    """`path`'s bytes; its last `depth` parts are inside the store and none may be a link.
    The store root above them may be one: it is the owner's choice, resolved once."""
    if _OPENAT:
        # O_NONBLOCK: a FIFO left in the store is refused below instead of hanging the open
        return _read_regular(path, _open_inside(path, depth, os.O_RDONLY | os.O_NONBLOCK))
    checked = _check_components(path, depth)
    # before the open: Windows refuses to open a directory with PermissionError
    if not stat.S_ISREG(checked.st_mode):
        raise _not_regular(path, checked)
    with open(path, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise _not_regular(path, st)
        data = f.read()
    after = os.lstat(path)
    # the name was swapped for a link between the check and the open
    if _is_link(after) or (after.st_dev, after.st_ino) != (st.st_dev, st.st_ino):
        raise _refused(path, depth)
    return data


def _decoded(data: bytes) -> str:
    text = data.decode("utf-8-sig")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_utf8(path: Path, depth: int) -> str:
    """`read_bytes` decoded as `Path.read_text(encoding="utf-8-sig")` would: BOM stripped and
    every line ending read as `\\n`."""
    return _decoded(read_bytes(path, depth))


class Directory:
    """A store directory held open while it is listed and its files are read, so they all come
    from the one directory that was opened (contracts/runtime.md, "Reads stay inside the store")."""

    def __init__(self, path: Path, depth: int, fd: int | None) -> None:
        self._path = path
        self._depth = depth
        self._fd = fd

    def names(self) -> list[str]:
        if self._fd is not None:
            return os.listdir(self._fd)
        _check_components(self._path, self._depth)
        return os.listdir(self._path)

    def read_utf8(self, name: str) -> str:
        """`read_utf8(path / name, depth + 1)`, opened relative to the held directory."""
        path = self._path / name
        if self._fd is None:
            return read_utf8(path, self._depth + 1)
        # O_NONBLOCK: a FIFO left in the store is refused below instead of hanging the open
        fd = _open_component(
            path, self._depth + 1, name, os.O_RDONLY | os.O_NONBLOCK, self._fd, write=False
        )
        return _decoded(_read_regular(path, fd))

    def replaced(self) -> bool:
        """Something else now sits at the path: another directory, a link, or nothing."""
        if self._fd is None:
            return False
        try:
            now = os.stat(self._path, follow_symlinks=False)
        except OSError:
            return True
        held = os.fstat(self._fd)
        return (now.st_dev, now.st_ino) != (held.st_dev, held.st_ino)


@contextmanager
def directory(path: Path, depth: int) -> Iterator[Directory]:
    """`path`, whose last `depth` parts must not be links, held open for the `with` block."""
    fd = _open_inside(path, depth, os.O_RDONLY | os.O_DIRECTORY) if _OPENAT else None
    try:
        yield Directory(path, depth, fd)
    finally:
        if fd is not None:
            os.close(fd)


def list_names(directory_path: Path, depth: int) -> list[str]:
    """The entry names of `directory_path`, whose last `depth` parts must not be links."""
    with directory(directory_path, depth) as held:
        return held.names()


def read_log_lines(path: Path, depth: int) -> list[str]:
    """`path`'s lines as `read_utf8` reads them; a log that does not exist yet has none."""
    if not os.path.lexists(path):
        return []
    return read_utf8(path, depth).split("\n")


def append_line(path: Path, depth: int, line: bytes) -> None:
    """Appends `line` and a newline to `path`, creating it, never through a link. A line torn by
    an interrupted write is closed first, so the new one is not swallowed by it."""
    if _OPENAT:
        fd = _open_inside(
            path, depth, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK, write=True
        )
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise _not_regular(path, st)
            torn = st.st_size > 0 and os.pread(fd, 1, st.st_size - 1) != b"\n"
            data = (b"\n" if torn else b"") + line + b"\n"
            # one write with O_APPEND: on a local filesystem concurrent appenders land whole lines
            if os.write(fd, data) != len(data):
                raise OSError(errno.EIO, "short write", str(path))
        finally:
            os.close(fd)
        return
    _append_checked(path, depth, line)


def _append_checked(path: Path, depth: int, line: bytes) -> None:
    """Without `dir_fd` (Windows), where O_APPEND is a seek before each write and not atomic:
    the store's write lock serialises appenders, and the file is checked before it is written."""
    from engmem.staging import DocumentLockedError, document_lock

    try:
        with document_lock(path):
            try:
                checked = _check_components(path, depth, write=True)
            except FileNotFoundError:
                checked = None
            if checked is not None and not stat.S_ISREG(checked.st_mode):
                raise _not_regular(path, checked)
            with open(path, "a+b") as f:
                st = os.fstat(f.fileno())
                after = os.lstat(path)
                if _is_link(after) or (after.st_dev, after.st_ino) != (st.st_dev, st.st_ino):
                    raise _refused(path, depth, write=True)
                torn = False
                if st.st_size > 0:
                    f.seek(-1, os.SEEK_END)
                    torn = f.read(1) != b"\n"
                # "a" mode: the write lands at the end wherever the read left the position
                f.write((b"\n" if torn else b"") + line + b"\n")
    except DocumentLockedError as exc:
        raise OSError(
            errno.EBUSY,
            f"{exc.lock} is held by another engmem call -- nothing was written; if no engmem "
            "process is running, an interrupted write left it behind: remove it",
        ) from None
