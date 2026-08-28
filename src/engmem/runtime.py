"""Shared ground for CLI entry points: store location and failure reporting."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def default_store() -> Path:
    return _absolute(_expand_home(Path("~")) / "Developer" / "engmem")


def _configured(value: str | None) -> str | None:
    """A blank setting means "not set"; the value is never trimmed, so a directory whose name
    really does end in a space still resolves to itself."""
    return value if value and value.strip() else None


def _expand_home(path: Path) -> Path:
    # Path.expanduser() raises RuntimeError instead of falling back when a leading `~` or
    # `~user` cannot be resolved (HOME unset with no passwd entry for the uid; a `~user` that
    # doesn't exist) — left untouched here so it becomes an ordinary relative path the
    # "store not found" diagnostic can still name, instead of an uncaught traceback that
    # leaves stdout empty. See contracts/runtime.md.
    try:
        return path.expanduser()
    except RuntimeError:
        return path


def _absolute(path: Path) -> Path:
    # Path.absolute() gained the branch below only in Python 3.13 (bpo-89812); before that, a
    # Windows drive-relative path such as "C:notes" comes back unchanged — still relative —
    # whenever the process's cwd sits on a different drive, because the pre-3.13 join reparses
    # `[cwd] + parts` from scratch and lets the bare drive-letter part re-anchor the result.
    # Hand-ported so every supported interpreter behaves the same way. See contracts/runtime.md.
    if path.is_absolute():
        return path
    cwd = os.path.abspath(path.drive) if path.drive else os.getcwd()
    return path.__class__(cwd) / path


def resolve_store(explicit: str | None) -> Path:
    # expanded because pathlib never expands a literal `~` out of argv or the environment,
    # absolute because the answer outlives this process — see contracts/runtime.md
    configured = _configured(explicit) or _configured(os.environ.get("ENGMEM_HOME"))
    if configured is None:
        return default_store()
    return _absolute(_expand_home(Path(configured)))


def fail(message: str) -> None:
    # Both streams: the consuming agent reads stdout and never stderr (ENGMEM-SPEC.md §5,
    # §10 principle VIII), so a failure named on stderr alone is a swallowed error.
    print(f"error: {message}", file=sys.stderr)
    print(f"error: {message}")
