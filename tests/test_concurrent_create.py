"""US-03: `engmem_create_draft` under concurrent creators — one id, one winner, no lost text."""

from __future__ import annotations

import errno
import threading
from importlib import resources
from pathlib import Path

import pytest

from conftest import ACTIVE_CONTENT, DRAFT_CONTENT, requires_symlinks
from mcp_harness import _call, _run, _text, _tools_call_msg

from engmem import mcp_server, staging
from engmem.scoring import search
from engmem.spine import load_store

CREATE_TOOL = mcp_server.CREATE_DRAFT_TOOL_NAME
DOC_ID = "20260101-widget-cache"


@pytest.fixture
def store(tmp_path) -> Path:
    (tmp_path / "sessions").mkdir()
    return tmp_path


def _draft(doc_id: str, marker: str) -> str:
    return DRAFT_CONTENT.replace("20260101-widget-cache", doc_id) + f"\nWriter marker: {marker}\n"


def _sessions_files(store: Path) -> list[str]:
    return sorted(p.name for p in (store / "sessions").iterdir())


def _create_concurrently(store: Path, requests: list[tuple[str, str]]) -> list[tuple[str, bool]]:
    """Every request released by one barrier, so each call starts before any has committed."""
    barrier = threading.Barrier(len(requests))
    results: list[tuple[str, bool] | None] = [None] * len(requests)

    def create(index: int, doc_id: str, content: str) -> None:
        barrier.wait()
        results[index] = mcp_server._handle_create_draft({"id": doc_id, "content": content}, store)

    threads = [
        threading.Thread(target=create, args=(i, doc_id, content))
        for i, (doc_id, content) in enumerate(requests)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return [r for r in results if r is not None]


def test_a_creator_that_lands_after_the_existence_check_is_refused_not_overwritten(store, monkeypatch):
    """AC-03.1, deterministic: the other writer lands in the window between the tool's own
    existence check and its commit, the window the old `exists()`-then-`os.replace` left open."""
    target = store / "sessions" / f"{DOC_ID}.md"
    other_writer = _draft(DOC_ID, "first")
    real_stage = mcp_server._stage_content

    def stage_after_another_creator(path, content):
        target.write_bytes(other_writer.encode("utf-8"))
        return real_stage(path, content)

    monkeypatch.setattr(mcp_server, "_stage_content", stage_after_another_creator)

    result, is_error = _call(
        store, name=CREATE_TOOL, arguments={"id": DOC_ID, "content": _draft(DOC_ID, "second")}
    )

    assert is_error, "the second creator was told it succeeded"
    assert DOC_ID in _text(result) and "already exists" in _text(result)
    assert target.read_bytes() == other_writer.encode("utf-8")
    assert _sessions_files(store) == [f"{DOC_ID}.md"], "the staged file must be gone"


@pytest.fixture(params=["link", "no-hard-links"])
def link_support(request, monkeypatch):
    if request.param == "no-hard-links":
        def unsupported(src, dst, *args, **kwargs):
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(staging.os, "link", unsupported)
    return request.param


@pytest.mark.parametrize("round_", range(5))
def test_concurrent_creators_of_one_id_get_exactly_one_success(store, round_, link_support):
    """AC-03.1 with real threads: exactly one success, and the stored text is the winner's.
    A loser that met the fallback's lock rather than the document still gets a conflict."""
    contents = [_draft(DOC_ID, f"writer-{n}") for n in range(8)]

    results = _create_concurrently(store, [(DOC_ID, content) for content in contents])

    winners = [i for i, (_, is_error) in enumerate(results) if not is_error]
    assert len(results) == len(contents)
    assert len(winners) == 1, [text for text, _ in results]
    for text, is_error in results:
        if is_error:
            assert DOC_ID in text and "never overwrites" in text
    stored = (store / "sessions" / f"{DOC_ID}.md").read_text(encoding="utf-8")
    assert stored == contents[winners[0]]
    assert _sessions_files(store) == [f"{DOC_ID}.md"]


def test_concurrent_creators_of_different_ids_both_keep_their_full_documents(store, link_support):
    """AC-03.3."""
    requests = [(f"2026010{n}-widget-cache", None) for n in range(1, 9)]
    requests = [(doc_id, _draft(doc_id, doc_id)) for doc_id, _ in requests]

    results = _create_concurrently(store, requests)

    assert all(not is_error for _, is_error in results), results
    for doc_id, content in requests:
        assert (store / "sessions" / f"{doc_id}.md").read_text(encoding="utf-8") == content
    loaded = load_store(store / "sessions")
    assert loaded.errors == []
    assert sorted(d.id for d in loaded.docs) == sorted(doc_id for doc_id, _ in requests)


_SUPERSEDED_CONTENT = ACTIVE_CONTENT.replace(
    "status: active", "status: superseded\nsuperseded_by: 20260201-widget-cache-v2"
).replace("superseded_by:\n", "", 1)


@pytest.mark.parametrize(
    "existing",
    [DRAFT_CONTENT, ACTIVE_CONTENT, _SUPERSEDED_CONTENT, "", "not a document at all\n"],
    ids=["draft", "active", "superseded", "empty", "unparseable"],
)
def test_re_creating_an_existing_id_is_refused_and_leaves_it_byte_identical(store, existing):
    """AC-03.2: refused whatever the occupant's status, or whether it parses at all."""
    target = store / "sessions" / f"{DOC_ID}.md"
    target.write_bytes(existing.encode("utf-8"))

    result, is_error = _call(
        store, name=CREATE_TOOL, arguments={"id": DOC_ID, "content": _draft(DOC_ID, "late")}
    )

    assert is_error
    assert DOC_ID in _text(result) and "already exists" in _text(result)
    assert target.read_bytes() == existing.encode("utf-8")
    assert _sessions_files(store) == [f"{DOC_ID}.md"]


@requires_symlinks
def test_a_symlink_planted_after_the_checks_is_never_written_through(store, monkeypatch, tmp_path):
    """The target name is taken by a link that appeared after `_resolve_write_target` ran: an
    exclusive create refuses it as occupied rather than replacing or following it."""
    target = store / "sessions" / f"{DOC_ID}.md"
    outside = tmp_path / "outside.md"
    real_stage = mcp_server._stage_content

    def stage_after_a_link_appears(path, content):
        target.symlink_to(outside)
        return real_stage(path, content)

    monkeypatch.setattr(mcp_server, "_stage_content", stage_after_a_link_appears)

    result, is_error = _call(
        store, name=CREATE_TOOL, arguments={"id": DOC_ID, "content": _draft(DOC_ID, "late")}
    )

    assert is_error
    assert "already exists" in _text(result)
    assert target.is_symlink()
    assert not outside.exists()


UNIQUE_TERM = "zanzibarquokka"


def test_an_interrupted_create_leaves_no_document_and_reports_no_success(store, monkeypatch):
    """AC-03.4: interrupted at the commit, the call raises instead of returning a success
    text, and nothing it staged is left for a search to find."""
    def interrupted(tmp_path, target):
        raise KeyboardInterrupt

    monkeypatch.setattr(mcp_server, "commit_new", interrupted)
    content = _draft(DOC_ID, UNIQUE_TERM)

    with pytest.raises(KeyboardInterrupt):
        mcp_server._handle_create_draft({"id": DOC_ID, "content": content}, store)

    assert _sessions_files(store) == []


def test_a_staged_file_left_by_a_crash_is_never_a_search_result(store):
    """AC-03.4 for a process killed between stage and commit, where no cleanup runs: even a
    complete, active document under the staged name is not a document of the store."""
    target = store / "sessions" / f"{DOC_ID}.md"
    staging.stage(target, (ACTIVE_CONTENT + f"\n{UNIQUE_TERM}\n").encode("utf-8"))

    loaded = load_store(store / "sessions")

    assert loaded.docs == [] and loaded.errors == []
    assert search(loaded.docs, UNIQUE_TERM).hits == []
    result, is_error = _call(
        store, name=CREATE_TOOL, arguments={"id": DOC_ID, "content": _draft(DOC_ID, "retry")}
    )
    assert not is_error, "the leftover must not block the id either"


def test_a_link_failure_that_is_not_missing_hard_links_is_a_fault_not_a_success(store, monkeypatch):
    """AC-03.4: an I/O error at the publish step is a -32603 the caller cannot mistake for a
    created draft, and it leaves neither a document nor a staged file."""
    def io_error(*args, **kwargs):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(staging.os, "link", io_error)

    _, responses, _ = _run(store, _tools_call_msg(
        1, name=CREATE_TOOL, arguments={"id": DOC_ID, "content": _draft(DOC_ID, "mine")}
    ))

    assert responses[0]["error"]["code"] == -32603
    assert "Input/output error" in responses[0]["error"]["message"]
    assert _sessions_files(store) == []


def test_the_start_template_tells_a_direct_writer_not_to_overwrite():
    """The shell path never reaches `commit_new`; the template's instruction is all it has."""
    template = (resources.files("engmem") / "templates" / "engmem.start.md").read_text(
        encoding="utf-8"
    )
    bullet = template.split("- If you can write files directly:", 1)[1].split("\n- ", 1)[0]
    words = " ".join(bullet.split())

    assert "only if no file by that name exists yet" in words
    assert "pick a new id" in words and "never overwrite" in words
