import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from conftest import TRIGGER_RULE

from engmem.cli import main

SKILL_NAMES = ("engmem", "engmem-save", "engmem-save-quick")
CODEX_BEGIN = "# engmem-mcp-server: begin (managed by `engmem install --agent codex`)"


def _skills_root(home: Path) -> Path:
    return home / ".agents" / "skills"


def _codex_config(home: Path) -> Path:
    return home / ".codex" / "config.toml"


def _engmem_entry(home: Path) -> dict:
    return tomllib.loads(_codex_config(home).read_text(encoding="utf-8"))["mcp_servers"]["engmem"]


def _expected_args(store: Path) -> list[str]:
    return ["-m", "engmem.cli", "mcp", "--store", str(store)]


def _split_command_line(line: str) -> list[str]:
    """Splits the way the shell the line is printed for will: POSIX rules, or Windows' own."""
    if sys.platform != "win32":
        return shlex.split(line)
    import ctypes
    from ctypes import wintypes

    command_line_to_argv = ctypes.windll.shell32.CommandLineToArgvW
    command_line_to_argv.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    command_line_to_argv.restype = ctypes.POINTER(wintypes.LPWSTR)
    count = ctypes.c_int()
    argv = command_line_to_argv("program " + line, ctypes.byref(count))
    try:
        return [argv[i] for i in range(1, count.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(argv)


def _install(*args) -> int:
    return main(["install", *args])


def _uninstall(*args) -> int:
    return main(["uninstall", *args])


# --- codex: skills ---------------------------------------------------------------


def test_codex_installs_one_skill_per_template_under_the_agents_skills_root(env, tmp_path):
    home, _ = env

    assert _install("--agent", "codex", "--store", str(tmp_path / "store")) == 0

    for name in SKILL_NAMES:
        lines = (_skills_root(home) / name / "SKILL.md").read_text(encoding="utf-8").splitlines()
        closing = lines.index("---", 1)
        assert lines[0] == "---" and f"name: {name}" in lines[1:closing], (
            f"{name}: Codex requires `name` in front matter that opens on line 1"
        )
        assert any(line.startswith("description:") for line in lines[1:closing])
        assert not any(line.startswith("argument-hint:") for line in lines[1:closing])


def test_codex_skill_bodies_mention_skills_the_way_codex_invokes_them(env, tmp_path):
    """Codex mentions a skill as `$name`; a `/engmem.save` left in a body points at nothing."""
    home, _ = env

    _install("--agent", "codex", "--store", str(tmp_path / "store"))

    bodies = {
        name: (_skills_root(home) / name / "SKILL.md").read_text(encoding="utf-8")
        for name in SKILL_NAMES
    }
    for name, body in bodies.items():
        assert "`/engmem" not in body and "# /engmem" not in body, f"{name}: a slash command survived"
        assert "$ARGUMENTS" not in body, f"{name}: `$ARGUMENTS` reads as a skill mention in Codex"
    assert "`$engmem-save`" in bodies["engmem"]
    assert "`$engmem-save-quick`" in bodies["engmem-save"]
    assert "`$engmem`" in bodies["engmem-save-quick"]
    assert "`~/Developer/engmem`" in bodies["engmem"], "a store path is not a command reference"


def test_codex_appends_the_trigger_rule_to_the_codex_agents_file(env, tmp_path):
    home, _ = env

    _install("--agent", "codex", "--store", str(tmp_path / "store"))

    assert TRIGGER_RULE in (home / ".codex" / "AGENTS.md").read_text(encoding="utf-8")


def test_codex_honours_codex_home(env, tmp_path, monkeypatch):
    home, _ = env
    codex_home = tmp_path / "elsewhere"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    _install("--agent", "codex", "--store", str(tmp_path / "store"))

    assert TRIGGER_RULE in (codex_home / "AGENTS.md").read_text(encoding="utf-8")
    assert "engmem" in tomllib.loads((codex_home / "config.toml").read_text())["mcp_servers"]
    assert not (home / ".codex").exists()


# --- codex: config.toml ----------------------------------------------------------


def test_codex_writes_an_mcp_entry_that_launches_this_interpreter_on_this_store(env, tmp_path):
    home, _ = env
    store = tmp_path / "store"

    _install("--agent", "codex", "--store", str(store))

    assert _engmem_entry(home) == {"command": sys.executable, "args": _expected_args(store)}


def test_codex_appends_to_a_config_without_rewriting_what_is_there(env, tmp_path):
    home, _ = env
    original = b'model = "o4"\n\n[mcp_servers.other]\ncommand = "other"  # keep me\n'
    _codex_config(home).parent.mkdir(parents=True)
    _codex_config(home).write_bytes(original)

    _install("--agent", "codex", "--store", str(tmp_path / "store"))

    written = _codex_config(home).read_bytes()
    assert written.startswith(original), "the user's own bytes, comments included, must survive"
    assert tomllib.loads(written.decode())["mcp_servers"]["other"] == {"command": "other"}


def test_codex_config_block_uses_the_files_own_line_ending(env, tmp_path):
    home, _ = env
    original = b'model = "o4"\r\n'
    _codex_config(home).parent.mkdir(parents=True)
    _codex_config(home).write_bytes(original)

    _install("--agent", "codex", "--store", str(tmp_path / "store"))

    added = _codex_config(home).read_bytes()[len(original):]
    assert added.count(b"\r\n") == added.count(b"\n") > 0


def test_codex_config_keeps_its_byte_order_mark(env, tmp_path):
    home, _ = env
    original = b'\xef\xbb\xbfmodel = "o4"\n'
    _codex_config(home).parent.mkdir(parents=True)
    _codex_config(home).write_bytes(original)

    _install("--agent", "codex", "--store", str(tmp_path / "store"))

    assert _codex_config(home).read_bytes().startswith(original)


def test_a_second_codex_install_changes_nothing(env, tmp_path, capsys):
    home, _ = env
    store = tmp_path / "store"
    _install("--agent", "codex", "--store", str(store))
    first = _codex_config(home).read_bytes()
    capsys.readouterr()

    _install("--agent", "codex", "--store", str(store))

    assert _codex_config(home).read_bytes() == first
    assert "MCP server entry already current" in capsys.readouterr().out


def test_a_codex_install_on_another_store_updates_the_block_in_place(env, tmp_path, capsys):
    home, _ = env
    _install("--agent", "codex", "--store", str(tmp_path / "old"))
    trailer = '\n[mcp_servers.after]\ncommand = "after"\n'
    with _codex_config(home).open("a", encoding="utf-8") as config:
        config.write(trailer)
    capsys.readouterr()

    _install("--agent", "codex", "--store", str(tmp_path / "new"))

    text = _codex_config(home).read_text(encoding="utf-8")
    assert text.count(CODEX_BEGIN) == 1
    assert text.endswith(trailer), "a table the user added after the block must stay where it was"
    assert _engmem_entry(home)["args"] == _expected_args(tmp_path / "new")
    assert "MCP server entry updated" in capsys.readouterr().out


def test_an_engmem_server_the_user_wrote_is_left_alone(env, tmp_path, capsys):
    home, _ = env
    original = b'[mcp_servers.engmem]\ncommand = "my-own-engmem"\n'
    _codex_config(home).parent.mkdir(parents=True)
    _codex_config(home).write_bytes(original)

    assert _install("--agent", "codex", "--store", str(tmp_path / "store")) == 0

    assert _codex_config(home).read_bytes() == original
    assert "did not write is left untouched" in capsys.readouterr().out


@pytest.mark.parametrize(
    "value",
    [
        pytest.param('quote" back\\slash', id="quote-and-backslash"),
        pytest.param("кириллица 🙂", id="non-ascii-and-astral"),
        pytest.param("delete\x7fchar", id="del"),
        pytest.param("tab\tnewline\n", id="control"),
    ],
)
def test_toml_string_round_trips_what_a_path_can_hold(value):
    """A `"` cannot be in a Windows file name, so the escaping is pinned here, not through a store."""
    from engmem.install import _toml_string

    assert tomllib.loads(f"value = {_toml_string(value)}")["value"] == value


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("кириллица 🙂", id="non-ascii-and-astral"),
        pytest.param("delete\x7fchar", id="del-is-escaped"),
    ],
)
def test_a_store_path_toml_must_escape_round_trips(env, tmp_path, name):
    home, _ = env
    store = tmp_path / name

    assert _install("--agent", "codex", "--store", str(store)) == 0

    assert _engmem_entry(home)["args"][-1] == str(store)


REFUSED_CONFIG_CASES = [
    pytest.param(b"this is = = not toml\n", "is not valid TOML", id="malformed"),
    pytest.param(b"mcp_servers = []\n", "is not a table", id="servers-not-a-table"),
    # an inline table cannot be extended by a later [mcp_servers.engmem] header
    pytest.param(
        b'mcp_servers = { other = { command = "x" } }\n', "add an `engmem` server", id="inline-table"
    ),
    pytest.param(
        CODEX_BEGIN.encode() + b'\n[mcp_servers.engmem]\ncommand = "x"\n',
        "no `# engmem-mcp-server: end` line",
        id="unterminated-block",
    ),
]


@pytest.mark.parametrize(("original", "expected_cause"), REFUSED_CONFIG_CASES)
def test_a_config_codex_cannot_take_the_entry_refuses_the_whole_install(
    env, tmp_path, capsys, original, expected_cause
):
    """The config is the one step that can refuse; skills and rule written before it would be a
    half-install the user has to clean up."""
    home, _ = env
    _codex_config(home).parent.mkdir(parents=True)
    _codex_config(home).write_bytes(original)

    exit_code = _install("--agent", "codex", "--store", str(tmp_path / "store"))

    captured = capsys.readouterr()
    assert exit_code == 2
    assert expected_cause in captured.out and expected_cause in captured.err
    assert _codex_config(home).read_bytes() == original
    assert not _skills_root(home).exists()
    assert not (home / ".codex" / "AGENTS.md").exists()


# --- codex: uninstall ------------------------------------------------------------


def test_codex_uninstall_hands_back_the_original_config_bytes(env, tmp_path, capsys):
    home, _ = env
    store = tmp_path / "store"
    original = b'model = "o4"\r\n[mcp_servers.other]\r\ncommand = "other"\r\n'
    _codex_config(home).parent.mkdir(parents=True)
    _codex_config(home).write_bytes(original)
    _install("--agent", "codex", "--store", str(store))
    capsys.readouterr()

    exit_code = _uninstall("--agent", "codex", "--store", str(store))

    out = capsys.readouterr().out
    assert exit_code == 0
    assert _codex_config(home).read_bytes() == original
    assert not any((_skills_root(home) / name).exists() for name in SKILL_NAMES)
    assert TRIGGER_RULE not in (home / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
    assert "3 template file(s) removed, 1 config entry(ies) removed" in out


def test_codex_uninstall_leaves_an_engmem_server_the_user_wrote(env, tmp_path, capsys):
    home, _ = env
    original = b'[mcp_servers.engmem]\ncommand = "my-own-engmem"\n'
    _codex_config(home).parent.mkdir(parents=True)
    _codex_config(home).write_bytes(original)

    assert _uninstall("--agent", "codex", "--store", str(tmp_path / "store")) == 0

    assert _codex_config(home).read_bytes() == original
    assert "0 config entry(ies) removed" in capsys.readouterr().out


def test_codex_uninstall_refuses_to_move_a_key_into_another_server(env, tmp_path, capsys):
    """A key below the block parses fine without engmem's header — under the table above it,
    where `enabled = false` would silently switch off the user's other server."""
    home, _ = env
    store = tmp_path / "store"
    _codex_config(home).parent.mkdir(parents=True)
    _codex_config(home).write_text('[mcp_servers.other]\ncommand = "x"\n', "utf-8")
    _install("--agent", "codex", "--store", str(store))
    with _codex_config(home).open("a", encoding="utf-8") as config:
        config.write("enabled = false\n")
    before = _codex_config(home).read_bytes()
    capsys.readouterr()

    exit_code = _uninstall("--agent", "codex", "--store", str(store))

    assert exit_code == 2
    assert "would change the rest of" in capsys.readouterr().out
    assert _codex_config(home).read_bytes() == before


def test_codex_uninstall_names_a_config_that_was_already_broken(env, tmp_path, capsys):
    home, _ = env
    _codex_config(home).parent.mkdir(parents=True)
    original = (CODEX_BEGIN + '\n[mcp_servers.engmem]\n# engmem-mcp-server: end\n= broken\n').encode()
    _codex_config(home).write_bytes(original)

    exit_code = _uninstall("--agent", "codex", "--store", str(tmp_path / "store"))

    assert exit_code == 2
    assert "is not valid TOML" in capsys.readouterr().out
    assert _codex_config(home).read_bytes() == original


def test_codex_uninstall_passes_over_a_broken_config_it_never_wrote_to(env, tmp_path, capsys):
    home, _ = env
    _codex_config(home).parent.mkdir(parents=True)
    original = b"this is = = not toml\n"
    _codex_config(home).write_bytes(original)

    exit_code = _uninstall("--agent", "codex", "--store", str(tmp_path / "store"))

    assert exit_code == 0, capsys.readouterr().out
    assert _codex_config(home).read_bytes() == original


def test_a_codex_reinstall_refuses_to_drop_a_key_the_user_put_in_the_block(env, tmp_path, capsys):
    home, _ = env
    _install("--agent", "codex", "--store", str(tmp_path / "old"))
    text = _codex_config(home).read_text(encoding="utf-8")
    _codex_config(home).write_text(
        text.replace("# engmem-mcp-server: end", "enabled = false\n# engmem-mcp-server: end"),
        "utf-8",
    )
    before = _codex_config(home).read_bytes()
    capsys.readouterr()

    exit_code = _install("--agent", "codex", "--store", str(tmp_path / "new"))

    assert exit_code == 2
    assert "(enabled)" in capsys.readouterr().out
    assert _codex_config(home).read_bytes() == before


def test_codex_install_warns_when_an_override_file_hides_the_rule(env, tmp_path, capsys):
    home, _ = env
    (home / ".codex").mkdir()
    (home / ".codex" / "AGENTS.override.md").write_text("# mine\n", "utf-8")

    _install("--agent", "codex", "--store", str(tmp_path / "store"))

    assert "AGENTS.override.md instead of AGENTS.md" in capsys.readouterr().out


def test_uninstalling_the_wrong_agent_names_a_codex_mcp_entry(env, tmp_path, capsys):
    home, _ = env
    store = tmp_path / "store"
    _install("--agent", "codex", "--store", str(store))
    for name in SKILL_NAMES:
        (_skills_root(home) / name / "SKILL.md").unlink()
    capsys.readouterr()

    _uninstall("--agent", "claude", "--store", str(store))

    assert "files for codex are present" in capsys.readouterr().out


# --- chatgpt ---------------------------------------------------------------------


def test_chatgpt_install_prints_a_tunnel_command_that_launches_this_store(env, tmp_path, capsys):
    home, _ = env
    store = tmp_path / "my store"

    assert _install("--agent", "chatgpt", "--store", str(store)) == 0

    out = capsys.readouterr().out
    init_line = next(line for line in out.splitlines() if "tunnel-client init" in line)
    tokens = _split_command_line(init_line.strip())
    mcp_command = tokens[tokens.index("--mcp-command") + 1]
    assert _split_command_line(mcp_command) == [sys.executable, *_expected_args(store)]
    assert "note: installed templates" not in out, "the tunnel command carries the store itself"


REPO = Path(__file__).resolve().parent.parent
SETUP_SCRIPT = REPO / "scripts" / "setup-openai.sh"
_UNNEGATED_PROTECTED = re.compile(r"(?<!not )\bprotected\b", re.IGNORECASE)
# the exact claims, pinned here once: engmem observes neither bridge's access control
TUNNEL_LINE = (
    "bridge: OpenAI's Secure MCP Tunnel is the one supported bridge; OpenAI decides who may reach "
    "it, and engmem did not verify that"
)
PUBLIC_LINE = (
    "public bridge: a public bridge (supergateway plus ngrok or cloudflared) is not protected: "
    "whoever holds its URL reads the store's documents, titles and search snippets; "
    "`engmem mcp --read-only` only refuses writes and does not make the store private"
)


def test_chatgpt_install_names_the_bridge_and_calls_neither_protected(env, tmp_path, capsys):
    """AC-19.4: engmem sees neither bridge's access control, so the summary claims none."""
    _install("--agent", "chatgpt", "--store", str(tmp_path / "store"))

    lines = capsys.readouterr().out.splitlines()
    assert TUNNEL_LINE in lines and PUBLIC_LINE in lines
    assert not re.search(r"\b(only|private|protected|secure[ds]?|safe)\b", TUNNEL_LINE.split(";")[1])
    assert not [line for line in lines if _UNNEGATED_PROTECTED.search(line)], lines


def _stub(bin_dir: Path, name: str, body: str) -> None:
    tool = bin_dir / name
    tool.write_text(body, encoding="utf-8")
    tool.chmod(0o755)


def _run_setup_script(tmp_path: Path, with_tunnel: bool) -> tuple[str, list[str]]:
    """Runs the real script with stubbed `uv`, `engmem` and `tunnel-client`; returns stdout and
    the `tunnel-client` subcommands it called."""
    home, bin_dir, calls = tmp_path / "home", tmp_path / "bin", tmp_path / "tunnel-calls"
    home.mkdir()
    bin_dir.mkdir()
    _stub(bin_dir, "uv", "#!/bin/sh\nexit 0\n")
    _stub(
        bin_dir, "engmem",
        f"#!{sys.executable}\nimport sys\nfrom engmem.cli import main\nsys.exit(main())\n",
    )
    _stub(bin_dir, "tunnel-client", f'#!/bin/sh\necho "$1" >> {shlex.quote(str(calls))}\nexit 0\n')
    env = {
        "PATH": os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"]),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "ENGMEM_HOME": str(tmp_path / "store"),
    }
    if with_tunnel:
        env |= {"TUNNEL_ID": "tunnel-test", "CONTROL_PLANE_API_KEY": "key-test"}
    result = subprocess.run(
        ["bash", str(SETUP_SCRIPT), "--skip-codex", "--store", str(tmp_path / "store")],
        env=env, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    called = calls.read_text(encoding="utf-8").split() if calls.exists() else []
    return result.stdout, called


@pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None, reason="a bash script, POSIX only"
)
@pytest.mark.parametrize("with_tunnel", [True, False], ids=["tunnel-initialised", "no-tunnel"])
def test_setup_script_summary_names_the_tunnel_only_when_it_initialised_one(tmp_path, with_tunnel):
    """AC-19.4 for scripts/setup-openai.sh: its summary says only what the run itself did."""
    out, called = _run_setup_script(tmp_path, with_tunnel)

    summary = out.split("==> Done", 1)[1]
    initialised = (
        "ChatGPT bridge: Secure MCP Tunnel, profile 'engmem' initialised; OpenAI decides who may "
        "reach it, and this script did not verify that"
    )
    assert (initialised in summary) is with_tunnel, summary
    assert ("ChatGPT bridge: none set up by this script" in summary) is not with_tunnel, summary
    assert (
        "public bridge (supergateway plus ngrok or cloudflared): not protected; whoever holds its "
        "URL reads documents, titles and search snippets, and --read-only only refuses writes"
    ) in summary
    assert not _UNNEGATED_PROTECTED.findall(out), out
    assert called == (["init", "doctor"] if with_tunnel else []), "the script never runs the tunnel"


def _canary_document() -> str:
    text = (REPO / "docs" / "remote-access.md").read_text(encoding="utf-8")
    block = text.split("<!-- canary-document: begin -->", 1)[1].split("<!-- canary-document: end -->", 1)[0]
    return block.split("```markdown\n", 1)[1].rsplit("```", 1)[0]


def test_the_acceptance_canary_document_is_a_valid_active_document_found_by_its_word(
    tmp_path, capsys
):
    """docs/remote-access.md tells the owner to save it as is; a drifted copy would make the
    AC-19 check fail for a reason that has nothing to do with the tunnel."""
    from engmem.spine import load_store

    sessions = tmp_path / "store" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "20260101-zebracanary-check.md").write_text(_canary_document(), encoding="utf-8")

    loaded = load_store(sessions)

    assert not loaded.errors and not loaded.warnings, (loaded.errors, loaded.warnings)
    [doc] = loaded.docs
    assert doc.status == "active" and "zebracanary" in doc.title.casefold()
    assert "zebracanary" in doc.body
    assert main(["search", "zebracanary", "--store", str(tmp_path / "store")]) == 0
    assert "20260101-zebracanary-check" in capsys.readouterr().out


def test_chatgpt_install_writes_nothing_outside_the_store(env, tmp_path):
    home, project = env

    _install("--agent", "chatgpt", "--store", str(tmp_path / "store"))

    assert list(home.iterdir()) == [] and list(project.iterdir()) == []


def test_chatgpt_uninstall_points_at_the_workspace(env, tmp_path, capsys):
    assert _uninstall("--agent", "chatgpt", "--store", str(tmp_path / "store")) == 0

    out = capsys.readouterr().out
    assert "delete the engmem app" in out
    assert "files for" not in out


@pytest.mark.parametrize("command", ["install", "uninstall"])
@pytest.mark.parametrize("agent", ["codex", "chatgpt"])
def test_local_is_rejected_for_codex_and_chatgpt(env, tmp_path, capsys, command, agent):
    home, project = env
    (project / ".git").mkdir()

    exit_code = main([command, "--agent", agent, "--local", "--store", str(tmp_path / "store")])

    assert exit_code == 2
    assert "--local has no effect" in capsys.readouterr().out
    assert not (home / ".codex").exists() and not (home / ".agents").exists()


def test_chatgpt_tunnel_command_is_quoted_for_cmd_on_windows(env, tmp_path, monkeypatch, capsys):
    """POSIX single quotes paste into cmd and PowerShell as literal characters."""
    monkeypatch.setattr("engmem.install.sys.platform", "win32")

    _install("--agent", "chatgpt", "--store", str(tmp_path / "my store"))

    init_line = next(line for line in capsys.readouterr().out.splitlines() if "tunnel-client init" in line)
    argument = init_line.split("--mcp-command ", 1)[1]
    assert argument.startswith('"') and argument.endswith('"') and "'" not in argument


def test_a_path_toml_cannot_hold_is_refused_by_name():
    from engmem.install import _SetupError, _toml_string

    with pytest.raises(_SetupError, match="not valid Unicode"):
        _toml_string("store-\udcff")


@pytest.mark.parametrize("other_server", [b"", b'[mcp_servers.other]\ncommand = "x"\n'])
def test_codex_uninstall_cuts_a_block_whose_lines_were_commented_out(
    env, tmp_path, capsys, other_server
):
    home, _ = env
    _codex_config(home).parent.mkdir(parents=True)
    commented = (
        CODEX_BEGIN.encode() + b'\n# [mcp_servers.engmem]\n# command = "x"\n# engmem-mcp-server: end\n'
    )
    _codex_config(home).write_bytes(other_server + commented)

    exit_code = _uninstall("--agent", "codex", "--store", str(tmp_path / "store"))

    assert exit_code == 0, capsys.readouterr().out
    assert _codex_config(home).read_bytes() == other_server


def test_codex_uninstall_keeps_an_empty_servers_table_the_user_declared(env, tmp_path):
    home, _ = env
    store = tmp_path / "store"
    _codex_config(home).parent.mkdir(parents=True)
    _codex_config(home).write_bytes(b"[mcp_servers]\n")
    _install("--agent", "codex", "--store", str(store))

    assert _uninstall("--agent", "codex", "--store", str(store)) == 0

    assert _codex_config(home).read_bytes() == b"[mcp_servers]\n"


def test_codex_uninstall_where_engmem_was_the_only_server(env, tmp_path, capsys):
    home, _ = env
    store = tmp_path / "store"
    _install("--agent", "codex", "--store", str(store))
    capsys.readouterr()

    exit_code = _uninstall("--agent", "codex", "--store", str(store))

    assert exit_code == 0, capsys.readouterr().out
    assert _codex_config(home).read_bytes() == b""
