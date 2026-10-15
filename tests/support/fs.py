from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def entry_names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


@contextmanager
def without_access(path: Path, restore_mode: int) -> Iterator[None]:
    """Mode 0o000 for the block, restored even if it raises, so tmp cleanup can remove it."""
    os.chmod(path, 0o000)
    try:
        yield
    finally:
        os.chmod(path, restore_mode)
