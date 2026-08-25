"""Suite-wide safety net and platform guards.

The network block enforces a product guarantee, not a test convenience: engmem makes zero
network calls of any kind (spec FR-018), and the cheapest way to keep it true is to make
the attempt raise here.

The two platform guards cover capabilities that are not universal and that fail in a way
looking like a product bug rather than an environment one: `chmod 000` does not block
reads for root or on Windows, and creating a symlink on Windows needs privileges the
runner may not have.
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch


def _blocked(*args, **kwargs):
    raise RuntimeError(
        "Network access attempted during tests: engmem must make zero network "
        "calls of any kind (spec FR-018). Blocked by tests/conftest.py."
    )


@pytest.fixture(scope="session", autouse=True)
def _no_network():
    mp = MonkeyPatch()
    mp.setattr(socket, "socket", _blocked)
    mp.setattr(socket, "create_connection", _blocked)
    yield
    mp.undo()


def _unreadable_paths_work() -> bool:
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


requires_unreadable_paths = pytest.mark.skipif(
    not _unreadable_paths_work(),
    reason="chmod 000 does not block reads for root or on Windows",
)

requires_symlinks = pytest.mark.skipif(
    not _symlinks_work(),
    reason="creating symlinks is not permitted on this platform",
)


def _real_user_paths() -> dict[Path, bytes | None]:
    """Files outside the sandbox that a mistake in an install/uninstall test could
    plausibly reach. Snapshotted, never written."""
    import hashlib

    home = Path(os.path.expanduser("~"))
    candidates = [
        home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
        home / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json",
        home / ".claude" / "CLAUDE.md",
    ]
    snapshot: dict[Path, bytes | None] = {}
    for path in candidates:
        try:
            snapshot[path] = hashlib.sha256(path.read_bytes()).digest()
        except OSError:
            snapshot[path] = None
    return snapshot


@pytest.fixture(scope="session", autouse=True)
def _real_user_files_are_untouched():
    """The install tests write agent configuration. Every one of them redirects HOME —
    but a single test that forgets would edit the developer's own Claude Desktop config,
    and the only symptom would be a puzzling change on their machine, days later.

    This makes that failure loud instead: the suite fails, naming the file.
    """
    before = _real_user_paths()
    yield
    after = _real_user_paths()
    changed = [str(p) for p in before if before[p] != after.get(p)]
    assert not changed, (
        "a test modified a real user file outside its sandbox: "
        + ", ".join(changed)
        + " — some test is not redirecting HOME"
    )

on_a_shared_runner = pytest.mark.skipif(
    bool(os.environ.get("CI")),
    reason="wall-clock budget is specified for the author's machine, not a shared runner",
)

def redirect_home(monkeypatch, home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOMEDRIVE", home.drive or "")
    monkeypatch.setenv("HOMEPATH", str(home)[len(home.drive):])
    # Claude Desktop's config lives under %APPDATA% on Windows
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
