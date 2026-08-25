"""Shared ground for CLI entry points: store location and failure reporting.
Nothing else belongs here.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def default_store() -> Path:
    return Path.home() / "Developer" / "engmem"


def resolve_store(explicit: str | None) -> Path:
    # pathlib never expands a literal `~` from argv, so do it here.
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("ENGMEM_HOME")
    if env:
        return Path(env).expanduser()
    return default_store()


def fail(message: str) -> None:
    # Both streams: the consuming agent only reads stdout (ENGMEM-SPEC.md §1),
    # so a failure must land there too, not only on stderr.
    print(f"error: {message}", file=sys.stderr)
    print(f"error: {message}")
