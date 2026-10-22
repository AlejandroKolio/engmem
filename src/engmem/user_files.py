"""Files engmem rewrites outside the store: an agent's instructions file and an MCP client's config,
which are the user's, and an installed template, engmem's until edited. Read with a named
failure, replaced atomically, through a link and never over it (contracts/install.md)."""

from __future__ import annotations

import os
from pathlib import Path

from engmem.staging import commit, discard, read_document, stage


class SetupError(Exception):
    """A step install/uninstall cannot complete; the message names the path and the cause, and
    reaches the user as an exit-2 diagnosis rather than a traceback."""


def ensure_directory(path: Path, what: str) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SetupError(f"cannot create the {what} {path}: {exc}") from exc


def read_user_file(path: Path) -> tuple[str, bytes]:
    """`(text, byte-order mark)` for a file engmem did not write; both failure modes name it."""
    try:
        return read_document(path)
    except UnicodeDecodeError as exc:
        raise SetupError(
            f"{path} is not valid UTF-8 ({exc.reason} at byte {exc.start}) — engmem will "
            f"not rewrite a file it cannot read. Re-save it as UTF-8 and re-run"
        ) from exc
    except OSError as exc:
        raise SetupError(f"cannot read {path}: {exc}") from exc


def replace_user_file(path: Path, text: str, bom: bytes = b"") -> None:
    """Atomic replacement for a file engmem does not own — see contracts/install.md."""
    # written through the link, never over it: `os.replace` on the link itself detaches a
    # CLAUDE.md symlinked into a dotfiles repo from what it points at
    target = Path(os.path.realpath(path)) if path.is_symlink() else path
    try:
        tmp_path = stage(target, bom + text.encode("utf-8"))
    except OSError as exc:
        raise SetupError(f"cannot write {path}: {exc}") from exc
    # BaseException, not OSError alone: a Ctrl-C landing between the stage and the commit must
    # still take the temp file with it
    try:
        commit(tmp_path, target)
    except OSError as exc:
        discard(tmp_path)
        raise SetupError(f"cannot write {path}: {exc}") from exc
    except BaseException:
        discard(tmp_path)
        raise
