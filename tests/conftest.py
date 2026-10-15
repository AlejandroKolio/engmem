"""Suite-wide safety net: engmem makes zero network calls (FR-018), so the attempt raises here."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch

from support.home import redirect_home


def _blocked(*args, **kwargs):
    raise RuntimeError(
        "Network access attempted during tests: engmem must make zero network "
        "calls of any kind (spec FR-018). Blocked by tests/conftest.py."
    )


@pytest.fixture(scope="session", autouse=True)
def _no_network():
    mp = MonkeyPatch()
    # name resolution is a network call in its own right, and it is the one a bug reaches first
    for name in ("socket", "create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex"):
        mp.setattr(socket, name, _blocked)
    yield
    mp.undo()


# resolved at import time, before any test can redirect HOME or the cache root
_REAL_HOME = Path(os.path.expanduser("~"))
# resolved the way cache.cache_root() does: hardcoding ~/.cache would watch a directory the
# product never touches on any machine that sets XDG_CACHE_HOME
_REAL_CACHE_ROOT = Path(os.environ.get("XDG_CACHE_HOME") or _REAL_HOME / ".cache") / "engmem"
# every place `settings.setting_file()` can land on this machine, whichever env it sees
_REAL_CONFIG_ROOTS = {
    Path(os.environ.get("XDG_CONFIG_HOME") or _REAL_HOME / ".config") / "engmem",
    _REAL_HOME / ".config" / "engmem",
    Path(os.environ.get("APPDATA") or _REAL_HOME / "AppData" / "Roaming") / "engmem",
}


def _real_user_paths() -> dict[Path, bytes | None]:
    """Real files and directories a test could reach by mistake; snapshotted, never written."""
    import hashlib

    snapshot: dict[Path, bytes | None] = {}
    for path in (
        _REAL_HOME / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
        _REAL_HOME / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json",
        _REAL_HOME / ".claude" / "CLAUDE.md",
        _REAL_HOME / ".codex" / "AGENTS.md",
        _REAL_HOME / ".codex" / "config.toml",
        *(root / name for root in _REAL_CONFIG_ROOTS for name in ("store", "mode")),
    ):
        try:
            snapshot[path] = hashlib.sha256(path.read_bytes()).digest()
        except OSError:
            snapshot[path] = None
    # uninstall empties whole directories and the cache prunes entries it does not recognise,
    # so for these it is the listing that has to survive, not one file's bytes
    for path in (
        _REAL_HOME / ".claude" / "commands",
        _REAL_HOME / ".copilot" / "skills",
        _REAL_HOME / ".agents" / "skills",
        _REAL_CACHE_ROOT,
        *_REAL_CONFIG_ROOTS,
    ):
        try:
            names = "\n".join(sorted(entry.name for entry in path.iterdir()))
        except OSError:
            snapshot[path] = None
        else:
            snapshot[path] = hashlib.sha256(names.encode("utf-8")).digest()
    return snapshot


@pytest.fixture(autouse=True)
def _real_user_files_are_untouched():
    """Per test, not per session: a session-wide check reports the damage against whichever test
    happened to run last."""
    before = _real_user_paths()
    yield
    after = _real_user_paths()
    changed = [str(p) for p in before if before[p] != after.get(p)]
    assert not changed, (
        "this test reached a real user path outside its sandbox: "
        + ", ".join(changed)
        + " — it is not redirecting HOME, or not sandboxing the cache root"
    )


@pytest.fixture(autouse=True)
def _isolated_cache_root(tmp_path_factory, monkeypatch):
    """The body cache lives outside the store (XDG, not the store path), so sandboxing the store
    alone still leaves a test writing to and pruning the developer's own `~/.cache/engmem`."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path_factory.mktemp("xdg-cache")))
    # the saved store choice is read by every command run without --store, and the saved mode by
    # every draft the MCP server creates; the developer's own would otherwise steer a test, and
    # `engmem store set` / `engmem mode set` would overwrite them
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg-config")))
    monkeypatch.delenv("ENGMEM_HOME", raising=False)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A redirected HOME plus a project cwd: install writes to both, and a test that forgets
    either edits the developer's own machine."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    redirect_home(monkeypatch, home)
    monkeypatch.chdir(project)
    return home, project
