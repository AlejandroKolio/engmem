"""`engmem uninstall` — the mirror of install.

Author-approved addition beyond ENGMEM-SPEC.md §11 (which freezes new commands until
Gate 1): removal was manual across four places, one of them the shared CLAUDE.md, where
a forgotten trigger rule silently keeps instructing agents to run a command that no
longer exists.
"""

from pathlib import Path

import pytest

from conftest import redirect_home

from engmem.cli import main

INSTALLED_NAMES = ("engmem.md", "engmem.save.md", "engmem.save.quick.md")
INSTALLED_NAMES_COPILOT_IDE = (
    "engmem.prompt.md",
    "engmem.save.prompt.md",
    "engmem.save.quick.prompt.md",
)
COPILOT_CLI_SKILLS = ("engmem", "engmem-save", "engmem-save-quick")
TRIGGER_RULE = (
    'Before proposing a plan, run `engmem search "<key terms for the task>"`'
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    redirect_home(monkeypatch, home)
    monkeypatch.chdir(project)
    return home, project


def _install(*args):
    return main(["install", *args])


def _uninstall(*args):
    return main(["uninstall", *args])


def test_uninstall_removes_templates_and_trigger_rule(env, tmp_path, capsys):
    home, project = env
    store = tmp_path / "store"
    _install("--agent", "claude", "--store", str(store))
    capsys.readouterr()

    exit_code = _uninstall("--agent", "claude", "--store", str(store))
    out = capsys.readouterr().out

    assert exit_code == 0

    commands_dir = home / ".claude" / "commands"
    for name in INSTALLED_NAMES:
        assert not (commands_dir / name).exists(), f"{name} should have been removed"

    claude_md = home / ".claude" / "CLAUDE.md"
    assert TRIGGER_RULE not in claude_md.read_text(encoding="utf-8")

    assert "3 template file(s) removed" in out


def test_uninstall_never_touches_the_store(env, tmp_path, capsys):
    """The store is the user's own documents, not the tool's files: uninstall must
    leave it alone and name its path so the user can decide separately."""
    home, project = env
    store = tmp_path / "store"
    _install("--agent", "claude", "--store", str(store))

    doc = store / "sessions" / "my-session.md"
    doc.write_text("---\nid: my-session\n---\n", encoding="utf-8")
    capsys.readouterr()

    _uninstall("--agent", "claude", "--store", str(store))
    out = capsys.readouterr().out

    assert doc.is_file(), "uninstall must not delete the user's documents"
    assert (store / "sessions").is_dir()
    assert str(store) in out, "uninstall must print the store path it left behind"


def test_uninstall_is_idempotent(env, tmp_path, capsys):
    home, project = env
    store = tmp_path / "store"
    _install("--agent", "claude", "--store", str(store))
    _uninstall("--agent", "claude", "--store", str(store))
    capsys.readouterr()

    exit_code = _uninstall("--agent", "claude", "--store", str(store))
    out = capsys.readouterr().out

    assert exit_code == 0, "a second uninstall must succeed, not fail"
    assert "0 template file(s) removed" in out


def test_uninstall_on_a_machine_that_never_installed(env, tmp_path, capsys):
    home, project = env
    store = tmp_path / "store"

    exit_code = _uninstall("--agent", "claude", "--store", str(store))

    assert exit_code == 0
    assert not store.exists(), "uninstall must not create a store as a side effect"


def test_uninstall_keeps_unrelated_instruction_lines(env, tmp_path, capsys):
    home, project = env
    store = tmp_path / "store"
    claude_md = home / ".claude" / "CLAUDE.md"
    claude_md.parent.mkdir(parents=True)
    claude_md.write_text(
        "# My rules\n\n- Always write tests first.\n- Prefer small commits.\n",
        encoding="utf-8",
    )

    _install("--agent", "claude", "--store", str(store))
    _uninstall("--agent", "claude", "--store", str(store))

    content = claude_md.read_text(encoding="utf-8")
    assert "Always write tests first." in content
    assert "Prefer small commits." in content
    assert "engmem search" not in content


def test_uninstall_copilot_ide_removes_prompt_files(env, tmp_path, capsys):
    home, project = env
    (project / ".git").mkdir()
    store = tmp_path / "store"
    _install("--agent", "copilot-ide", "--store", str(store))

    exit_code = _uninstall("--agent", "copilot-ide", "--store", str(store))

    assert exit_code == 0
    prompts_dir = project / ".github" / "prompts"
    for name in INSTALLED_NAMES_COPILOT_IDE:
        assert not (prompts_dir / name).exists()

    instructions = project / ".github" / "copilot-instructions.md"
    assert "engmem search" not in instructions.read_text(encoding="utf-8")


def test_uninstall_copilot_cli_removes_skill_directories(env, tmp_path, capsys):
    home, project = env
    store = tmp_path / "store"
    _install("--agent", "copilot-cli", "--store", str(store))

    exit_code = _uninstall("--agent", "copilot-cli", "--store", str(store))

    assert exit_code == 0
    skills_root = home / ".copilot" / "skills"
    for skill in COPILOT_CLI_SKILLS:
        assert not (skills_root / skill).exists(), f"skill {skill} should be gone"


def test_uninstall_leaves_foreign_skills_alone(env, tmp_path, capsys):
    home, project = env
    store = tmp_path / "store"
    skills_root = home / ".copilot" / "skills"
    other = skills_root / "someone-elses-skill"
    other.mkdir(parents=True)
    (other / "SKILL.md").write_text("---\nname: someone-elses-skill\n---\n", encoding="utf-8")

    _install("--agent", "copilot-cli", "--store", str(store))
    _uninstall("--agent", "copilot-cli", "--store", str(store))

    assert (other / "SKILL.md").is_file(), "only engmem's own skills may be removed"


def test_uninstall_repo_scoped_without_git_repo_fails_loudly(env, tmp_path, capsys):
    home, project = env
    store = tmp_path / "store"

    exit_code = _uninstall("--agent", "copilot-ide", "--store", str(store))
    err = capsys.readouterr().err

    assert exit_code == 2
    assert str(project) in err
    assert ".git" in err


def test_uninstall_bare_copilot_agent_value_is_rejected(env, capsys):
    # D14, same reasoning as install's identical test: an unrecognized --agent
    # must fail via `_fail()` (both streams, exit 2), never argparse `choices=`
    # (stderr-only, SystemExit) — the stdout-only reader must see why it failed.
    exit_code = _uninstall("--agent", "copilot")

    assert exit_code == 2
    stdout = capsys.readouterr().out
    assert "unknown agent" in stdout and "'copilot'" in stdout
    assert "copilot-ide" in stdout and "copilot-cli" in stdout


def test_uninstall_reports_skills_it_could_not_remove(env, tmp_path, monkeypatch):
    """A failure partway through the loop must not abort the run with a traceback,
    leaving some skills deleted and the summary never printed (principle VIII)."""
    home, project = env
    store = tmp_path / "store"
    _install("--agent", "copilot-cli", "--store", str(store))

    real_unlink = Path.unlink

    def refuse_second_skill(self, *args, **kwargs):
        if self.parent.name == "engmem-save":
            raise PermissionError("file is in use")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse_second_skill)

    code = _uninstall("--agent", "copilot-cli", "--store", str(store))

    assert code == 2
    skills = home / ".copilot" / "skills"
    assert not (skills / "engmem" / "SKILL.md").exists(), "the others must still be removed"
    assert (skills / "engmem-save" / "SKILL.md").exists()


def test_uninstall_does_not_delete_unrelated_line_containing_trigger_substring(env, tmp_path):
    """D12 — data loss: uninstall used to delete every line containing the bare
    substring `` `engmem search ``, so a user's own unrelated sentence that happened
    to mention it was destroyed along with the real trigger rule."""
    home, project = env
    store = tmp_path / "store"
    claude_md = home / ".claude" / "CLAUDE.md"
    claude_md.parent.mkdir(parents=True)
    original = (
        "# My instructions\n\n"
        "Never run `engmem search` on the production store.\n"
        "Some other unrelated rule.\n"
    )
    claude_md.write_text(original, encoding="utf-8")

    exit_code = _uninstall("--agent", "claude", "--store", str(store))

    assert exit_code == 0
    assert claude_md.read_text(encoding="utf-8") == original, (
        "uninstall must never delete a line just because it mentions "
        "`engmem search` — only the line(s) engmem itself wrote"
    )


def test_uninstall_removes_pre_sentinel_bare_trigger_rule_line(env, tmp_path):
    """D12 migration: installs already in the wild wrote the bare rule line with no
    sentinel. uninstall must still find and remove exactly that literal line, or it
    is orphaned forever once this fix ships."""
    home, project = env
    store = tmp_path / "store"
    claude_md = home / ".claude" / "CLAUDE.md"
    claude_md.parent.mkdir(parents=True)
    claude_md.write_text(
        "# My rules\n\n- Prefer small commits.\n" + TRIGGER_RULE + "\n",
        encoding="utf-8",
    )

    exit_code = _uninstall("--agent", "claude", "--store", str(store))

    content = claude_md.read_text(encoding="utf-8")
    assert exit_code == 0
    assert TRIGGER_RULE not in content
    assert "Prefer small commits." in content


def test_uninstall_notes_when_a_different_agents_files_are_present(env, tmp_path, capsys):
    """D19: `uninstall --agent copilot-cli` after `install --agent claude` used to
    print `0 template file(s) removed`, exit 0, and leave every claude file on disk
    with no hint the operator likely meant a different --agent."""
    home, project = env
    (project / ".git").mkdir()
    store = tmp_path / "store"
    _install("--agent", "claude", "--store", str(store))
    capsys.readouterr()

    exit_code = _uninstall("--agent", "copilot-cli", "--store", str(store))
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "0 template file(s) removed" in out

    commands_dir = home / ".claude" / "commands"
    for name in INSTALLED_NAMES:
        assert (commands_dir / name).is_file(), (
            "uninstalling the wrong agent must never touch another agent's files"
        )
    assert "claude" in out.lower(), (
        "0 removed while another agent's files exist must be called out, not silent"
    )


# --- claude-desktop: uninstall removes only the mcpServers.engmem key -------------

import json
import sys


def _claude_desktop_config_path(home: Path) -> Path:
    """Mirrors `install._claude_desktop_config_path`. Hardcoding the macOS tree here made
    every claude-desktop test fail on Windows, where the product correctly writes under
    %APPDATA% — the test was asserting the wrong location, not the code."""
    if sys.platform == "win32":
        return home / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
    return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"


def test_uninstall_claude_desktop_removes_only_the_engmem_entry(env, tmp_path, capsys):
    home, project = env
    store = tmp_path / "store"
    config_path = _claude_desktop_config_path(home)
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "some-other-server": {"command": "/usr/bin/other", "args": []}
                },
                "someUnrelatedTopLevelKey": True,
            }
        ),
        encoding="utf-8",
    )
    _install("--agent", "claude-desktop", "--store", str(store))
    capsys.readouterr()

    exit_code = _uninstall("--agent", "claude-desktop", "--store", str(store))
    out = capsys.readouterr().out

    assert exit_code == 0
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert "engmem" not in config["mcpServers"]
    assert config["mcpServers"]["some-other-server"] == {"command": "/usr/bin/other", "args": []}
    assert config["someUnrelatedTopLevelKey"] is True
    assert "1 config entry(ies) removed" in out


def test_uninstall_claude_desktop_is_idempotent(env, tmp_path, capsys):
    home, project = env
    store = tmp_path / "store"
    _install("--agent", "claude-desktop", "--store", str(store))
    _uninstall("--agent", "claude-desktop", "--store", str(store))
    capsys.readouterr()

    exit_code = _uninstall("--agent", "claude-desktop", "--store", str(store))
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "0 config entry(ies) removed" in out


def test_uninstall_claude_desktop_on_a_machine_that_never_installed(env, tmp_path, capsys):
    home, project = env
    store = tmp_path / "store"

    exit_code = _uninstall("--agent", "claude-desktop", "--store", str(store))

    assert exit_code == 0
    assert not _claude_desktop_config_path(home).exists()


def test_uninstall_claude_desktop_does_not_reject_missing_git_repo(env, tmp_path, capsys):
    # not repo-scoped: it writes under $HOME, so no --local, no .git guard
    home, project = env
    store = tmp_path / "store"

    exit_code = _uninstall("--agent", "claude-desktop", "--store", str(store))

    assert exit_code == 0


def test_uninstall_claude_desktop_rejects_malformed_config_without_discarding_it(env, tmp_path, capsys):
    home, project = env
    store = tmp_path / "store"
    config_path = _claude_desktop_config_path(home)
    config_path.parent.mkdir(parents=True)
    original = "{not valid json"
    config_path.write_text(original, encoding="utf-8")

    exit_code = _uninstall("--agent", "claude-desktop", "--store", str(store))

    assert exit_code == 2
    assert config_path.read_text(encoding="utf-8") == original, (
        "a malformed config must never be silently discarded or overwritten"
    )


def test_uninstall_reports_correct_noun_when_config_removal_fails(env, tmp_path, capsys):
    """D12: the summary's failure line hardcoded "file(s)" even for claude-desktop,
    where every other line on the same run already says "config entry(ies)"."""
    home, project = env
    store = tmp_path / "store"
    config_path = _claude_desktop_config_path(home)
    config_path.parent.mkdir(parents=True)
    config_path.write_text('{"mcpServers": {"otherServer": ', encoding="utf-8")

    exit_code = _uninstall("--agent", "claude-desktop", "--store", str(store))
    out = capsys.readouterr().out

    assert exit_code == 2
    assert "config entry(ies) could not be removed" in out
    assert "file(s) could not be removed" not in out


def test_uninstall_leading_line_does_not_read_as_success_when_removal_fails(
    env, tmp_path, capsys
):
    """D12: `engmem uninstalled: 0 ... removed` on a failed run reads as a completed,
    successful uninstall to a reader that only sees stdout and stops at the first
    line — the exit code (2) and the second line are not enough to undo that."""
    home, project = env
    store = tmp_path / "store"
    config_path = _claude_desktop_config_path(home)
    config_path.parent.mkdir(parents=True)
    config_path.write_text('{"mcpServers": {"otherServer": ', encoding="utf-8")

    exit_code = _uninstall("--agent", "claude-desktop", "--store", str(store))
    out = capsys.readouterr().out

    assert exit_code == 2
    assert "engmem uninstalled:" not in out
    assert "engmem uninstall incomplete:" in out


def test_uninstall_claude_desktop_malformed_config_diagnosis_reaches_stdout(
    env, tmp_path, capsys
):
    """D12: install routes the analogous failure through `_fail()` to both streams;
    uninstall's own copy of the same failure wrote only to stderr, which the
    stdout-only consumer never reads (principle VIII, ENGMEM-SPEC.md §1)."""
    home, project = env
    store = tmp_path / "store"
    config_path = _claude_desktop_config_path(home)
    config_path.parent.mkdir(parents=True)
    config_path.write_text('{"mcpServers": {"otherServer": ', encoding="utf-8")

    exit_code = _uninstall("--agent", "claude-desktop", "--store", str(store))
    out = capsys.readouterr().out

    assert exit_code == 2
    assert "is not valid JSON" in out


def test_uninstall_claude_desktop_rejects_local_flag(env, tmp_path, capsys):
    home, project = env
    store = tmp_path / "store"

    exit_code = _uninstall("--agent", "claude-desktop", "--local", "--store", str(store))
    err = capsys.readouterr().err

    assert exit_code == 2
    assert "--local" in err
    assert "claude-desktop" in err


def test_uninstall_notes_claude_desktop_when_its_entry_is_present_and_a_different_agent_uninstalled(
    env, tmp_path, capsys
):
    home, project = env
    (project / ".git").mkdir()
    store = tmp_path / "store"
    _install("--agent", "claude-desktop", "--store", str(store))
    capsys.readouterr()

    exit_code = _uninstall("--agent", "claude", "--store", str(store))
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "0 template file(s) removed" in out
    assert "claude-desktop" in out.lower()
    config_path = _claude_desktop_config_path(home)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert "engmem" in config["mcpServers"], (
        "uninstalling a different --agent must never touch claude-desktop's entry"
    )


def test_uninstall_copilot_cli_agent_rejects_local_flag(env, capsys):
    """Same rejection as install: the two commands must agree on which agents are
    home-scoped, or uninstall looks in a directory install never wrote to."""
    exit_code = main(["uninstall", "--agent", "copilot-cli", "--local"])
    err = capsys.readouterr().err

    assert exit_code == 2
    assert "--local" in err
    assert "copilot-cli" in err
