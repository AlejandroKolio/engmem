"""Store files read without leaving the store through a link (US-22). Rules and their reasons:
contracts/runtime.md, "Reads stay inside the store"."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

# the parts of a path that sit inside the store: `sessions/<id>.md`, `versions/<id>/<version>.md`
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
    """A store path that passes through a link or junction; nothing behind it was read."""


def _refused(path: Path, depth: int) -> OutsideStoreError:
    inside = "/".join(path.parts[-depth:])
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


def _open_inside(path: Path, depth: int, flags: int) -> int:
    """An fd for `path`, opened one component at a time below the store root, none followed."""
    fd = os.open(path.parents[depth - 1], os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[-depth:-1]:
            child = _open_component(path, depth, part, os.O_RDONLY | os.O_DIRECTORY, fd)
            os.close(fd)
            fd = child
        return _open_component(path, depth, path.name, flags, fd)
    finally:
        os.close(fd)


def _open_component(path: Path, depth: int, name: str, flags: int, dir_fd: int) -> int:
    try:
        return os.open(name, flags | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError as exc:
        # ELOOP on Linux and macOS, EMLINK on FreeBSD, ENOTDIR when O_DIRECTORY meets the link
        try:
            linked = stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)
        except OSError:
            linked = False
        if linked or exc.errno in (errno.ELOOP, errno.EMLINK):
            raise _refused(path, depth) from None
        raise


def _check_components(path: Path, depth: int) -> os.stat_result:
    """Without `dir_fd` (Windows): each part below the root is checked, then opened by name.
    Returns the last part's `lstat`."""
    current = path.parents[depth - 1]
    for part in path.parts[-depth:]:
        current = current / part
        st = os.lstat(current)
        if _is_link(st):
            raise _refused(path, depth)
    return st


def _not_regular(path: Path, st: os.stat_result) -> OSError:
    if stat.S_ISDIR(st.st_mode):
        return IsADirectoryError(errno.EISDIR, "Is a directory", str(path))
    return OSError(errno.EINVAL, "Not a regular file", str(path))


def read_bytes(path: Path, depth: int) -> bytes:
    """`path`'s bytes; its last `depth` parts are inside the store and none may be a link.
    The store root above them may be one: it is the owner's choice, resolved once."""
    if _OPENAT:
        # O_NONBLOCK: a FIFO left in the store is refused below instead of hanging the open
        fd = _open_inside(path, depth, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise _not_regular(path, st)
            return f.read()
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


def read_utf8(path: Path, depth: int) -> str:
    """`read_bytes` decoded as `Path.read_text(encoding="utf-8-sig")` would: BOM stripped and
    every line ending read as `\\n`."""
    text = read_bytes(path, depth).decode("utf-8-sig")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def list_names(directory: Path, depth: int) -> list[str]:
    """The entry names of `directory`, whose last `depth` parts must not be links."""
    if _OPENAT:
        fd = _open_inside(directory, depth, os.O_RDONLY | os.O_DIRECTORY)
        try:
            return os.listdir(fd)
        finally:
            os.close(fd)
    _check_components(directory, depth)
    return os.listdir(directory)
