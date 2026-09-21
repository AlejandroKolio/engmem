"""`engmem uninstall` — removal was otherwise manual across four places, one of them the shared
instructions file."""

import json
import os
from pathlib import Path

import pytest

from conftest import (
    INSTALLED_NAMES,
    INSTALLED_NAMES_COPILOT_IDE,
    INSTRUCTIONS_BYTE_CASES,
    LEGACY_TRIGGER_RULE,
    TRIGGER_RULE,
    claude_desktop_config_path,
    requires_permission_enforcement,
    requires_symlinks,
)

from engmem.cli import main
from engmem.install import TRIGGER_SENTINEL

COPILOT_CLI_SKILLS = ("engmem", "engmem-save", "engmem-save-quick")


def _install(*args):
    return main(["install", *args])


def _uninstall(*args):
    return main(["uninstall", *args])


UNINSTALL_REMOVES_ARTIFACTS_CASES = [
    pytest.param(
        ["--agent", "claude"],
        False,
        lambda home, project: home / ".claude" / "commands",
        INSTALLED_NAMES,
        lambda home, project: home / ".claude" / "CLAUDE.md",
        TRIGGER_RULE,
        id="claude",
    ),
    pytest.param(
        ["--agent", "copilot-ide"],
        True,
        lambda home, project: project / ".github" / "prompts",
        INSTALLED_NAMES_COPILOT_IDE,
        lambda home, project: project / ".github" / "copilot-instructions.md",
        "engmem search",
        id="copilot-ide",
    ),
    pytest.param(
        ["--agent", "copilot-cli"],
        False,
        lambda home, project: home / ".copilot" / "skills",
        COPILOT_CLI_SKILLS,
        None,
        None,
        id="copilot-cli",
    ),
]


@pytest.mark.parametrize(
    (
        "agent_args",
        "needs_git_repo",
        "root_dir_fn",
        "installed_names",
        "instructions_file_fn",
        "trigger_absent_substring",
    ),
    UNINSTALL_REMOVES_ARTIFACTS_CASES,
)
def test_uninstall_removes_agent_artifacts_and_trigger_rule(
    env,
    tmp_path,
    capsys,
    agent_args,
    needs_git_repo,
    root_dir_fn,
    installed_names,
    instructions_file_fn,
    trigger_absent_substring,
):
    home, project = env
    if needs_git_repo:
        (project / ".git").mkdir()
    store = tmp_path / "store"
    _install(*agent_args, "--store", str(store))
    capsys.readouterr()

    exit_code = _uninstall(*agent_args, "--store", str(store))
    out = capsys.readouterr().out

    assert exit_code == 0

    root_dir = root_dir_fn(home, project)
    for name in installed_names:
        assert not (root_dir / name).exists(), f"{name} should have been removed"

    assert f"{len(installed_names)} template file(s) removed" in out

    if instructions_file_fn is not None:
        content = instructions_file_fn(home, project).read_text(encoding="utf-8")
        assert trigger_absent_substring not in content


def test_uninstall_never_touches_the_store(env, tmp_path, capsys):
    """The store is the user's own documents: uninstall leaves it alone and names its path."""
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


SECOND_UNINSTALL_CASES = [
    pytest.param("claude", "0 template file(s) removed", id="claude"),
    pytest.param("claude-desktop", "0 config entry(ies) removed", id="claude-desktop"),
]


@pytest.mark.parametrize(("agent", "expected_summary"), SECOND_UNINSTALL_CASES)
def test_a_second_uninstall_changes_nothing(env, tmp_path, capsys, agent, expected_summary):
    home, project = env
    store = tmp_path / "store"
    _install("--agent", agent, "--store", str(store))
    _uninstall("--agent", agent, "--store", str(store))
    capsys.readouterr()

    exit_code = _uninstall("--agent", agent, "--store", str(store))
    out = capsys.readouterr().out

    assert exit_code == 0, "a second uninstall must succeed, not fail"
    assert expected_summary in out


NEVER_INSTALLED_CASES = [
    pytest.param("claude", lambda home, store: store, id="claude"),
    pytest.param(
        "claude-desktop",
        lambda home, store: claude_desktop_config_path(home),
        id="claude-desktop",
    ),
]


@pytest.mark.parametrize(("agent", "sentinel_path_fn"), NEVER_INSTALLED_CASES)
def test_uninstall_on_a_machine_that_never_installed(
    env, tmp_path, capsys, agent, sentinel_path_fn
):
    home, project = env
    store = tmp_path / "store"

    exit_code = _uninstall("--agent", agent, "--store", str(store))

    assert exit_code == 0
    assert not sentinel_path_fn(home, store).exists(), (
        "uninstall must not create anything as a side effect"
    )


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


@pytest.mark.parametrize(("original", "newline"), INSTRUCTIONS_BYTE_CASES)
def test_install_then_uninstall_hands_back_the_original_bytes(env, tmp_path, original, newline):
    """Removal rewrites the whole file too, so it owes the same debt install does: rebuilt from
    `read_text` the file comes back with every line re-ended and the BOM gone, and `git status`
    reports the whole of a Windows user's CLAUDE.md changed by a command that removed two lines."""
    home, project = env
    store = tmp_path / "store"
    claude_md = home / ".claude" / "CLAUDE.md"
    claude_md.parent.mkdir(parents=True)
    claude_md.write_bytes(original)

    assert _install("--agent", "claude", "--store", str(store)) == 0
    assert claude_md.read_bytes().endswith(TRIGGER_RULE.encode("utf-8") + newline), (
        "precondition: install must have appended the rule in the file's own dialect"
    )

    exit_code = _uninstall("--agent", "claude", "--store", str(store))

    assert exit_code == 0
    assert claude_md.read_bytes() == original


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
    # same reasoning as install's identical test: an unrecognized --agent must fail
    # via `runtime.fail` (both streams, exit 2), never argparse `choices=`
    # (stderr-only, SystemExit) — the stdout-only reader must see why it failed.
    exit_code = _uninstall("--agent", "copilot")

    assert exit_code == 2
    stdout = capsys.readouterr().out
    assert "unknown agent" in stdout and "'copilot'" in stdout
    assert "copilot-ide" in stdout and "copilot-cli" in stdout


def test_uninstall_reports_skills_it_could_not_remove(env, tmp_path, monkeypatch):
    """A failure partway through the loop must not abort with a traceback, leaving some skills
    deleted and no summary."""
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


@requires_permission_enforcement
def test_uninstall_reports_an_instructions_file_it_could_not_rewrite(env, tmp_path, capsys):
    """The templates are already gone by then: aborting with a traceback would hide both what was
    removed and what is left to clean up by hand."""
    home, project = env
    store = tmp_path / "store"
    _install("--agent", "claude", "--store", str(store))
    claude_md = home / ".claude" / "CLAUDE.md"
    original = claude_md.read_bytes()
    os.chmod(claude_md.parent, 0o500)
    try:
        exit_code = _uninstall("--agent", "claude", "--store", str(store))
        out = capsys.readouterr().out
    finally:
        os.chmod(claude_md.parent, 0o700)

    assert exit_code == 2
    assert "engmem uninstall incomplete:" in out
    assert "3 template file(s) removed" in out, "what did come off must still be reported"
    assert str(claude_md) in out, "the file left for the user to fix must be named on stdout"
    assert claude_md.read_bytes() == original, "a failed rewrite must not damage the file"


def test_uninstall_of_an_instructions_file_engmem_cannot_decode_is_reported(env, tmp_path, capsys):
    home, project = env
    store = tmp_path / "store"
    claude_md = home / ".claude" / "CLAUDE.md"
    claude_md.parent.mkdir(parents=True)
    original = "# Мои правила\n".encode("cp1251")
    claude_md.write_bytes(original)

    exit_code = _uninstall("--agent", "claude", "--store", str(store))
    out = capsys.readouterr().out

    assert exit_code == 2
    assert "engmem uninstall incomplete:" in out
    assert claude_md.read_bytes() == original
    assert "UTF-8" in out


@requires_symlinks
def test_uninstall_writes_through_a_symlinked_instructions_file(env, tmp_path):
    home, project = env
    store = tmp_path / "store"
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    real = dotfiles / "CLAUDE.md"
    real.write_text("# My rules\n", encoding="utf-8")
    claude_md = home / ".claude" / "CLAUDE.md"
    claude_md.parent.mkdir(parents=True)
    claude_md.symlink_to(real)
    _install("--agent", "claude", "--store", str(store))

    exit_code = _uninstall("--agent", "claude", "--store", str(store))

    assert exit_code == 0
    assert claude_md.is_symlink(), "uninstall must not turn the link into a regular file"
    assert real.read_text(encoding="utf-8") == "# My rules\n"


def test_uninstall_survives_a_failed_config_write_without_losing_the_config(
    env, tmp_path, capsys, monkeypatch
):
    home, project = env
    store = tmp_path / "store"
    config_path = claude_desktop_config_path(home)
    _install("--agent", "claude-desktop", "--store", str(store))
    original = config_path.read_bytes()

    def refuse_to_commit(tmp, target):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("engmem.install.commit", refuse_to_commit)

    exit_code = _uninstall("--agent", "claude-desktop", "--store", str(store))
    out = capsys.readouterr().out

    assert exit_code == 2
    assert config_path.read_bytes() == original
    assert [p.name for p in config_path.parent.iterdir()] == [config_path.name], (
        "the staged temp file must not be left behind"
    )
    assert "config entry(ies) could not be removed" in out


def test_uninstall_does_not_delete_unrelated_line_containing_trigger_substring(env, tmp_path):
    """Data loss: deleting every line containing the bare substring destroyed a user's own
    unrelated sentence."""
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


TRIGGER_RULE_REMOVAL_CASES = [
    pytest.param(LEGACY_TRIGGER_RULE, True, id="legacy-rule-with-sentinel"),
    # installs already in the wild wrote the rule line with no sentinel at all and would
    # otherwise be orphaned forever — for the legacy wording and the current one alike
    pytest.param(LEGACY_TRIGGER_RULE, False, id="legacy-rule-bare"),
    pytest.param(TRIGGER_RULE, False, id="current-rule-bare"),
]


@pytest.mark.parametrize(("rule", "with_sentinel"), TRIGGER_RULE_REMOVAL_CASES)
def test_uninstall_removes_a_trigger_rule_line_engmem_wrote(env, tmp_path, rule, with_sentinel):
    home, project = env
    store = tmp_path / "store"
    claude_md = home / ".claude" / "CLAUDE.md"
    claude_md.parent.mkdir(parents=True)
    sentinel = TRIGGER_SENTINEL + "\n" if with_sentinel else ""
    claude_md.write_text(
        "# My rules\n\n- Prefer small commits.\n" + sentinel + rule + "\n",
        encoding="utf-8",
    )

    exit_code = _uninstall("--agent", "claude", "--store", str(store))

    assert exit_code == 0
    assert claude_md.read_text(encoding="utf-8") == "# My rules\n\n- Prefer small commits.\n"


def _claude_templates_are_intact(home):
    commands_dir = home / ".claude" / "commands"
    for name in INSTALLED_NAMES:
        assert (commands_dir / name).is_file(), (
            "uninstalling the wrong agent must never touch another agent's files"
        )


def _claude_desktop_entry_is_intact(home):
    config = json.loads(claude_desktop_config_path(home).read_text(encoding="utf-8"))
    assert "engmem" in config["mcpServers"], (
        "uninstalling a different --agent must never touch claude-desktop's entry"
    )


def _both_are_intact(home):
    _claude_templates_are_intact(home)
    _claude_desktop_entry_is_intact(home)


OTHER_AGENT_PRESENT_CASES = [
    pytest.param(
        ("claude",), "copilot-cli", "claude", _claude_templates_are_intact, id="claude"
    ),
    pytest.param(
        ("claude-desktop",),
        "claude",
        "claude-desktop",
        _claude_desktop_entry_is_intact,
        id="claude-desktop",
    ),
    # two agents present at once. With one installed per row the nudge is pinned only on its
    # non-emptiness: a lookup that answers a fixed agent, or one that stops filtering by
    # presence and names every agent that is not the one being uninstalled, produces exactly
    # one name either way and passes the rows above
    pytest.param(
        ("claude", "claude-desktop"),
        "copilot-cli",
        "claude, claude-desktop",
        _both_are_intact,
        id="claude-and-claude-desktop",
    ),
]


@pytest.mark.parametrize(
    ("installed_agents", "uninstalled_agent", "expected_note", "assert_intact"),
    OTHER_AGENT_PRESENT_CASES,
)
def test_uninstalling_the_wrong_agent_names_the_one_that_is_installed(
    env, tmp_path, capsys, installed_agents, uninstalled_agent, expected_note, assert_intact
):
    """Uninstalling the wrong agent printed `0 removed`, exit 0, with no hint the operator meant
    a different one. The nudge exists to tell that apart from "never installed", so it has to
    name the agents that are actually present: one naming an agent the user never installed
    sends them to re-run and collect another 0."""
    home, project = env
    (project / ".git").mkdir()
    store = tmp_path / "store"
    for agent in installed_agents:
        _install("--agent", agent, "--store", str(store))
    capsys.readouterr()

    exit_code = _uninstall("--agent", uninstalled_agent, "--store", str(store))
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "0 template file(s) removed" in out
    assert f"files for {expected_note} are present" in out, (
        "the nudge must name exactly the agents whose artifacts are on this machine"
    )
    assert_intact(home)


# --- claude-desktop: uninstall removes only the mcpServers.engmem key -------------


def test_uninstall_claude_desktop_removes_only_the_engmem_entry(env, tmp_path, capsys):
    home, project = env
    store = tmp_path / "store"
    config_path = claude_desktop_config_path(home)
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


def test_uninstall_claude_desktop_malformed_config_removal_failure_is_fully_diagnosed(
    env, tmp_path, capsys
):
    """On this exact failure, uninstall once hardcoded the wrong noun ("file(s)" instead of
    "config entry(ies)"), printed a leading `0 ... removed` line that reads as success to a
    reader who stops at the first line, and wrote its JSON diagnosis to stderr only, invisible
    to the stdout-only consumer."""
    home, project = env
    store = tmp_path / "store"
    config_path = claude_desktop_config_path(home)
    config_path.parent.mkdir(parents=True)
    original = '{"mcpServers": {"otherServer": '
    config_path.write_text(original, encoding="utf-8")

    exit_code = _uninstall("--agent", "claude-desktop", "--store", str(store))
    out = capsys.readouterr().out

    assert exit_code == 2
    assert "config entry(ies) could not be removed" in out
    assert "file(s) could not be removed" not in out
    assert "engmem uninstalled:" not in out
    assert "engmem uninstall incomplete:" in out
    assert "is not valid JSON" in out
    assert config_path.read_text(encoding="utf-8") == original, (
        "a malformed config must never be silently discarded or overwritten"
    )


UNINSTALL_REJECTS_LOCAL_CASES = [
    pytest.param("claude-desktop", True, id="claude-desktop"),
    # install and uninstall must agree on which agents are home-scoped, or uninstall looks
    # where install never wrote
    pytest.param("copilot-cli", False, id="copilot-cli"),
]


@pytest.mark.parametrize(("agent", "include_store"), UNINSTALL_REJECTS_LOCAL_CASES)
def test_uninstall_rejects_local_flag_for_home_scoped_agents(
    env, tmp_path, capsys, agent, include_store
):
    args = ["--agent", agent, "--local"]
    if include_store:
        args += ["--store", str(tmp_path / "store")]

    exit_code = _uninstall(*args)
    err = capsys.readouterr().err

    assert exit_code == 2
    assert "--local" in err
    assert agent in err
