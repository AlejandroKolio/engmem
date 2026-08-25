from pathlib import Path

import pytest

from conftest import redirect_home

from engmem.cli import main

SOURCE_TEMPLATE_NAMES = ("engmem.start.md", "engmem.save.md", "engmem.save.quick.md")
INSTALLED_NAMES = ("engmem.md", "engmem.save.md", "engmem.save.quick.md")
INSTALLED_NAMES_COPILOT_IDE = (
    "engmem.prompt.md",
    "engmem.save.prompt.md",
    "engmem.save.quick.prompt.md",
)
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


def test_fresh_install_creates_store_templates_stamp_and_trigger(env, tmp_path):
    home, project = env
    store = tmp_path / "store"

    exit_code = _run_install("--agent", "claude", "--store", str(store))

    assert exit_code == 0
    assert (store / "sessions").is_dir()
    assert (store / ".git").is_dir()

    commands_dir = home / ".claude" / "commands"
    for name in INSTALLED_NAMES:
        _assert_installed_template(commands_dir / name, name)

    assert not (commands_dir / "engmem.start.md").exists(), (
        "start template must install as engmem.md so the command is /engmem, "
        "not /engmem.start"
    )

    claude_md = home / ".claude" / "CLAUDE.md"
    assert claude_md.is_file()
    assert claude_md.read_text(encoding="utf-8").count(TRIGGER_RULE) == 1


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


def test_local_flag_scopes_to_project_directory(env, tmp_path):
    home, project = env
    (project / ".git").mkdir()

    exit_code = _run_install("--agent", "claude", "--local")

    assert exit_code == 0

    commands_dir = project / ".claude" / "commands"
    for name in INSTALLED_NAMES:
        _assert_installed_template(commands_dir / name, name)

    assert not (home / ".claude" / "commands").exists()

    claude_md = project / "CLAUDE.md"
    assert claude_md.is_file()
    assert claude_md.read_text(encoding="utf-8").count(TRIGGER_RULE) == 1


def test_copilot_ide_agent_installs_prompt_files_to_github_prompts(env, tmp_path):
    home, project = env
    (project / ".git").mkdir()
    store = tmp_path / "store"

    exit_code = _run_install("--agent", "copilot-ide", "--store", str(store))

    assert exit_code == 0

    prompts_dir = project / ".github" / "prompts"
    for name in INSTALLED_NAMES_COPILOT_IDE:
        _assert_installed_template(prompts_dir / name, name)

    assert not (prompts_dir / "engmem.md").exists(), (
        "the Copilot IDE plugin only reads .prompt.md — plain .md is ignored"
    )

    instructions = project / ".github" / "copilot-instructions.md"
    assert instructions.is_file()
    assert instructions.read_text(encoding="utf-8").count(TRIGGER_RULE) == 1


def test_bare_copilot_agent_value_is_rejected(env, capsys):
    # D14: argparse `choices=` would reject this via `parser.error()`, which is
    # stderr-only and raises SystemExit before `_cmd_install` ever runs — invisible
    # to the stdout-only reader. An unknown --agent must fail the same way every
    # other usage error here does: `_fail()`, exit 2, no exception.
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
    """Skill names can't contain dots, so the commands are /engmem-save and
    /engmem-save-quick — but the template bodies cross-reference the Claude
    spellings (/engmem.save, /engmem.save.quick). Installed SKILL.md files must
    carry the names the Copilot CLI user can actually type."""
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
    """Exit code 2, like every other CLI-usage failure in this tool — not the 1 that a
    string-valued SystemExit yields, and not an exception escaping main()."""
    home, project = env
    store = tmp_path / "store"
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))

    code = _run_install("--agent", "claude", "--store", str(store))
    err = capsys.readouterr().err

    assert code == 2
    assert "git" in err
    assert err.startswith("error:")


def test_template_without_front_matter_names_the_cause_not_stopiteration():
    """`assert` is compiled away under `python -O`, leaving a bare StopIteration from
    the closing-fence scan — a failure with no diagnosable cause (principle VIII)."""
    from engmem.install import _to_skill_front_matter

    with pytest.raises(ValueError) as excinfo:
        _to_skill_front_matter("no front matter here\n", "engmem")

    assert "front matter" in str(excinfo.value)
    assert "engmem" in str(excinfo.value)


def test_template_with_unclosed_front_matter_names_the_cause():
    from engmem.install import _to_skill_front_matter

    with pytest.raises(ValueError) as excinfo:
        _to_skill_front_matter("---\ndescription: x\n\nBody with no closing fence\n", "engmem")

    assert "front matter" in str(excinfo.value)


def test_install_reports_on_stdout_when_trigger_rule_already_present(env, tmp_path, capsys):
    """D12: a silent skip left no way to tell an install that added the rule apart
    from one that quietly did nothing — and the substring `TRIGGER_MARKER` used to
    detect "already present" can match a user's own unrelated prose that happens to
    mention `engmem search`, so that skip needs to be visible, not silent."""
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
    """D12: engmem must anchor detection/removal on a marker it writes itself, not
    a bare substring an unrelated user sentence could also contain."""
    from engmem.install import TRIGGER_SENTINEL

    home, project = env
    store = tmp_path / "store"

    _run_install("--agent", "claude", "--store", str(store))

    content = (home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert TRIGGER_SENTINEL in content


def test_store_path_is_an_existing_file_fails_with_exit_2(env, tmp_path, capsys):
    """D16: `_ensure_store`'s docstring promises exit 2 for precondition failures;
    an existing regular file at the store path used to raise NotADirectoryError and
    exit 1 with a traceback instead."""
    home, project = env
    store = tmp_path / "store"
    store.write_text("not a directory", encoding="utf-8")

    exit_code = _run_install("--agent", "claude", "--store", str(store))
    out = capsys.readouterr()

    assert exit_code == 2
    assert out.err.startswith("error:")
    assert str(store) in out.err
    assert str(store) in out.out, "the failure must be visible on stdout too (D14)"


def test_claude_commands_destination_is_an_existing_file_fails_with_exit_2(env, tmp_path, capsys):
    """D16: an existing regular file at `~/.claude/commands` used to raise
    FileExistsError from `dest_dir.mkdir(...)` and exit 1 with a traceback, after the
    store had already been created (partial install)."""
    home, project = env
    store = tmp_path / "store"
    commands_path = home / ".claude" / "commands"
    commands_path.parent.mkdir(parents=True)
    commands_path.write_text("not a directory", encoding="utf-8")

    exit_code = _run_install("--agent", "claude", "--store", str(store))
    out = capsys.readouterr()

    assert exit_code == 2
    assert out.err.startswith("error:")
    assert str(commands_path) in out.err
    assert str(commands_path) in out.out, "the failure must be visible on stdout too (D14)"


# --- claude-desktop: writes one entry into Claude Desktop's own MCP config file,
# no templates and no trigger rule (there is no commands directory or global
# instructions file for this agent) ------------------------------------------------

import json
import os
import sys


def _claude_desktop_config_path(home: Path) -> Path:
    """Mirrors `install._claude_desktop_config_path`. Hardcoding the macOS tree here made
    every claude-desktop test fail on Windows, where the product correctly writes under
    %APPDATA% — the test was asserting the wrong location, not the code."""
    if sys.platform == "win32":
        return home / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
    return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"


def test_claude_desktop_install_writes_mcp_server_entry(env, tmp_path):
    home, project = env
    store = tmp_path / "store"

    exit_code = _run_install("--agent", "claude-desktop", "--store", str(store))

    assert exit_code == 0
    config_path = _claude_desktop_config_path(home)
    assert config_path.is_file()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    entry = config["mcpServers"]["engmem"]
    assert entry["command"] == sys.executable
    assert entry["args"] == ["-m", "engmem.cli", "mcp", "--store", str(store)]


def test_claude_desktop_install_writes_no_templates_and_no_trigger_rule(env, tmp_path):
    """Claude Desktop has no commands directory and no global instructions file to
    append a trigger rule to — install for this mode writes only the config entry."""
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
    """The config file belongs to the user and may already list other MCP servers —
    install must merge the `engmem` key, never overwrite the file wholesale."""
    home, project = env
    store = tmp_path / "store"
    config_path = _claude_desktop_config_path(home)
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


def test_claude_desktop_install_creates_config_when_absent(env, tmp_path):
    home, project = env
    store = tmp_path / "store"
    config_path = _claude_desktop_config_path(home)
    assert not config_path.exists()

    exit_code = _run_install("--agent", "claude-desktop", "--store", str(store))

    assert exit_code == 0
    assert config_path.is_file()


def test_claude_desktop_install_is_idempotent(env, tmp_path):
    home, project = env
    store = tmp_path / "store"

    _run_install("--agent", "claude-desktop", "--store", str(store))
    config_path = _claude_desktop_config_path(home)
    before = config_path.read_bytes()

    exit_code = _run_install("--agent", "claude-desktop", "--store", str(store))

    assert exit_code == 0
    assert config_path.read_bytes() == before


def test_claude_desktop_install_rejects_malformed_config_without_discarding_it(env, tmp_path, capsys):
    home, project = env
    store = tmp_path / "store"
    config_path = _claude_desktop_config_path(home)
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


def test_claude_desktop_agent_rejects_local_flag(env, tmp_path, capsys):
    """claude-desktop always writes under $HOME, never a project directory — --local
    would silently be a no-op, so it is rejected explicitly instead."""
    home, project = env
    store = tmp_path / "store"

    exit_code = _run_install("--agent", "claude-desktop", "--local", "--store", str(store))
    err = capsys.readouterr().err

    assert exit_code == 2
    assert "--local" in err
    assert "claude-desktop" in err
    assert not _claude_desktop_config_path(home).exists()


def test_copilot_cli_agent_rejects_local_flag(env, tmp_path, capsys):
    """copilot-cli reads skills only from ~/.copilot/skills, so --local cannot be
    honoured. Accepting it silently installed to $HOME anyway — and, because --local
    also demands a git repo root, first refused to run outside one for no reason."""
    home, project = env
    store = tmp_path / "store"

    exit_code = _run_install("--agent", "copilot-cli", "--local", "--store", str(store))
    out, err = capsys.readouterr()

    assert exit_code == 2
    assert "--local" in err
    assert "copilot-cli" in err
    assert not (home / ".copilot").exists()


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
    """%APPDATA% is normally set, but a stripped environment (a service account, a
    minimal CI shell) must still resolve somewhere sane rather than crashing."""
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
    """The bug this guards: `monkeypatch.setenv("HOME", ...)` is a no-op on Windows —
    `expanduser` there reads `USERPROFILE`, then `HOMEDRIVE`+`HOMEPATH`, never `HOME`. The
    install tests passed on POSIX while writing into the runner's real profile, and the
    first Windows CI run reported exactly that."""
    home, _ = env

    assert Path.home() == home
    assert Path(os.path.expanduser("~")) == home
    assert _claude_desktop_config_path(home).is_relative_to(home), (
        "the Desktop config must resolve inside the sandbox on every platform"
    )
