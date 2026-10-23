"""Git for tests that need a real repository; pair it with the `hermetic_git` fixture
(tests/conftest.py)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def git(checkout: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(checkout), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    return completed.stdout.strip()
