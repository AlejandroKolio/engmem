"""Reading and writing whole documents without disturbing their bytes, shared by
`backfill` and the MCP write tools."""

from __future__ import annotations

import os
import re
from pathlib import Path
from uuid import uuid4

_BOM = b"\xef\xbb\xbf"
_NEWLINE_RE = re.compile(r"\r\n|\r|\n")


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
