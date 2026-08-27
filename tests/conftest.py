"""Suite-wide safety net: engmem makes zero network calls (FR-018), so the attempt raises here."""

from __future__ import annotations

import os
import socket
import subprocess
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
    # name resolution is a network call in its own right, and it is the one a bug reaches first
    for name in ("socket", "create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex"):
        mp.setattr(socket, name, _blocked)
    yield
    mp.undo()


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


# resolved at import time, before any test can redirect HOME or the cache root
_REAL_HOME = Path(os.path.expanduser("~"))
# resolved the way cache.cache_root() does: hardcoding ~/.cache would watch a directory the
# product never touches on any machine that sets XDG_CACHE_HOME
_REAL_CACHE_ROOT = Path(os.environ.get("XDG_CACHE_HOME") or _REAL_HOME / ".cache") / "engmem"


def _real_user_paths() -> dict[Path, bytes | None]:
    """Real files and directories a test could reach by mistake; snapshotted, never written."""
    import hashlib

    snapshot: dict[Path, bytes | None] = {}
    for path in (
        _REAL_HOME / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
        _REAL_HOME / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json",
        _REAL_HOME / ".claude" / "CLAUDE.md",
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
        _REAL_CACHE_ROOT,
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
    monkeypatch.delenv("ENGMEM_HOME", raising=False)

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


FIXTURES = Path(__file__).parent / "fixtures" / "sessions"

# the install/uninstall symmetry is itself under test, so these stay literals here rather than
# being imported from engmem.install, which would assert the product's values against themselves
INSTALLED_NAMES = ("engmem.md", "engmem.save.md", "engmem.save.quick.md")
INSTALLED_NAMES_COPILOT_IDE = (
    "engmem.prompt.md",
    "engmem.save.prompt.md",
    "engmem.save.quick.prompt.md",
)
TRIGGER_RULE = (
    'Before proposing a plan, run `engmem search "<key terms for the task>"`'
)

# a Windows-authored instructions file, as `(original bytes, its line ending)`: CRLF and a BOM
# are exactly what `read_text` rewrites silently, and install and uninstall must both hand the
# bytes back the way they found them — shared so the two halves of the round trip cannot drift
INSTRUCTIONS_BYTE_CASES = [
    pytest.param(b"# My rules\r\n- Prefer small commits.\r\n", b"\r\n", id="crlf"),
    pytest.param(b"\xef\xbb\xbf# My rules\n", b"\n", id="utf-8-bom"),
]


def fixture_docs():
    from engmem.spine import load_store

    return load_store(FIXTURES).docs


def write_file(dir_path: Path, name: str, text: str) -> Path:
    r"""Writes `text` verbatim: `write_text` translates every `\n` to `\r\n` on Windows, so a
    caller pinning a document's line endings was handed a document it never asked for."""
    path = dir_path / name
    path.write_bytes(text.encode("utf-8"))
    return path


def claude_desktop_config_path(home: Path) -> Path:
    """Hardcoding the macOS tree failed every claude-desktop test on Windows, where the product
    correctly uses %APPDATA%."""
    if sys.platform == "win32":
        return home / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
    return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"


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


# the two documents the MCP write tools move between; identical copies in two test
# modules would drift apart silently, and the transition between them is the thing under test
DRAFT_CONTENT = """---
id: 20260101-widget-cache
title: WidgetCache rollout
date: 2026-01-01
task_date: 2026-01-01
status: draft
superseded_by:
backfilled: false
tags: []
entities: []
related: []
covers_files: []
verified_at_commit:
capture_minutes:
---

## Pre-reg

Naive baseline: read the code and guess at the rollout shape.
pre-reg source: self (no sub-agent available)
"""

ACTIVE_CONTENT = """---
id: 20260101-widget-cache
title: WidgetCache rollout
date: 2026-01-01
task_date: 2026-01-01
status: active
superseded_by:
backfilled: false
tags: [cache]
entities: [WidgetCache, CacheWarmer]
related: []
covers_files: [WidgetCache.java]
verified_at_commit: abc1234
capture_minutes: 14
---

## Pre-reg

Naive baseline: read the code and guess at the rollout shape.
pre-reg source: self (no sub-agent available)

## Decision Log

Chose CacheWarmer over a lazy cache fill because cold start latency was too high.

## Landmines

CacheWarmer must run before WidgetCache accepts traffic or the first request hangs.

## Cold-start primer

WidgetCache serves cached widgets behind CacheWarmer, which pre-fills on boot.

## Reuse Log

Prior docs used: none.

## Search Trace

shell
"""


def run_tool(tool: Path, store: Path) -> subprocess.CompletedProcess[str]:
    """The Gate 1 tools are checked through their real command line, never by import."""
    return subprocess.run(
        [sys.executable, str(tool), "--store", str(store)],
        capture_output=True, text=True, timeout=30,
    )
