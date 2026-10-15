"""telemetry, cost and feedback are appended and read only inside the store (US-23)."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from support import rpc
from support.docs import write_file
from support.platform import requires_permission_enforcement, requires_symlinks

from engmem import confined
from engmem.cli import main
from engmem.confined import OutsideStoreError

SESSION = "20260105-session"
FOUND = "20260101-idempotency-adr"
QUERY = "PaymentCommand IdempotencyKey"
SENTINEL = b"OUTSIDE-SENTINEL: never appended to\n"


def _doc(doc_id: str, status: str) -> str:
    return (
        f"---\nid: {doc_id}\ntitle: {doc_id}\ndate: 2026-01-01\nstatus: {status}\n"
        f"entities: [PaymentCommand, IdempotencyKey]\nmode: daily\n---\n\n"
        "## Decision Log\n\nA repeated PaymentCommand must not repeat its side effect.\n"
    )


@pytest.fixture
def store(tmp_path) -> Path:
    sessions = tmp_path / "store" / "sessions"
    sessions.mkdir(parents=True)
    write_file(sessions, f"{FOUND}.md", _doc(FOUND, "active"))
    write_file(sessions, f"{SESSION}.md", _doc(SESSION, "draft"))
    (tmp_path / "outside").mkdir()
    return tmp_path / "store"


def _sentinel(store: Path, name: str) -> Path:
    """`store/<name>` made a link to an outside file holding `SENTINEL`."""
    outside = store.parent / "outside" / name
    outside.write_bytes(SENTINEL)
    (store / name).unlink(missing_ok=True)
    (store / name).symlink_to(outside)
    return outside


def _cli(capsys, store: Path, *args: str) -> tuple[int, str]:
    code = main([*args, "--store", str(store)])
    out, err = capsys.readouterr()
    return code, out + err


@pytest.fixture(params=["openat", "checked"])
def writer(request, monkeypatch):
    """Both append paths: `dir_fd` opens where the platform has them, per-part checks otherwise."""
    if request.param == "openat" and not confined._OPENAT:
        pytest.skip("this platform has no dir_fd opens")
    monkeypatch.setattr(confined, "_OPENAT", request.param == "openat")
    return request.param


# ---------------------------------------------------------------------------
# AC-23.1: each log a link to an outside sentinel
# ---------------------------------------------------------------------------


def _search(capsys, store):
    return _cli(capsys, store, "search", QUERY, "--session", SESSION)


def _role_search(capsys, store):
    return _cli(capsys, store, "search", QUERY, "--role", "decisions", "--session", SESSION)


def _read(capsys, store):
    return _cli(capsys, store, "read", FOUND, "--session", SESSION)


def _baseline(capsys, store):
    return _cli(capsys, store, "cost", "record-baseline", SESSION, "--tokens", "100")


def _feedback(capsys, store):
    return _cli(capsys, store, "feedback", "record", SESSION, FOUND, "helped")


@requires_symlinks
@pytest.mark.parametrize(
    "log, act, refusal",
    [
        pytest.param("telemetry.jsonl", _search, "note: telemetry not recorded", id="search"),
        pytest.param("telemetry.jsonl", _role_search, "note: telemetry not recorded",
                     id="role-search"),
        pytest.param("cost.jsonl", _search, "note: cost not recorded", id="search-cost"),
        pytest.param("cost.jsonl", _read, "note: cost not recorded", id="read-cost"),
        pytest.param("cost.jsonl", _baseline, "cannot write", id="record-baseline"),
        pytest.param("feedback.jsonl", _feedback, "cannot write", id="feedback"),
    ],
)
def test_a_linked_log_is_never_appended_to_and_the_miss_is_shown(
    store, capsys, writer, log, act, refusal
):
    _search(capsys, store)  # the find a feedback record assesses
    outside = _sentinel(store, log)

    _code, output = act(capsys, store)

    assert outside.read_bytes() == SENTINEL
    assert refusal in output and "passes through a link" in output
    assert "nothing was written" in output


@requires_symlinks
def test_the_read_only_mcp_search_does_not_append_telemetry_through_a_link(store, writer):
    outside = _sentinel(store, "telemetry.jsonl")

    _code, responses, _raw = rpc.run(
        store, rpc.tools_call_msg(1, arguments={"query": QUERY}), read_only=True
    )
    text = rpc.text(responses[0]["result"])

    assert outside.read_bytes() == SENTINEL
    assert FOUND in text and "telemetry not recorded" in text


@requires_symlinks
@pytest.mark.parametrize(
    "log, name, arguments",
    [
        pytest.param("cost.jsonl", "engmem_read", {"id": FOUND, "session_id": SESSION},
                     id="read-cost"),
        pytest.param("feedback.jsonl", "engmem_record_feedback",
                     {"session_id": SESSION, "doc_id": FOUND, "assessment": "helped"},
                     id="feedback"),
        pytest.param("cost.jsonl", "engmem_record_baseline",
                     {"session_id": SESSION, "tokens": 100}, id="record-baseline"),
    ],
)
def test_the_mcp_write_paths_do_not_append_through_a_link(store, capsys, log, name, arguments):
    _search(capsys, store)
    outside = _sentinel(store, log)

    _code, responses, _raw = rpc.run(store, rpc.tools_call_msg(1, name=name, arguments=arguments))

    assert outside.read_bytes() == SENTINEL
    assert "passes through a link" in rpc.text(responses[0]["result"])


@requires_symlinks
@pytest.mark.parametrize(
    "log, command",
    [
        pytest.param("telemetry.jsonl", ["telemetry"], id="telemetry"),
        pytest.param("cost.jsonl", ["cost", "summary", "--session", SESSION], id="cost"),
        pytest.param("feedback.jsonl", ["feedback", "summary"], id="feedback"),
    ],
)
def test_a_summary_never_reads_a_linked_log(store, capsys, log, command):
    """A feedback line carries the user's own decision text, so reading through the link would
    print whatever the outside file says."""
    outside = store.parent / "outside" / log
    outside.write_text(
        json.dumps({"session_id": SESSION, "doc_id": FOUND, "assessment": "helped",
                    "decision": "OUTSIDE-DECISION-TEXT", "channel": "cli"}) + "\n",
        encoding="utf-8",
    )
    (store / log).symlink_to(outside)

    code, output = _cli(capsys, store, *command)

    assert code == 2
    assert "OUTSIDE-DECISION-TEXT" not in output and "passes through a link" in output


# ---------------------------------------------------------------------------
# AC-23.2: a destination that is not a writable regular file
# ---------------------------------------------------------------------------


def test_a_directory_in_the_logs_place_is_named_and_the_search_still_answers(store, capsys, writer):
    (store / "telemetry.jsonl").mkdir()

    code, output = _search(capsys, store)

    assert code == 0 and FOUND in output
    assert "note: telemetry not recorded" in output


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_a_fifo_in_the_logs_place_is_refused_instead_of_blocking(store, capsys):
    os.mkfifo(store / "telemetry.jsonl")

    code, output = _search(capsys, store)

    assert code == 0 and "note: telemetry not recorded" in output and "Not a regular file" in output


@requires_permission_enforcement
def test_an_unwritable_log_is_named_and_not_reported_as_recorded(store, capsys, writer):
    _search(capsys, store)
    (store / "feedback.jsonl").write_bytes(b"")
    (store / "feedback.jsonl").chmod(0o444)
    try:
        code, output = _feedback(capsys, store)
    finally:
        (store / "feedback.jsonl").chmod(0o644)

    assert code == 2 and "cannot write" in output and "recorded helped" not in output


# ---------------------------------------------------------------------------
# AC-23.3: concurrent appends
# ---------------------------------------------------------------------------


def test_concurrent_appends_each_land_as_one_whole_line(store, writer):
    """Each append that returned is in the log once, whole; one that did not says why. A busy
    lock on a slow filesystem is a named miss, not a lost record, so it is allowed here."""
    path = store / "cost.jsonl"
    path.write_bytes(b'{"torn": ')  # an interrupted write left a line open
    count = 40
    landed: list[int] = []
    refused: list[OSError] = []

    def append(n: int) -> None:
        line = json.dumps({"n": n, "pad": "x" * 2000}).encode("utf-8")
        try:
            confined.append_line(path, confined.LOG_DEPTH, line)
        except OSError as exc:
            refused.append(exc)
            return
        landed.append(n)

    threads = [threading.Thread(target=append, args=(n,)) for n in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    lines = [line for line in path.read_bytes().split(b"\n") if line]
    assert lines[0] == b'{"torn": '
    assert sorted(json.loads(line)["n"] for line in lines[1:]) == sorted(landed)
    assert len(landed) + len(refused) == count
    assert all("is held by another engmem call" in str(exc) for exc in refused)


# ---------------------------------------------------------------------------
# AC-23.4: the log swapped for a link between the check and the open
# ---------------------------------------------------------------------------


@requires_symlinks
@pytest.mark.parametrize(
    "log, act",
    [
        pytest.param("telemetry.jsonl", _search, id="telemetry"),
        pytest.param("cost.jsonl", _read, id="cost"),
        pytest.param("feedback.jsonl", _feedback, id="feedback"),
    ],
)
def test_without_dir_fd_a_swap_after_the_check_is_not_written_through(
    store, capsys, monkeypatch, log, act
):
    _search(capsys, store)
    monkeypatch.setattr(confined, "_OPENAT", False)
    path = store / log
    path.write_bytes(b"")
    outside = store.parent / "outside" / log
    outside.write_bytes(SENTINEL)
    real_check = confined._check_components

    def check_then_swap(checked_path, depth, write=False):
        result = real_check(checked_path, depth, write)
        if checked_path == path and not path.is_symlink():
            path.unlink()
            path.symlink_to(outside)
        return result

    monkeypatch.setattr(confined, "_check_components", check_then_swap)

    _code, output = act(capsys, store)

    assert outside.read_bytes() == SENTINEL
    assert "passes through a link" in output


@requires_symlinks
def test_with_dir_fd_a_link_already_there_fails_the_open(store):
    if not confined._OPENAT:
        pytest.skip("this platform has no dir_fd opens")
    outside = _sentinel(store, "telemetry.jsonl")

    with pytest.raises(OutsideStoreError, match="nothing was written"):
        confined.append_line(store / "telemetry.jsonl", confined.LOG_DEPTH, b"{}")
    assert outside.read_bytes() == SENTINEL


def test_a_new_log_is_created_like_open_would_create_it(store, writer):
    confined.append_line(store / "cost.jsonl", confined.LOG_DEPTH, b"{}")

    assert (store / "cost.jsonl").read_bytes() == b"{}\n"
    if os.name != "nt":
        umask = os.umask(0)
        os.umask(umask)
        assert (store / "cost.jsonl").stat().st_mode & 0o777 == 0o666 & ~umask


@requires_symlinks
def test_a_dangling_log_link_creates_nothing_outside(store, capsys, writer):
    """Opening a dangling link for append would create its target, outside the store."""
    target = store.parent / "outside" / "created-by-engmem.jsonl"
    (store / "telemetry.jsonl").symlink_to(target)

    _code, output = _search(capsys, store)

    assert not target.exists()
    assert "note: telemetry not recorded" in output and "passes through a link" in output


def test_without_dir_fd_appenders_take_the_stores_write_lock(store, monkeypatch):
    """On Windows O_APPEND is a seek then a write, so two appenders can overwrite each other;
    the lock serialises them, and a held lock is a named miss, never a torn log."""
    from engmem import staging

    monkeypatch.setattr(confined, "_OPENAT", False)
    monkeypatch.setattr(staging, "_LOCK_WAIT_SECONDS", 0.05)
    path = store / "cost.jsonl"
    (store / ".cost.jsonl.write-lock").mkdir()

    with pytest.raises(OSError, match="is held by another engmem call") as raised:
        confined.append_line(path, confined.LOG_DEPTH, b"{}")
    assert str(store / ".cost.jsonl.write-lock") in str(raised.value)
    assert "remove it" in str(raised.value)
    assert not path.exists()


@pytest.mark.parametrize("entry_there, busy", [(True, True), (False, False)])
def test_a_windows_permission_error_is_a_busy_lock_only_while_the_lock_exists(
    tmp_path, monkeypatch, entry_there, busy
):
    """A lock pending delete answers `mkdir` with access denied; a store that refuses the
    create does too, and waiting that out would report a permission problem as a lock."""
    from engmem import staging

    lock = tmp_path / ".cost.jsonl.write-lock"
    if entry_there:
        lock.mkdir()

    def denied(_path, *_args, **_kwargs):
        raise PermissionError(13, "Access is denied", str(lock))

    monkeypatch.setattr(staging, "_WINDOWS", True)
    monkeypatch.setattr(staging.os, "mkdir", denied)

    if busy:
        assert staging._take_lock(lock) is False
    else:
        with pytest.raises(PermissionError):
            staging._take_lock(lock)


def test_without_dir_fd_a_store_that_refuses_the_lock_fails_at_once(store, monkeypatch):
    from engmem import staging

    monkeypatch.setattr(confined, "_OPENAT", False)
    monkeypatch.setattr(staging, "_WINDOWS", True)
    monkeypatch.setattr(staging, "_LOCK_WAIT_SECONDS", 60.0)

    def denied(path, *_args, **_kwargs):
        raise PermissionError(13, "Access is denied", str(path))

    monkeypatch.setattr(staging.os, "mkdir", denied)

    with pytest.raises(PermissionError, match="Access is denied"):
        confined.append_line(store / "cost.jsonl", confined.LOG_DEPTH, b"{}")


@requires_symlinks
def test_feedback_never_takes_its_finds_from_a_linked_telemetry_log(store, capsys):
    """The finds a feedback record may assess come from telemetry: an outside file could
    otherwise vouch for a document no search of this session showed."""
    outside = store.parent / "outside" / "telemetry.jsonl"
    outside.write_text(
        json.dumps({"session_id": SESSION, "surfaced": [FOUND], "hits": [], "channel": "cli",
                    "result": "hit"}) + "\n",
        encoding="utf-8",
    )
    (store / "telemetry.jsonl").symlink_to(outside)

    code, output = _feedback(capsys, store)

    assert code == 2 and "passes through a link" in output
    assert not (store / "feedback.jsonl").exists()


@requires_symlinks
@pytest.mark.parametrize(
    "log, command",
    [
        pytest.param("telemetry.jsonl", ["telemetry"], id="telemetry"),
        pytest.param("cost.jsonl", ["cost", "summary", "--session", SESSION], id="cost"),
        pytest.param("feedback.jsonl", ["feedback", "summary"], id="feedback"),
    ],
)
def test_a_summary_names_a_dangling_log_link_instead_of_reading_it_as_empty(
    store, capsys, log, command
):
    """The writer refuses the same name, so "0 rows" here would contradict every search."""
    (store / log).symlink_to(store.parent / "outside" / "gone.jsonl")

    code, output = _cli(capsys, store, *command)

    assert code == 2 and "passes through a link" in output


@requires_symlinks
def test_feedback_names_a_dangling_telemetry_link_instead_of_finding_no_finds(store, capsys):
    (store / "telemetry.jsonl").symlink_to(store.parent / "outside" / "gone.jsonl")

    code, output = _feedback(capsys, store)

    assert code == 2 and "passes through a link" in output
    assert "was not shown by any search" not in output


def test_a_windows_lock_released_between_the_mkdir_and_the_check_is_taken(tmp_path, monkeypatch):
    """The first `mkdir` hit the entry pending delete; it was gone by the check. A second
    `mkdir` takes the lock instead of calling a busy moment the store's refusal."""
    from engmem import staging

    lock = tmp_path / ".cost.jsonl.write-lock"
    real_mkdir = staging.os.mkdir
    calls = []

    def denied_once(path, *args, **kwargs):
        calls.append(path)
        if len(calls) == 1:
            raise PermissionError(13, "Access is denied", str(path))
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(staging, "_WINDOWS", True)
    monkeypatch.setattr(staging.os, "mkdir", denied_once)

    assert staging._take_lock(lock) is True
    assert lock.is_dir() and len(calls) == 2
