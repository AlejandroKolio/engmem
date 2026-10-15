"""Fixtures shared by the doctor and upgrade tests."""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

import pytest


@pytest.fixture
def home(env) -> Path:
    home, project = env
    (project / ".git").mkdir()
    return home


@pytest.fixture
def shell_path(tmp_path, monkeypatch) -> Path:
    """A launcher for this interpreter first on PATH: the developer's own PATH may carry another
    engmem, or none; the rest of PATH stays, for the `git` install runs."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if sys.platform == "win32":
        (bin_dir / "engmem.bat").write_bytes(f'@"{sys.executable}" -m engmem.cli %*\r\n'.encode())
    else:
        launcher = bin_dir / "engmem"
        launcher.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -m engmem.cli \"$@\"\n")
        launcher.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")]))
    return bin_dir
