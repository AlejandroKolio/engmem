from __future__ import annotations

import sys
from pathlib import Path

import pytest


def redirect_home(monkeypatch, home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOMEDRIVE", home.drive or "")
    monkeypatch.setenv("HOMEPATH", str(home)[len(home.drive):])
    # Claude Desktop's config lives under %APPDATA% on Windows
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    # Codex reads $CODEX_HOME before ~/.codex; a developer who sets it would otherwise be written to
    monkeypatch.delenv("CODEX_HOME", raising=False)


# the install/uninstall symmetry is itself under test, so these stay literals here rather than
# being imported from engmem.install, which would assert the product's values against themselves
INSTALLED_NAMES = ("engmem.md", "engmem.save.md", "engmem.save.quick.md")
INSTALLED_NAMES_COPILOT_IDE = (
    "engmem.prompt.md",
    "engmem.save.prompt.md",
    "engmem.save.quick.prompt.md",
)
TRIGGER_RULE = (
    'Before proposing a plan, run `engmem search "<key terms for the task>" --session '
    "<draft-id>` (the draft `/engmem` just created; without it the search is unattributed)"
)
LEGACY_TRIGGER_RULE = 'Before proposing a plan, run `engmem search "<key terms for the task>"`'

# a Windows-authored instructions file, as `(original bytes, its line ending)`: CRLF and a BOM
# are exactly what `read_text` rewrites silently, and install and uninstall must both hand the
# bytes back the way they found them — shared so the two halves of the round trip cannot drift
INSTRUCTIONS_BYTE_CASES = [
    pytest.param(b"# My rules\r\n- Prefer small commits.\r\n", b"\r\n", id="crlf"),
    pytest.param(b"\xef\xbb\xbf# My rules\n", b"\n", id="utf-8-bom"),
]


def claude_desktop_config_path(home: Path) -> Path:
    """Hardcoding the macOS tree failed every claude-desktop test on Windows, where the product
    correctly uses %APPDATA%."""
    if sys.platform == "win32":
        return home / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
    return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"

