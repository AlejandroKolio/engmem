from pathlib import Path

import pytest

from conftest import (
    INSTALLED_NAMES,
    INSTALLED_NAMES_COPILOT_IDE,
    INSTRUCTIONS_BYTE_CASES,
    TRIGGER_RULE,
    claude_desktop_config_path,
    requires_permission_enforcement,
    requires_posix_modes,
    requires_symlinks,
)

from engmem.cli import main

SOURCE_TEMPLATE_NAMES = ("engmem.start.md", "engmem.save.md", "engmem.save.quick.md")
COPILOT_CLI_SKILLS = {
    "engmem": "<!-- engmem-template: engmem.start v0.1.0 -->",
    "engmem-save": "<!-- engmem-template: engmem.save v0.1.0 -->",
    "engmem-save-quick": "<!-- engmem-template: engmem.save.quick v0.1.0 -->",
}
STAMPS = {
    "engmem.md": "<!-- engmem-template: engmem.start v0.1.0 -->",
    "engmem.save.md": "<!-- engmem-template: engmem.save v0.1.0 -->",
    "engmem.save.quick.md": "<!-- engmem-template: engmem.save.quick v0.1.0 -->",
    "engmem.prompt.md": "<!-- engmem-template: engmem.start v0.1.0 -->",
    "engmem.save.prompt.md": "<!-- engmem-template: engmem.save v0.1.0 -->",
    "engmem.save.quick.prompt.md": "<!-- engmem-template: engmem.save.quick v0.1.0 -->",
}


def _run_install(*args):
    return main(["install", *args])


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".git" not in p.parts
    }


def _assert_installed_template(path: Path, installed_name: str) -> None:
    assert path.is_file(), f"{installed_name} not installed"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "---", (
        f"{installed_name}: YAML front matter must open on the very first line "
        f"(Claude Code ignores it otherwise); got {lines[0]!r}"
    )
    closing = lines.index("---", 1)
    assert lines[closing + 1] == STAMPS[installed_name], (
        f"{installed_name}: version stamp must be the first line after front matter"
    )


def _assert_installed_skill(skills_root: Path, skill_name: str) -> None:
    path = skills_root / skill_name / "SKILL.md"
    assert path.is_file(), f"{skill_name}/SKILL.md not installed"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "---", f"{skill_name}/SKILL.md: front matter must open on line 1"
    closing = lines.index("---", 1)
    front_matter = lines[1:closing]
    assert f"name: {skill_name}" in front_matter, (
        f"{skill_name}/SKILL.md: front matter must declare name: {skill_name} "
        f"(Copilot CLI requires it); got {front_matter!r}"
    )
    assert not any(line.startswith("argument-hint:") for line in front_matter), (
        f"{skill_name}/SKILL.md: argument-hint is not a Copilot skill field and "
        f"must be stripped"
    )
    assert lines[closing + 1] == COPILOT_CLI_SKILLS[skill_name], (
        f"{skill_name}/SKILL.md: version stamp must be the first line after front matter"
    )


_SOURCE_FILENAME_REASON = (
    "the start template must install under its user-facing command name, not the "
    "source template's own filename"
)
_COPILOT_IDE_DOUBLE_EXTENSION_REASON = (
    "the Copilot IDE plugin only reads .prompt.md — a plain .md in .github/prompts is "
    "silently ignored"
)

FRONT_END_INSTALL_CASES = [
    pytest.param(
        ["--agent", "claude"],
        False,
        lambda home, project: home / ".claude" / "commands",
        lambda home, project: home / ".claude" / "CLAUDE.md",
        INSTALLED_NAMES,
        "engmem.start.md",
        _SOURCE_FILENAME_REASON,
        None,
        id="claude-global",
    ),
    pytest.param(
        ["--agent", "claude", "--local"],
        True,
        lambda home, project: project / ".claude" / "commands",
        lambda home, project: project / "CLAUDE.md",
        INSTALLED_NAMES,
        "engmem.start.md",
        _SOURCE_FILENAME_REASON,
        lambda home, project: home / ".claude" / "commands",
        id="claude-local",
    ),
    pytest.param(
        ["--agent", "copilot-ide"],
        True,
        lambda home, project: project / ".github" / "prompts",
        lambda home, project: project / ".github" / "copilot-instructions.md",
        INSTALLED_NAMES_COPILOT_IDE,
        "engmem.md",
        _COPILOT_IDE_DOUBLE_EXTENSION_REASON,
        None,
        id="copilot-ide",
    ),
]


@pytest.mark.parametrize(
    (
        "agent_args",
        "needs_git_repo",
        "commands_dir_fn",
        "instructions_file_fn",
        "installed_names",
        "unwanted_old_name",
        "unwanted_old_name_reason",
        "unwanted_commands_dir_fn",
    ),
    FRONT_END_INSTALL_CASES,
)
def test_fresh_install_creates_templates_stamp_and_trigger(
    env,
    tmp_path,
    agent_args,
    needs_git_repo,
    commands_dir_fn,
    instructions_file_fn,
    installed_names,
    unwanted_old_name,
    unwanted_old_name_reason,
    unwanted_commands_dir_fn,
):
    home, project = env
    if needs_git_repo:
        (project / ".git").mkdir()
    store = tmp_path / "store"

    exit_code = _run_install(*agent_args, "--store", str(store))

    assert exit_code == 0
    assert (store / "sessions").is_dir()
    assert (store / ".git").is_dir()

    commands_dir = commands_dir_fn(home, project)
    for name in installed_names:
        _assert_installed_template(commands_dir / name, name)

    assert not (commands_dir / unwanted_old_name).exists(), unwanted_old_name_reason

    if unwanted_commands_dir_fn is not None:
        assert not unwanted_commands_dir_fn(home, project).exists(), (
            "--local must scope templates to the project, never HOME as well"
        )

    instructions_file = instructions_file_fn(home, project)
    assert instructions_file.is_file()
    assert instructions_file.read_text(encoding="utf-8").count(TRIGGER_RULE) == 1


def test_second_install_is_idempotent(env, tmp_path):
    home, project = env
    store = tmp_path / "store"

    _run_install("--agent", "claude", "--store", str(store))

    (store / "sessions" / "existing-doc.md").write_text("---\nid: existing-doc\n---\n")

    before_home = _snapshot(home)
    before_store = _snapshot(store)

    exit_code = _run_install("--agent", "claude", "--store", str(store))

    assert exit_code == 0
    assert _snapshot(home) == before_home
    assert _snapshot(store) == before_store

    claude_md = home / ".claude" / "CLAUDE.md"
    assert claude_md.read_text(encoding="utf-8").count(TRIGGER_RULE) == 1


def test_reworded_trigger_rule_is_not_duplicated(env, tmp_path):
    home, project = env
    store = tmp_path / "store"

    claude_md = home / ".claude" / "CLAUDE.md"
    claude_md.parent.mkdir(parents=True)
    claude_md.write_text(
        "# My rules\n\n- Always start by running `engmem search` on the key terms of "
        "the task, then propose a plan.\n",
        encoding="utf-8",
    )

    _run_install("--agent", "claude", "--store", str(store))

    content = claude_md.read_text(encoding="utf-8")
    assert content.count("`engmem search") == 1, (
        "a user-reworded trigger rule mentioning `engmem search` must not be "
        "duplicated by install"
    )


def test_repo_scoped_install_without_git_repo_fails_loudly(env, tmp_path, capsys):
    home, project = env
    store = tmp_path / "store"

    exit_code = _run_install("--agent", "claude", "--local", "--store", str(store))

    assert exit_code == 2
    assert not (project / ".claude").exists()
    assert not store.exists()

    stderr = capsys.readouterr().err
    assert str(project) in stderr, "error must name the CWD that was rejected"
    assert ".git" in stderr, "error must name what was expected (a .git directory)"


def test_bare_copilot_agent_value_is_rejected(env, capsys):
    # D14: argparse `choices=` would reject this via `parser.error()`, which raises SystemExit
    # before `_cmd_install` ever runs — an exit code, never a return, and never this message.
    exit_code = _run_install("--agent", "copilot")

    assert exit_code == 2
    stdout = capsys.readouterr().out
    assert "unknown agent" in stdout and "'copilot'" in stdout
    assert "copilot-ide" in stdout and "copilot-cli" in stdout, (
        "bare `copilot` must be rejected pointing users at the explicit mode names"
    )


def test_copilot_cli_agent_installs_personal_skills(env, tmp_path):
    home, project = env
    store = tmp_path / "store"

    exit_code = _run_install("--agent", "copilot-cli", "--store", str(store))

    assert exit_code == 0

    skills_root = home / ".copilot" / "skills"
    for skill_name in COPILOT_CLI_SKILLS:
        _assert_installed_skill(skills_root, skill_name)

    assert not (project / ".github").exists(), (
        "copilot-cli skills are HOME-scoped personal skills, not a repo file"
    )


def test_copilot_cli_agent_does_not_require_git_repo_cwd(env, tmp_path):
    # copilot-cli writes only under $HOME (~/.copilot/skills/), so unlike the
    # repo-scoped modes it must work even when CWD is not a git repository
    home, project = env
    store = tmp_path / "store"

    exit_code = _run_install("--agent", "copilot-cli", "--store", str(store))

    assert exit_code == 0
    assert (home / ".copilot" / "skills" / "engmem" / "SKILL.md").is_file()


def test_copilot_cli_agent_second_install_is_idempotent(env, tmp_path):
    home, project = env
    store = tmp_path / "store"

    _run_install("--agent", "copilot-cli", "--store", str(store))

    before = _snapshot(home)

    exit_code = _run_install("--agent", "copilot-cli", "--store", str(store))

    assert exit_code == 0
    assert _snapshot(home) == before


def test_copilot_cli_skill_bodies_use_hyphenated_command_names(env, tmp_path):
    """Skill names cannot contain dots, so installed files must carry the names a Copilot CLI user
    can actually type."""
    home, project = env
    store = tmp_path / "store"

    _run_install("--agent", "copilot-cli", "--store", str(store))

    skills_root = home / ".copilot" / "skills"
    texts = {
        name: (skills_root / name / "SKILL.md").read_text(encoding="utf-8")
        for name in COPILOT_CLI_SKILLS
    }

    for name, text in texts.items():
        assert "/engmem.save" not in text, (
            f"{name}/SKILL.md still references a dotted command name that does "
            f"not exist in Copilot CLI"
        )

    assert "/engmem-save" in texts["engmem"]  # start template points at the save step
    assert "# /engmem-save " in texts["engmem-save"]  # title carries the real command
    assert "# /engmem-save-quick " in texts["engmem-save-quick"]


def test_claude_and_copilot_ide_installs_keep_dotted_command_names(env, tmp_path):
    home, project = env
    (project / ".git").mkdir()
    store = tmp_path / "store"

    _run_install("--agent", "claude", "--store", str(store))
    _run_install("--agent", "copilot-ide", "--store", str(store))

    claude_start = (home / ".claude" / "commands" / "engmem.md").read_text(encoding="utf-8")
    ide_start = (project / ".github" / "prompts" / "engmem.prompt.md").read_text(
        encoding="utf-8"
    )

    assert "/engmem.save" in claude_start, (
        "the hyphen substitution is copilot-cli-only; Claude commands stay dotted"
    )
    assert "/engmem.save" in ide_start, (
        "the IDE prompt files keep the dotted names their commands actually use"
    )


def test_missing_git_binary_fails_with_named_cause(env, tmp_path, monkeypatch, capsys):
    """Exit code 2, not the 1 a string-valued SystemExit yields. The fragments asserted below
    are the ones only the narrow FileNotFoundError clause emits — both messages say "git"."""
    home, project = env
    store = tmp_path / "store"
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))

    code = _run_install("--agent", "claude", "--store", str(store))
    err = capsys.readouterr().err

    assert code == 2
    assert "not found on PATH" in err and "Install git" in err
    assert "cannot run `git init`" not in err, "the generic OSError clause must not swallow this"
    assert err.startswith("error:")


def test_a_git_that_cannot_be_executed_fails_with_a_named_cause(env, tmp_path, monkeypatch, capsys):
    """A `git` on PATH that cannot be exec'd raises PermissionError, which the branch naming a
    missing binary never sees — and an uncaught OSError here is the traceback §5 rules out."""
    home, project = env
    store = tmp_path / "store"

    def refuse_to_exec(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("engmem.install.subprocess.run", refuse_to_exec)

    code = _run_install("--agent", "claude", "--store", str(store))
    out = capsys.readouterr()

    assert code == 2
    assert out.err.startswith("error:")
    assert "cannot run `git init`" in out.err and str(store) in out.err
    assert "not found on PATH" not in out.err, "an unexecutable `git` is present, not missing"
    assert "Permission denied" in out.out, "the OS reason must reach the stdout-only consumer"


MALFORMED_SKILL_TEMPLATE_CASES = [
    # `assert` is compiled away under `python -O`, leaving a bare StopIteration with no
    # diagnosable cause. Each case asserts the fragment only its own branch produces: both
    # messages contain "front matter", so that word alone cannot tell the two apart.
    pytest.param("no front matter here\n", "does not open", id="no-front-matter"),
    pytest.param(
        "---\ndescription: x\n\nBody with no closing fence\n",
        "unclosed",
        id="unclosed-front-matter",
    ),
]


@pytest.mark.parametrize(("template_text", "expected_substring"), MALFORMED_SKILL_TEMPLATE_CASES)
def test_malformed_skill_template_names_the_cause(template_text, expected_substring):
    from engmem.install import _to_skill_front_matter

    with pytest.raises(ValueError) as excinfo:
        _to_skill_front_matter(template_text, "engmem")

    message = str(excinfo.value)
    assert expected_substring in message
    assert "engmem" in message, "the message must name the skill it failed on"


def test_install_with_no_store_flag_falls_back_to_the_documented_default(env, monkeypatch):
    """The first command every user runs is `engmem install` with no flag, and nothing reached
    the default-store branch."""
    home, _ = env
    monkeypatch.delenv("ENGMEM_HOME", raising=False)

    code = _run_install("--agent", "claude")

    assert code == 0
    store = home / "Developer" / "engmem"
    assert (store / "sessions").is_dir()
    assert (store / ".git").is_dir()


def test_install_reports_on_stdout_when_trigger_rule_already_present(env, tmp_path, capsys):
    """D12: a silent skip left no way to tell an install that added the rule from one that quietly
    did nothing."""
    home, project = env
    store = tmp_path / "store"
    claude_md = home / ".claude" / "CLAUDE.md"
    claude_md.parent.mkdir(parents=True)
    original = (
        "Never run `engmem search` on the production store.\n"
        "Some other unrelated rule.\n"
    )
    claude_md.write_text(original, encoding="utf-8")

    exit_code = _run_install("--agent", "claude", "--store", str(store))
    out = capsys.readouterr().out

    assert exit_code == 0
    assert claude_md.read_text(encoding="utf-8") == original, (
        "install must not touch a file it decided already carries the rule"
    )
    assert "already present" in out.lower() or "not added" in out.lower(), (
        "a skipped trigger rule must be reported on stdout, not left silent"
    )


def test_fresh_install_trigger_rule_is_sentinel_delimited(env, tmp_path):
    """D12: detection must anchor on a marker engmem writes itself, not a substring a user's own
    prose could contain."""
    from engmem.install import TRIGGER_SENTINEL

    home, project = env
    store = tmp_path / "store"

    _run_install("--agent", "claude", "--store", str(store))

    content = (home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert TRIGGER_SENTINEL in content


# D16: an existing regular file at a destination path used to raise NotADirectoryError (or
# exit 1 with a traceback, for the commands path) instead of a clean exit-2 diagnostic. The
# directory checks covered two of the paths install writes; every other one — the template
# files themselves, the copilot-cli skill directories, an instructions file engmem cannot
# decode — still crashed with a traceback, which §5 rules out for all of them.


def _occupy_store(home, project, store):
    store.write_text("not a directory", encoding="utf-8")
    return store


def _occupy_commands_dir(home, project, store):
    blocked = home / ".claude" / "commands"
    blocked.parent.mkdir(parents=True)
    blocked.write_text("not a directory", encoding="utf-8")
    return blocked


def _occupy_template_path(home, project, store):
    blocked = home / ".claude" / "commands" / "engmem.md"
    blocked.mkdir(parents=True)
    return blocked


def _write_undecodable_instructions(home, project, store):
    blocked = home / ".claude" / "CLAUDE.md"
    blocked.parent.mkdir(parents=True)
    blocked.write_bytes(b"\xff\xfe not utf-8 at all\n")
    return blocked


def _occupy_desktop_config_dir(home, project, store):
    blocked = claude_desktop_config_path(home).parent
    blocked.parent.mkdir(parents=True)
    blocked.write_text("not a directory", encoding="utf-8")
    return blocked


def _occupy_skill_dir(home, project, store):
    blocked = home / ".copilot" / "skills" / "engmem"
    blocked.parent.mkdir(parents=True)
    blocked.write_text("not a directory", encoding="utf-8")
    return blocked


UNUSABLE_DESTINATION_CASES = [
    pytest.param("claude", _occupy_store, id="store-path-is-a-file"),
    pytest.param("claude", _occupy_commands_dir, id="commands-dir-is-a-file"),
    pytest.param("claude", _occupy_template_path, id="template-path-is-a-directory"),
    pytest.param("claude", _write_undecodable_instructions, id="instructions-are-not-utf-8"),
    pytest.param("copilot-cli", _occupy_skill_dir, id="skill-dir-is-a-file"),
    pytest.param(
        "claude-desktop", _occupy_desktop_config_dir, id="desktop-config-dir-is-a-file"
    ),
]


@pytest.mark.parametrize(("agent", "block"), UNUSABLE_DESTINATION_CASES)
def test_a_destination_install_cannot_use_fails_with_exit_2(env, tmp_path, capsys, agent, block):
    home, project = env
    store = tmp_path / "store"

    blocked_path = block(home, project, store)
    exit_code = _run_install("--agent", agent, "--store", str(store))
    out = capsys.readouterr()

    assert exit_code == 2, "a blocked destination must be diagnosed, never a traceback"
    assert out.err.startswith("error:")
    assert str(blocked_path) in out.err
    assert str(blocked_path) in out.out, "the failure must be visible on stdout too (D14)"


UNDECODABLE_USER_FILE_CASES = [
    pytest.param("claude", lambda home: home / ".claude" / "CLAUDE.md", id="instructions"),
    # the config is read through the same helper, and an undecodable one would otherwise reach
    # `json.loads` as a UnicodeDecodeError — a ValueError `cmd_install` does not catch
    pytest.param(
        "claude-desktop", claude_desktop_config_path, id="claude-desktop-config"
    ),
]


@pytest.mark.parametrize(("agent", "path_fn"), UNDECODABLE_USER_FILE_CASES)
def test_a_user_file_engmem_cannot_decode_is_left_untouched(env, tmp_path, capsys, agent, path_fn):
    """Install rewrites both of these files whole; one it could not read would be rewritten from a
    lossy or empty reading of it."""
    home, project = env
    store = tmp_path / "store"
    path = path_fn(home)
    path.parent.mkdir(parents=True)
    original = "# Мои правила\n".encode("cp1251")
    path.write_bytes(original)

    exit_code = _run_install("--agent", agent, "--store", str(store))
    out = capsys.readouterr()

    assert exit_code == 2, "an undecodable file must be diagnosed, never a traceback"
    assert path.read_bytes() == original
    assert "UTF-8" in out.err and str(path) in out.err
    assert "UTF-8" in out.out and str(path) in out.out, (
        "the cause must reach the stdout-only consumer too (D14)"
    )


@pytest.mark.parametrize(("original", "newline"), INSTRUCTIONS_BYTE_CASES)
def test_install_appends_without_rewriting_the_bytes_already_there(
    env, tmp_path, original, newline
):
    r"""`read_text` translates the line endings and hides the BOM, so a caller that rebuilds the
    file from what it read hands the user a whole-file diff for a two-line append."""
    from engmem.install import TRIGGER_SENTINEL

    home, project = env
    store = tmp_path / "store"
    claude_md = home / ".claude" / "CLAUDE.md"
    claude_md.parent.mkdir(parents=True)
    claude_md.write_bytes(original)

    exit_code = _run_install("--agent", "claude", "--store", str(store))

    assert exit_code == 0
    appended = TRIGGER_SENTINEL.encode("utf-8") + newline + TRIGGER_RULE.encode("utf-8") + newline
    assert claude_md.read_bytes() == original + appended


@requires_symlinks
def test_a_symlinked_instructions_file_is_written_through_not_replaced(env, tmp_path):
    """A CLAUDE.md symlinked into a dotfiles repo is the common setup; replacing the link with a
    regular file detaches it from the repo that is supposed to track it."""
    home, project = env
    store = tmp_path / "store"
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    real = dotfiles / "CLAUDE.md"
    real.write_text("# My rules\n", encoding="utf-8")
    claude_md = home / ".claude" / "CLAUDE.md"
    claude_md.parent.mkdir(parents=True)
    claude_md.symlink_to(real)

    exit_code = _run_install("--agent", "claude", "--store", str(store))

    assert exit_code == 0
    assert claude_md.is_symlink(), "install must not turn the link into a regular file"
    content = real.read_text(encoding="utf-8")
    assert content.startswith("# My rules\n")
    assert TRIGGER_RULE in content, "the rule belongs in the file the link points at"


# --- claude-desktop: writes one entry into Claude Desktop's own MCP config file,
# no templates and no trigger rule (there is no commands directory or global
# instructions file for this agent) ------------------------------------------------

import json
import os
import sys


def test_claude_desktop_install_writes_mcp_server_entry(env, tmp_path):
    home, project = env
    store = tmp_path / "store"

    exit_code = _run_install("--agent", "claude-desktop", "--store", str(store))

    assert exit_code == 0
    config_path = claude_desktop_config_path(home)
    assert config_path.is_file()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    entry = config["mcpServers"]["engmem"]
    assert entry["command"] == sys.executable
    assert entry["args"] == ["-m", "engmem.cli", "mcp", "--store", str(store)]


def test_claude_desktop_config_records_a_relative_store_as_an_absolute_path(env):
    """The entry outlives this process: Claude Desktop launches the server from a working
    directory of its own, where `--store notes` names a different directory, or none."""
    home, project = env

    exit_code = _run_install("--agent", "claude-desktop", "--store", "notes")

    assert exit_code == 0
    config = json.loads(claude_desktop_config_path(home).read_text(encoding="utf-8"))
    args = config["mcpServers"]["engmem"]["args"]
    recorded = Path(args[args.index("--store") + 1])

    assert recorded.is_absolute(), f"a relative store was recorded verbatim: {recorded}"
    assert recorded == project / "notes"


def test_claude_desktop_install_writes_no_templates_and_no_trigger_rule(env, tmp_path):
    """Claude Desktop has no commands directory and no instructions file, so install writes only
    the config entry."""
    home, project = env
    store = tmp_path / "store"

    _run_install("--agent", "claude-desktop", "--store", str(store))

    assert not (home / ".claude" / "commands").exists()
    assert not (home / ".claude" / "CLAUDE.md").exists()


def test_claude_desktop_install_does_not_require_git_repo_cwd(env, tmp_path):
    home, project = env
    store = tmp_path / "store"

    exit_code = _run_install("--agent", "claude-desktop", "--store", str(store))

    assert exit_code == 0


def test_claude_desktop_install_merges_into_existing_config_preserving_other_servers(env, tmp_path):
    """The config belongs to the user and may already list other MCP servers, so install must
    merge, never overwrite."""
    home, project = env
    store = tmp_path / "store"
    config_path = claude_desktop_config_path(home)
    config_path.parent.mkdir(parents=True)
    original = {
        "mcpServers": {
            "some-other-server": {"command": "/usr/bin/other", "args": ["--flag"]}
        },
        "someUnrelatedTopLevelKey": True,
    }
    config_path.write_text(json.dumps(original), encoding="utf-8")

    exit_code = _run_install("--agent", "claude-desktop", "--store", str(store))

    assert exit_code == 0
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["mcpServers"]["some-other-server"] == original["mcpServers"]["some-other-server"]
    assert config["someUnrelatedTopLevelKey"] is True
    assert config["mcpServers"]["engmem"]["command"] == sys.executable


def test_claude_desktop_install_accepts_a_config_that_carries_a_byte_order_mark(env, tmp_path):
    """`json.loads` rejects a leading BOM, so a reading that kept one would refuse the install on a
    config that is perfectly valid."""
    home, project = env
    store = tmp_path / "store"
    config_path = claude_desktop_config_path(home)
    config_path.parent.mkdir(parents=True)
    original = {"mcpServers": {"some-other-server": {"command": "/usr/bin/other", "args": []}}}
    config_path.write_bytes(b"\xef\xbb\xbf" + json.dumps(original).encode("utf-8"))

    exit_code = _run_install("--agent", "claude-desktop", "--store", str(store))

    assert exit_code == 0, "a BOM-prefixed config must be read, not rejected as invalid JSON"
    written = config_path.read_bytes()
    assert not written.startswith(b"\xef\xbb\xbf"), (
        "engmem re-serialises this document whole, and RFC 8259 forbids emitting a BOM"
    )
    config = json.loads(written.decode("utf-8"))
    assert config["mcpServers"]["engmem"]["command"] == sys.executable
    assert config["mcpServers"]["some-other-server"] == original["mcpServers"]["some-other-server"]


def test_claude_desktop_install_creates_config_when_absent(env, tmp_path):
    home, project = env
    store = tmp_path / "store"
    config_path = claude_desktop_config_path(home)
    assert not config_path.exists()

    exit_code = _run_install("--agent", "claude-desktop", "--store", str(store))

    assert exit_code == 0
    assert config_path.is_file()


def test_claude_desktop_install_is_idempotent(env, tmp_path):
    home, project = env
    store = tmp_path / "store"

    _run_install("--agent", "claude-desktop", "--store", str(store))
    config_path = claude_desktop_config_path(home)
    before = config_path.read_bytes()

    exit_code = _run_install("--agent", "claude-desktop", "--store", str(store))

    assert exit_code == 0
    assert config_path.read_bytes() == before


def test_claude_desktop_install_rejects_malformed_config_without_discarding_it(env, tmp_path, capsys):
    home, project = env
    store = tmp_path / "store"
    config_path = claude_desktop_config_path(home)
    config_path.parent.mkdir(parents=True)
    original = "{not valid json"
    config_path.write_text(original, encoding="utf-8")

    exit_code = _run_install("--agent", "claude-desktop", "--store", str(store))
    out = capsys.readouterr()

    assert exit_code == 2
    assert out.err.startswith("error:")
    assert config_path.read_text(encoding="utf-8") == original, (
        "a malformed config must never be silently discarded or overwritten"
    )


def test_claude_desktop_install_survives_a_failed_write_without_losing_the_config(
    env, tmp_path, capsys, monkeypatch
):
    """The file holds every other MCP server the user configured, and engmem rewrites it whole —
    a torn write costs them entries engmem cannot put back."""
    home, project = env
    store = tmp_path / "store"
    config_path = claude_desktop_config_path(home)
    config_path.parent.mkdir(parents=True)
    original = json.dumps(
        {"mcpServers": {"some-other-server": {"command": "/usr/bin/other", "args": []}}}
    ).encode("utf-8")
    config_path.write_bytes(original)

    def refuse_to_commit(tmp, target):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("engmem.install.commit", refuse_to_commit)

    exit_code = _run_install("--agent", "claude-desktop", "--store", str(store))
    out = capsys.readouterr()

    assert exit_code == 2
    assert config_path.read_bytes() == original, (
        "a failed write must leave the config exactly as it was"
    )
    assert [p.name for p in config_path.parent.iterdir()] == [config_path.name], (
        "the staged temp file must not be left behind"
    )
    assert str(config_path) in out.out


def test_an_interrupt_during_the_commit_leaves_no_staged_copy_behind(env, tmp_path, monkeypatch):
    """The staged file is a full copy of a config that routinely carries other servers' API
    tokens, so an OSError-only cleanup orphans one on every interrupted run."""
    home, project = env
    store = tmp_path / "store"
    config_path = claude_desktop_config_path(home)
    config_path.parent.mkdir(parents=True)
    original = json.dumps(
        {"mcpServers": {"some-other-server": {"command": "/usr/bin/other", "args": []}}}
    ).encode("utf-8")
    config_path.write_bytes(original)

    def interrupted_commit(tmp, target):
        raise KeyboardInterrupt

    monkeypatch.setattr("engmem.install.commit", interrupted_commit)

    with pytest.raises(KeyboardInterrupt):
        _run_install("--agent", "claude-desktop", "--store", str(store))

    assert config_path.read_bytes() == original
    assert [p.name for p in config_path.parent.iterdir()] == [config_path.name], (
        "the staged copy must not outlive the interrupt that stopped the commit"
    )


@requires_permission_enforcement
def test_claude_desktop_install_into_an_unwritable_directory_fails_with_exit_2(
    env, tmp_path, capsys
):
    home, project = env
    store = tmp_path / "store"
    config_path = claude_desktop_config_path(home)
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{}", encoding="utf-8")
    os.chmod(config_path.parent, 0o500)
    try:
        exit_code = _run_install("--agent", "claude-desktop", "--store", str(store))
        out = capsys.readouterr()
    finally:
        os.chmod(config_path.parent, 0o700)

    assert exit_code == 2, "an unwritable config directory must be diagnosed, not a traceback"
    assert str(config_path) in out.err
    assert str(config_path) in out.out


@requires_posix_modes
def test_a_claude_desktop_config_engmem_creates_is_private(env, tmp_path):
    """Other servers' entries in this file routinely carry API tokens, and engmem is the one
    creating it when it is absent."""
    import stat

    home, project = env
    store = tmp_path / "store"

    _run_install("--agent", "claude-desktop", "--store", str(store))

    config_path = claude_desktop_config_path(home)
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


@requires_posix_modes
def test_installing_into_an_existing_config_keeps_the_mode_the_user_set(env, tmp_path):
    import stat

    home, project = env
    store = tmp_path / "store"
    config_path = claude_desktop_config_path(home)
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{}", encoding="utf-8")
    os.chmod(config_path, 0o640)

    _run_install("--agent", "claude-desktop", "--store", str(store))

    assert stat.S_IMODE(config_path.stat().st_mode) == 0o640


HOME_SCOPED_AGENT_REJECTS_LOCAL_CASES = [
    # claude-desktop always writes under $HOME, so `--local` would silently be a no-op
    pytest.param(
        "claude-desktop", lambda home: claude_desktop_config_path(home), id="claude-desktop"
    ),
    # copilot-cli reads skills only from `~/.copilot/skills`, so `--local` cannot be honoured
    pytest.param("copilot-cli", lambda home: home / ".copilot", id="copilot-cli"),
]


@pytest.mark.parametrize(("agent", "sentinel_path_fn"), HOME_SCOPED_AGENT_REJECTS_LOCAL_CASES)
def test_home_scoped_agent_rejects_local_flag(env, tmp_path, capsys, agent, sentinel_path_fn):
    home, project = env
    store = tmp_path / "store"

    exit_code = _run_install("--agent", agent, "--local", "--store", str(store))
    err = capsys.readouterr().err

    assert exit_code == 2
    assert "--local" in err
    assert agent in err
    assert not sentinel_path_fn(home).exists()


# ---------------------------------------------------------------------------
# Claude Desktop's config lives in a different place per platform, and only one
# of those branches is ever taken on the machine running the suite.
# ---------------------------------------------------------------------------


def test_claude_desktop_config_path_on_windows(monkeypatch, tmp_path):
    from engmem.install import _claude_desktop_config_path

    monkeypatch.setattr("engmem.install.sys.platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))

    path = _claude_desktop_config_path()

    assert path == tmp_path / "Roaming" / "Claude" / "claude_desktop_config.json"


def test_claude_desktop_config_path_on_windows_without_appdata(monkeypatch, tmp_path):
    """A stripped environment without %APPDATA% must still resolve somewhere sane rather than
    crashing."""
    from engmem.install import _claude_desktop_config_path

    monkeypatch.setattr("engmem.install.sys.platform", "win32")
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    path = _claude_desktop_config_path()

    assert path == tmp_path / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"


def test_claude_desktop_config_path_off_windows(monkeypatch, tmp_path):
    from engmem.install import _claude_desktop_config_path

    monkeypatch.setattr("engmem.install.sys.platform", "darwin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    path = _claude_desktop_config_path()

    assert path == (
        tmp_path / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    )


def test_the_sandbox_home_is_actually_where_the_code_looks(env):
    """`setenv("HOME")` is a no-op on Windows, where `expanduser` reads USERPROFILE, then
    HOMEDRIVE+HOMEPATH."""
    home, _ = env

    assert Path.home() == home
    assert Path(os.path.expanduser("~")) == home
    assert claude_desktop_config_path(home).is_relative_to(home), (
        "the Desktop config must resolve inside the sandbox on every platform"
    )
