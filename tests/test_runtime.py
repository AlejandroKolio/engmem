"""Contract tests for `engmem.runtime`: where the store resolves to, and how a failure is named.

Every subcommand starts by calling `resolve_store`, so a wrong answer here is a wrong store for
the whole CLI — and `install --agent claude-desktop` writes that answer into a config file another
process reads back later.
"""

from __future__ import annotations

from pathlib import PureWindowsPath

import pytest

from conftest import redirect_home, requires_symlinks, requires_windows

from engmem import runtime
from engmem.runtime import default_store, fail, resolve_store


# blank in every spelling a shell produces: `export ENGMEM_HOME="$UNSET"`, a quoted
# `--store " "`, a value that survived a `\t`-separated config line
BLANK = [pytest.param("", id="empty"), pytest.param(" ", id="space"), pytest.param("\t\n", id="ws")]


@pytest.fixture
def home(tmp_path, monkeypatch):
    """HOME redirected under tmp_path, ENGMEM_HOME unset — the state a test must opt out of."""
    redirect_home(monkeypatch, tmp_path / "home")
    monkeypatch.delenv("ENGMEM_HOME", raising=False)
    return tmp_path / "home"


# --- precedence: --store, then ENGMEM_HOME, then the default (ENGMEM-SPEC.md §3) -----------


def test_store_falls_back_to_the_documented_default_when_nothing_is_given(home):
    """Neither --store nor ENGMEM_HOME must land the store somewhere other than the path the
    docs tell the reader to look in."""
    assert resolve_store(None) == home / "Developer" / "engmem"
    assert default_store() == home / "Developer" / "engmem"


def test_engmem_home_is_used_when_no_flag_is_given(home, monkeypatch, tmp_path):
    monkeypatch.setenv("ENGMEM_HOME", str(tmp_path / "from-env"))

    assert resolve_store(None) == tmp_path / "from-env"


def test_an_explicit_store_wins_over_engmem_home(home, monkeypatch, tmp_path):
    """The flag is the most specific instruction the user can give; an exported ENGMEM_HOME in the
    shell they happen to be in must not quietly redirect a `--store` they typed on purpose."""
    monkeypatch.setenv("ENGMEM_HOME", str(tmp_path / "from-env"))

    assert resolve_store(str(tmp_path / "from-flag")) == tmp_path / "from-flag"


# --- a blank setting is not a setting -------------------------------------------------------


@pytest.mark.parametrize("blank", BLANK)
def test_a_blank_store_flag_falls_through_to_engmem_home(home, monkeypatch, tmp_path, blank):
    monkeypatch.setenv("ENGMEM_HOME", str(tmp_path / "from-env"))

    assert resolve_store(blank) == tmp_path / "from-env"


@pytest.mark.parametrize("blank", BLANK)
def test_a_blank_engmem_home_falls_through_to_the_default(home, monkeypatch, blank):
    """`export ENGMEM_HOME="$SOMETHING_UNSET"` is the ordinary way to get one of these. Honouring
    it created a store named after the whitespace, next to whatever the shell's cwd was, and said
    nothing about it."""
    monkeypatch.setenv("ENGMEM_HOME", blank)

    assert resolve_store(None) == home / "Developer" / "engmem"


@pytest.mark.parametrize("blank", BLANK)
def test_a_blank_flag_and_a_blank_engmem_home_both_fall_through_to_the_default(home, monkeypatch, blank):
    """Neither source names anything: `--store " "` typed over an inherited `ENGMEM_HOME=`
    must still land on the documented default, not on whichever of the two blanks lost."""
    monkeypatch.setenv("ENGMEM_HOME", blank)

    assert resolve_store(blank) == home / "Developer" / "engmem"


@pytest.mark.parametrize("name", ["notes ", " notes"], ids=["trailing", "leading"])
def test_a_path_that_merely_has_whitespace_at_an_end_is_kept_intact(home, monkeypatch, tmp_path, name):
    """Only an entirely blank value is discarded. A directory whose name really does end in a
    space is a legal POSIX path, and trimming it would silently point the store at a sibling."""
    monkeypatch.chdir(tmp_path)

    assert resolve_store(name) == tmp_path / name


# --- the shape of the answer: expanded, absolute, not resolved ------------------------------


def test_tilde_in_store_path_is_expanded(home):
    """A quoted `--store "~/notes"` reaches argv as a literal tilde and silently creates a
    directory named `~`."""
    resolved = resolve_store("~/notes")

    assert resolved.is_absolute()
    assert "~" not in str(resolved)
    assert resolved == home / "notes"


def test_tilde_in_engmem_home_is_expanded(home, monkeypatch):
    monkeypatch.setenv("ENGMEM_HOME", "~/notes")

    assert resolve_store(None) == home / "notes"


@pytest.mark.parametrize(
    "source",
    [pytest.param("flag", id="--store"), pytest.param("env", id="ENGMEM_HOME")],
)
def test_a_relative_store_is_made_absolute(home, monkeypatch, tmp_path, source):
    """`install --agent claude-desktop --store ./notes` writes the resolved path into
    claude_desktop_config.json, and Claude Desktop launches the server from a working directory
    of its own — a relative path there names a different directory, or none."""
    monkeypatch.chdir(tmp_path)
    if source == "env":
        monkeypatch.setenv("ENGMEM_HOME", "notes")
        resolved = resolve_store(None)
    else:
        resolved = resolve_store("notes")

    assert resolved.is_absolute()
    assert resolved == tmp_path / "notes"


@requires_symlinks
def test_a_symlinked_store_path_is_not_resolved_away(home, tmp_path):
    """A store reached through a symlink is a supported setup: the messages engmem prints back
    must name the path the user configured, not its canonical form."""
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)

    assert resolve_store(str(tmp_path / "link")) == tmp_path / "link"


def test_a_parent_reference_is_not_normalised_away(home, monkeypatch, tmp_path):
    """`..` is resolved by the kernel against the real parent, so collapsing it lexically here
    would name a different directory whenever the path crosses a symlink."""
    monkeypatch.chdir(tmp_path)

    assert resolve_store("a/../b") == tmp_path / "a" / ".." / "b"


# --- an unresolvable `~`/`~user` must not crash ---------------------------------------------


@pytest.mark.parametrize(
    "source",
    [pytest.param("flag", id="--store"), pytest.param("env", id="ENGMEM_HOME")],
)
def test_an_unresolvable_user_tilde_does_not_raise(home, monkeypatch, tmp_path, source):
    """`Path.expanduser()` raises RuntimeError — it does not fall back — for a `~user` with no
    matching passwd entry, and nothing between here and `cli.main` catches it. Left untouched,
    the text turns into an ordinary relative path component; `resolve_store` must return it
    rather than let the exception escape as a bare traceback with an empty stdout."""
    monkeypatch.chdir(tmp_path)
    unresolvable = "~nosuchuser12345/notes"

    if source == "env":
        monkeypatch.setenv("ENGMEM_HOME", unresolvable)
        resolved = resolve_store(None)
    else:
        resolved = resolve_store(unresolvable)

    assert resolved.is_absolute()
    assert resolved == tmp_path / "~nosuchuser12345" / "notes"


def test_default_store_does_not_raise_when_home_cannot_be_determined(monkeypatch):
    """The container-with-an-arbitrary-uid case: no `HOME`, and the uid has no passwd entry.
    `posixpath.expanduser` already returns `~` unchanged for exactly this reason; `Path.expanduser()`
    (which `default_store` reaches through `_expand_home(Path("~"))`) raises RuntimeError
    instead, and `_expand_home` is what hands the `~` back unexpanded."""
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("ENGMEM_HOME", raising=False)

    def _no_passwd_entry(_uid):
        raise KeyError("no such user")

    monkeypatch.setattr(runtime.os, "getuid", lambda: 4242, raising=False)
    pwd = pytest.importorskip("pwd")
    monkeypatch.setattr(pwd, "getpwuid", _no_passwd_entry)

    resolved = default_store()

    assert resolved.is_absolute()
    assert resolved.name == "engmem"


def test_an_unresolvable_tilde_is_named_on_stdout_not_only_a_traceback(tmp_path, monkeypatch, capsys):
    """End-to-end: before this fix, `main(["search", ..., "--store", "~nosuchuser.../x"])` raised
    RuntimeError out of `resolve_store`, with no handler between it and `cli.main` — the
    traceback went to stderr only, and the stdout-reading consumer (ENGMEM-SPEC.md §10
    principle VIII) saw nothing. `cli.py` is unmodified; this proves the existing
    store-not-found path (cli.py's `fail`, on both streams) is what the caller now reaches."""
    from engmem.cli import main

    monkeypatch.chdir(tmp_path)

    exit_code = main(["search", "anything", "--store", "~nosuchuser12345/notes"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.out.strip() != "", "stdout must not be empty on a terminal failure"
    assert "nosuchuser12345" in captured.out
    assert "nosuchuser12345" in captured.err


# --- the answer is absolute on every interpreter, not only 3.13+ ----------------------------


def test_a_drive_relative_windows_path_is_made_absolute(monkeypatch):
    """`PureWindowsPath('D:/work', 'C:notes') == PureWindowsPath('C:notes')`: a drive-letter
    component re-anchors whatever came before it, so the naive `_from_parts([cwd] + parts)`
    join `Path.absolute()` used before Python 3.13 (bpo-89812) silently returns a *relative*
    path when the process's cwd sits on a drive other than the one the caller named. `os` is
    patched rather than `chdir`d — a POSIX interpreter has no drives to move between, and this
    is `_absolute`'s own branch under test, not the platform's."""
    monkeypatch.setattr(runtime.os.path, "abspath", lambda drive: drive + "\\work")
    monkeypatch.setattr(runtime.os, "getcwd", lambda: "D:\\elsewhere")

    resolved = runtime._absolute(PureWindowsPath("C:notes"))

    assert resolved.is_absolute()
    assert resolved == PureWindowsPath("C:\\work\\notes")


@requires_windows
def test_resolve_store_is_absolute_for_a_drive_relative_windows_input(home, monkeypatch):
    """Real end-to-end confirmation on the one platform that can run it for real: `--store
    C:notes` must resolve to an absolute path even when the process's cwd is on a different
    drive than `C:`."""
    import os as real_os

    current_drive = real_os.path.splitdrive(real_os.getcwd())[0].rstrip(":").upper()
    other_drive = "Z" if current_drive != "Z" else "Y"

    resolved = resolve_store(f"{other_drive}:notes")

    assert resolved.is_absolute()
    assert resolved.name == "notes"


# --- failure reporting (ENGMEM-SPEC.md §5, §10 principle VIII) ------------------------------


def test_a_failure_is_named_on_stdout_as_well_as_stderr(capsys):
    """The consumer is an agent that reads stdout and never stderr, so a diagnostic written only
    to stderr is a swallowed error."""
    fail("store not found: /nowhere")

    captured = capsys.readouterr()
    assert captured.out == "error: store not found: /nowhere\n"
    assert captured.err == "error: store not found: /nowhere\n"


def test_fail_does_not_exit_so_the_caller_owns_the_exit_code(capsys):
    """`_cmd_backfill` reports one unreadable document and keeps going through the rest of the
    batch; `_validate_agent` reports and returns 2. An exit inside `fail` would take that choice
    away from every caller at once."""
    assert fail("first") is None
    assert fail("second") is None

    assert capsys.readouterr().out.count("error: ") == 2
