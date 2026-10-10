"""US-06: `engmem doctor` names the store, its source, the executables, sessions access and the
wiring for one client, says what to do about each failure, and changes nothing."""

from __future__ import annotations

import errno
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import (
    FIXTURES,
    LEGACY_TRIGGER_RULE,
    claude_desktop_config_path,
    redirect_home,
    requires_permission_enforcement,
    requires_posix_modes,
)

from engmem import __version__
from engmem.cli import main
from engmem.install import TUNNEL_BRIDGE_STATUS



@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    redirect_home(monkeypatch, tmp_path / "home")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    monkeypatch.chdir(project)
    return tmp_path / "home"


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


def _install(agent: str, store: Path, capsys) -> None:
    assert main(["install", "--agent", agent, "--store", str(store)]) == 0
    assert main(["store", "set", str(store)]) == 0
    capsys.readouterr()


def _doctor(capsys, *args: str) -> tuple[int, list[str]]:
    code = main(["doctor", *args])
    return code, capsys.readouterr().out.splitlines()


def _with(lines: list[str], status: str, subject: str) -> list[str]:
    return [line for line in lines if line.startswith(f"{status}: {subject}:")]


def _subjects(lines: list[str]) -> set[str]:
    return {line.split(": ", 2)[1] for line in lines[1:]}


def _desktop_entry(home: Path, command: str, store: Path | None) -> Path:
    path = claude_desktop_config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    args = ["-m", "engmem.cli", "mcp", *(["--store", str(store)] if store is not None else [])]
    path.write_text(json.dumps({"mcpServers": {"engmem": {"command": command, "args": args}}}),
                    encoding="utf-8")
    return path


def _fake_executable(path: Path, posix: str, windows: str) -> Path:
    """An sh script, or a `.bat` on Windows, where a script without an extension cannot run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        path = path.with_suffix(".bat")
        path.write_bytes(f"@echo off\r\n{windows}\r\n".encode())
        return path
    path.write_text(f"#!/bin/sh\n{posix}\n")
    path.chmod(0o755)
    return path


def _sleeping_child() -> str:
    """A grandchild that keeps the probe's output open after the wrapper itself is killed."""
    return f'"{sys.executable}" -c "import time; time.sleep(20)"'


def _with_docs(store: Path) -> None:
    for doc in FIXTURES.glob("*.md"):
        (store / "sessions" / doc.name).write_bytes(doc.read_bytes())


# --- AC-06.1: the facts for the chosen client -----------------------------------------------


def test_codex_report_names_store_source_executables_sessions_and_wiring(
    home, tmp_path, shell_path, capsys
):
    store = tmp_path / "notes"
    _install("codex", store, capsys)
    _with_docs(store)

    code, lines = _doctor(capsys, "--agent", "codex")

    assert code == 0, lines
    assert lines[0].startswith("engmem doctor --agent codex: 0 error(s)"), lines
    [store_line] = _with(lines, "ok", "store")
    assert str(store) in store_line and "saved choice" in store_line
    [sessions_line] = _with(lines, "ok", "sessions")
    assert str(store / "sessions") in sessions_line and "document(s)" in sessions_line
    assert _with(lines, "ok", "sessions write")
    [running] = _with(lines, "ok", "engmem")
    assert sys.executable in running and __version__ in running
    [shell] = _with(lines, "ok", "engmem command")
    assert __version__ in shell
    assert len(_with(lines, "ok", "skill")) == 3
    assert _with(lines, "ok", "trigger rule")
    [entry] = _with(lines, "ok", "mcp entry")
    assert str(store) in entry and "same store" in entry
    [command] = _with(lines, "ok", "mcp command")
    assert sys.executable in command and __version__ in command


EXPECTED_SUBJECTS = {
    "claude": {"template", "trigger rule", "engmem command", "sandbox"},
    "copilot-ide": {"template", "trigger rule", "engmem command", "sandbox"},
    "copilot-cli": {"skill", "engmem command", "sandbox"},
    "codex": {"skill", "trigger rule", "engmem command", "mcp entry", "mcp command", "sandbox"},
    "claude-desktop": {"mcp entry", "mcp command", "sandbox"},
    "chatgpt": {"tunnel", "public bridge"},
}


@pytest.mark.parametrize("agent", sorted(EXPECTED_SUBJECTS))
def test_each_client_gets_the_checks_its_wiring_has(home, tmp_path, shell_path, capsys, agent):
    store = tmp_path / "notes"
    _install(agent, store, capsys)

    code, lines = _doctor(capsys, "--agent", agent)

    common = {"store", "sessions", "sessions write", "engmem"}
    assert _subjects(lines) == common | EXPECTED_SUBJECTS[agent], lines
    assert not [line for line in lines if line.startswith("error:")], lines
    assert code == 0


def test_the_default_client_is_claude_like_install(home, tmp_path, shell_path, capsys):
    _install("claude", tmp_path / "notes", capsys)

    code, lines = _doctor(capsys)

    assert code == 0
    assert lines[0].startswith("engmem doctor --agent claude:")


def test_an_unknown_client_is_a_usage_error(home, capsys):
    code, lines = _doctor(capsys, "--agent", "emacs")

    assert code == 2
    assert lines[0].startswith("error: engmem doctor: unknown agent 'emacs'")


def test_local_with_a_home_scoped_client_is_a_usage_error(home, capsys):
    code, lines = _doctor(capsys, "--agent", "codex", "--local")

    assert code == 2 and "--local" in lines[0]


def test_an_empty_store_is_named_as_empty_memory(home, tmp_path, shell_path, capsys):
    _install("claude", tmp_path / "notes", capsys)

    code, lines = _doctor(capsys)

    [line] = _with(lines, "warning", "sessions")
    assert "no documents" in line
    assert code == 0


@pytest.mark.parametrize(
    ("stamp", "reason"),
    [("v0.0.1", "engmem 0.0.1"), (None, "no engmem version stamp")],
    ids=["stale", "unstamped"],
)
def test_a_template_another_version_installed_is_a_warning(
    home, tmp_path, shell_path, capsys, stamp, reason
):
    _install("claude", tmp_path / "notes", capsys)
    template = home / ".claude" / "commands" / "engmem.md"
    text = template.read_text(encoding="utf-8")
    current = f"v{__version__} -->"
    text = text.replace(current, f"{stamp} -->") if stamp else text.replace("engmem-template:", "x:")
    template.write_text(text, encoding="utf-8")

    code, lines = _doctor(capsys)

    [line] = _with(lines, "warning", "template")
    assert str(template) in line and reason in line and "engmem install --agent claude" in line
    assert code == 0


# --- AC-06.2: CLI and MCP on different stores ------------------------------------------------


@pytest.mark.parametrize("source", ["saved choice", "ENGMEM_HOME"])
def test_a_different_mcp_store_is_a_warning_naming_both_paths_and_sources(
    home, tmp_path, monkeypatch, capsys, source
):
    wired, chosen = tmp_path / "wired", tmp_path / "chosen"
    (chosen / "sessions").mkdir(parents=True)
    if source == "ENGMEM_HOME":
        monkeypatch.setenv("ENGMEM_HOME", str(chosen))
    else:
        assert main(["store", "set", str(chosen)]) == 0
    config = _desktop_entry(home, sys.executable, wired)
    capsys.readouterr()

    code, lines = _doctor(capsys, "--agent", "claude-desktop")

    [line] = _with(lines, "warning", "mcp entry")
    assert str(chosen) in line and source in line
    assert str(wired) in line and str(config) in line
    assert "engmem install --agent claude-desktop --store" in line
    assert "not broken by itself" in line
    assert not [l for l in lines if l.startswith("error:")], lines
    assert code == 0


def test_an_entry_without_a_store_resolves_at_launch_and_is_unverified(home, tmp_path, capsys):
    (tmp_path / "notes" / "sessions").mkdir(parents=True)
    main(["store", "set", str(tmp_path / "notes")])
    _desktop_entry(home, sys.executable, None)
    capsys.readouterr()

    _code, lines = _doctor(capsys, "--agent", "claude-desktop")

    [line] = _with(lines, "unverified", "mcp entry")
    assert "without --store" in line and str(tmp_path / "notes") in line


def test_an_override_that_hides_the_store_a_client_would_use_is_named(
    home, tmp_path, capsys
):
    saved, other = tmp_path / "saved", tmp_path / "other"
    for store in (saved, other):
        (store / "sessions").mkdir(parents=True)
    main(["store", "set", str(saved)])
    capsys.readouterr()

    _code, lines = _doctor(capsys, "--agent", "chatgpt", "--store", str(other))

    [line] = _with(lines, "warning", "store")
    assert str(saved) in line and "--store" in line


# --- AC-06.3: what is missing or denied, by path, error type and action ---------------------


def test_a_missing_mcp_command_names_the_path_the_error_and_the_fix(home, tmp_path, capsys):
    (tmp_path / "notes" / "sessions").mkdir(parents=True)
    main(["store", "set", str(tmp_path / "notes")])
    gone = tmp_path / "gone" / "python"
    _desktop_entry(home, str(gone), tmp_path / "notes")
    capsys.readouterr()

    code, lines = _doctor(capsys, "--agent", "claude-desktop")

    [line] = _with(lines, "error", "mcp command")
    assert str(gone) in line and "FileNotFoundError" in line
    assert "engmem install --agent claude-desktop" in line
    assert code == 1
    assert lines[0].startswith("engmem doctor --agent claude-desktop: 1 error(s)")


@requires_posix_modes
def test_an_mcp_command_without_the_execute_bit_is_named(home, tmp_path, capsys):
    (tmp_path / "notes" / "sessions").mkdir(parents=True)
    main(["store", "set", str(tmp_path / "notes")])
    command = _fake_executable(tmp_path / "py" / "python", "exit 0", "exit /b 0")
    command.chmod(0o644)
    _desktop_entry(home, str(command), tmp_path / "notes")
    capsys.readouterr()

    code, lines = _doctor(capsys, "--agent", "claude-desktop")

    [line] = _with(lines, "error", "mcp command")
    assert str(command) in line and "not executable" in line and "PermissionError" in line
    assert code == 1


def test_an_mcp_python_that_cannot_import_engmem_is_named(home, tmp_path, capsys):
    (tmp_path / "notes" / "sessions").mkdir(parents=True)
    main(["store", "set", str(tmp_path / "notes")])
    command = _fake_executable(
        tmp_path / "py" / "python",
        "echo \"No module named engmem\" >&2\nexit 1",
        "echo No module named engmem 1>&2\r\nexit /b 1",
    )
    _desktop_entry(home, str(command), tmp_path / "notes")
    capsys.readouterr()

    code, lines = _doctor(capsys, "--agent", "claude-desktop")

    [line] = _with(lines, "error", "mcp command")
    assert "exited 1" in line and "No module named engmem" in line
    assert "engmem install --agent claude-desktop" in line
    assert code == 1


def test_an_mcp_command_that_hangs_is_stopped_and_named(home, tmp_path, monkeypatch, capsys):
    (tmp_path / "notes" / "sessions").mkdir(parents=True)
    main(["store", "set", str(tmp_path / "notes")])
    command = _fake_executable(tmp_path / "py" / "python", _sleeping_child(), _sleeping_child())
    _desktop_entry(home, str(command), tmp_path / "notes")
    monkeypatch.setattr("engmem.doctor._PROBE_SECONDS", 0.5)
    capsys.readouterr()

    started = time.monotonic()
    code, lines = _doctor(capsys, "--agent", "claude-desktop")

    assert time.monotonic() - started < 10
    [line] = _with(lines, "error", "mcp command")
    assert "TimeoutExpired" in line and str(command) in line
    assert code == 1


def test_an_mcp_command_on_another_engmem_version_is_a_warning(home, tmp_path, capsys):
    (tmp_path / "notes" / "sessions").mkdir(parents=True)
    main(["store", "set", str(tmp_path / "notes")])
    command = _fake_executable(tmp_path / "py" / "python", "echo 'engmem 0.0.1'", "echo engmem 0.0.1")
    _desktop_entry(home, str(command), tmp_path / "notes")
    capsys.readouterr()

    code, lines = _doctor(capsys, "--agent", "claude-desktop")

    [line] = _with(lines, "warning", "mcp command")
    assert "0.0.1" in line and __version__ in line
    assert code == 0


@pytest.mark.parametrize("pythonpath", [None, "."], ids=["cwd", "relative-pythonpath"])
def test_no_probe_imports_an_engmem_package_from_the_current_directory(
    home, tmp_path, shell_path, monkeypatch, capsys, pythonpath
):
    """`python -m` puts the cwd first on sys.path; a project's own `engmem/` must not run."""
    _install("codex", tmp_path / "notes", capsys)
    marker = tmp_path / "hostile-ran"
    hostile = Path.cwd() / "engmem"
    hostile.mkdir()
    (hostile / "__init__.py").write_text(f"open({str(marker)!r}, 'w').close()\n", encoding="utf-8")
    (hostile / "cli.py").write_text("print('engmem 9.9.9')\n", encoding="utf-8")
    if pythonpath is not None:
        monkeypatch.setenv("PYTHONPATH", pythonpath)

    _code, lines = _doctor(capsys, "--agent", "codex")

    [command] = _with(lines, "ok", "mcp command")
    assert __version__ in command
    assert _with(lines, "ok", "engmem command")
    assert not marker.exists()


def test_an_engmem_command_in_the_current_directory_is_not_taken_from_path(
    home, tmp_path, monkeypatch, capsys
):
    """Windows' lookup tries the current directory before PATH; a relative PATH entry is the
    same lookup on every platform."""
    _install("claude", tmp_path / "notes", capsys)
    marker = tmp_path / "hostile-ran"
    _fake_executable(
        Path.cwd() / "engmem",
        f"touch '{marker}'\necho 'engmem 9.9.9'",
        f'type nul > "{marker}"\r\necho engmem 9.9.9',
    )
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", os.pathsep.join([str(empty), ".", ""]))

    code, lines = _doctor(capsys)

    [line] = _with(lines, "error", "engmem command")
    assert "is not on this shell's PATH" in line, line
    assert not marker.exists()
    assert code == 1


def test_engmem_missing_from_the_shell_path_is_named(home, tmp_path, monkeypatch, capsys):
    _install("claude", tmp_path / "notes", capsys)
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    code, lines = _doctor(capsys)

    [line] = _with(lines, "error", "engmem command")
    assert "PATH" in line and "FileNotFoundError" in line and "install" in line
    assert code == 1


def test_a_missing_store_names_the_path_the_error_and_the_fix(home, tmp_path, shell_path, capsys):
    _install("claude", tmp_path / "notes", capsys)
    missing = tmp_path / "missing"
    main(["store", "set", str(missing)])
    capsys.readouterr()

    code, lines = _doctor(capsys)

    [line] = _with(lines, "error", "sessions")
    assert str(missing / "sessions") in line and "FileNotFoundError" in line
    assert "engmem install --agent claude" in line
    assert not _with(lines, "ok", "sessions write") and not _with(lines, "error", "sessions write")
    assert not missing.exists()
    assert code == 1


@requires_permission_enforcement
def test_a_sessions_directory_denying_writes_is_named(home, tmp_path, shell_path, capsys):
    store = tmp_path / "notes"
    _install("claude", store, capsys)
    sessions = store / "sessions"
    sessions.chmod(0o555)
    try:
        code, lines = _doctor(capsys)
        left = sorted(p.name for p in sessions.iterdir())
    finally:
        sessions.chmod(0o755)

    [line] = _with(lines, "error", "sessions write")
    assert str(sessions) in line and "PermissionError" in line and "write access" in line
    assert left == []
    assert code == 1


@requires_permission_enforcement
def test_a_sessions_directory_denying_reads_is_named(home, tmp_path, shell_path, capsys):
    store = tmp_path / "notes"
    _install("claude", store, capsys)
    sessions = store / "sessions"
    sessions.chmod(0o000)
    try:
        code, lines = _doctor(capsys)
    finally:
        sessions.chmod(0o755)

    [line] = _with(lines, "error", "sessions")
    assert str(sessions) in line and "PermissionError" in line and "read access" in line
    assert code == 1


def test_a_broken_saved_choice_is_an_error_and_the_rest_is_still_checked(
    home, tmp_path, shell_path, capsys
):
    _install("claude", tmp_path / "notes", capsys)
    setting = Path(os.environ["XDG_CONFIG_HOME"]) / "engmem" / "store"
    setting.write_bytes(b"relative/path\n")

    code, lines = _doctor(capsys)

    [line] = _with(lines, "error", "store")
    assert str(setting) in line
    assert len(_with(lines, "ok", "template")) == 3
    assert code == 1


def test_templates_that_were_never_installed_are_named_with_the_install_command(
    home, tmp_path, shell_path, capsys
):
    (tmp_path / "notes" / "sessions").mkdir(parents=True)
    main(["store", "set", str(tmp_path / "notes")])
    capsys.readouterr()

    code, lines = _doctor(capsys)

    missing = _with(lines, "error", "template")
    assert len(missing) == 3
    assert all("FileNotFoundError" in l and "engmem install --agent claude" in l for l in missing)
    [rule] = _with(lines, "warning", "trigger rule")
    assert "engmem install --agent claude" in rule
    assert code == 1


@pytest.mark.parametrize("content", [None, "{not json"], ids=["absent", "unparseable"])
def test_a_missing_or_unreadable_mcp_entry_is_an_error(home, tmp_path, capsys, content):
    (tmp_path / "notes" / "sessions").mkdir(parents=True)
    main(["store", "set", str(tmp_path / "notes")])
    config = claude_desktop_config_path(home)
    if content is not None:
        config.parent.mkdir(parents=True)
        config.write_text(content, encoding="utf-8")
    capsys.readouterr()

    code, lines = _doctor(capsys, "--agent", "claude-desktop")

    [line] = _with(lines, "error", "mcp entry")
    assert str(config) in line
    if content is None:
        assert "engmem install --agent claude-desktop" in line
    assert code == 1


@pytest.mark.parametrize(
    ("rule", "status"),
    [(LEGACY_TRIGGER_RULE, "warning"), ("Always run `engmem search` first.", "ok")],
    ids=["earlier-wording", "own-wording"],
)
def test_the_trigger_rule_wording_decides_its_line(home, tmp_path, shell_path, capsys, rule, status):
    _install("claude", tmp_path / "notes", capsys)
    (home / ".claude" / "CLAUDE.md").write_text(f"# rules\n{rule}\n", encoding="utf-8")

    _code, lines = _doctor(capsys)

    [line] = [l for l in lines if ": trigger rule:" in l]
    assert line.startswith(f"{status}: trigger rule:"), line


def test_an_unreadable_instructions_file_is_an_error(home, tmp_path, shell_path, capsys):
    _install("claude", tmp_path / "notes", capsys)
    rules = home / ".claude" / "CLAUDE.md"
    rules.unlink()
    rules.mkdir()

    code, lines = _doctor(capsys)

    [line] = _with(lines, "error", "trigger rule")
    assert str(rules) in line
    assert code == 1


def test_a_codex_override_file_that_hides_the_rule_is_named(home, tmp_path, shell_path, capsys):
    _install("codex", tmp_path / "notes", capsys)
    override = home / ".codex" / "AGENTS.override.md"
    override.write_text("# mine\n", encoding="utf-8")

    _code, lines = _doctor(capsys, "--agent", "codex")

    assert any(str(override) in line for line in _with(lines, "warning", "trigger rule")), lines


def test_a_local_install_is_checked_where_it_was_written(home, tmp_path, shell_path, capsys):
    assert main(["install", "--local", "--store", str(tmp_path / "notes")]) == 0
    main(["store", "set", str(tmp_path / "notes")])
    capsys.readouterr()

    code, lines = _doctor(capsys, "--local")

    templates = _with(lines, "ok", "template")
    assert len(templates) == 3 and all(str(Path.cwd() / ".claude") in l for l in templates)
    assert code == 0


@pytest.mark.parametrize("on_path", [True, False], ids=["on-path", "not-on-path"])
def test_a_bare_mcp_command_depends_on_the_clients_path(
    home, tmp_path, shell_path, monkeypatch, capsys, on_path
):
    (tmp_path / "notes" / "sessions").mkdir(parents=True)
    main(["store", "set", str(tmp_path / "notes")])
    _desktop_entry(home, "engmem", tmp_path / "notes")
    if not on_path:
        monkeypatch.setenv("PATH", str(tmp_path / "project"))
    capsys.readouterr()

    code, lines = _doctor(capsys, "--agent", "claude-desktop")

    status = "unverified" if on_path else "error"
    [line] = _with(lines, status, "mcp command")
    assert "PATH" in line and "engmem install --agent claude-desktop" in line
    assert code == (0 if on_path else 1)


def test_an_mcp_entry_of_another_shape_is_not_run(home, tmp_path, monkeypatch, capsys):
    (tmp_path / "notes" / "sessions").mkdir(parents=True)
    main(["store", "set", str(tmp_path / "notes")])
    path = claude_desktop_config_path(home)
    path.parent.mkdir(parents=True)
    args = ["-X", "utf8", "-m", "engmem.cli", "mcp", "--store", str(tmp_path / "notes")]
    path.write_text(json.dumps({"mcpServers": {"engmem": {"command": sys.executable, "args": args}}}),
                    encoding="utf-8")
    ran = []
    monkeypatch.setattr("engmem.doctor.subprocess.run", lambda *a, **k: ran.append(a))
    capsys.readouterr()

    _code, lines = _doctor(capsys, "--agent", "claude-desktop")

    [line] = _with(lines, "unverified", "mcp command")
    assert "not run" in line
    assert ran == []


@pytest.mark.parametrize(
    ("command", "status", "words"),
    [("bin\x00python", "error", "NUL byte"), (os.path.join("bin", "python"), "warning", "relative path")],
    ids=["nul", "relative"],
)
def test_an_mcp_command_that_names_no_fixed_file_is_named(
    home, tmp_path, capsys, command, status, words
):
    (tmp_path / "notes" / "sessions").mkdir(parents=True)
    main(["store", "set", str(tmp_path / "notes")])
    _desktop_entry(home, command, tmp_path / "notes")
    capsys.readouterr()

    _code, lines = _doctor(capsys, "--agent", "claude-desktop")

    [line] = _with(lines, status, "mcp command")
    assert words in line and "\x00" not in line


def test_an_mcp_entry_without_a_command_is_an_error(home, tmp_path, capsys):
    (tmp_path / "notes" / "sessions").mkdir(parents=True)
    main(["store", "set", str(tmp_path / "notes")])
    path = claude_desktop_config_path(home)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"mcpServers": {"engmem": {"args": ["mcp"]}}}), encoding="utf-8")
    capsys.readouterr()

    code, lines = _doctor(capsys, "--agent", "claude-desktop")

    [line] = _with(lines, "error", "mcp command")
    assert "no command" in line and str(path) in line
    assert code == 1


def _nul_config(home: Path, tmp_path: Path, where: str, capsys) -> Path:
    bad = str(tmp_path / "a\x00b")
    if where == "writable-root":
        _install("codex", tmp_path / "notes", capsys)
        roots = json.dumps([bad])
        _codex_config(home, tail=f"\n[sandbox_workspace_write]\nwritable_roots = {roots}\n")
        return home / ".codex" / "config.toml"
    (tmp_path / "notes" / "sessions").mkdir(parents=True)
    main(["store", "set", str(tmp_path / "notes")])
    args = ["-m", "engmem.cli", "mcp", "--store", bad]
    if where == "desktop-store":
        path = claude_desktop_config_path(home)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"mcpServers": {"engmem": {"command": sys.executable, "args": args}}}),
                        encoding="utf-8")
        capsys.readouterr()
        return path
    path = home / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    command = json.dumps(sys.executable)
    path.write_text(f"[mcp_servers.engmem]\ncommand = {command}\nargs = {json.dumps(args)}\n",
                    encoding="utf-8")
    capsys.readouterr()
    return path


@pytest.mark.parametrize("where", ["writable-root", "codex-store", "desktop-store"])
def test_a_nul_byte_in_a_config_path_is_a_finding_not_a_traceback(
    home, tmp_path, shell_path, capsys, where
):
    config = _nul_config(home, tmp_path, where, capsys)
    agent = "claude-desktop" if where == "desktop-store" else "codex"

    _code, lines = _doctor(capsys, "--agent", agent)

    assert lines[0].startswith(f"engmem doctor --agent {agent}:")
    [line] = [l for l in lines if str(config) in l and "\\x00" in l]
    assert line.startswith(("error:", "warning:")), line
    assert "\x00" not in "\n".join(lines)


@pytest.mark.parametrize("where", ["codex-store", "desktop-store"])
def test_store_show_names_a_nul_byte_in_an_mcp_store_instead_of_crashing(
    home, tmp_path, capsys, where
):
    config = _nul_config(home, tmp_path, where, capsys)

    assert main(["store", "show"]) == 0

    out = capsys.readouterr().out
    assert any(str(config) in l and "\\x00" in l for l in out.splitlines()), out
    assert "\x00" not in out


@pytest.fixture
def realpath_as_on_windows(monkeypatch):
    """Windows' non-strict `realpath` returns a path with a NUL byte unchanged instead of raising
    (gh-106242), so a check that waits for the `ValueError` never fires there."""
    monkeypatch.setattr(os.path, "realpath", lambda path, *args, **kwargs: os.fspath(path))


def test_a_nul_store_is_invalid_even_where_realpath_does_not_raise(tmp_path, realpath_as_on_windows):
    from engmem.install import McpWiring, WiringVerdict, wiring_verdict

    wiring = McpWiring("codex", tmp_path / "config.toml", tmp_path / "a\x00b", managed=True)

    assert wiring_verdict(wiring, tmp_path / "notes") is WiringVerdict.INVALID


def test_a_nul_writable_root_is_unusable_even_where_realpath_does_not_raise(
    tmp_path, realpath_as_on_windows
):
    from engmem.doctor import _real

    assert _real(str(tmp_path / "a\x00b")) is None
    assert _real(str(tmp_path)) == str(tmp_path)


def _executable_file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    path.chmod(0o755)
    return path


@pytest.mark.parametrize("name", ["tool.EXE", "tool.exe"])
def test_on_windows_a_name_that_carries_its_extension_is_found_as_given(
    tmp_path, monkeypatch, name
):
    from engmem.doctor import _on_path

    bin_dir = tmp_path / "bin"
    found = _executable_file(bin_dir / name)
    monkeypatch.setattr("engmem.doctor.sys.platform", "win32")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    monkeypatch.setenv("PATH", str(bin_dir))

    assert _on_path(name) == str(found)


def test_on_windows_a_bare_name_still_gets_each_pathext_suffix(tmp_path, monkeypatch):
    from engmem.doctor import _on_path

    bin_dir = tmp_path / "bin"
    found = _executable_file(bin_dir / "tool.BAT")
    monkeypatch.setattr("engmem.doctor.sys.platform", "win32")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    monkeypatch.setenv("PATH", str(bin_dir))

    assert _on_path("tool") == str(found)


def test_a_bare_mcp_command_with_its_extension_is_found_on_path(home, tmp_path, monkeypatch, capsys):
    (tmp_path / "notes" / "sessions").mkdir(parents=True)
    main(["store", "set", str(tmp_path / "notes")])
    bin_dir = tmp_path / "bin"
    _executable_file(bin_dir / "tool.bat")
    monkeypatch.setenv("PATH", str(bin_dir))
    _desktop_entry(home, "tool.bat", tmp_path / "notes")
    capsys.readouterr()

    _code, lines = _doctor(capsys, "--agent", "claude-desktop")

    [line] = _with(lines, "unverified", "mcp command")
    assert str(bin_dir / "tool.bat") in line


# --- AC-06.4: diagnostics change nothing ----------------------------------------------------


def _snapshot(root: Path) -> dict[str, bytes | tuple[str, ...]]:
    snapshot: dict[str, bytes | tuple[str, ...]] = {}
    for path in sorted(root.rglob("*")):
        key = str(path.relative_to(root))
        if path.is_dir():
            snapshot[key] = tuple(sorted(child.name for child in path.iterdir()))
        else:
            snapshot[key] = path.read_bytes()
    return snapshot


@pytest.mark.parametrize("agent", ["codex", "claude-desktop", "claude"])
def test_doctor_leaves_configs_templates_and_the_store_byte_identical(
    home, tmp_path, shell_path, capsys, agent
):
    store = tmp_path / "notes"
    _install(agent, store, capsys)
    _with_docs(store)
    config_root = Path(os.environ["XDG_CONFIG_HOME"])
    before = (_snapshot(tmp_path), _snapshot(config_root))

    _doctor(capsys, "--agent", agent)

    assert (_snapshot(tmp_path), _snapshot(config_root)) == before


def test_the_version_probes_write_no_bytecode_into_the_interpreter(
    home, tmp_path, shell_path, monkeypatch, capsys
):
    """A probe that imports engmem may otherwise drop `__pycache__` into the install it checks."""
    _install("codex", tmp_path / "notes", capsys)
    real_run = subprocess.run
    calls = []

    def recording_run(*args, **kwargs):
        calls.append(kwargs)
        return real_run(*args, **kwargs)

    monkeypatch.setattr("engmem.doctor.subprocess.run", recording_run)
    _doctor(capsys, "--agent", "codex")

    assert len(calls) == 2
    for kwargs in calls:
        assert kwargs["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
        assert kwargs["env"]["PYTHONSAFEPATH"] == "1"
        assert Path(kwargs["cwd"]) == Path(Path.cwd().anchor)


def test_a_probe_whose_write_fails_is_named_and_leaves_no_file(
    home, tmp_path, shell_path, monkeypatch, capsys
):
    store = tmp_path / "notes"
    _install("claude", store, capsys)

    def failing_fsync(fd):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(os, "fsync", failing_fsync)
    code, lines = _doctor(capsys)

    [line] = _with(lines, "error", "sessions write")
    assert "OSError" in line and str(store / "sessions") in line
    assert list((store / "sessions").iterdir()) == []
    assert code == 1


def test_an_interrupted_probe_leaves_no_file(home, tmp_path, shell_path, monkeypatch, capsys):
    store = tmp_path / "notes"
    _install("claude", store, capsys)

    def interrupted(fd):
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "fsync", interrupted)
    with pytest.raises(KeyboardInterrupt):
        main(["doctor"])

    assert list((store / "sessions").iterdir()) == []


def test_a_probe_interrupted_before_its_removal_is_still_removed(
    home, tmp_path, shell_path, monkeypatch, capsys
):
    store = tmp_path / "notes"
    _install("claude", store, capsys)
    real_unlink = Path.unlink
    interrupted = []

    def interrupting_unlink(self, *args, **kwargs):
        if self.parent == store / "sessions" and not interrupted:
            interrupted.append(self)
            raise KeyboardInterrupt
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", interrupting_unlink)
    with pytest.raises(KeyboardInterrupt):
        main(["doctor"])
    monkeypatch.undo()

    assert interrupted
    assert list((store / "sessions").iterdir()) == []


def test_a_probe_that_cannot_be_removed_is_named_for_removal_by_hand(
    home, tmp_path, shell_path, monkeypatch, capsys
):
    store = tmp_path / "notes"
    _install("claude", store, capsys)
    real_unlink = Path.unlink

    def refusing_unlink(self, *args, **kwargs):
        if self.parent == store / "sessions":
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refusing_unlink)
    code, lines = _doctor(capsys)
    monkeypatch.undo()

    [line] = _with(lines, "error", "sessions write")
    [left] = list((store / "sessions").iterdir())
    assert str(left) in line and "delete it by hand" in line
    assert code == 1


# --- AC-06.5: host success does not vouch for a client's sandbox ----------------------------


def _codex_config(home: Path, top: str = "", tail: str = "") -> None:
    config = home / ".codex" / "config.toml"
    config.write_text(top + config.read_text(encoding="utf-8") + tail, encoding="utf-8")


def test_a_store_outside_codex_writable_roots_is_named_with_the_setting(
    home, tmp_path, shell_path, capsys
):
    store = tmp_path / "notes"
    _install("codex", store, capsys)

    _code, lines = _doctor(capsys, "--agent", "codex")

    assert _with(lines, "ok", "sessions write")
    [line] = _with(lines, "warning", "sandbox")
    assert "writable_roots" in line and str(home / ".codex" / "config.toml") in line
    assert not _with(lines, "ok", "sandbox")


def test_a_store_inside_codex_writable_roots_is_still_unverified(
    home, tmp_path, shell_path, capsys
):
    store = tmp_path / "notes"
    _install("codex", store, capsys)
    roots = json.dumps([str(tmp_path)])
    _codex_config(home, tail=f"\n[sandbox_workspace_write]\nwritable_roots = {roots}\n")

    _code, lines = _doctor(capsys, "--agent", "codex")

    [line] = _with(lines, "unverified", "sandbox")
    assert "writable_roots" in line and "not verified" in line
    assert not _with(lines, "warning", "sandbox") and not _with(lines, "ok", "sandbox")


@pytest.mark.parametrize(
    ("mode", "status"), [("danger-full-access", "unverified"), ("read-only", "warning")]
)
def test_codex_sandbox_mode_decides_the_sandbox_line(
    home, tmp_path, shell_path, capsys, mode, status
):
    _install("codex", tmp_path / "notes", capsys)
    _codex_config(home, top=f"sandbox_mode = {json.dumps(mode)}\n")

    _code, lines = _doctor(capsys, "--agent", "codex")

    [line] = [l for l in lines if ": sandbox:" in l]
    assert line.startswith(f"{status}: sandbox:") and mode in line


@pytest.mark.parametrize("agent", sorted(EXPECTED_SUBJECTS))
def test_no_client_is_ever_reported_ok_from_inside_its_sandbox(
    home, tmp_path, shell_path, capsys, agent
):
    _install(agent, tmp_path / "notes", capsys)

    _code, lines = _doctor(capsys, "--agent", agent)

    assert _with(lines, "ok", "sessions write")
    [line] = [l for l in lines if ": sandbox:" in l or ": tunnel:" in l]
    assert line.startswith(("unverified:", "warning:")), line


# --- US-19: ChatGPT's bridge is named, never called protected --------------------------------


def _fake_tunnel_client(bin_dir: Path, ran: Path) -> Path:
    """An executable `tunnel-client` that leaves `ran` behind if anything runs it."""
    if sys.platform == "win32":
        tool = bin_dir / "tunnel-client.bat"
        tool.write_bytes(f'@echo ran> "{ran}"\r\n'.encode())
    else:
        tool = bin_dir / "tunnel-client"
        tool.write_text(f"#!/bin/sh\necho ran > {shlex.quote(str(ran))}\n")
        tool.chmod(0o755)
    return tool


@pytest.mark.parametrize("present", [True, False], ids=["on-path", "missing"])
def test_chatgpt_bridge_lines_name_what_was_seen_and_claim_no_protection(
    home, tmp_path, monkeypatch, capsys, present
):
    _install("chatgpt", tmp_path / "notes", capsys)
    ran = tmp_path / "ran"
    bin_dir = tmp_path / "tools"
    bin_dir.mkdir()
    tool = _fake_tunnel_client(bin_dir, ran) if present else None
    monkeypatch.setenv("PATH", str(bin_dir))

    code, lines = _doctor(capsys, "--agent", "chatgpt")

    [tunnel] = [line for line in lines if ": tunnel:" in line]
    assert tunnel.startswith(f"unverified: tunnel: {TUNNEL_BRIDGE_STATUS}. "), tunnel
    assert "does not read its profile" in tunnel
    if present:
        # casefolded: Windows' lookup spells the extension the way PATHEXT does
        assert f"is {tool} on this shell's PATH (not run)".casefold() in tunnel.casefold()
    else:
        assert "is not on this shell's PATH" in tunnel
    [public] = _with(lines, "unverified", "public bridge")
    assert "is not protected" in public and "only refuses writes" in public
    assert not [line for line in lines if re.search(r"(?<!not )\bprotected\b", line, re.I)], lines
    assert not ran.exists(), "doctor ran tunnel-client"
    assert code == 0


# --- the report is stdout's ----------------------------------------------------------------


def test_the_report_is_on_stdout_and_stderr_stays_quiet(home, tmp_path, shell_path, capsys):
    _install("claude", tmp_path / "notes", capsys)

    main(["doctor"])

    captured = capsys.readouterr()
    assert captured.out.startswith("engmem doctor --agent claude:")
    assert captured.err == ""
