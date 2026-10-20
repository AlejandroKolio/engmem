from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest
import yaml


def _permissions_should_be_enforced() -> bool:
    if sys.platform == "win32":
        return False
    return os.geteuid() != 0


def _symlinks_work() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "target"
        target.write_text("x", encoding="utf-8")
        try:
            (Path(tmp) / "link").symlink_to(target)
        except (OSError, NotImplementedError):
            return False
    return True


requires_permission_enforcement = pytest.mark.skipif(
    not _permissions_should_be_enforced(),
    reason="chmod does not restrict root, and on Windows only toggles the read-only bit",
)

requires_symlinks = pytest.mark.skipif(
    not _symlinks_work(),
    reason="creating symlinks is not permitted on this platform",
)

requires_posix_modes = pytest.mark.skipif(
    sys.platform == "win32",
    reason="chmod only toggles the read-only bit on Windows; st_mode reports 0o666/0o444",
)

requires_windows = pytest.mark.skipif(
    sys.platform != "win32",
    reason="drive letters and per-drive CWDs (os.path.abspath('C:')) only exist on Windows",
)


on_a_shared_runner = pytest.mark.skipif(
    bool(os.environ.get("CI")),
    reason="wall-clock budget is specified for the author's machine, not a shared runner",
)


# libyaml accepts a tab after `key:` that the pure-Python loader rejects (contracts/cache.md)
requires_libyaml = pytest.mark.skipif(not yaml.__with_libyaml__, reason="PyYAML built without libyaml")
