from __future__ import annotations

from pathlib import Path


def entry_names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())
