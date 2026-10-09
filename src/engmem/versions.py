"""Retained copies of the exact document versions a search showed, so a Reuse Log quote is checked
against the version it was taken from (US-13). Rules and their reasons: contracts/gate1.md,
"Versioned citations"."""

from __future__ import annotations

import os
import re
from pathlib import Path

from engmem.cache import identity_for
from engmem.spine import Doc, split_front_matter, validate_doc_id
from engmem.staging import commit_new, discard, stage, version_of

VERSIONS_DIR = "versions"
# a clone with `core.autocrlf` would rewrite every copy's line endings, and a copy whose bytes
# changed no longer proves anything (contracts/gate1.md, "Versioned citations")
GITATTRIBUTES = b"* -text\n"
VERSION_SEPARATOR = "@"
_VERSION_RE = re.compile(r"[0-9a-f]{16}")


class RetentionError(Exception):
    """A version that cannot be retained for a reason other than an `OSError`."""


def is_version(text: str) -> bool:
    return bool(_VERSION_RE.fullmatch(text))


def split_reference(reference: str) -> tuple[str, str | None]:
    """`(cited id, version)` from a `prior-doc` cell's `<id>@<version>`; None for a bare id."""
    if VERSION_SEPARATOR not in reference:
        return reference, None
    doc_id, version = reference.rsplit(VERSION_SEPARATOR, 1)
    return doc_id, version


def retained_path(store: Path, doc_id: str, version: str) -> Path:
    return store / VERSIONS_DIR / doc_id / f"{version}.md"


def _plain_directory(directory: Path) -> None:
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    if directory.is_symlink() or not directory.is_dir():
        raise RetentionError(f"{str(directory)!r} is not a plain directory")


def _publish(target: Path, data: bytes) -> bool:
    """Writes `data` as `target` unless an entry by that name exists; False when one did."""
    tmp_path = stage(target, data)
    try:
        commit_new(tmp_path, target)
    except FileExistsError:
        discard(tmp_path)
        return False
    except BaseException:
        discard(tmp_path)
        raise
    return True


def _keep_line_endings(root: Path) -> None:
    """`versions/.gitattributes`, written once; a file the user already keeps there is theirs."""
    _publish(root / ".gitattributes", GITATTRIBUTES)


def _verify_or_repair(target: Path, data: bytes, version: str) -> None:
    """An existing copy must be a regular file holding `data`; altered bytes are replaced with
    `data`, which provably is `version`, and anything else there is refused."""
    if target.is_symlink() or not target.is_file():
        raise RetentionError(f"{target.name!r} exists and is not a regular file")
    if version_of(target.read_bytes()) == version:
        return
    tmp_path = stage(target, data)
    try:
        os.replace(tmp_path, target)
    except BaseException:
        discard(tmp_path)
        raise


def retain(store: Path, doc: Doc) -> str:
    """The version of `doc`'s bytes as it was read, kept intact under `versions/`. Raises
    `RetentionError` or `OSError`; a document that changed since it was read is refused."""
    invalid = validate_doc_id(doc.id)
    if invalid is not None:
        raise RetentionError(f"id {doc.id!r} cannot name a directory: {invalid}")
    data = doc.path.read_bytes()
    # stat after the read: a change before it or during it leaves a different identity behind
    if identity_for(doc.path) != doc.source_identity:
        raise RetentionError(f"{doc.path.name!r} changed on disk since it was read")

    version = version_of(data)
    root = store / VERSIONS_DIR
    _plain_directory(root)
    _keep_line_endings(root)
    _plain_directory(root / doc.id)
    target = retained_path(store, doc.id, version)
    if not os.path.lexists(target) and _publish(target, data):
        return version
    if not os.path.lexists(target):
        # `commit_new` refused without an entry there: an interrupted create left its lock
        raise RetentionError(f"{target.name!r} could not be created (a create lock is held)")
    _verify_or_repair(target, data, version)
    return version


def retained_body(store: Path, doc_id: str, version: str) -> tuple[str | None, str | None]:
    """`(body, None)` when the retained copy exists and hashes to `version`, else
    `(None, reason)`."""
    if not is_version(version):
        return None, f"{version!r} is not a version (16 lowercase hex digits)"
    if validate_doc_id(doc_id) is not None:
        return None, f"id {doc_id!r} cannot name a retained copy"
    path = retained_path(store, doc_id, version)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None, f"not retained: no copy at {VERSIONS_DIR}/{doc_id}/{version}.md"
    except OSError as exc:
        return None, f"retained copy unreadable ({exc})"
    actual = version_of(data)
    if actual != version:
        return None, f"retained copy fails its integrity check: its bytes are version {actual}"
    try:
        _front_matter, body = split_front_matter(data.decode("utf-8-sig"))
    except ValueError as exc:
        return None, f"retained copy does not parse ({exc})"
    return body, None
