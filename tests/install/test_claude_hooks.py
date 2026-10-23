"""US-29: `install --agent claude --hooks` adds one SessionStart hook to Claude Code's settings and
uninstall removes exactly it (contracts/install.md, "The SessionStart hook")."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from support.git import git, requires_git
from support.platform import requires_symlinks

from engmem.claude_hooks import HOOK_COMMAND
from engmem.cli import main

ENTRY = {"hooks": [{"type": "command", "command": HOOK_COMMAND, "timeout": 10}]}
LOOKALIKE = "/opt/tools/bin/engmem draft status --hook"


def _run(capsys, *args: str) -> tuple[int, str]:
    code = main([*args])
    out, err = capsys.readouterr()
    return code, out + err


def _install(capsys, store: Path, *extra: str) -> tuple[int, str]:
    return _run(capsys, "install", "--agent", "claude", "--hooks", *extra, "--store", str(store))


def _uninstall(capsys, store: Path, *extra: str) -> tuple[int, str]:
    return _run(capsys, "uninstall", "--agent", "claude", *extra, "--store", str(store))


def _settings(home: Path) -> Path:
    return home / ".claude" / "settings.json"


def _write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def home(env) -> Path:
    return env[0]


def test_install_writes_exactly_the_entry_and_a_second_install_changes_nothing(home, store, capsys):
    code, out = _install(capsys, store)

    assert code == 0, out
    assert _read(_settings(home)) == {"hooks": {"SessionStart": [ENTRY]}}
    assert f"SessionStart hook added to {_settings(home)}" in out
    before = _settings(home).read_bytes()

    code, out = _install(capsys, store)

    assert code == 0, out
    assert "SessionStart hook already present" in out
    assert _settings(home).read_bytes() == before


def test_install_without_hooks_writes_no_settings(home, store, capsys):
    assert _run(capsys, "install", "--agent", "claude", "--store", str(store))[0] == 0

    assert not _settings(home).exists()


def test_other_settings_and_hooks_are_kept(home, store, capsys):
    other = {"matcher": "startup", "hooks": [{"type": "command", "command": "echo hi"}]}
    _write(_settings(home), {"model": "x", "hooks": {"SessionStart": [other], "Stop": []}})

    assert _install(capsys, store)[0] == 0

    assert _read(_settings(home)) == {
        "model": "x", "hooks": {"SessionStart": [other, ENTRY], "Stop": []}
    }


def test_uninstall_removes_only_engmems_handler_and_the_containers_it_empties(home, store, capsys):
    # another agent's files make uninstall nudge when it finds nothing for claude
    assert _run(capsys, "install", "--agent", "copilot-cli", "--store", str(store))[0] == 0
    mixed = {"matcher": "startup", "hooks": [ENTRY["hooks"][0], {"type": "command", "command": "a"}]}
    _write(_settings(home), {"model": "x", "hooks": {"SessionStart": [mixed, ENTRY]}})

    code, out = _uninstall(capsys, store)

    assert code == 0, out
    assert "2 hook(s) removed" in out
    assert "no claude files were found" not in out
    assert _read(_settings(home)) == {
        "model": "x",
        "hooks": {"SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "a"}]}]},
    }
    _write(_settings(home), {"model": "x", "hooks": {"SessionStart": [ENTRY]}})

    assert _uninstall(capsys, store)[0] == 0

    assert _read(_settings(home)) == {"model": "x"}


@pytest.mark.parametrize(
    "content",
    [None, b"{not json", b"\xff\xfe not utf-8"],
    ids=["absent", "invalid-json", "not-utf-8"],
)
def test_uninstall_ignores_a_settings_file_with_no_entry(home, store, capsys, content):
    if content is not None:
        _settings(home).parent.mkdir(parents=True)
        _settings(home).write_bytes(content)

    code, out = _uninstall(capsys, store)

    assert code == 0, out
    assert "incomplete" not in out
    assert (_settings(home).read_bytes() if content is not None else None) == content


def test_a_lookalike_hook_is_left_alone_and_named(home, store, capsys):
    _write(_settings(home), {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": LOOKALIKE}]}]}})
    original = _settings(home).read_bytes()

    code, out = _uninstall(capsys, store)
    assert code == 0, out
    assert f"has a hook running `{LOOKALIKE}`" in out
    assert _settings(home).read_bytes() == original

    code, out = _install(capsys, store)
    assert code == 0, out
    assert f"has a hook running `{LOOKALIKE}`" in out
    assert _read(_settings(home))["hooks"]["SessionStart"][0]["hooks"][0]["command"] == LOOKALIKE


@pytest.mark.parametrize(
    ("content", "named"),
    [
        (b"{not json", "is not valid JSON"),
        (b"[1]", "does not contain a JSON object"),
        (b'{"hooks": "x"}', "`hooks` key that is not a JSON object"),
        (b'{"hooks": {"SessionStart": {}}}', "`hooks.SessionStart` that is not a JSON array"),
        (b'{"hooks": {"SessionStart": "x"}}', "`hooks.SessionStart` that is not a JSON array"),
        (b'{"hooks": {"SessionStart": [{"hooks": "x"}]}}', "not an object with a `hooks` array"),
    ],
)
def test_a_settings_file_of_unknown_shape_is_refused_and_nothing_is_installed(
    home, store, capsys, content, named
):
    _settings(home).parent.mkdir(parents=True)
    _settings(home).write_bytes(content)

    code, out = _install(capsys, store)

    assert code == 2
    assert named in out
    assert _settings(home).read_bytes() == content
    assert not (home / ".claude" / "commands").exists()
    assert not (home / ".claude" / "CLAUDE.md").exists()


def test_hooks_is_refused_for_another_agent(home, store, capsys):
    code, out = _run(capsys, "install", "--agent", "codex", "--hooks", "--store", str(store))

    assert code == 2
    assert "for --agent claude only" in out
    assert not (home / ".codex").exists()


@requires_symlinks
def test_a_linked_settings_file_is_written_through(home, store, tmp_path, capsys):
    target = tmp_path / "dotfiles" / "settings.json"
    _write(target, {"model": "x"})
    _settings(home).parent.mkdir(parents=True)
    _settings(home).symlink_to(target)

    assert _install(capsys, store)[0] == 0

    assert _settings(home).is_symlink()
    assert _read(target) == {"model": "x", "hooks": {"SessionStart": [ENTRY]}}


@requires_git
@pytest.mark.usefixtures("hermetic_git")
class TestLocal:
    """`--local` writes the personal `.claude/settings.local.json` in a real repository; the
    hermetic git config and the redirected XDG_CONFIG_HOME keep the developer's own global ignore
    file from deciding the note."""

    @pytest.fixture
    def project(self, env) -> Path:
        project = env[1]
        git(project, "init", "-q")
        return project

    def test_local_writes_the_personal_file_never_the_committed_one(self, project, store, capsys):
        code, out = _install(capsys, store, "--local")

        assert code == 0, out
        assert _read(project / ".claude" / "settings.local.json") == {"hooks": {"SessionStart": [ENTRY]}}
        assert not (project / ".claude" / "settings.json").exists()
        assert "is not git-ignored" in out

    def test_an_ignored_local_file_draws_no_note(self, project, store, capsys):
        (project / ".gitignore").write_text(".claude/settings.local.json\n", encoding="utf-8")

        code, out = _install(capsys, store, "--local")

        assert code == 0, out
        assert "git-ignored" not in out

    def test_a_git_that_cannot_answer_is_its_own_note(
        self, env, store, tmp_path, monkeypatch, capsys
    ):
        project = env[1]
        (project / ".git").mkdir()
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))

        code, out = _install(capsys, store, "--local")

        assert code == 0, out
        local = project / ".claude" / "settings.local.json"
        assert f"note: could not check whether {local} is git-ignored" in out.splitlines()

    def test_no_git_to_ask_is_its_own_note(self, project, store, tmp_path, monkeypatch, capsys):
        git(store, "init", "-q")
        empty = tmp_path / "empty-path"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))

        code, out = _install(capsys, store, "--local")

        assert code == 0, out
        assert _read(project / ".claude" / "settings.local.json") == {"hooks": {"SessionStart": [ENTRY]}}
        local = project / ".claude" / "settings.local.json"
        assert f"note: could not check whether {local} is git-ignored" in out.splitlines()
        assert "is not git-ignored" not in out


def test_uninstall_reports_a_broken_settings_file_that_holds_the_entry(home, store, capsys):
    _settings(home).parent.mkdir(parents=True)
    content = f'{{"hooks": {{"SessionStart": [{{"command": "{HOOK_COMMAND}"'.encode()
    _settings(home).write_bytes(content)

    code, out = _uninstall(capsys, store)

    assert code == 2
    assert "is not valid JSON" in out and "hook could not be removed" in out
    assert _settings(home).read_bytes() == content
