"""US-05: one saved store choice, read by every command run without an override, never rewritten
by one, and never swapped for another store when it cannot be used."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import (
    DRAFT_CONTENT,
    FIXTURES,
    claude_desktop_config_path,
    redirect_home,
    requires_permission_enforcement,
    requires_symlinks,
)

from engmem import runtime
from engmem.cli import main
from engmem.runtime import StoreSettingError, StoreSource, locate_store, resolve_store


@pytest.fixture
def home(tmp_path, monkeypatch):
    redirect_home(monkeypatch, tmp_path / "home")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    return tmp_path / "home"


@pytest.fixture
def setting(monkeypatch, tmp_path) -> Path:
    config = tmp_path / "xdg-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    return config / "engmem" / "store"


def _store_with_docs(root: Path) -> Path:
    (root / "sessions").mkdir(parents=True)
    for doc in FIXTURES.glob("*.md"):
        (root / "sessions" / doc.name).write_bytes(doc.read_bytes())
    return root


def _save(setting: Path, content: bytes) -> None:
    setting.parent.mkdir(parents=True, exist_ok=True)
    setting.write_bytes(content)


def _fixture_ids() -> list[str]:
    return [doc.stem for doc in FIXTURES.glob("*.md")]


def _search(capsys, *args: str) -> tuple[int, str, str]:
    code = main(["search", "cache", *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --- where the choice lives ----------------------------------------------------------------


def test_the_setting_file_honours_xdg_config_home(setting):
    assert runtime.store_setting_file() == setting


def test_without_xdg_config_home_the_setting_lives_under_dot_config(home, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr("engmem.runtime.sys.platform", "darwin")

    assert runtime.store_setting_file() == home / ".config" / "engmem" / "store"


def test_a_relative_xdg_config_home_is_ignored(home, monkeypatch):
    """The XDG spec calls a relative value invalid; honouring it would make the saved choice
    depend on the directory each command happens to run in."""
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative-config")
    monkeypatch.setattr("engmem.runtime.sys.platform", "darwin")

    assert runtime.store_setting_file() == home / ".config" / "engmem" / "store"


@pytest.mark.parametrize("appdata", [True, False], ids=["appdata", "no-appdata"])
def test_on_windows_the_setting_lives_under_appdata(home, monkeypatch, tmp_path, appdata):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr("engmem.runtime.sys.platform", "win32")
    if appdata:
        monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    else:
        monkeypatch.delenv("APPDATA", raising=False)

    expected = (tmp_path / "roaming") if appdata else home / "AppData" / "Roaming"
    assert runtime.store_setting_file() == expected / "engmem" / "store"


# --- AC-05.1: a saved choice is what a command without overrides uses ------------------------


def test_no_saved_choice_keeps_the_previous_default(home, setting):
    choice = locate_store(None)

    assert choice.path == home / "Developer" / "engmem"
    assert choice.source is StoreSource.DEFAULT
    assert not setting.exists()


def test_a_saved_choice_is_used_without_overrides(home, setting, tmp_path):
    _save(setting, f"{tmp_path / 'notes'}\n".encode())

    choice = locate_store(None)

    assert choice.path == tmp_path / "notes"
    assert choice.source is StoreSource.SAVED


@pytest.mark.parametrize("ending", ["", "\n", "\r\n"], ids=["bare", "lf", "crlf"])
def test_the_one_line_may_end_with_or_without_a_newline(home, setting, tmp_path, ending):
    _save(setting, f"{tmp_path / 'notes'}{ending}".encode())

    assert resolve_store(None) == tmp_path / "notes"


def test_a_utf8_bom_from_a_windows_editor_is_not_part_of_the_path(home, setting, tmp_path):
    _save(setting, b"\xef\xbb\xbf" + str(tmp_path / "notes").encode())

    assert resolve_store(None) == tmp_path / "notes"


def test_a_leading_tilde_in_the_file_is_expanded(home, setting):
    _save(setting, b"~/notes\n")

    assert resolve_store(None) == home / "notes"


def test_store_set_then_a_new_cli_process_searches_the_saved_store(home, setting, tmp_path, capsys):
    store = _store_with_docs(tmp_path / "notes")

    assert main(["store", "set", str(store)]) == 0
    capsys.readouterr()
    result = subprocess.run(
        [sys.executable, "-m", "engmem.cli", "search", "cache"],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert any(doc_id in result.stdout for doc_id in _fixture_ids())


def test_store_set_writes_one_absolute_line(home, setting, capsys):
    assert main(["store", "set", "notes"]) == 0

    expected = Path.cwd() / "notes"
    assert setting.read_bytes() == f"{expected}\n".encode()
    out = capsys.readouterr().out
    assert f"saved {expected}" in out and str(setting) in out


def test_store_set_overwrites_a_setting_it_cannot_read(home, setting, tmp_path):
    """The command that names the broken file has to be the one that can fix it."""
    _save(setting, b"\xff\xfe")

    assert main(["store", "set", str(tmp_path / "notes")]) == 0

    assert resolve_store(None) == tmp_path / "notes"


@requires_symlinks
def test_store_set_writes_through_a_symlinked_setting(home, setting, tmp_path):
    """A dotfiles checkout holding the real file is the ordinary reason for the link."""
    real = tmp_path / "dotfiles" / "engmem-store"
    real.parent.mkdir()
    real.write_bytes(b"/old\n")
    setting.parent.mkdir(parents=True)
    setting.symlink_to(real)

    assert main(["store", "set", str(tmp_path / "notes")]) == 0

    assert setting.is_symlink()
    assert real.read_bytes() == f"{tmp_path / 'notes'}\n".encode()


def test_store_set_says_the_store_does_not_exist_yet(home, setting, tmp_path, capsys):
    assert main(["store", "set", str(tmp_path / "not-yet")]) == 0

    assert "engmem install" in capsys.readouterr().out


def test_store_show_names_the_store_on_its_first_line(home, setting, tmp_path, capsys):
    """The installed templates read the store path from this line before writing a draft."""
    store = _store_with_docs(tmp_path / "notes")
    _save(setting, f"{store}\n".encode())

    assert main(["store", "show"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"store: {store}"
    assert lines[1] == f"source: saved choice ({setting})"


def test_store_show_without_a_saved_choice_names_the_default(home, setting, capsys):
    assert main(["store", "show"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"store: {home / 'Developer' / 'engmem'}"
    assert lines[1].startswith("source: default") and str(setting) in lines[1]


def test_the_start_template_reads_the_store_from_store_show():
    from engmem.install import _template_source

    text = _template_source("engmem.start.md")

    assert "engmem store show" in text
    assert "store/engmem" not in text  # the XDG path is spelled right where it is spelled out
    assert "engmem/store" in text


# --- AC-05.2: precedence, and an override never rewrites the saved choice --------------------


@pytest.mark.parametrize(
    ("flag", "env", "saved", "expected", "source"),
    [
        pytest.param("flag", "env", "saved", "flag", StoreSource.FLAG, id="flag-wins"),
        pytest.param(None, "env", "saved", "env", StoreSource.ENV, id="env-over-saved"),
        pytest.param(None, None, "saved", "saved", StoreSource.SAVED, id="saved-over-default"),
        pytest.param(None, " ", "saved", "saved", StoreSource.SAVED, id="blank-env-is-unset"),
    ],
)
def test_precedence_is_flag_then_env_then_saved_then_default(
    home, setting, monkeypatch, tmp_path, flag, env, saved, expected, source
):
    _save(setting, f"{tmp_path / saved}\n".encode())
    if env is not None:
        monkeypatch.setenv("ENGMEM_HOME", str(tmp_path / env) if env.strip() else env)

    choice = locate_store(str(tmp_path / flag) if flag else None)

    assert choice.path == tmp_path / expected
    assert choice.source is source


@pytest.mark.parametrize("override", ["flag", "env"])
def test_an_override_never_reads_a_broken_setting(home, setting, monkeypatch, tmp_path, override):
    """A one-off store must stay usable while the saved one is being repaired."""
    _save(setting, b"")
    if override == "env":
        monkeypatch.setenv("ENGMEM_HOME", str(tmp_path / "other"))

    assert resolve_store(str(tmp_path / "other") if override == "flag" else None) == tmp_path / "other"


@pytest.mark.parametrize("override", ["flag", "env"])
def test_commands_with_an_override_leave_the_saved_choice_unchanged(
    home, setting, monkeypatch, tmp_path, capsys, override
):
    saved = f"{tmp_path / 'saved'}\n".encode()
    _save(setting, saved)
    other = _store_with_docs(tmp_path / "other")
    flag = ["--store", str(other)] if override == "flag" else []
    if override == "env":
        monkeypatch.setenv("ENGMEM_HOME", str(other))

    assert main(["search", "cache", *flag]) == 0
    assert main(["install", "--agent", "claude", *flag]) == 0
    assert main(["store", "show", *flag]) == 0

    assert setting.read_bytes() == saved
    out = capsys.readouterr().out
    assert f"overrides the saved choice {tmp_path / 'saved'}" in out


def test_install_with_a_store_flag_creates_no_saved_choice(home, setting, tmp_path, capsys):
    assert main(["install", "--agent", "claude", "--store", str(tmp_path / "notes")]) == 0

    assert not setting.exists()
    assert f"engmem store set {tmp_path / 'notes'}" in capsys.readouterr().out


def test_install_without_flags_uses_the_saved_choice(home, setting, tmp_path, capsys):
    _save(setting, f"{tmp_path / 'notes'}\n".encode())

    assert main(["install", "--agent", "claude"]) == 0

    assert (tmp_path / "notes" / "sessions").is_dir()
    out = capsys.readouterr().out
    assert f"store={tmp_path / 'notes'}" in out
    assert "note:" not in out


def test_store_set_under_engmem_home_says_the_env_still_wins(home, setting, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ENGMEM_HOME", str(tmp_path / "env"))

    assert main(["store", "set", str(tmp_path / "notes")]) == 0

    out = capsys.readouterr().out
    assert "ENGMEM_HOME" in out and "precedence" in out


# --- AC-05.3: an unusable choice is named, and no other store is used instead ---------------


BROKEN_SETTINGS = [
    pytest.param(b"", "empty", id="empty"),
    pytest.param(b"   \n", "empty", id="blank"),
    pytest.param(b"/one\n/two\n", "one line", id="two-lines"),
    pytest.param(b"/one\n\n", "one line", id="trailing-blank-line"),
    pytest.param(b"notes\n", "absolute", id="relative"),
    pytest.param(b"\xff\xfe/notes", "UTF-8", id="not-utf8"),
    pytest.param(b"/no\x00tes", "NUL", id="nul"),
]


@pytest.mark.parametrize(("content", "reason"), BROKEN_SETTINGS)
def test_a_broken_setting_is_a_named_error(home, setting, content, reason):
    _save(setting, content)

    with pytest.raises(StoreSettingError) as raised:
        resolve_store(None)

    assert str(setting) in str(raised.value) and reason in str(raised.value)


def test_a_directory_where_the_setting_belongs_is_a_named_error(home, setting):
    setting.mkdir(parents=True)

    with pytest.raises(StoreSettingError, match="cannot read"):
        resolve_store(None)


@requires_symlinks
def test_a_dangling_symlinked_setting_is_not_mistaken_for_no_setting(home, setting, tmp_path):
    setting.parent.mkdir(parents=True)
    setting.symlink_to(tmp_path / "gone")

    with pytest.raises(StoreSettingError, match="missing"):
        resolve_store(None)


@requires_permission_enforcement
def test_an_unreadable_setting_is_a_named_error(home, setting, tmp_path):
    _save(setting, f"{tmp_path / 'notes'}\n".encode())
    setting.chmod(0)
    try:
        with pytest.raises(StoreSettingError, match="cannot read"):
            resolve_store(None)
    finally:
        setting.chmod(0o600)


@pytest.mark.parametrize("command", [["search", "cache"], ["roles"], ["telemetry"],
                                     ["backfill", "--all", "--yes"], ["store", "show"]])
def test_every_command_reports_a_broken_setting_on_both_streams(home, setting, capsys, command):
    _store_with_docs(home / "Developer" / "engmem")
    _save(setting, b"notes\n")

    assert main(command) == 2

    captured = capsys.readouterr()
    for stream in (captured.out, captured.err):
        assert "error:" in stream and str(setting) in stream
    assert not any(doc_id in captured.out for doc_id in _fixture_ids())


def test_install_refuses_a_broken_setting_before_writing_anything(home, setting, capsys):
    _save(setting, b"notes\n")

    assert main(["install", "--agent", "claude"]) == 2

    assert not (home / ".claude").exists()
    assert not (home / "Developer").exists()
    assert str(setting) in capsys.readouterr().out


def test_uninstall_refuses_a_broken_setting_before_removing_anything(home, setting, capsys):
    assert main(["install", "--agent", "claude"]) == 0
    _save(setting, b"notes\n")

    assert main(["uninstall", "--agent", "claude"]) == 2

    assert (home / ".claude" / "commands" / "engmem.md").is_file()
    assert str(setting) in capsys.readouterr().out


def test_a_saved_store_that_is_missing_is_named_and_the_default_is_not_searched(
    home, setting, tmp_path, capsys
):
    _store_with_docs(home / "Developer" / "engmem")
    missing = tmp_path / "unplugged-drive" / "notes"
    _save(setting, f"{missing}\n".encode())

    code, out, err = _search(capsys)

    assert code == 2
    assert str(missing / "sessions") in out and "does not exist" in out
    assert "engmem store show" in out
    assert not any(doc_id in out for doc_id in _fixture_ids())


def test_backfill_into_a_missing_saved_store_writes_nowhere(home, setting, tmp_path, capsys):
    default = _store_with_docs(home / "Developer" / "engmem")
    before = {p.name: p.read_bytes() for p in (default / "sessions").iterdir()}
    _save(setting, f"{tmp_path / 'missing'}\n".encode())

    assert main(["backfill", "--all", "--yes"]) == 2

    assert {p.name: p.read_bytes() for p in (default / "sessions").iterdir()} == before
    assert str(tmp_path / "missing") in capsys.readouterr().out


def _mcp(*messages: dict, cwd: Path) -> subprocess.CompletedProcess[str]:
    stdin = "".join(json.dumps(m) + "\n" for m in messages)
    return subprocess.run(
        [sys.executable, "-m", "engmem.cli", "mcp"], input=stdin, cwd=cwd,
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )


def test_mcp_without_a_flag_refuses_a_broken_setting_on_stderr_only(home, setting, tmp_path):
    _save(setting, b"notes\n")

    result = _mcp({"jsonrpc": "2.0", "id": 1, "method": "ping"}, cwd=tmp_path)

    assert result.returncode == 2
    assert result.stdout == ""
    assert str(setting) in result.stderr and "Traceback" not in result.stderr


def test_mcp_create_draft_into_a_missing_saved_store_names_it_and_writes_nowhere(
    home, setting, tmp_path
):
    default = home / "Developer" / "engmem"
    (default / "sessions").mkdir(parents=True)
    missing = tmp_path / "missing"
    _save(setting, f"{missing}\n".encode())

    result = _mcp(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "engmem_create_draft",
            "arguments": {"id": "20260101-widget-cache", "content": DRAFT_CONTENT},
        }},
        cwd=tmp_path,
    )

    response = json.loads(result.stdout.splitlines()[0])
    assert response["result"]["isError"] is True
    assert str(missing / "sessions") in response["result"]["content"][0]["text"]
    assert list((default / "sessions").iterdir()) == []
    assert not missing.exists()


# --- AC-05.4: MCP wiring that records another store is shown, never migrated ----------------


def _desktop_entry(home: Path, store: Path) -> Path:
    path = claude_desktop_config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": {
        "other": {"command": "x"},
        "engmem": {"command": sys.executable, "args": ["-m", "engmem.cli", "mcp", "--store", str(store)]},
    }}), encoding="utf-8")
    return path


def test_show_flags_a_desktop_entry_that_points_at_another_store(home, setting, tmp_path, capsys):
    wired = _store_with_docs(tmp_path / "wired")
    chosen = _store_with_docs(tmp_path / "chosen")
    _save(setting, f"{chosen}\n".encode())
    config = _desktop_entry(home, wired)
    config_before = config.read_bytes()
    stores_before = {p: p.read_bytes() for s in (wired, chosen) for p in s.rglob("*") if p.is_file()}

    assert main(["store", "show"]) == 0

    out = capsys.readouterr().out
    mismatch = [line for line in out.splitlines() if line.startswith("mismatch: claude-desktop")]
    assert len(mismatch) == 1, out
    assert str(wired) in mismatch[0] and str(config) in mismatch[0]
    assert f"engmem install --agent claude-desktop --store {chosen}" in mismatch[0]
    assert config.read_bytes() == config_before
    assert {p: p.read_bytes() for s in (wired, chosen) for p in s.rglob("*") if p.is_file()} == stores_before


def test_show_reports_a_desktop_entry_on_the_same_store_as_matching(home, setting, tmp_path, capsys):
    store = _store_with_docs(tmp_path / "notes")
    _save(setting, f"{store}\n".encode())
    _desktop_entry(home, store)

    assert main(["store", "show"]) == 0

    out = capsys.readouterr().out
    assert "mismatch" not in out
    assert any(line.startswith("claude-desktop:") and "matches" in line for line in out.splitlines())


def test_show_flags_a_codex_block_engmem_wrote_with_the_install_command(home, setting, tmp_path, capsys):
    wired = tmp_path / "wired"
    assert main(["install", "--agent", "codex", "--store", str(wired)]) == 0
    chosen = tmp_path / "chosen"
    _save(setting, f"{chosen}\n".encode())
    capsys.readouterr()

    assert main(["store", "show"]) == 0

    out = capsys.readouterr().out
    assert f"engmem install --agent codex --store {chosen}" in out
    assert any(line.startswith("mismatch: codex") for line in out.splitlines())


def test_show_flags_a_codex_entry_the_user_wrote_without_offering_install(home, setting, tmp_path, capsys):
    codex = home / ".codex"
    codex.mkdir(parents=True)
    (codex / "config.toml").write_text(
        f'[mcp_servers.engmem]\ncommand = "engmem"\nargs = ["mcp", "--store={tmp_path / "wired"}"]\n',
        encoding="utf-8",
    )

    assert main(["store", "show"]) == 0

    mismatch = [l for l in capsys.readouterr().out.splitlines() if l.startswith("mismatch: codex")]
    assert len(mismatch) == 1
    assert "by hand" in mismatch[0] and "engmem install" not in mismatch[0]


def test_show_names_an_entry_without_a_store_flag_as_resolved_at_launch(home, setting, capsys):
    path = claude_desktop_config_path(home)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"mcpServers": {"engmem": {"command": "engmem", "args": ["mcp"]}}}),
                    encoding="utf-8")

    assert main(["store", "show"]) == 0

    assert "without --store" in capsys.readouterr().out


def test_show_reports_a_wiring_config_it_cannot_parse_and_still_answers(home, setting, capsys):
    path = claude_desktop_config_path(home)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    assert main(["store", "show"]) == 0

    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("store: ")
    assert any(line.startswith("warning: claude-desktop") and str(path) in line for line in out.splitlines())


def test_show_with_no_wiring_says_none_was_found(home, setting, capsys):
    assert main(["store", "show"]) == 0

    assert "mcp wiring: none found" in capsys.readouterr().out


def test_store_set_shows_the_wiring_it_now_disagrees_with(home, setting, tmp_path, capsys):
    _desktop_entry(home, tmp_path / "wired")

    assert main(["store", "set", str(tmp_path / "chosen")]) == 0

    assert any(l.startswith("mismatch: claude-desktop") for l in capsys.readouterr().out.splitlines())


# --- store set: usage failures ---------------------------------------------------------------


@pytest.mark.parametrize("raw", ["", "  "], ids=["empty", "blank"])
def test_store_set_refuses_a_blank_path(home, setting, capsys, raw):
    assert main(["store", "set", raw]) == 2

    assert not setting.exists()
    assert "error:" in capsys.readouterr().out


def test_store_set_refuses_a_path_with_a_line_break(home, setting, tmp_path, capsys):
    assert main(["store", "set", str(tmp_path / "a\nb")]) == 2

    assert not setting.exists()
    assert "line break" in capsys.readouterr().out


@requires_permission_enforcement
def test_store_set_names_a_setting_it_cannot_write(home, setting, tmp_path, capsys):
    setting.parent.mkdir(parents=True)
    setting.parent.chmod(0o500)
    try:
        assert main(["store", "set", str(tmp_path / "notes")]) == 2
    finally:
        setting.parent.chmod(0o700)

    captured = capsys.readouterr()
    assert str(setting) in captured.out and str(setting) in captured.err
    assert list(setting.parent.iterdir()) == []


def test_store_requires_a_subcommand(capsys):
    with pytest.raises(SystemExit) as exited:
        main(["store"])

    assert exited.value.code == 2
    assert "error:" in capsys.readouterr().out


def test_no_saved_choice_is_ever_written_by_a_read(home, setting, tmp_path, capsys):
    _store_with_docs(home / "Developer" / "engmem")

    for command in (["search", "cache"], ["roles"], ["store", "show"]):
        main(command)

    assert not setting.parent.exists()


def test_show_with_an_override_still_names_a_broken_saved_choice(home, setting, tmp_path, capsys):
    """The override keeps working, but the check is the command that must not hide the file."""
    _save(setting, b"notes\n")

    assert main(["store", "show", "--store", str(tmp_path / "other")]) == 2

    captured = capsys.readouterr()
    assert captured.out.splitlines()[0] == f"store: {tmp_path / 'other'}"
    assert str(setting) in captured.out and str(setting) in captured.err


# --- ARCH-001: a wiring's --store is read the way `engmem mcp` reads it ---------------------


def _codex_entry(home: Path, args: list[str]) -> None:
    codex = home / ".codex"
    codex.mkdir(parents=True, exist_ok=True)
    (codex / "config.toml").write_text(
        f"[mcp_servers.engmem]\ncommand = \"engmem\"\nargs = {json.dumps(args)}\n", encoding="utf-8"
    )


def _desktop_args(home: Path, args: list[str]) -> None:
    path = claude_desktop_config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": {"engmem": {"command": "engmem", "args": args}}}),
                    encoding="utf-8")


def _wiring_report(capsys, agent: str) -> str:
    assert main(["store", "show"]) == 0
    lines = [l for l in capsys.readouterr().out.splitlines() if agent in l and "mcp wiring" not in l]
    assert len(lines) == 1, lines
    return lines[0]


@pytest.mark.parametrize("agent", ["codex", "claude-desktop"])
@pytest.mark.parametrize("spelling", [["--store", "~/notes"], ["--store=~/notes"]], ids=["split", "joined"])
def test_a_tilde_in_the_wiring_is_the_store_the_server_expands_it_to(home, setting, capsys, agent, spelling):
    """Neither client runs the args through a shell; `engmem mcp` expands the `~` itself."""
    _save(setting, b"~/notes\n")
    (_codex_entry if agent == "codex" else _desktop_args)(home, ["mcp", *spelling])

    line = _wiring_report(capsys, agent)

    assert not line.startswith("mismatch") and line.endswith("matches"), line


@pytest.mark.parametrize("agent", ["codex", "claude-desktop"])
def test_a_blank_store_in_the_wiring_is_no_store_at_all(home, setting, capsys, agent):
    """The server treats `--store=` as unset and falls through to its own resolution."""
    (_codex_entry if agent == "codex" else _desktop_args)(home, ["mcp", "--store="])

    assert "without --store" in _wiring_report(capsys, agent)


def test_a_relative_store_in_the_wiring_is_named_as_having_no_fixed_base(home, setting, capsys):
    _desktop_args(home, ["mcp", "--store", "notes"])

    line = _wiring_report(capsys, "claude-desktop")

    assert "no fixed base" in line and "mismatch" not in line and "matches" not in line


def test_the_last_store_flag_in_the_wiring_is_the_one_the_server_uses(home, setting, tmp_path, capsys):
    _save(setting, f"{tmp_path / 'notes'}\n".encode())
    _desktop_args(home, ["mcp", "--store", str(tmp_path / "old"), "--store", str(tmp_path / "notes")])

    assert _wiring_report(capsys, "claude-desktop").endswith("matches")


def test_the_start_template_separates_an_error_after_the_store_from_an_error_instead_of_it():
    """`store show` under an override prints `store:` first and the broken saved choice last."""
    from engmem.install import _template_source

    text = " ".join(_template_source("engmem.start.md").split())

    assert "When the first line is `store:`, use that path" in text
    assert "When the first line is `error:`, report it and write the draft nowhere" in text
