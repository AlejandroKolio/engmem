"""A load lists and reads `sessions/` through one held directory (SC-002), and a `sessions/`
replaced during the load is read again rather than paired with the new files' identities
(contracts/runtime.md, "Reads stay inside the store")."""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from support.docs import write_file
from support.platform import requires_permission_enforcement, requires_symlinks

from engmem import confined, spine
from engmem.cache import identity_for
from engmem.spine import Problem, load_store, parse_document

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="no FIFOs or /dev/fd on Windows")
NEEDS_DIR_FD = pytest.mark.skipif(not confined._OPENAT, reason="this platform has no dir_fd opens")


def _doc(doc_id: str, text: str) -> str:
    return (
        f"---\nid: {doc_id}\ntitle: {doc_id}\ndate: 2026-01-01\nstatus: active\n"
        f"entities: [WidgetCache]\n---\n\n## Notes\n\n{text}\n"
    )


def _sessions(root: Path, texts: dict[str, str]) -> Path:
    sessions = root / "sessions"
    sessions.mkdir(parents=True)
    for doc_id, text in texts.items():
        write_file(sessions, f"{doc_id}.md", _doc(doc_id, text))
    return sessions


OLD = {"doc-a": "old a", "doc-b": "old b", "doc-c": "old c"}
NEW = {"doc-a": "the new text of a, longer", "doc-b": "the new text of b, longer",
       "doc-c": "the new text of c, longer"}


@pytest.fixture(params=["openat", "checked"])
def reader(request, monkeypatch):
    if request.param == "openat" and not confined._OPENAT:
        pytest.skip("this platform has no dir_fd opens")
    monkeypatch.setattr(confined, "_OPENAT", request.param == "openat")
    return request.param


def _swap_in_new_directory(store: Path) -> None:
    os.replace(store / "sessions", store / f"sessions-gone-{len(list(store.iterdir()))}")
    sessions = _sessions(store, NEW)
    for path in sessions.iterdir():
        os.utime(path, ns=(10**18, 10**18))


@NEEDS_DIR_FD
def test_a_sessions_directory_replaced_mid_load_is_read_again_whole(tmp_path, monkeypatch):
    """Each document's bytes, identity and file must be the same file; without the reload the
    documents after the swap paired the held directory's old text with the new file's stat."""
    sessions = _sessions(tmp_path, OLD)
    real_parse = spine._parse_text
    calls = []

    def swap_once(path, text, identity):
        calls.append(path)
        if len(calls) == 1:
            _swap_in_new_directory(tmp_path)
        return real_parse(path, text, identity)

    monkeypatch.setattr(spine, "_parse_text", swap_once)

    result = load_store(sessions)

    assert result.scan_error is None and result.errors == []
    assert sorted(d.id for d in result.docs) == sorted(NEW)
    for doc in result.docs:
        assert doc.source_identity == identity_for(doc.path), doc.id
        assert doc.body.strip().endswith(NEW[doc.id]), doc.id


@NEEDS_DIR_FD
def test_a_sessions_directory_replaced_on_every_pass_is_reported_not_guessed(tmp_path, monkeypatch):
    sessions = _sessions(tmp_path, OLD)
    real_parse = spine._parse_text

    def swap_always(path, text, identity):
        _swap_in_new_directory(tmp_path)
        return real_parse(path, text, identity)

    monkeypatch.setattr(spine, "_parse_text", swap_always)

    result = load_store(sessions)

    assert result.docs == []
    assert result.scan_error is not None
    assert "replaced while it was read, twice; search again" in result.scan_error.message


@NEEDS_DIR_FD
def test_a_sessions_directory_gone_mid_load_is_a_scan_error_not_an_exception(tmp_path, monkeypatch):
    sessions = _sessions(tmp_path, OLD)
    real_parse = spine._parse_text

    def remove_once(path, text, identity):
        if sessions.exists():
            os.replace(sessions, tmp_path / "sessions-gone")
        return real_parse(path, text, identity)

    monkeypatch.setattr(spine, "_parse_text", remove_once)

    result = load_store(sessions)

    assert result.docs == []
    assert result.scan_error is not None
    assert "cannot list directory" in result.scan_error.message


@requires_permission_enforcement
def test_one_unreadable_document_is_its_own_error_not_a_scan_error(tmp_path, reader):
    sessions = _sessions(tmp_path, OLD)
    locked = sessions / "doc-b.md"
    locked.chmod(0)
    try:
        result = load_store(sessions)
    finally:
        locked.chmod(0o644)

    assert result.scan_error is None
    assert sorted(d.id for d in result.docs) == ["doc-a", "doc-c"]
    assert [p.path.name for p in result.errors] == ["doc-b.md"]


def _store_of_every_failure(tmp_path: Path) -> Path:
    sessions = _sessions(tmp_path, {"doc-a": "plain", "doc-z": "last"})
    (sessions / "bad-bytes.md").write_bytes(b"---\nid: bad\n---\n\n\xff\xfe broken\n")
    (sessions / "folder.md").mkdir()
    write_file(tmp_path, "outside.md", _doc("outside", "OUTSIDE"))
    try:
        (sessions / "linked.md").symlink_to(tmp_path / "outside.md")
    except (OSError, NotImplementedError):
        pass
    # no FIFO here: without O_NONBLOCK it would hang these tests instead of failing them, so it
    # has a test of its own with a bounded wait
    return sessions


def _assert_reads_as_the_per_file_reader(sessions: Path) -> None:
    """The same documents and the same error text, in the same order, as reading each file on
    its own path. Warnings are not compared: each comes from a field of a document compared here."""
    result = load_store(sessions)

    expected_docs, expected_errors = [], []
    for path in sorted(p for p in sessions.iterdir() if p.name.endswith(".md")):
        try:
            expected_docs.append(parse_document(path))
        except Exception as exc:
            expected_errors.append(Problem(path=path, message=f"{path.name}: {exc}"))
    assert [(d.id, d.body, d.source_identity) for d in result.docs] == [
        (d.id, d.body, d.source_identity) for d in expected_docs
    ]
    assert [(p.path, p.message) for p in result.errors] == [
        (p.path, p.message) for p in expected_errors
    ]
    assert result.scan_error is None


def test_every_document_and_error_reads_as_the_per_file_reader_gives_it(tmp_path):
    sessions = _store_of_every_failure(tmp_path)

    _assert_reads_as_the_per_file_reader(sessions)

    (folder,) = [p for p in load_store(sessions).errors if p.path.name == "folder.md"]
    assert folder.message == f"folder.md: [Errno 21] Is a directory: {str(folder.path)!r}"


@requires_permission_enforcement
def test_a_document_that_cannot_be_opened_reads_as_the_per_file_reader_names_it(tmp_path):
    """The open's error names the file as the per-file reader's open does: an absolute path given
    to the held directory's open would change the message, and ignore the held directory."""
    sessions = _store_of_every_failure(tmp_path)
    locked = sessions / "doc-z.md"
    locked.chmod(0)
    try:
        _assert_reads_as_the_per_file_reader(sessions)
    finally:
        locked.chmod(0o644)


@POSIX_ONLY
def test_a_fifo_named_like_a_document_is_refused_without_waiting(tmp_path):
    sessions = _sessions(tmp_path, {"doc-a": "plain"})
    fifo = sessions / "pipe.md"
    os.mkfifo(fifo)
    results = []
    worker = threading.Thread(target=lambda: results.append(load_store(sessions)), daemon=True)

    worker.start()
    worker.join(timeout=10)

    if worker.is_alive():
        # a reader stuck in open() is released by a writer, so a failing run leaves nothing blocked
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        pytest.fail("loading a store with a FIFO in it blocked on the FIFO")
    (result,) = results
    assert [d.id for d in result.docs] == ["doc-a"]
    assert [p.path.name for p in result.errors] == ["pipe.md"]


def _open_fds() -> int:
    for listing in ("/proc/self/fd", "/dev/fd"):
        if os.path.isdir(listing):
            return len(os.listdir(listing))
    pytest.skip("no fd listing on this platform")


@POSIX_ONLY
def test_a_load_leaves_no_descriptor_open_whatever_it_hit(tmp_path, monkeypatch):
    sessions = _store_of_every_failure(tmp_path)
    before = _open_fds()

    load_store(sessions)
    after_errors = _open_fds()

    def interrupt(path, text, identity):
        raise KeyboardInterrupt

    monkeypatch.setattr(spine, "_parse_text", interrupt)
    with pytest.raises(KeyboardInterrupt):
        load_store(sessions)

    assert after_errors == before
    assert _open_fds() == before


@requires_symlinks
def test_a_linked_file_through_the_held_directory_is_refused_by_name(tmp_path, reader):
    sessions = _sessions(tmp_path, {"doc-a": "plain"})
    write_file(tmp_path, "outside.md", _doc("outside", "OUTSIDE"))
    (sessions / "linked.md").symlink_to(tmp_path / "outside.md")

    result = load_store(sessions)

    assert [d.id for d in result.docs] == ["doc-a"]
    (problem,) = result.errors
    assert "sessions/linked.md passes through a link" in problem.message


NAMES = ["b.md", "B.md", "a.md", "A.md", "Ä.md", "ä.md", "é.md", "É.md", "Z.md", "z.md"]


def test_the_name_order_is_the_order_pathlib_gives_on_each_platform():
    windows = [p.name for p in sorted(PureWindowsPath("d") / n for n in NAMES)]
    posix = [p.name for p in sorted(PurePosixPath("d") / n for n in NAMES)]

    assert sorted(NAMES, key=str.lower) == windows
    assert sorted(NAMES) == posix
    assert spine._NAME_ORDER is (str.lower if os.name == "nt" else None)


def test_a_load_orders_documents_as_the_platform_orders_their_paths(tmp_path):
    sessions = _sessions(tmp_path, {"B": "upper", "a": "lower"})

    ids = [d.id for d in load_store(sessions).docs]

    assert ids == [p.stem for p in sorted(sessions.iterdir())]
    assert ids == (["a", "B"] if os.name == "nt" else ["B", "a"])
