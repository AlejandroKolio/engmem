"""Contract tests for the MCP write tools: `engmem_create_draft`,
`engmem_complete_draft`, `engmem_mark_superseded`.

These give a shell-less runtime (Claude Desktop) the save half of the engmem
workflow — `engmem.start.md` and `engmem.save.md` cannot create or finish a draft
without them. See those templates for the exact document shape these tools accept.

Same transport rule as `test_mcp_server.py`: stdout carries newline-delimited
JSON-RPC frames and nothing else. `_run` below parses every line strictly.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from conftest import requires_symlinks

from engmem import mcp_server
from engmem.mcp_server import serve
from engmem.spine import load_store

CREATE_TOOL = mcp_server.CREATE_DRAFT_TOOL_NAME
COMPLETE_TOOL = mcp_server.COMPLETE_DRAFT_TOOL_NAME
SUPERSEDE_TOOL = mcp_server.MARK_SUPERSEDED_TOOL_NAME


# ---------------------------------------------------------------------------
# helpers — deliberately self-contained rather than imported from
# test_mcp_server.py, so this file has no coupling to that one's fixtures.
# ---------------------------------------------------------------------------


def _stdin_of_messages(*messages: dict) -> io.StringIO:
    text = "".join(json.dumps(m) + "\n" for m in messages)
    return io.StringIO(text)


def _run(store: Path, *messages: dict) -> tuple[int, list[dict], str]:
    stdin = _stdin_of_messages(*messages)
    stdout = io.StringIO()
    exit_code = serve(store, stdin=stdin, stdout=stdout)
    raw = stdout.getvalue()
    responses = []
    for line in raw.splitlines():
        assert line.strip(), "blank stdout line — must never be written"
        responses.append(json.loads(line))  # raises loudly if a frame is not JSON
    return exit_code, responses, raw


def _call_msg(msg_id: int, *, name: str, arguments: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def _call(store: Path, *, name: str, arguments: dict) -> tuple[dict, bool]:
    """Runs a single tools/call and returns (result, is_error)."""
    _, responses, _ = _run(store, _call_msg(1, name=name, arguments=arguments))
    result = responses[0]["result"]
    return result, bool(result.get("isError"))


def _text(result: dict) -> str:
    return result["content"][0]["text"]


DRAFT_CONTENT = """---
id: 20260101-widget-cache
title: WidgetCache rollout
date: 2026-01-01
task_date: 2026-01-01
status: draft
superseded_by:
backfilled: false
tags: []
entities: []
related: []
covers_files: []
verified_at_commit:
capture_minutes:
---

## Pre-reg

Naive baseline: read the code and guess at the rollout shape.
pre-reg source: self (no sub-agent available)
"""

ACTIVE_CONTENT = """---
id: 20260101-widget-cache
title: WidgetCache rollout
date: 2026-01-01
task_date: 2026-01-01
status: active
superseded_by:
backfilled: false
tags: [cache]
entities: [WidgetCache, CacheWarmer]
related: []
covers_files: [WidgetCache.java]
verified_at_commit: abc1234
capture_minutes: 14
---

## Pre-reg

Naive baseline: read the code and guess at the rollout shape.
pre-reg source: self (no sub-agent available)

## Decision Log

Chose CacheWarmer over a lazy cache fill because cold start latency was too high.

## Landmines

CacheWarmer must run before WidgetCache accepts traffic or the first request hangs.

## Cold-start primer

WidgetCache serves cached widgets behind CacheWarmer, which pre-fills on boot.

## Reuse Log

Prior docs used: none.

## Search Trace

shell
"""


@pytest.fixture
def store(tmp_path) -> Path:
    (tmp_path / "sessions").mkdir()
    return tmp_path


def _sessions_files(store: Path) -> list[str]:
    return sorted(p.name for p in (store / "sessions").iterdir())


# ---------------------------------------------------------------------------
# engmem_create_draft
# ---------------------------------------------------------------------------


def test_create_draft_writes_a_document_load_store_parses_cleanly(store):
    result, is_error = _call(
        store, name=CREATE_TOOL, arguments={"id": "20260101-widget-cache", "content": DRAFT_CONTENT}
    )

    assert not is_error
    assert "20260101-widget-cache" in _text(result)

    written = (store / "sessions" / "20260101-widget-cache.md").read_text(encoding="utf-8")
    assert written == DRAFT_CONTENT

    load_result = load_store(store / "sessions")
    assert load_result.errors == []
    doc = {d.id: d for d in load_result.docs}["20260101-widget-cache"]
    assert doc.status == "draft"


def test_create_draft_refuses_to_overwrite_an_existing_file(store):
    existing = store / "sessions" / "20260101-widget-cache.md"
    existing.write_text("not touched", encoding="utf-8")

    result, is_error = _call(
        store, name=CREATE_TOOL, arguments={"id": "20260101-widget-cache", "content": DRAFT_CONTENT}
    )

    assert is_error
    assert "already exists" in _text(result)
    assert existing.read_text(encoding="utf-8") == "not touched"


def test_create_draft_rejects_status_other_than_draft(store):
    result, is_error = _call(
        store, name=CREATE_TOOL, arguments={"id": "20260101-widget-cache", "content": ACTIVE_CONTENT}
    )

    assert is_error
    assert "draft" in _text(result)
    assert _sessions_files(store) == []


def test_create_draft_rejects_id_mismatch_between_argument_and_front_matter(store):
    result, is_error = _call(
        store, name=CREATE_TOOL, arguments={"id": "20260101-something-else", "content": DRAFT_CONTENT}
    )

    assert is_error
    assert "20260101-something-else" in _text(result)
    assert "20260101-widget-cache" in _text(result)
    assert _sessions_files(store) == []


def test_create_draft_rejects_unparseable_content_no_traceback(store):
    broken = "---\nid: 20260101-widget-cache\nstatus: draft\n"  # unclosed front matter

    result, is_error = _call(
        store, name=CREATE_TOOL, arguments={"id": "20260101-widget-cache", "content": broken}
    )

    assert is_error
    assert "Traceback" not in _text(result)
    assert _sessions_files(store) == []


def test_create_draft_missing_id_argument_is_invalid_params(store):
    _, responses, _ = _run(
        store, _call_msg(1, name=CREATE_TOOL, arguments={"content": DRAFT_CONTENT})
    )

    assert responses[0]["error"]["code"] == -32602
    assert "id" in responses[0]["error"]["message"]


def test_create_draft_missing_content_argument_is_invalid_params(store):
    _, responses, _ = _run(
        store, _call_msg(1, name=CREATE_TOOL, arguments={"id": "20260101-widget-cache"})
    )

    assert responses[0]["error"]["code"] == -32602
    assert "content" in responses[0]["error"]["message"]


# ---------------------------------------------------------------------------
# escape vectors — shared path-containment logic under `engmem_create_draft`,
# which every write tool routes through
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    [
        "../../etc/passwd",
        "/etc/passwd",
        "..",
        ".",
        ".hidden-doc",
        "widget\x00cache-job",
        "widget\\cache-job",
    ],
    ids=[
        "dotdot-traversal",
        "absolute-path",
        "bare-dotdot",
        "bare-dot",
        "leading-dot",
        "embedded-nul",
        "backslash-separator",
    ],
)
def test_create_draft_rejects_every_escape_vector(store, bad_id):
    result, is_error = _call(
        store, name=CREATE_TOOL, arguments={"id": bad_id, "content": DRAFT_CONTENT}
    )

    assert is_error
    assert "Traceback" not in _text(result)
    # nothing was created anywhere in or under the store — not even the temp
    # staging file `_stage_content` writes before validation
    assert set(p.name for p in store.iterdir()) == {"sessions"}
    assert _sessions_files(store) == []


def test_create_draft_with_empty_id_is_invalid_params(store):
    """An empty `id` never reaches the filesystem-safety checks at all — it fails
    the same "required, non-empty string" schema check every other required
    write-tool argument does, at the protocol level, before `_ToolError` is even
    in play."""
    _, responses, _ = _run(
        store, _call_msg(1, name=CREATE_TOOL, arguments={"id": "", "content": DRAFT_CONTENT})
    )

    assert responses[0]["error"]["code"] == -32602
    assert set(p.name for p in store.iterdir()) == {"sessions"}
    assert _sessions_files(store) == []


@requires_symlinks
def test_create_draft_rejects_a_sessions_dir_that_is_a_symlink_out_of_the_store(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    (store_dir / "sessions").symlink_to(outside, target_is_directory=True)

    result, is_error = _call(
        store_dir,
        name=CREATE_TOOL,
        arguments={"id": "20260101-widget-cache", "content": DRAFT_CONTENT},
    )

    assert is_error
    assert list(outside.iterdir()) == [], "must never write through the escaping symlink"


# ---------------------------------------------------------------------------
# engmem_complete_draft
# ---------------------------------------------------------------------------


def _write_raw(store: Path, doc_id: str, content: str) -> Path:
    path = store / "sessions" / f"{doc_id}.md"
    path.write_text(content, encoding="utf-8")
    return path


def test_complete_draft_transitions_status_from_draft_to_active(store):
    _write_raw(store, "20260101-widget-cache", DRAFT_CONTENT)

    result, is_error = _call(
        store,
        name=COMPLETE_TOOL,
        arguments={"id": "20260101-widget-cache", "content": ACTIVE_CONTENT},
    )

    assert not is_error
    text = _text(result)
    assert "draft" in text and "active" in text

    written = (store / "sessions" / "20260101-widget-cache.md").read_text(encoding="utf-8")
    assert written == ACTIVE_CONTENT

    load_result = load_store(store / "sessions")
    assert load_result.errors == []
    doc = {d.id: d for d in load_result.docs}["20260101-widget-cache"]
    assert doc.status == "active"
    assert "## Decision Log" in doc.body
    assert "## Landmines" in doc.body
    assert "## Cold-start primer" in doc.body
    assert "## Reuse Log" in doc.body
    assert "## Search Trace" in doc.body


def test_complete_draft_refuses_when_no_draft_exists(store):
    result, is_error = _call(
        store,
        name=COMPLETE_TOOL,
        arguments={"id": "20260101-widget-cache", "content": ACTIVE_CONTENT},
    )

    assert is_error
    assert "no draft" in _text(result).lower() or "does not exist" in _text(result).lower()
    assert _sessions_files(store) == []


def test_complete_draft_refuses_to_overwrite_a_non_draft_document(store):
    original = _write_raw(store, "20260101-widget-cache", ACTIVE_CONTENT).read_text(
        encoding="utf-8"
    )

    result, is_error = _call(
        store,
        name=COMPLETE_TOOL,
        arguments={"id": "20260101-widget-cache", "content": ACTIVE_CONTENT},
    )

    assert is_error
    assert "active" in _text(result)
    assert (store / "sessions" / "20260101-widget-cache.md").read_text(
        encoding="utf-8"
    ) == original


def test_complete_draft_rejects_new_content_that_does_not_move_to_active(store):
    original = _write_raw(store, "20260101-widget-cache", DRAFT_CONTENT).read_text(
        encoding="utf-8"
    )

    result, is_error = _call(
        store,
        name=COMPLETE_TOOL,
        arguments={"id": "20260101-widget-cache", "content": DRAFT_CONTENT},
    )

    assert is_error
    assert "active" in _text(result)
    assert (store / "sessions" / "20260101-widget-cache.md").read_text(
        encoding="utf-8"
    ) == original


def test_complete_draft_rejects_id_mismatch(store):
    original = _write_raw(store, "20260101-widget-cache", DRAFT_CONTENT).read_text(
        encoding="utf-8"
    )

    mismatched = ACTIVE_CONTENT.replace(
        "id: 20260101-widget-cache", "id: 20260101-something-else"
    )
    result, is_error = _call(
        store, name=COMPLETE_TOOL, arguments={"id": "20260101-widget-cache", "content": mismatched}
    )

    assert is_error
    assert (store / "sessions" / "20260101-widget-cache.md").read_text(
        encoding="utf-8"
    ) == original


@requires_symlinks
def test_complete_draft_refuses_to_write_through_a_symlinked_target(tmp_path):
    store_dir = tmp_path / "store"
    (store_dir / "sessions").mkdir(parents=True)
    outside_target = tmp_path / "outside.md"
    outside_target.write_text("not engmem's to touch", encoding="utf-8")
    (store_dir / "sessions" / "20260101-widget-cache.md").symlink_to(outside_target)

    result, is_error = _call(
        store_dir,
        name=COMPLETE_TOOL,
        arguments={"id": "20260101-widget-cache", "content": ACTIVE_CONTENT},
    )

    assert is_error
    assert "symlink" in _text(result).lower()
    assert outside_target.read_text(encoding="utf-8") == "not engmem's to touch"


def test_complete_draft_missing_content_argument_is_invalid_params(store):
    _write_raw(store, "20260101-widget-cache", DRAFT_CONTENT)

    _, responses, _ = _run(
        store, _call_msg(1, name=COMPLETE_TOOL, arguments={"id": "20260101-widget-cache"})
    )

    assert responses[0]["error"]["code"] == -32602


# ---------------------------------------------------------------------------
# engmem_mark_superseded
# ---------------------------------------------------------------------------


def test_mark_superseded_sets_status_and_superseded_by(store):
    _write_raw(store, "20260101-widget-cache", ACTIVE_CONTENT)

    result, is_error = _call(
        store,
        name=SUPERSEDE_TOOL,
        arguments={"id": "20260101-widget-cache", "superseded_by": "20260201-widget-cache-v2"},
    )

    assert not is_error
    text = _text(result)
    assert "20260101-widget-cache" in text
    assert "20260201-widget-cache-v2" in text

    load_result = load_store(store / "sessions")
    assert load_result.errors == []
    doc = {d.id: d for d in load_result.docs}["20260101-widget-cache"]
    assert doc.status == "superseded"
    assert doc.superseded_by == "20260201-widget-cache-v2"
    # everything else in the document is untouched
    assert "CacheWarmer must run before WidgetCache" in doc.body


def test_mark_superseded_refuses_on_nonexistent_document(store):
    result, is_error = _call(
        store,
        name=SUPERSEDE_TOOL,
        arguments={"id": "20260101-widget-cache", "superseded_by": "20260201-widget-cache-v2"},
    )

    assert is_error
    assert _sessions_files(store) == []


def test_mark_superseded_refuses_when_current_status_is_not_active(store):
    original = _write_raw(store, "20260101-widget-cache", DRAFT_CONTENT).read_text(
        encoding="utf-8"
    )

    result, is_error = _call(
        store,
        name=SUPERSEDE_TOOL,
        arguments={"id": "20260101-widget-cache", "superseded_by": "20260201-widget-cache-v2"},
    )

    assert is_error
    assert "draft" in _text(result)
    assert (store / "sessions" / "20260101-widget-cache.md").read_text(
        encoding="utf-8"
    ) == original


def test_mark_superseded_refuses_self_supersede(store):
    _write_raw(store, "20260101-widget-cache", ACTIVE_CONTENT)

    result, is_error = _call(
        store,
        name=SUPERSEDE_TOOL,
        arguments={"id": "20260101-widget-cache", "superseded_by": "20260101-widget-cache"},
    )

    assert is_error


def test_mark_superseded_rejects_bad_superseded_by_shape(store):
    original = _write_raw(store, "20260101-widget-cache", ACTIVE_CONTENT).read_text(
        encoding="utf-8"
    )

    result, is_error = _call(
        store,
        name=SUPERSEDE_TOOL,
        arguments={"id": "20260101-widget-cache", "superseded_by": "../../etc/passwd"},
    )

    assert is_error
    assert (store / "sessions" / "20260101-widget-cache.md").read_text(
        encoding="utf-8"
    ) == original


def test_mark_superseded_missing_superseded_by_argument_is_invalid_params(store):
    _write_raw(store, "20260101-widget-cache", ACTIVE_CONTENT)

    _, responses, _ = _run(
        store, _call_msg(1, name=SUPERSEDE_TOOL, arguments={"id": "20260101-widget-cache"})
    )

    assert responses[0]["error"]["code"] == -32602


# ---------------------------------------------------------------------------
# a document written by these tools is findable by engmem_search in the same
# session — the create-then-search half of the flywheel this task exists to fix
# ---------------------------------------------------------------------------


def test_a_completed_document_is_findable_by_search_in_the_same_session(store):
    create_msg = _call_msg(
        1, name=CREATE_TOOL, arguments={"id": "20260101-widget-cache", "content": DRAFT_CONTENT}
    )
    complete_msg = _call_msg(
        2,
        name=COMPLETE_TOOL,
        arguments={"id": "20260101-widget-cache", "content": ACTIVE_CONTENT},
    )
    search_msg = _call_msg(3, name="engmem_search", arguments={"query": "WidgetCache"})

    _, responses, _ = _run(store, create_msg, complete_msg, search_msg)

    assert not responses[0]["result"].get("isError")
    assert not responses[1]["result"].get("isError")
    search_text = responses[2]["result"]["content"][0]["text"]
    assert "20260101-widget-cache" in search_text


# ---------------------------------------------------------------------------
# tools/list — the three new tools are discoverable and correctly scoped
# ---------------------------------------------------------------------------


def test_tools_list_declares_all_three_write_tools(store):
    _, responses, _ = _run(store, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    names = {t["name"] for t in responses[0]["result"]["tools"]}
    assert {CREATE_TOOL, COMPLETE_TOOL, SUPERSEDE_TOOL} <= names


def test_tools_list_create_and_complete_require_id_and_content(store):
    _, responses, _ = _run(store, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    by_name = {t["name"]: t for t in responses[0]["result"]["tools"]}
    assert set(by_name[CREATE_TOOL]["inputSchema"]["required"]) == {"id", "content"}
    assert set(by_name[COMPLETE_TOOL]["inputSchema"]["required"]) == {"id", "content"}
    assert set(by_name[SUPERSEDE_TOOL]["inputSchema"]["required"]) == {"id", "superseded_by"}


# ---------------------------------------------------------------------------
# front-matter patching — `key:` that is not a key
# ---------------------------------------------------------------------------


def _write_doc(store: Path, doc_id: str, front_matter: str) -> Path:
    path = store / "sessions" / f"{doc_id}.md"
    path.write_text(
        f"---\n{front_matter}---\n\n## 8. Decision Log\n\nBody text.\n", encoding="utf-8"
    )
    return path


def test_supersede_ignores_a_key_indented_inside_a_block_scalar(store):
    """`status:` inside a `|` block scalar is prose, not a key. YAML requires that content
    to be indented, and the patcher anchors on column zero, so the real key is the one
    rewritten and the prose survives byte-for-byte."""
    path = _write_doc(
        store,
        "20260101-widget-cache",
        "id: 20260101-widget-cache\n"
        "title: Widget cache\n"
        "date: 2026-01-01\n"
        "status: active\n"
        "notes: |\n"
        "  status: this line is prose, not a key\n",
    )

    result, is_error = _call(
        store,
        name=SUPERSEDE_TOOL,
        arguments={"id": "20260101-widget-cache", "superseded_by": "20260201-widget-cache-v2"},
    )

    assert not is_error, _text(result)
    text = path.read_text(encoding="utf-8")
    assert "  status: this line is prose, not a key\n" in text
    doc = {d.id: d for d in load_store(store / "sessions").docs}["20260101-widget-cache"]
    assert doc.status == "superseded"


def test_supersede_refuses_when_a_quoted_value_puts_a_second_key_at_column_zero(store):
    """A multi-line double-quoted scalar can legally place `status:` at column zero. The
    patcher cannot tell that apart from a real key, so it refuses rather than guessing —
    an unwritten document beats a corrupted one."""
    path = _write_doc(
        store,
        "20260101-widget-cache",
        "id: 20260101-widget-cache\n"
        'title: "first line\n'
        'status: still inside the quoted title"\n'
        "date: 2026-01-01\n"
        "status: active\n",
    )
    before = path.read_text(encoding="utf-8")

    result, is_error = _call(
        store,
        name=SUPERSEDE_TOOL,
        arguments={"id": "20260101-widget-cache", "superseded_by": "20260201-widget-cache-v2"},
    )

    assert is_error
    assert "more than one" in _text(result)
    assert path.read_text(encoding="utf-8") == before, "a refusal must not touch the file"


def test_supersede_discards_a_patch_that_did_not_produce_the_expected_state(store, monkeypatch):
    """The staged write re-parses before committing. If patching ever rewrote the wrong
    line, the document on disk must be left exactly as it was."""
    path = _write_doc(
        store,
        "20260101-widget-cache",
        "id: 20260101-widget-cache\ntitle: Widget cache\ndate: 2026-01-01\nstatus: active\n",
    )
    before = path.read_text(encoding="utf-8")
    monkeypatch.setattr(
        mcp_server, "_patch_front_matter_line", lambda text, key, value: text
    )

    result, is_error = _call(
        store,
        name=SUPERSEDE_TOOL,
        arguments={"id": "20260101-widget-cache", "superseded_by": "20260201-widget-cache-v2"},
    )

    assert is_error
    assert path.read_text(encoding="utf-8") == before
    assert _sessions_files(store) == ["20260101-widget-cache.md"], "no temp file left behind"


def test_a_large_document_round_trips_and_leaves_no_staged_file(store):
    """There is no size cap on `content`, deliberately: the caller is the user's own agent
    on the user's own machine, and an arbitrary limit would reject a legitimately long
    session document. What must hold at any size is that the bytes arrive intact, the store
    still parses, and the staging file is never orphaned. Measured cost is roughly 15x the
    payload in peak memory, so a cap becomes worth discussing only in the hundreds of MB."""
    body = "x" * (2 * 1024 * 1024)
    content = (
        "---\nid: 20260101-widget-cache\ntitle: Widget cache\ndate: 2026-01-01\n"
        f"status: draft\n---\n\n## Pre-reg\n\n{body}\n"
    )

    result, is_error = _call(
        store,
        name=mcp_server.CREATE_DRAFT_TOOL_NAME,
        arguments={"id": "20260101-widget-cache", "content": content},
    )

    assert not is_error, _text(result)
    written = (store / "sessions" / "20260101-widget-cache.md").read_text(encoding="utf-8")
    assert written == content, "the document must arrive byte-for-byte"
    assert load_store(store / "sessions").errors == []
    assert _sessions_files(store) == ["20260101-widget-cache.md"], "no staged file left behind"
