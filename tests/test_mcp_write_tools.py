"""Contract tests for the MCP write tools, which give a shell-less runtime the save half of the
workflow."""

from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path

import pytest

from conftest import (
    ACTIVE_CONTENT,
    DRAFT_CONTENT,
    requires_permission_enforcement,
    requires_posix_modes,
    requires_symlinks,
)

from engmem import mcp_server
from engmem.mcp_server import serve
from engmem.spine import load_store, split_front_matter

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


@pytest.mark.parametrize(
    "name, arguments, missing_field",
    [
        (CREATE_TOOL, {"content": DRAFT_CONTENT}, "id"),
        (CREATE_TOOL, {"id": "20260101-widget-cache"}, "content"),
    ],
    ids=["create_missing_id", "create_missing_content"],
)
def test_create_draft_missing_required_argument_is_invalid_params(
    store, name, arguments, missing_field
):
    _, responses, _ = _run(store, _call_msg(1, name=name, arguments=arguments))

    assert responses[0]["error"]["code"] == -32602
    assert missing_field in responses[0]["error"]["message"]


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


def test_create_draft_reports_missing_store_without_dying(tmp_path):
    """Parallel to the read tools' `store not found` result: `sessions/` was never created, e.g. no
    `engmem install` ran yet."""
    result, is_error = _call(
        tmp_path, name=CREATE_TOOL, arguments={"id": "20260101-widget-cache", "content": DRAFT_CONTENT}
    )

    assert is_error
    assert "store not found" in _text(result)


def test_write_tool_reports_an_unresolvable_sessions_path_as_a_tool_error(store, monkeypatch):
    """`_resolve_sessions_dir`'s own `resolve()` calls can raise `OSError` (a symlink loop, a
    revoked mid-op permission) distinct from `sessions/` simply not existing."""
    real_resolve = Path.resolve

    def flaky_resolve(self, *args, **kwargs):
        if self.name == "sessions":
            raise OSError(62, "Too many levels of symbolic links")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", flaky_resolve)

    result, is_error = _call(
        store, name=CREATE_TOOL, arguments={"id": "20260101-widget-cache", "content": DRAFT_CONTENT}
    )

    assert is_error
    assert "could not resolve" in _text(result).lower()


def test_write_tool_refuses_when_the_resolved_target_escapes_sessions(store, monkeypatch):
    """Defense-in-depth: with `doc_id` already validated (no `/`, no `..`), `target.resolve()`
    escaping `sessions/` while not itself a symlink should not happen via any legal `doc_id` — this
    pins that the guard still fires if it ever does."""
    real_resolve = Path.resolve

    def flaky_resolve(self, *args, **kwargs):
        if self.name == "20260101-widget-cache.md":
            return Path("/tmp/escaped-20260101-widget-cache.md")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", flaky_resolve)

    result, is_error = _call(
        store, name=CREATE_TOOL, arguments={"id": "20260101-widget-cache", "content": DRAFT_CONTENT}
    )

    assert is_error
    assert "outside sessions" in _text(result).lower()


def test_create_draft_with_empty_id_is_invalid_params(store):
    """An empty `id` fails the schema check at protocol level, before `_ToolError` is even in
    play."""
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
    assert "content" in responses[0]["error"]["message"]


def test_complete_draft_refuses_when_existing_draft_does_not_parse(store):
    """A draft `_stage_content` never wrote — hand-edited or corrupted on disk — must be refused,
    not trusted enough to overwrite blindly."""
    _write_raw(store, "20260101-widget-cache", "---\nid: 20260101-widget-cache\nstatus: draft\n")

    result, is_error = _call(
        store, name=COMPLETE_TOOL, arguments={"id": "20260101-widget-cache", "content": ACTIVE_CONTENT}
    )

    assert is_error
    assert "does not parse" in _text(result)


def test_complete_draft_rejects_unparseable_new_content(store):
    """Counterpart to `test_create_draft_rejects_unparseable_content_no_traceback` for the
    draft-to-active leg: the existing draft is fine, the replacement content is not."""
    _write_raw(store, "20260101-widget-cache", DRAFT_CONTENT)
    broken = "---\nid: 20260101-widget-cache\nstatus: active\n"  # unclosed front matter

    result, is_error = _call(
        store, name=COMPLETE_TOOL, arguments={"id": "20260101-widget-cache", "content": broken}
    )

    assert is_error
    assert "does not parse" in _text(result)
    assert (store / "sessions" / "20260101-widget-cache.md").read_text(
        encoding="utf-8"
    ) == DRAFT_CONTENT


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
    assert "superseded_by" in responses[0]["error"]["message"]


def test_mark_superseded_refuses_when_existing_document_does_not_parse(store):
    _write_raw(store, "20260101-widget-cache", "---\nid: 20260101-widget-cache\nstatus: active\n")

    result, is_error = _call(
        store,
        name=SUPERSEDE_TOOL,
        arguments={"id": "20260101-widget-cache", "superseded_by": "20260201-widget-cache-v2"},
    )

    assert is_error
    assert "does not parse" in _text(result)


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
    """`status:` inside a `|` block scalar is prose; YAML requires it indented, and the patcher
    anchors on column zero."""
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


def test_supersede_patches_the_real_key_when_a_quoted_value_holds_a_lookalike(store):
    """A multi-line quoted scalar can legally put `status:` at column zero. The patcher asks the
    parser which pairs are top-level, so the one inside the title is not a second key."""
    path = _write_doc(
        store,
        "20260101-widget-cache",
        "id: 20260101-widget-cache\n"
        'title: "first line\n'
        'status: still inside the quoted title"\n'
        "date: 2026-01-01\n"
        "status: active\n",
    )

    result, is_error = _call(
        store,
        name=SUPERSEDE_TOOL,
        arguments={"id": "20260101-widget-cache", "superseded_by": "20260201-widget-cache-v2"},
    )

    assert not is_error, _text(result)
    after = path.read_text(encoding="utf-8")
    assert "status: still inside the quoted title" in after
    assert "\nstatus: superseded\n" in after
    assert load_store(store / "sessions").docs[0].title.startswith("first line")


def test_supersede_refuses_when_front_matter_has_a_duplicate_status_key(store):
    """Two `status:` lines mean `_patch_front_matter_line` cannot know which one the caller
    means — it refuses rather than guessing, and the document must be left byte-identical."""
    path = _write_doc(
        store,
        "20260101-widget-cache",
        "id: 20260101-widget-cache\ntitle: Widget cache\ndate: 2026-01-01\n"
        "status: draft\nstatus: active\n",
    )
    before = path.read_bytes()

    result, is_error = _call(
        store,
        name=SUPERSEDE_TOOL,
        arguments={"id": "20260101-widget-cache", "superseded_by": "20260201-widget-cache-v2"},
    )

    assert is_error
    assert "more than one" in _text(result)
    assert path.read_bytes() == before


def test_supersede_refuses_when_superseded_by_is_a_block_sequence(store):
    """A `superseded_by:` value spanning more than one line (here, a block sequence) cannot be
    rewritten on a single line — refused, not mangled."""
    path = _write_doc(
        store,
        "20260101-widget-cache",
        "id: 20260101-widget-cache\ntitle: Widget cache\ndate: 2026-01-01\n"
        "status: active\nsuperseded_by:\n  - old-value\n  - another\n",
    )
    before = path.read_bytes()

    result, is_error = _call(
        store,
        name=SUPERSEDE_TOOL,
        arguments={"id": "20260101-widget-cache", "superseded_by": "20260201-widget-cache-v2"},
    )

    assert is_error
    assert "spans more than one line" in _text(result)
    assert path.read_bytes() == before


def test_supersede_discards_a_patch_that_did_not_produce_the_expected_state(store, monkeypatch):
    """The staged write re-parses before committing, so a patch that hit the wrong line leaves the
    document untouched."""
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


def test_supersede_refuses_front_matter_whose_anchor_the_first_patch_would_drop(store):
    """`status: &st active` loses its anchor when the status line is rewritten, so the second
    patch composes text whose `*st` is undefined — a document problem the caller is told about,
    never a -32603 with raw parser text in it."""
    path = _write_doc(
        store,
        "20260101-widget-cache",
        "id: 20260101-widget-cache\ntitle: Widget cache\ndate: 2026-01-01\n"
        "status: &st active\nnote: *st\n",
    )
    before = path.read_bytes()

    _, responses, _ = _run(
        store,
        _call_msg(
            1,
            name=SUPERSEDE_TOOL,
            arguments={
                "id": "20260101-widget-cache",
                "superseded_by": "20260201-widget-cache-v2",
            },
        ),
    )

    assert "error" not in responses[0], responses[0]
    result = responses[0]["result"]
    assert result.get("isError")
    assert "anchor" in _text(result)
    assert path.read_bytes() == before
    assert _sessions_files(store) == ["20260101-widget-cache.md"], "no temp file left behind"


def test_supersede_refuses_a_document_that_changed_between_its_two_reads(store, monkeypatch):
    """The handler validates one read and rebuilds the file from another. A writer landing
    between them must not have its version replaced by the copy read before it."""
    path = _write_doc(
        store,
        "20260101-widget-cache",
        "id: 20260101-widget-cache\ntitle: Widget cache\ndate: 2026-01-01\nstatus: active\n",
    )
    newer = (
        "---\nid: 20260101-widget-cache\ntitle: Widget cache\ndate: 2026-01-01\n"
        "status: active\n---\n\n## 8. Decision Log\n\nBody rewritten by another writer.\n"
    )

    real_read_document = mcp_server.read_document

    def racing_read(target: Path):
        # the concurrent write lands after this handler has its text, exactly where a
        # staleness check that is not there cannot see it
        text, bom = real_read_document(target)
        target.write_text(newer, encoding="utf-8")
        return text, bom

    monkeypatch.setattr(mcp_server, "read_document", racing_read)

    result, is_error = _call(
        store,
        name=SUPERSEDE_TOOL,
        arguments={"id": "20260101-widget-cache", "superseded_by": "20260201-widget-cache-v2"},
    )

    assert is_error, _text(result)
    assert "changed on disk" in _text(result)
    assert path.read_text(encoding="utf-8") == newer, "the newer version must survive intact"
    assert _sessions_files(store) == ["20260101-widget-cache.md"], "no temp file left behind"


def test_supersede_refuses_a_document_that_stopped_being_utf8_between_its_two_reads(
    store, monkeypatch
):
    """The second read decodes as well as reads. A writer replacing the file with bytes that
    are not UTF-8 is a document problem the caller is told about, never a -32603."""
    path = _write_doc(
        store,
        "20260101-widget-cache",
        "id: 20260101-widget-cache\ntitle: Widget cache\ndate: 2026-01-01\nstatus: active\n",
    )
    corrupt = b"---\nid: 20260101-widget-cache\ntitle: \xff\xfe\n---\n\nBody.\n"

    real_parse_document = mcp_server.parse_document

    def racing_parse(target: Path):
        # the concurrent write lands after the validating parse, so the raw re-read below is
        # the call that meets the undecodable bytes
        doc = real_parse_document(target)
        target.write_bytes(corrupt)
        return doc

    monkeypatch.setattr(mcp_server, "parse_document", racing_parse)

    _, responses, _ = _run(
        store,
        _call_msg(
            1,
            name=SUPERSEDE_TOOL,
            arguments={
                "id": "20260101-widget-cache",
                "superseded_by": "20260201-widget-cache-v2",
            },
        ),
    )

    assert "error" not in responses[0], responses[0]
    result = responses[0]["result"]
    assert result.get("isError"), _text(result)
    assert "could not be re-read" in _text(result)
    assert path.read_bytes() == corrupt, "nothing may be written over the newer version"
    assert _sessions_files(store) == ["20260101-widget-cache.md"], "no temp file left behind"


def test_a_large_document_round_trips_and_leaves_no_staged_file(store):
    """No size cap is deliberate; at any size the bytes must arrive intact and the staging file
    must never be orphaned."""
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


@pytest.mark.parametrize(
    "arguments",
    [
        {"id": "20260101-widget-cache", "content": DRAFT_CONTENT + "\ud800"},
        {"id": "20260101-widget-cache\ud800", "content": DRAFT_CONTENT},
    ],
    ids=["in_the_content", "in_the_id"],
)
def test_create_draft_refuses_content_that_cannot_be_encoded_as_utf8(store, arguments):
    """`content.encode("utf-8")` raises on an unpaired surrogate. Refused at the envelope, so it
    never reaches the write path — and never as a -32603 blamed on the server."""
    _, responses, _ = _run(store, _call_msg(1, name=CREATE_TOOL, arguments=arguments))

    assert responses[0]["error"]["code"] == -32600
    assert _sessions_files(store) == [], "nothing written, and no staged file left behind"


@requires_posix_modes
def test_completing_a_draft_keeps_the_documents_file_mode(store):
    """The MCP write path replaces the file too, so it inherits the same rule `backfill` does."""
    _call(store, name="engmem_create_draft",
          arguments={"id": "20260101-widget-cache", "content": DRAFT_CONTENT})
    path = store / "sessions" / "20260101-widget-cache.md"
    os.chmod(path, 0o640)

    _call(store, name="engmem_complete_draft",
          arguments={"id": "20260101-widget-cache", "content": ACTIVE_CONTENT})

    assert stat.S_IMODE(os.stat(path).st_mode) == 0o640
    assert load_store(store / "sessions").docs[0].status == "active"


def test_a_documents_line_endings_survive_the_mcp_write(store):
    r"""Regression for the Windows CI leg — `write_text` translated them there. On POSIX
    `os.linesep` is `\n`, so this passes either way; `test_staging.py` pins the rest."""
    content = DRAFT_CONTENT.replace("\n", "\r\n")

    _call(store, name="engmem_create_draft",
          arguments={"id": "20260101-widget-cache", "content": content})

    written = (store / "sessions" / "20260101-widget-cache.md").read_bytes()
    assert written == content.encode("utf-8")


def _make_active(store: Path) -> None:
    _write_doc(
        store,
        "20260101-widget-cache",
        "id: 20260101-widget-cache\ntitle: Widget cache\ndate: 2026-01-01\nstatus: active\n",
    )


@pytest.mark.parametrize(
    "setup, name, arguments",
    [
        (lambda store: None, CREATE_TOOL,
         {"id": "20260101-widget-cache", "content": DRAFT_CONTENT}),
        (lambda store: _call(store, name=CREATE_TOOL,
                             arguments={"id": "20260101-widget-cache", "content": DRAFT_CONTENT}),
         COMPLETE_TOOL,
         {"id": "20260101-widget-cache", "content": ACTIVE_CONTENT}),
        (_make_active, SUPERSEDE_TOOL,
         {"id": "20260101-widget-cache", "superseded_by": "20260201-widget-cache-v2"}),
    ],
    ids=["create", "complete", "supersede"],
)
def test_a_failing_commit_still_discards_the_staged_file(store, monkeypatch, setup, name, arguments):
    """`commit` stats and chmods before it replaces, so it can raise where the bare `os.replace`
    it succeeded could not — and the staged file must go either way."""
    setup(store)

    def failing_commit(tmp_path, target):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(mcp_server, "commit", failing_commit)
    _, responses, _ = _run(store, _call_msg(1, name=name, arguments=arguments))

    # the code and the message together, so a renamed argument or tool cannot leave this green
    # while failing before anything is staged — where "no .tmp survived" is trivially true
    assert responses[0]["error"]["code"] == -32603
    assert "No space left" in responses[0]["error"]["message"]
    assert not [f for f in _sessions_files(store) if f.endswith(".tmp")]


@pytest.mark.parametrize("raw", [b"\xef\xbb\xbf", b""], ids=["bom", "no-bom"])
@pytest.mark.parametrize("nl", ["\r\n", "\r", "\n"], ids=["crlf", "cr", "lf"])
def test_mark_superseded_preserves_the_documents_bytes(store, raw, nl):
    """It rebuilds the whole file from what it read, so `read_text` dropped the BOM and
    rewrote every line ending — see contracts/backfill.md, "Line endings"."""
    sessions = store / "sessions"
    document = (
        f"---{nl}id: 20260101-widget-cache{nl}title: T{nl}date: 2026-01-01{nl}"
        f"task_date: 2026-01-01{nl}status: active{nl}tags: [x]{nl}entities: [E]{nl}"
        f"---{nl}{nl}## Pre-reg{nl}{nl}Body.{nl}"
    )
    path = sessions / "20260101-widget-cache.md"
    path.write_bytes(raw + document.encode("utf-8"))
    (sessions / "20260201-widget-cache-v2.md").write_text(
        ACTIVE_CONTENT.replace("20260101-widget-cache", "20260201-widget-cache-v2"),
        encoding="utf-8",
    )

    result, is_error = _call(store, name=SUPERSEDE_TOOL, arguments={
        "id": "20260101-widget-cache", "superseded_by": "20260201-widget-cache-v2",
    })

    assert not is_error, _text(result)
    after = path.read_bytes()
    assert after.startswith(raw)
    # every line ending is the document's own: `^` under `re.MULTILINE` anchors after `\n`
    # only, so a regex patcher matched nothing on `\r` and appended instead of replacing
    assert after.decode("utf-8").count(nl) == document.count(nl) + 1
    # the lines were *patched*, not appended: `$` never matches before a `\r`, so an anchored
    # pattern matched nothing on CRLF and the append branch wrote a second `status:` line
    front_matter, _ = split_front_matter(after[len(raw):].decode("utf-8"))
    assert [ln for ln in front_matter.splitlines() if ln.startswith("status:")] == [
        "status: superseded"
    ]
    assert len([ln for ln in front_matter.splitlines() if ln.startswith("superseded_by:")]) == 1
    reloaded = load_store(sessions)
    doc = next(d for d in reloaded.docs if d.id == "20260101-widget-cache")
    assert doc.status == "superseded"
    assert doc.superseded_by == "20260201-widget-cache-v2"


@requires_permission_enforcement
@pytest.mark.parametrize(
    "name, arguments",
    [
        (CREATE_TOOL, {"id": "20260101-widget-cache", "content": DRAFT_CONTENT}),
        (COMPLETE_TOOL, {"id": "20260101-widget-cache", "content": ACTIVE_CONTENT}),
        (SUPERSEDE_TOOL, {"id": "20260101-widget-cache",
                          "superseded_by": "20260201-widget-cache-v2"}),
    ],
    ids=["create", "complete", "supersede"],
)
def test_a_write_tool_reports_an_unreadable_store_as_a_tool_error(tmp_path, name, arguments):
    """`_resolve_sessions_dir` had the same bare `is_dir()`, so every write tool turned an
    unreadable store into a `-32603 internal error` — the channel for engmem's own bugs."""
    store = tmp_path / "store"
    (store / "sessions").mkdir(parents=True)

    os.chmod(store, 0o000)
    try:
        result, is_error = _call(store, name=name, arguments=arguments)
    finally:
        os.chmod(store, 0o755)

    assert is_error
    assert "store not readable" in _text(result)


@pytest.mark.parametrize(
    "front_matter",
    [
        "{id: doc-a, title: T, date: 2026-01-01, task_date: 2026-01-01, "
        "status: active, tags: [x], entities: [E]}",
        # a root flow mapping can put a key at column 0, so the column test alone missed it
        "{\nid: doc-a,\ntitle: T,\ndate: 2026-01-01,\ntask_date: 2026-01-01,\n"
        "status: active,\ntags: [x],\nentities: [E]\n}",
        "  id: doc-a\n  title: T\n  date: 2026-01-01\n  task_date: 2026-01-01\n"
        "  status: active\n  tags: [x]\n  entities: [E]",
    ],
    ids=["flow-one-line", "flow-multi-line", "indented"],
)
def test_supersede_refuses_a_key_that_is_not_a_line_of_its_own(store, front_matter):
    """The replacement is written at column 0 alone on its line. `flow-multi-line` pins the
    flow guard and `indented` pins the column guard; `flow-one-line` is refused by either and
    is here to document the shape."""
    sessions = store / "sessions"
    path = sessions / "doc-a.md"
    path.write_text(f"---\n{front_matter}\n---\n\n# Title\n\nbody\n", encoding="utf-8")
    before = path.read_bytes()

    _, responses, _ = _run(store, _call_msg(1, name=SUPERSEDE_TOOL, arguments={
        "id": "doc-a", "superseded_by": "20260201-widget-cache-v2",
    }))

    # a regression escapes as a protocol fault, so name that rather than showing a KeyError
    assert "error" not in responses[0], responses[0]
    result = responses[0]["result"]
    assert result.get("isError") is True
    assert "not a line of its own" in result["content"][0]["text"]
    assert path.read_bytes() == before
