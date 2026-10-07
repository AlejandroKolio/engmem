"""US-04: `engmem_complete_draft` completes exactly the draft version the client read, and two
engmem writers of one document cannot both pass their checks and both replace it."""

from __future__ import annotations

import re
import threading
from pathlib import Path

import pytest

from conftest import ACTIVE_CONTENT, DRAFT_CONTENT
from mcp_harness import _call, _run, _text, _tools_call_msg

from engmem import mcp_server, staging
from engmem.spine import load_store, parse_document

CREATE_TOOL = mcp_server.CREATE_DRAFT_TOOL_NAME
COMPLETE_TOOL = mcp_server.COMPLETE_DRAFT_TOOL_NAME
SUPERSEDE_TOOL = mcp_server.MARK_SUPERSEDED_TOOL_NAME
DOC_ID = "20260101-widget-cache"
VERSION_RE = re.compile(r"version: ([0-9a-f]+)\)")
CURRENT_VERSION_RE = re.compile(r'expected_version: "([0-9a-f]+)"')


@pytest.fixture
def store(tmp_path) -> Path:
    (tmp_path / "sessions").mkdir()
    return tmp_path


@pytest.fixture
def target(store) -> Path:
    return store / "sessions" / f"{DOC_ID}.md"


def _sessions_files(store: Path) -> list[str]:
    return sorted(p.name for p in (store / "sessions").iterdir())


def _finished(marker: str) -> str:
    return ACTIVE_CONTENT + f"\nWriter marker: {marker}\n"


def _create(store: Path, content: str = DRAFT_CONTENT) -> str:
    """Creates the draft through the tool and returns the version it reported."""
    result, is_error = _call(store, name=CREATE_TOOL, arguments={"id": DOC_ID, "content": content})
    assert not is_error, _text(result)
    match = VERSION_RE.search(_text(result))
    assert match, f"create_draft did not report a version: {_text(result)}"
    return match.group(1)


def _complete(store: Path, content: str, expected_version: str | None) -> tuple[str, bool]:
    arguments = {"id": DOC_ID, "content": content}
    if expected_version is not None:
        arguments["expected_version"] = expected_version
    return mcp_server._handle_complete_draft(arguments, store)


def test_completing_the_version_read_publishes_the_full_active_document(store, target):
    """AC-04.1."""
    version = _create(store)

    result, is_error = _call(
        store,
        name=COMPLETE_TOOL,
        arguments={"id": DOC_ID, "content": ACTIVE_CONTENT, "expected_version": version},
    )

    assert not is_error, _text(result)
    assert target.read_bytes() == ACTIVE_CONTENT.encode("utf-8")
    loaded = load_store(store / "sessions")
    assert [d.status for d in loaded.docs] == ["active"]
    assert _sessions_files(store) == [f"{DOC_ID}.md"], "no staged file or lock left behind"


def test_the_version_create_reports_is_the_version_of_the_bytes_it_wrote(store, target):
    version = _create(store)

    assert version == staging.version_of(target.read_bytes())


def test_a_completion_landing_inside_anothers_check_to_replace_window_is_refused(
    store, target, monkeypatch
):
    """AC-04.2, deterministic: B starts while A has passed every check and is about to replace
    the draft — the window the old check-then-`os.replace` left open to a second completer."""
    version = _create(store)
    real_commit = mcp_server.commit
    a_in_window, release_a = threading.Event(), threading.Event()

    def commit_pausing_a(tmp_path, path):
        if threading.current_thread().name == "A":
            a_in_window.set()
            assert release_a.wait(10)
        real_commit(tmp_path, path)

    monkeypatch.setattr(mcp_server, "commit", commit_pausing_a)
    results: dict[str, tuple[str, bool]] = {}

    def complete(name: str) -> None:
        results[name] = _complete(store, _finished(name), version)

    a = threading.Thread(target=complete, args=("A",), name="A")
    b = threading.Thread(target=complete, args=("B",), name="B")
    a.start()
    assert a_in_window.wait(10)
    b.start()
    b.join(0.5)
    release_a.set()
    a.join()
    b.join()

    assert not results["A"][1], results["A"][0]
    assert results["B"][1], "both completions were told they succeeded"
    assert "not 'draft'" in results["B"][0]
    assert target.read_bytes() == _finished("A").encode("utf-8")
    assert _sessions_files(store) == [f"{DOC_ID}.md"]


@pytest.mark.parametrize("round_number", range(10))
def test_concurrent_completions_of_one_version_have_exactly_one_winner(store, target, round_number):
    """AC-04.2 under real thread contention: every loser is refused and the file holds the
    winner's text, byte for byte."""
    version = _create(store)
    writers = 8
    barrier = threading.Barrier(writers)
    results: list[tuple[str, bool] | None] = [None] * writers

    def complete(index: int) -> None:
        barrier.wait()
        results[index] = _complete(store, _finished(f"writer-{index}"), version)

    threads = [threading.Thread(target=complete, args=(i,)) for i in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    winners = [i for i, result in enumerate(results) if result is not None and not result[1]]
    assert len(winners) == 1, [r[0] for r in results if r is not None and not r[1]]
    assert target.read_bytes() == _finished(f"writer-{winners[0]}").encode("utf-8")
    assert _sessions_files(store) == [f"{DOC_ID}.md"]


def _recreate_through_engmem(store: Path, target: Path, newer: str) -> None:
    target.unlink()
    _create(store, newer)


def _edit_in_place(store: Path, target: Path, newer: str) -> None:
    target.write_bytes(newer.encode("utf-8"))


@pytest.mark.parametrize("change", [_recreate_through_engmem, _edit_in_place])
def test_completing_a_version_the_draft_has_moved_past_is_refused_with_the_current_one(
    store, target, change
):
    """AC-04.3: refused, told to re-read, handed the current draft and its version — and that
    version then completes."""
    read_version = _create(store)
    newer = DRAFT_CONTENT + "\nAdded by another agent after the read.\n"
    change(store, target, newer)

    text, is_error = _complete(store, ACTIVE_CONTENT, read_version)

    assert is_error, "a stale completion replaced a newer draft"
    assert "Re-read" in text
    assert "Added by another agent after the read." in text, "the current draft was not handed back"
    assert target.read_bytes() == newer.encode("utf-8")
    assert _sessions_files(store) == [f"{DOC_ID}.md"]

    current = CURRENT_VERSION_RE.search(text)
    assert current and current.group(1) == staging.version_of(newer.encode("utf-8"))
    text, is_error = _complete(store, ACTIVE_CONTENT, current.group(1))
    assert not is_error, text
    assert target.read_bytes() == ACTIVE_CONTENT.encode("utf-8")


@pytest.mark.parametrize(
    "on_disk",
    [ACTIVE_CONTENT, ACTIVE_CONTENT.replace("status: active", "status: superseded")],
    ids=["active", "superseded"],
)
@pytest.mark.parametrize("version_kind", ["its own version", "no version"])
def test_completing_a_finished_document_changes_neither_content_nor_status(
    store, target, on_disk, version_kind
):
    """AC-04.4: naming the document's own current version does not get a non-draft past the
    status check."""
    target.write_bytes(on_disk.encode("utf-8"))
    version = staging.version_of(on_disk.encode("utf-8")) if version_kind == "its own version" else None

    text, is_error = _complete(store, _finished("late"), version)

    assert is_error
    assert "not 'draft'" in text
    assert target.read_bytes() == on_disk.encode("utf-8")
    assert _sessions_files(store) == [f"{DOC_ID}.md"]


def test_a_completion_without_a_version_still_completes_a_draft(store, target):
    """Clients and installed templates that predate `expected_version` keep working, with the
    weaker guarantee the contract records."""
    _create(store)

    text, is_error = _complete(store, ACTIVE_CONTENT, None)

    assert not is_error, text
    assert target.read_bytes() == ACTIVE_CONTENT.encode("utf-8")


def test_surrounding_whitespace_in_the_version_is_not_a_conflict(store, target):
    version = _create(store)

    text, is_error = _complete(store, ACTIVE_CONTENT, f"  {version}\n")

    assert not is_error, text


@pytest.mark.parametrize("bad", [123, ["abc"], {"v": 1}, True])
def test_a_non_string_expected_version_is_invalid_params(store, target, bad):
    _create(store)

    _, responses, _ = _run(
        store,
        _tools_call_msg(
            1,
            name=COMPLETE_TOOL,
            arguments={"id": DOC_ID, "content": ACTIVE_CONTENT, "expected_version": bad},
        ),
    )

    assert responses[0]["error"]["code"] == mcp_server.INVALID_PARAMS
    assert "expected_version" in responses[0]["error"]["message"]
    assert target.read_bytes() == DRAFT_CONTENT.encode("utf-8")


def test_the_complete_schema_offers_expected_version_without_requiring_it(store):
    _, responses, _ = _run(store, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    complete = next(t for t in responses[0]["result"]["tools"] if t["name"] == COMPLETE_TOOL)
    assert complete["inputSchema"]["properties"]["expected_version"]["type"] == "string"
    assert "expected_version" not in complete["inputSchema"]["required"]
    assert "expected_version" in complete["description"]


# ---------------------------------------------------------------------------
# the write lock itself
# ---------------------------------------------------------------------------


def _lock_of(target: Path) -> Path:
    return target.with_name(f".{target.name}.write-lock")


@pytest.mark.parametrize(
    "tool, arguments, on_disk",
    [
        (COMPLETE_TOOL, {"id": DOC_ID, "content": ACTIVE_CONTENT}, DRAFT_CONTENT),
        (SUPERSEDE_TOOL, {"id": DOC_ID, "superseded_by": "20260201-widget-cache-v2"}, ACTIVE_CONTENT),
    ],
    ids=["complete", "supersede"],
)
def test_a_lock_left_by_an_interrupted_write_refuses_by_name_and_writes_nothing(
    store, target, monkeypatch, tool, arguments, on_disk
):
    monkeypatch.setattr(staging, "_LOCK_WAIT_SECONDS", 0.05)
    target.write_bytes(on_disk.encode("utf-8"))
    _lock_of(target).mkdir()

    result, is_error = _call(store, name=tool, arguments=arguments)

    assert is_error
    assert f".{DOC_ID}.md.write-lock" in _text(result)
    assert "nothing was written" in _text(result)
    assert target.read_bytes() == on_disk.encode("utf-8")
    assert _lock_of(target).is_dir(), "another holder's lock must not be removed"


def test_the_lock_is_released_when_the_replace_itself_fails(store, target, monkeypatch):
    version = _create(store)

    def failing_commit(tmp_path, path):
        raise OSError(5, "simulated I/O error")

    monkeypatch.setattr(mcp_server, "commit", failing_commit)
    with pytest.raises(OSError):
        _complete(store, ACTIVE_CONTENT, version)

    assert _sessions_files(store) == [f"{DOC_ID}.md"], "lock or staged file left behind"


def test_two_supersedes_of_one_document_cannot_both_win(store, target, monkeypatch):
    """The same window, the other rewriting tool: without the lock both see `status: active`
    and the later replace drops the earlier successor while both report success."""
    target.write_bytes(ACTIVE_CONTENT.encode("utf-8"))
    real_commit = mcp_server.commit
    a_in_window, release_a = threading.Event(), threading.Event()

    def commit_pausing_a(tmp_path, path):
        if threading.current_thread().name == "A":
            a_in_window.set()
            assert release_a.wait(10)
        real_commit(tmp_path, path)

    monkeypatch.setattr(mcp_server, "commit", commit_pausing_a)
    successors = {"A": "20260201-widget-cache-a", "B": "20260201-widget-cache-b"}
    results: dict[str, tuple[str, bool]] = {}

    def supersede(name: str) -> None:
        results[name] = mcp_server._handle_mark_superseded(
            {"id": DOC_ID, "superseded_by": successors[name]}, store
        )

    a = threading.Thread(target=supersede, args=("A",), name="A")
    b = threading.Thread(target=supersede, args=("B",), name="B")
    a.start()
    assert a_in_window.wait(10)
    b.start()
    b.join(0.5)
    release_a.set()
    a.join()
    b.join()

    assert not results["A"][1], results["A"][0]
    assert results["B"][1], "both supersedes were told they succeeded"
    assert parse_document(target).superseded_by == successors["A"]
    assert _sessions_files(store) == [f"{DOC_ID}.md"]
