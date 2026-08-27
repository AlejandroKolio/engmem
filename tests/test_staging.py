"""Unit tests for `engmem.staging`, the staged write shared by `backfill` and the MCP tools."""

from __future__ import annotations

import os
import stat

import pytest

from conftest import requires_posix_modes

from engmem.staging import commit, discard, stage


def test_the_staged_file_is_a_hidden_sibling_no_scan_picks_up(tmp_path):
    target = tmp_path / "widget-cache-warmup.md"
    target.write_bytes(b"old")

    tmp = stage(target, b"new")

    assert tmp.parent == target.parent
    assert tmp.name.startswith(".") and tmp.name.endswith(".tmp")
    assert not tmp.name.endswith(".md")
    assert tmp.read_bytes() == b"new"
    discard(tmp)


def test_the_api_is_bytes_only(tmp_path):
    """The structural half of "never text mode", and the half that holds on every platform."""
    target = tmp_path / "widget-cache-warmup.md"

    with pytest.raises(TypeError):
        stage(target, "one\r\ntwo\n")

    # and a `stage` that raises takes its own temp file with it — the caller was never handed
    # a path to discard, so a full disk would otherwise leave one per document, per re-run
    assert list(tmp_path.iterdir()) == []


def test_content_is_written_verbatim(tmp_path):
    r"""Regression for the Windows CI leg: `write_text` translates `\n` to `os.linesep`, so on
    POSIX this passes either way — it is `os.linesep == "\r\n"` that this catches."""
    target = tmp_path / "widget-cache-warmup.md"
    content = "one\r\ntwo\rthree\n".encode("utf-8")

    tmp = stage(target, content)

    assert tmp.read_bytes() == content
    discard(tmp)


@requires_posix_modes
def test_the_staged_file_is_never_wider_than_0600(tmp_path):
    """It holds a whole document, and is read and parsed before any chmod could run."""
    target = tmp_path / "widget-cache-warmup.md"
    target.write_bytes(b"old")
    os.chmod(target, 0o666)

    tmp = stage(target, b"new")

    assert stat.S_IMODE(os.stat(tmp).st_mode) == 0o600
    discard(tmp)


@requires_posix_modes
def test_commit_restores_the_mode_of_what_was_there(tmp_path):
    target = tmp_path / "widget-cache-warmup.md"
    target.write_bytes(b"old")
    os.chmod(target, 0o640)

    commit(stage(target, b"new"), target)

    assert stat.S_IMODE(os.stat(target).st_mode) == 0o640
    assert target.read_bytes() == b"new"


@requires_posix_modes
def test_a_new_document_keeps_the_staged_0600(tmp_path):
    """There is no previous mode to restore, and a session document is private by default."""
    target = tmp_path / "widget-cache-warmup.md"

    commit(stage(target, b"new"), target)

    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600


def test_commit_leaves_no_temp_file_behind(tmp_path):
    target = tmp_path / "widget-cache-warmup.md"

    commit(stage(target, b"new"), target)

    assert [p.name for p in tmp_path.iterdir()] == ["widget-cache-warmup.md"]


def test_discard_of_an_already_gone_file_is_not_an_error(tmp_path):
    """Cleanup runs from an `except BaseException:`; raising there would replace the reason."""
    target = tmp_path / "widget-cache-warmup.md"
    tmp = stage(target, b"new")
    tmp.unlink()

    discard(tmp)  # must not raise


def test_discard_swallows_an_os_error_that_is_not_missing(tmp_path):
    """`missing_ok=True` only suppresses a vanished file; any other `OSError` — a directory
    where a file was expected, say — must not escape a best-effort cleanup call."""
    stray = tmp_path / ".widget-cache-warmup.md.stray.tmp"
    stray.mkdir()

    discard(stray)  # must not raise

    assert stray.is_dir()  # untouched: discard gave up rather than raising


@requires_posix_modes
def test_commit_tolerates_a_chown_failure(tmp_path, monkeypatch):
    """Only root can give a file away; everywhere else `os.chown` raises and there is nothing
    to restore — but the mode restore (which precedes the chown attempt) and the replace must
    still happen."""
    target = tmp_path / "widget-cache-warmup.md"
    target.write_bytes(b"old")
    os.chmod(target, 0o640)

    def _boom(path, uid, gid):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "chown", _boom)

    commit(stage(target, b"new"), target)

    assert target.read_bytes() == b"new"
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o640
