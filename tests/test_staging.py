"""Unit tests for `engmem.staging`, the staged write shared by `backfill` and the MCP tools."""

from __future__ import annotations

import errno
import os
import stat
import threading

import pytest

from conftest import requires_posix_modes, requires_symlinks

from engmem import staging
from engmem.staging import commit, commit_new, discard, stage


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


# --- commit_new: the exclusive create behind `engmem_create_draft` (US-03)


@pytest.fixture(params=["link", "no-hard-links"])
def link_support(request, monkeypatch):
    """Every `commit_new` guarantee holds on both paths: the hard link, and the `mkdir` lock a
    filesystem without hard links (FAT, exFAT) falls back to."""
    if request.param == "no-hard-links":
        def unsupported(src, dst, *args, **kwargs):
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(staging.os, "link", unsupported)
    return request.param


def _create_lock(target):
    return target.with_name(f".{target.name}.create-lock")


def test_commit_new_publishes_the_staged_content_and_leaves_nothing_else(tmp_path, link_support):
    target = tmp_path / "widget-cache-warmup.md"

    commit_new(stage(target, b"new"), target)

    assert target.read_bytes() == b"new"
    assert [p.name for p in tmp_path.iterdir()] == ["widget-cache-warmup.md"]


@requires_posix_modes
def test_commit_new_keeps_the_staged_0600(tmp_path, link_support):
    target = tmp_path / "widget-cache-warmup.md"

    commit_new(stage(target, b"new"), target)

    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600


def test_commit_new_never_replaces_an_existing_document(tmp_path, link_support):
    """The caller's existence check can be stale by the time it commits; this is the check
    that cannot be."""
    target = tmp_path / "widget-cache-warmup.md"
    target.write_bytes(b"theirs")
    tmp = stage(target, b"mine")

    with pytest.raises(FileExistsError):
        commit_new(tmp, target)

    assert target.read_bytes() == b"theirs"
    assert tmp.read_bytes() == b"mine", "left for the caller to discard"
    assert not _create_lock(target).exists()
    discard(tmp)


@requires_symlinks
def test_commit_new_treats_a_dangling_symlink_as_occupied(tmp_path, link_support):
    target = tmp_path / "widget-cache-warmup.md"
    outside = tmp_path / "elsewhere" / "outside.md"
    target.symlink_to(outside)
    tmp = stage(target, b"mine")

    with pytest.raises(FileExistsError):
        commit_new(tmp, target)

    assert target.is_symlink() and not outside.exists()
    discard(tmp)


def test_commit_new_without_hard_links_refuses_while_another_create_holds_the_lock(
    tmp_path, monkeypatch
):
    """The lock is what makes the fallback's check-then-rename exclusive; a lock left by a
    crashed create fails closed and names itself."""
    def unsupported(*args, **kwargs):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(staging.os, "link", unsupported)
    target = tmp_path / "widget-cache-warmup.md"
    _create_lock(target).mkdir()
    tmp = stage(target, b"mine")

    with pytest.raises(FileExistsError) as raised:
        commit_new(tmp, target)

    assert _create_lock(target).name in str(raised.value)
    assert not target.exists()
    discard(tmp)


def test_commit_new_without_hard_links_releases_the_lock_when_interrupted(tmp_path, monkeypatch):
    """AC-03.4: an interrupted create leaves neither a document nor a lock that would refuse
    the id on the retry."""
    def unsupported(*args, **kwargs):
        raise OSError(errno.ENOTSUP, "Operation not supported")

    def interrupted(src, dst):
        raise KeyboardInterrupt

    monkeypatch.setattr(staging.os, "link", unsupported)
    monkeypatch.setattr(staging.os, "replace", interrupted)
    target = tmp_path / "widget-cache-warmup.md"
    tmp = stage(target, b"mine")

    with pytest.raises(KeyboardInterrupt):
        commit_new(tmp, target)

    assert not target.exists()
    assert not _create_lock(target).exists()
    discard(tmp)


def test_commit_new_without_hard_links_is_a_success_even_if_the_lock_cannot_be_removed(
    tmp_path, monkeypatch
):
    """The document is already published when the lock is released; failing there would
    report a create that happened as one that did not, and the retry would hit "exists"."""
    def unsupported(*args, **kwargs):
        raise PermissionError(1, "Operation not permitted")

    def stuck(path):
        raise OSError(16, "Device or resource busy")

    monkeypatch.setattr(staging.os, "link", unsupported)
    monkeypatch.setattr(staging.os, "rmdir", stuck)
    target = tmp_path / "widget-cache-warmup.md"

    commit_new(stage(target, b"new"), target)

    assert target.read_bytes() == b"new"


def _link_failing_with(monkeypatch, exc: OSError) -> None:
    def failing(*args, **kwargs):
        raise exc

    monkeypatch.setattr(staging.os, "link", failing)


@pytest.mark.parametrize(
    "code", [errno.EIO, errno.ENOSPC, errno.EACCES], ids=["EIO", "ENOSPC", "EACCES"]
)
def test_commit_new_propagates_a_link_failure_that_is_not_missing_hard_links(
    tmp_path, monkeypatch, code
):
    """The lock excludes other lock holders only: a creator that fell back on a transient error
    could replace one whose link succeeded meanwhile, so such an error is raised, not retried."""
    _link_failing_with(monkeypatch, OSError(code, os.strerror(code)))
    target = tmp_path / "widget-cache-warmup.md"
    tmp = stage(target, b"mine")

    with pytest.raises(OSError) as raised:
        commit_new(tmp, target)

    assert raised.value.errno == code
    assert not target.exists()
    assert not _create_lock(target).exists()
    assert tmp.read_bytes() == b"mine", "left for the caller to discard"
    discard(tmp)


@pytest.mark.parametrize(
    "code",
    sorted({errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS}),
    ids=lambda code: errno.errorcode[code],
)
def test_commit_new_falls_back_to_the_lock_where_hard_links_are_unavailable(
    tmp_path, monkeypatch, code
):
    _link_failing_with(monkeypatch, OSError(code, os.strerror(code)))
    target = tmp_path / "widget-cache-warmup.md"

    commit_new(stage(target, b"new"), target)

    assert target.read_bytes() == b"new"


@pytest.mark.parametrize("winerror", [1, 50], ids=["ERROR_INVALID_FUNCTION", "ERROR_NOT_SUPPORTED"])
def test_commit_new_falls_back_on_the_windows_no_hard_links_codes(tmp_path, monkeypatch, winerror):
    """Windows reports these as `EINVAL`, which on its own is no evidence links are missing."""
    exc = OSError(errno.EINVAL, "Incorrect function")
    exc.winerror = winerror
    _link_failing_with(monkeypatch, exc)
    monkeypatch.setattr(staging, "_WINDOWS", True)
    target = tmp_path / "widget-cache-warmup.md"

    commit_new(stage(target, b"new"), target)

    assert target.read_bytes() == b"new"


@pytest.mark.parametrize("windows", [True, False], ids=["windows", "posix"])
def test_an_access_denied_lock_is_busy_on_windows_only(tmp_path, monkeypatch, windows):
    """NTFS answers `mkdir` of a name still pending delete with access denied; that is a lock
    another creator just released, a conflict to report, not a host fault."""
    _link_failing_with(monkeypatch, PermissionError(errno.EPERM, "Operation not permitted"))

    def access_denied(path, *args, **kwargs):
        raise PermissionError(errno.EACCES, "Access is denied")

    monkeypatch.setattr(staging.os, "mkdir", access_denied)
    monkeypatch.setattr(staging, "_WINDOWS", windows)
    target = tmp_path / "widget-cache-warmup.md"
    tmp = stage(target, b"mine")

    with pytest.raises(PermissionError if not windows else FileExistsError) as raised:
        commit_new(tmp, target)

    assert isinstance(raised.value, FileExistsError) is windows
    assert not target.exists()
    discard(tmp)


def _write_lock(target):
    return target.with_name(f".{target.name}.write-lock")


def test_document_lock_makes_a_second_writer_wait_for_the_first(tmp_path):
    """US-04: the read-check-replace inside the lock is one step to every other holder."""
    target = tmp_path / "widget-cache-warmup.md"
    order: list[str] = []
    first_inside, release_first = threading.Event(), threading.Event()

    def first() -> None:
        with staging.document_lock(target):
            order.append("first in")
            first_inside.set()
            assert release_first.wait(10)
            order.append("first out")

    def second() -> None:
        with staging.document_lock(target):
            order.append("second in")

    a = threading.Thread(target=first)
    a.start()
    assert first_inside.wait(10)
    b = threading.Thread(target=second)
    b.start()
    b.join(0.2)
    release_first.set()
    a.join()
    b.join()

    assert order == ["first in", "first out", "second in"]
    assert not _write_lock(target).exists()


def test_document_lock_held_past_the_wait_names_itself_and_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(staging, "_LOCK_WAIT_SECONDS", 0.05)
    target = tmp_path / "widget-cache-warmup.md"
    _write_lock(target).mkdir()

    with pytest.raises(staging.DocumentLockedError) as raised:
        with staging.document_lock(target):
            pytest.fail("entered a lock another writer holds")

    assert raised.value.lock == _write_lock(target)
    assert _write_lock(target).is_dir()


@pytest.mark.parametrize("windows", [True, False])
def test_an_access_denied_write_lock_is_busy_on_windows_only(tmp_path, monkeypatch, windows):
    monkeypatch.setattr(staging, "_LOCK_WAIT_SECONDS", 0.05)

    def denied(path, *args, **kwargs):
        raise PermissionError(errno.EACCES, "Access is denied")

    monkeypatch.setattr(staging.os, "mkdir", denied)
    monkeypatch.setattr(staging, "_WINDOWS", windows)
    target = tmp_path / "widget-cache-warmup.md"

    with pytest.raises(staging.DocumentLockedError if windows else PermissionError):
        with staging.document_lock(target):
            pytest.fail("entered a lock that could not be taken")


def test_document_lock_is_released_when_the_body_raises(tmp_path):
    target = tmp_path / "widget-cache-warmup.md"

    with pytest.raises(KeyboardInterrupt):
        with staging.document_lock(target):
            raise KeyboardInterrupt

    assert not _write_lock(target).exists()


def test_version_of_is_a_digest_of_the_exact_bytes():
    assert staging.version_of(b"---\nid: a\n---\n") == staging.version_of(b"---\nid: a\n---\n")
    assert staging.version_of(b"body\n") != staging.version_of(b"body\r\n")
    assert staging.version_of(b"body") != staging.version_of(b"\xef\xbb\xbfbody")
