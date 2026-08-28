"""Shared ground for CLI entry points: store location and failure reporting."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def default_store() -> Path:
    return Path.home() / "Developer" / "engmem"


def _configured(value: str | None) -> str | None:
    """A blank setting means "not set"; the value is never trimmed, so a directory whose name
    really does end in a space still resolves to itself."""
    return value if value and value.strip() else None


def resolve_store(explicit: str | None) -> Path:
    # expanded because pathlib never expands a literal `~` out of argv or the environment,
    # absolute because the answer outlives this process — see contracts/runtime.md
    configured = _configured(explicit) or _configured(os.environ.get("ENGMEM_HOME"))
    if configured is None:
        return default_store()
    return Path(configured).expanduser().absolute()


def fail(message: str) -> None:
    # Both streams: the consuming agent reads stdout and never stderr (ENGMEM-SPEC.md §5,
    # §10 principle VIII), so a failure named on stderr alone is a swallowed error.
    print(f"error: {message}", file=sys.stderr)
    print(f"error: {message}")
