"""Contract tests for the MCP write tools, which give a shell-less runtime the save half of the
workflow."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from support import fs, rpc
from support.docs import ACTIVE_CONTENT, DRAFT_CONTENT, replaced
from support.fs import entry_names
from support.platform import (
    requires_permission_enforcement,
    requires_posix_modes,
    requires_symlinks,
)

from engmem import drafts, mcp_server
from engmem.spine import load_store, split_front_matter
from engmem.staging import version_of

CREATE_TOOL = mcp_server.CREATE_DRAFT_TOOL_NAME
COMPLETE_TOOL = mcp_server.COMPLETE_DRAFT_TOOL_NAME
SUPERSEDE_TOOL = mcp_server.MARK_SUPERSEDED_TOOL_NAME


@pytest.fixture
def store(tmp_path) -> Path:
    (tmp_path / "sessions").mkdir()
    return tmp_path


def _sessions_snapshot(store: Path) -> dict[str, str]:
    """Name -> content for everything under `sessions/`: what a refused write must leave
    untouched, whether or not the document it names exists yet."""
    return {p.name: p.read_text(encoding="utf-8") for p in (store / "sessions").iterdir()}


def _write_raw(store: Path, doc_id: str, content: str) -> Path:
    path = store / "sessions" / f"{doc_id}.md"
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# engmem_create_draft
# ---------------------------------------------------------------------------


def test_create_draft_writes_a_document_load_store_parses_cleanly(store):
    result, is_error = rpc.call(
        store, name=CREATE_TOOL, arguments={"id": "20260101-widget-cache", "content": DRAFT_CONTENT}
    )

    assert not is_error
    assert "20260101-widget-cache" in rpc.text(result)

    written = (store / "sessions" / "20260101-widget-cache.md").read_text(encoding="utf-8")
    assert written == DRAFT_CONTENT

    load_result = load_store(store / "sessions")
    assert load_result.errors == []
    doc = {d.id: d for d in load_result.docs}["20260101-widget-cache"]
    assert doc.status == "draft"


def test_create_draft_success_text_carries_the_id_into_the_next_search(store):
    """The draft id is produced here and needed on the next search call; without the directive
    nothing carries it across, and every search of the task is logged unattributed."""
    result, is_error = rpc.call(
        store, name=CREATE_TOOL, arguments={"id": "20260101-widget-cache", "content": DRAFT_CONTENT}
    )
    text = rpc.text(result)

    assert not is_error
    assert "session_id" in text, "the id alone does not say what to do with it"
    assert mcp_server.TOOL_NAME in text and mcp_server.ROLE_TOOL_NAME in text, (
        "the directive must name the tools the id is passed to"
    )
    assert text.count("20260101-widget-cache") >= 2, (
        "the value to pass must appear beside the directive, not only in the written path"
    )


def test_create_draft_refuses_to_overwrite_an_existing_file(store):
    existing = store / "sessions" / "20260101-widget-cache.md"
    existing.write_text("not touched", encoding="utf-8")

    result, is_error = rpc.call(
        store, name=CREATE_TOOL, arguments={"id": "20260101-widget-cache", "content": DRAFT_CONTENT}
    )

    assert is_error
    assert "already exists" in rpc.text(result)
    assert existing.read_text(encoding="utf-8") == "not touched"


def test_create_draft_rejects_status_other_than_draft(store):
    result, is_error = rpc.call(
        store, name=CREATE_TOOL, arguments={"id": "20260101-widget-cache", "content": ACTIVE_CONTENT}
    )

    assert is_error
    assert "draft" in rpc.text(result)
    assert entry_names(store / "sessions") == []


# --- the two content-writing tools share the same front-matter checks; each row differs only
# in which leg of the draft -> active transition is being asked for.

_MISMATCHED_ACTIVE = replaced(
    ACTIVE_CONTENT, "id: 20260101-widget-cache", "id: 20260101-something-else"
)


@pytest.mark.parametrize(
    "tool, existing, doc_id, content",
    [
        pytest.param(CREATE_TOOL, None, "20260101-something-else", DRAFT_CONTENT, id="create"),
        pytest.param(
            COMPLETE_TOOL,
            DRAFT_CONTENT,
            "20260101-widget-cache",
            _MISMATCHED_ACTIVE,
            id="complete",
        ),
    ],
)
def test_a_write_tool_rejects_an_id_that_does_not_match_the_front_matter(
    store, tool, existing, doc_id, content
):
    if existing is not None:
        _write_raw(store, "20260101-widget-cache", existing)
    before = _sessions_snapshot(store)

    result, is_error = rpc.call(
        store, name=tool, arguments=rpc.versioned(store, {"id": doc_id, "content": content})
    )

    assert is_error
    assert "20260101-something-else" in rpc.text(result)
    assert "20260101-widget-cache" in rpc.text(result)
    assert _sessions_snapshot(store) == before


@pytest.mark.parametrize(
    "tool, existing, status",
    [
        pytest.param(CREATE_TOOL, None, "draft", id="create"),
        pytest.param(COMPLETE_TOOL, DRAFT_CONTENT, "active", id="complete"),
    ],
)
def test_a_write_tool_rejects_unparseable_content_without_a_traceback(
    store, tool, existing, status
):
    if existing is not None:
        _write_raw(store, "20260101-widget-cache", existing)
    before = _sessions_snapshot(store)
    broken = f"---\nid: 20260101-widget-cache\nstatus: {status}\n"  # unclosed front matter

    result, is_error = rpc.call(
        store,
        name=tool,
        arguments=rpc.versioned(
            store, {"id": "20260101-widget-cache", "content": broken}
        )
    )

    assert is_error
    assert "does not parse" in rpc.text(result)
    assert "Traceback" not in rpc.text(result)
    assert _sessions_snapshot(store) == before


@pytest.mark.parametrize(
    "name, arguments, missing_field",
    [
        pytest.param(CREATE_TOOL, {"content": DRAFT_CONTENT}, "id", id="create_missing_id"),
        pytest.param(
            CREATE_TOOL,
            {"id": "20260101-widget-cache"},
            "content",
            id="create_missing_content",
        ),
        pytest.param(
            COMPLETE_TOOL,
            {"id": "20260101-widget-cache"},
            "content",
            id="complete_missing_content",
        ),
        pytest.param(
            SUPERSEDE_TOOL,
            {"id": "20260101-widget-cache"},
            "superseded_by",
            id="supersede_missing_superseded_by",
        ),
    ],
)
def test_a_write_tool_missing_a_required_argument_is_invalid_params(
    store, name, arguments, missing_field
):
    """The schema check runs in `_handle_tools_call` before the handler reads anything, so no
    document has to exist on disk for any of these."""
    _, responses, _ = rpc.run(store, rpc.tools_call_msg(1, name=name, arguments=arguments))

    assert responses[0]["error"]["code"] == rpc.INVALID_PARAMS
    assert missing_field in responses[0]["error"]["message"]


# ---------------------------------------------------------------------------
# escape vectors — shared path-containment logic under `engmem_create_draft`,
# which every write tool routes through
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    [
        pytest.param("../../etc/passwd", id="dotdot-traversal"),
        pytest.param("/etc/passwd", id="absolute-path"),
        pytest.param("..", id="bare-dotdot"),
        pytest.param(".", id="bare-dot"),
        pytest.param(".hidden-doc", id="leading-dot"),
        pytest.param("widget\x00cache-job", id="embedded-nul"),
        pytest.param("widget\\cache-job", id="backslash-separator"),
    ],
)
def test_create_draft_rejects_every_escape_vector(store, bad_id):
    result, is_error = rpc.call(
        store, name=CREATE_TOOL, arguments={"id": bad_id, "content": DRAFT_CONTENT}
    )

    assert is_error
    assert "Traceback" not in rpc.text(result)
    # nothing was created anywhere in or under the store — not even the temp
    # staging file `_stage_content` writes before validation
    assert set(p.name for p in store.iterdir()) == {"sessions"}
    assert entry_names(store / "sessions") == []


def test_create_draft_reports_missing_store_without_dying(tmp_path):
    """Parallel to the read tools' `store not found` result: `sessions/` was never created, e.g. no
    `engmem install` ran yet."""
    result, is_error = rpc.call(
        tmp_path, name=CREATE_TOOL, arguments={"id": "20260101-widget-cache", "content": DRAFT_CONTENT}
    )

    assert is_error
    assert "store not found" in rpc.text(result)


def test_write_tool_reports_an_unresolvable_sessions_path_as_a_tool_error(store, monkeypatch):
    """`_resolve_sessions_dir`'s own `resolve()` calls can raise `OSError` (a symlink loop, a
    revoked mid-op permission) distinct from `sessions/` simply not existing."""
    real_resolve = Path.resolve

    def flaky_resolve(self, *args, **kwargs):
        if self.name == "sessions":
            raise OSError(62, "Too many levels of symbolic links")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", flaky_resolve)

    result, is_error = rpc.call(
        store, name=CREATE_TOOL, arguments={"id": "20260101-widget-cache", "content": DRAFT_CONTENT}
    )

    assert is_error
    assert "could not resolve" in rpc.text(result).lower()


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

    result, is_error = rpc.call(
        store, name=CREATE_TOOL, arguments={"id": "20260101-widget-cache", "content": DRAFT_CONTENT}
    )

    assert is_error
    assert "outside sessions" in rpc.text(result).lower()


def test_create_draft_with_empty_id_is_invalid_params(store):
    """An empty `id` fails the schema check at protocol level, before `_ToolError` is even in
    play."""
    _, responses, _ = rpc.run(
        store, rpc.tools_call_msg(1, name=CREATE_TOOL, arguments={"id": "", "content": DRAFT_CONTENT})
    )

    assert responses[0]["error"]["code"] == rpc.INVALID_PARAMS
    assert set(p.name for p in store.iterdir()) == {"sessions"}
    assert entry_names(store / "sessions") == []


@requires_symlinks
def test_create_draft_rejects_a_sessions_dir_that_is_a_symlink_out_of_the_store(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    (store_dir / "sessions").symlink_to(outside, target_is_directory=True)

    result, is_error = rpc.call(
        store_dir,
        name=CREATE_TOOL,
        arguments={"id": "20260101-widget-cache", "content": DRAFT_CONTENT},
    )

    assert is_error
    assert list(outside.iterdir()) == [], "must never write through the escaping symlink"


# ---------------------------------------------------------------------------
# engmem_complete_draft
# ---------------------------------------------------------------------------


def test_complete_draft_transitions_status_from_draft_to_active(store):
    _write_raw(store, "20260101-widget-cache", DRAFT_CONTENT)

    result, is_error = rpc.call(
        store,
        name=COMPLETE_TOOL,
        arguments=rpc.versioned(store, {"id": "20260101-widget-cache", "content": ACTIVE_CONTENT}),
    )

    assert not is_error
    text = rpc.text(result)
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


# a sentence that really is in the cited document, so only the defect under test is left
_QUOTE = '"Chose CacheWarmer over a lazy cache fill"'


def _with_reuse_rows(*rows: str) -> str:
    return replaced(
        ACTIVE_CONTENT,
        "## Reuse Log\n\nPrior docs used: none.\n\n## Search Trace",
        "## Reuse Log\n\n"
        "| prior-doc | taken | impact | classification |\n"
        "|---|---|---|---|\n" + "".join(f"{row}\n" for row in rows) + "\n"
        "## Search Trace",
    )


def _write_cited_doc(store: Path) -> None:
    _write_raw(
        store,
        "20260101-other-doc",
        replaced(ACTIVE_CONTENT, "id: 20260101-widget-cache", "id: 20260101-other-doc"),
    )


@pytest.mark.parametrize(
    "cited, quote",
    [
        pytest.param("some-prior-doc.md", "a quote that cannot be checked", id="no_such_document"),
        pytest.param(
            "20260101-other-doc.md",
            "Chose CacheWarmer over a lazy cache fill",
            id="md_suffix_on_a_real_id",
        ),
    ],
)
def test_complete_draft_warns_but_still_succeeds_on_a_cited_id_not_in_the_store(
    store, cited, quote
):
    """A document's id is its front-matter `id`, else the filename stem — never the filename, so
    a `.md` suffix never resolves and the warning must still name the row."""
    _write_cited_doc(store)
    _write_raw(store, "20260101-widget-cache", DRAFT_CONTENT)
    content = _with_reuse_rows(f'| {cited} | "{quote}" | helped | reuse |')

    result, is_error = rpc.call(
        store,
        name=COMPLETE_TOOL,
        arguments=rpc.versioned(
            store, {"id": "20260101-widget-cache", "content": content}
        )
    )

    assert not is_error
    text = rpc.text(result)
    assert "draft" in text and "active" in text
    assert "warning" in text.lower()
    assert cited in text
    assert "not in the store" in text
    assert "20260101-widget-cache.md:" in text
    assert "prior-doc takes the document's id" in text
    assert "uv run --project <engmem-checkout> python <engmem-checkout>/tools/verify_citations.py --store" in text
    written = (store / "sessions" / "20260101-widget-cache.md").read_text(encoding="utf-8")
    assert written == content


def test_complete_draft_has_no_warning_when_the_citation_verifies(store):
    _write_cited_doc(store)
    _write_raw(store, "20260101-widget-cache", DRAFT_CONTENT)
    content = _with_reuse_rows(f"| 20260101-other-doc | {_QUOTE} | helped | reuse |")

    result, is_error = rpc.call(
        store,
        name=COMPLETE_TOOL,
        arguments=rpc.versioned(
            store, {"id": "20260101-widget-cache", "content": content}
        )
    )

    assert not is_error
    text = rpc.text(result)
    assert "draft" in text and "active" in text
    assert "warning" not in text.lower()


def _refused(store: Path, content: str) -> str:
    """Runs `engmem_complete_draft` and asserts the whole store survived the refusal intact."""
    before = _sessions_snapshot(store)
    result, is_error = rpc.call(
        store,
        name=COMPLETE_TOOL,
        arguments=rpc.versioned(
            store, {"id": "20260101-widget-cache", "content": content}
        )
    )
    assert is_error
    assert _sessions_snapshot(store) == before
    text = rpc.text(result)
    assert "draft -> active" not in text
    return text


@pytest.mark.parametrize(
    "row, expected_rejection",
    [
        pytest.param(
            "| 20260101-other-doc | used it without a direct quote | helped | reuse |",
            "no quoted span of four or more characters",
            id="no_quote",
        ),
        pytest.param(
            f"| 20260101-other-doc | {_QUOTE} | helped | helpful |",
            'classification "helpful" is not one of reuse / anti-reuse / harmful',
            id="unrecognized_classification",
        ),
        pytest.param(
            f"| 20260101-other-doc | {_QUOTE} | helped |",
            "no classification cell, or an empty one (expected one of "
            "reuse / anti-reuse / harmful)",
            id="no_classification_cell",
        ),
    ],
)
def test_complete_draft_refuses_a_bad_reuse_log_row_and_echoes_it(store, row, expected_rejection):
    _write_cited_doc(store)
    _write_raw(store, "20260101-widget-cache", DRAFT_CONTENT)

    text = _refused(store, _with_reuse_rows(row))

    assert f"Reuse Log row rejected — {expected_rejection}: {row}" in text


def test_complete_draft_reports_every_defect_of_every_bad_row(store):
    _write_cited_doc(store)
    _write_raw(store, "20260101-widget-cache", DRAFT_CONTENT)
    good = f"| 20260101-other-doc | {_QUOTE} | helped | reuse |"
    unquoted = "| 20260101-other-doc | paraphrased from memory | helped | Reused |"
    misspelt = '| 20260101-other-doc | "CacheWarmer must run before WidgetCache" | hurt | harmfull |'

    text = _refused(store, _with_reuse_rows(good, unquoted, misspelt))

    assert text.count("Reuse Log row rejected") == 3
    assert f"no quoted span of four or more characters: {unquoted}" in text
    assert f'classification "Reused" is not one of reuse / anti-reuse / harmful: {unquoted}' in text
    assert f'classification "harmfull" is not one of reuse / anti-reuse / harmful: {misspelt}' in text
    assert good not in text


def test_complete_draft_refuses_a_bad_row_before_any_store_scan(store, monkeypatch):
    """The row check is decidable from the content alone, so it must not depend on a store the
    citation check may not even be able to read."""
    _write_raw(store, "20260101-widget-cache", DRAFT_CONTENT)

    def _boom(_store):
        raise OSError("sessions/ unreadable")

    monkeypatch.setattr(drafts.gate1, "evaluate", _boom)
    row = "| some-prior-doc | used it without a direct quote | helped | reuse |"

    text = _refused(store, _with_reuse_rows(row))

    assert f"Reuse Log row rejected — no quoted span of four or more characters: {row}" in text
    assert "citation check did not run" not in text


@pytest.mark.parametrize(
    "quoted",
    [
        pytest.param('"Chose CacheWarmer over a lazy cache fill"', id="straight"),
        pytest.param("“Chose CacheWarmer over a lazy cache fill”", id="curly"),
        pytest.param("«Chose CacheWarmer over a lazy cache fill»", id="guillemets"),
    ],
)
def test_complete_draft_accepts_each_quote_style_the_count_accepts(store, quoted):
    _write_cited_doc(store)
    _write_raw(store, "20260101-widget-cache", DRAFT_CONTENT)
    row = f"| 20260101-other-doc | {quoted} | helped | anti-reuse |"

    result, is_error = rpc.call(
        store,
        name=COMPLETE_TOOL,
        arguments=rpc.versioned(
            store, {"id": "20260101-widget-cache", "content": _with_reuse_rows(row)}
        ),
    )

    assert not is_error
    text = rpc.text(result)
    assert "rejected" not in text
    assert "warning" not in text.lower()


def test_complete_draft_warns_on_a_quote_absent_from_the_cited_document(store):
    _write_cited_doc(store)
    _write_raw(store, "20260101-widget-cache", DRAFT_CONTENT)
    content = _with_reuse_rows(
        '| 20260101-other-doc | "this exact sentence is not in the cited document" '
        "| helped | reuse |"
    )

    result, is_error = rpc.call(
        store,
        name=COMPLETE_TOOL,
        arguments=rpc.versioned(
            store, {"id": "20260101-widget-cache", "content": content}
        )
    )

    assert not is_error
    text = rpc.text(result)
    assert "quote not found in" in text
    assert "this exact sentence is not in the cited document" in text


def test_complete_draft_only_reports_its_own_documents_citation_problems(store):
    other_content = _with_reuse_rows(
        '| leaked-sentinel-doc.md | "a quote that cannot be checked" | helped | reuse |'
    ).replace("id: 20260101-widget-cache", "id: 20260101-other-doc")
    _write_raw(store, "20260101-other-doc", other_content)
    _write_raw(store, "20260101-widget-cache", DRAFT_CONTENT)

    result, is_error = rpc.call(
        store,
        name=COMPLETE_TOOL,
        arguments=rpc.versioned(store, {"id": "20260101-widget-cache", "content": ACTIVE_CONTENT}),
    )

    assert not is_error
    text = rpc.text(result)
    assert "leaked-sentinel-doc.md" not in text
    assert "warning" not in text.lower()


def test_complete_draft_degrades_to_a_note_when_the_citation_check_itself_raises(
    store, monkeypatch
):
    _write_raw(store, "20260101-widget-cache", DRAFT_CONTENT)

    def _boom(_store):
        raise OSError("disk gremlin")

    monkeypatch.setattr(drafts.gate1, "evaluate", _boom)

    result, is_error = rpc.call(
        store,
        name=COMPLETE_TOOL,
        arguments=rpc.versioned(store, {"id": "20260101-widget-cache", "content": ACTIVE_CONTENT}),
    )

    assert not is_error
    text = rpc.text(result)
    assert "draft" in text and "active" in text
    assert "note: citation check did not run" in text
    written = (store / "sessions" / "20260101-widget-cache.md").read_text(encoding="utf-8")
    assert written == ACTIVE_CONTENT


def test_complete_draft_warns_about_a_missing_reuse_log_and_skips_the_citation_check(
    store, monkeypatch
):
    """Nothing to check means nothing to scan the store for, so the warning stands alone — the
    patched `evaluate` would report itself if it ran."""
    _write_raw(store, "20260101-widget-cache", DRAFT_CONTENT)

    def _boom(_store):
        raise OSError("sessions/ unreadable")

    monkeypatch.setattr(drafts.gate1, "evaluate", _boom)
    content = replaced(ACTIVE_CONTENT, "## Reuse Log\n\nPrior docs used: none.\n\n", "")

    result, is_error = rpc.call(
        store,
        name=COMPLETE_TOOL,
        arguments=rpc.versioned(
            store, {"id": "20260101-widget-cache", "content": content}
        )
    )

    assert not is_error
    text = rpc.text(result)
    assert "no Reuse Log section" in text
    assert "it is now active" in text
    assert "active documents missing a Reuse Log section" in text
    assert "citation check did not run" not in text


def test_complete_draft_refuses_when_no_draft_exists(store):
    result, is_error = rpc.call(
        store,
        name=COMPLETE_TOOL,
        arguments=rpc.versioned(store, {"id": "20260101-widget-cache", "content": ACTIVE_CONTENT}),
    )

    assert is_error
    assert "no draft" in rpc.text(result).lower() or "does not exist" in rpc.text(result).lower()
    assert entry_names(store / "sessions") == []


def test_complete_draft_refuses_to_overwrite_a_non_draft_document(store):
    original = _write_raw(store, "20260101-widget-cache", ACTIVE_CONTENT).read_text(
        encoding="utf-8"
    )

    result, is_error = rpc.call(
        store,
        name=COMPLETE_TOOL,
        arguments=rpc.versioned(store, {"id": "20260101-widget-cache", "content": ACTIVE_CONTENT}),
    )

    assert is_error
    assert "active" in rpc.text(result)
    assert (store / "sessions" / "20260101-widget-cache.md").read_text(
        encoding="utf-8"
    ) == original


def test_complete_draft_rejects_new_content_that_does_not_move_to_active(store):
    original = _write_raw(store, "20260101-widget-cache", DRAFT_CONTENT).read_text(
        encoding="utf-8"
    )

    result, is_error = rpc.call(
        store,
        name=COMPLETE_TOOL,
        arguments=rpc.versioned(store, {"id": "20260101-widget-cache", "content": DRAFT_CONTENT}),
    )

    assert is_error
    assert "active" in rpc.text(result)
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

    result, is_error = rpc.call(
        store_dir,
        name=COMPLETE_TOOL,
        arguments=rpc.versioned(store_dir, {"id": "20260101-widget-cache", "content": ACTIVE_CONTENT}),
    )

    assert is_error
    assert "symlink" in rpc.text(result).lower()
    assert outside_target.read_text(encoding="utf-8") == "not engmem's to touch"


@pytest.mark.parametrize(
    "tool, arguments",
    [
        pytest.param(
            COMPLETE_TOOL,
            {"id": "20260101-widget-cache", "content": ACTIVE_CONTENT},
            id="complete",
        ),
        pytest.param(
            SUPERSEDE_TOOL,
            {"id": "20260101-widget-cache", "superseded_by": "20260201-widget-cache-v2"},
            id="supersede",
        ),
    ],
)
def test_a_write_tool_refuses_a_document_on_disk_that_does_not_parse(store, tool, arguments):
    """A document `_stage_content` never wrote — hand-edited or corrupted on disk — must be
    refused, not trusted enough to modify blindly."""
    unparseable = "---\nid: 20260101-widget-cache\nstatus: active\n"  # unclosed front matter
    _write_raw(store, "20260101-widget-cache", unparseable)

    result, is_error = rpc.call(store, name=tool, arguments=arguments)

    assert is_error
    assert "does not parse" in rpc.text(result)
    assert (store / "sessions" / "20260101-widget-cache.md").read_text(
        encoding="utf-8"
    ) == unparseable


# ---------------------------------------------------------------------------
# engmem_mark_superseded
# ---------------------------------------------------------------------------


def test_mark_superseded_sets_status_and_superseded_by(store):
    _write_raw(store, "20260101-widget-cache", ACTIVE_CONTENT)

    result, is_error = rpc.call(
        store,
        name=SUPERSEDE_TOOL,
        arguments=rpc.versioned(
            store, {"id": "20260101-widget-cache", "superseded_by": "20260201-widget-cache-v2"}
        ),
    )

    assert not is_error
    text = rpc.text(result)
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
    result, is_error = rpc.call(
        store,
        name=SUPERSEDE_TOOL,
        arguments=rpc.versioned(
            store, {"id": "20260101-widget-cache", "superseded_by": "20260201-widget-cache-v2"}
        ),
    )

    assert is_error
    assert entry_names(store / "sessions") == []


def test_mark_superseded_refuses_when_current_status_is_not_active(store):
    original = _write_raw(store, "20260101-widget-cache", DRAFT_CONTENT).read_text(
        encoding="utf-8"
    )

    result, is_error = rpc.call(
        store,
        name=SUPERSEDE_TOOL,
        arguments=rpc.versioned(
            store, {"id": "20260101-widget-cache", "superseded_by": "20260201-widget-cache-v2"}
        ),
    )

    assert is_error
    assert "draft" in rpc.text(result)
    assert (store / "sessions" / "20260101-widget-cache.md").read_text(
        encoding="utf-8"
    ) == original


def test_mark_superseded_refuses_self_supersede(store):
    _write_raw(store, "20260101-widget-cache", ACTIVE_CONTENT)

    result, is_error = rpc.call(
        store,
        name=SUPERSEDE_TOOL,
        arguments=rpc.versioned(
            store, {"id": "20260101-widget-cache", "superseded_by": "20260101-widget-cache"}
        ),
    )

    assert is_error


def test_mark_superseded_rejects_bad_superseded_by_shape(store):
    original = _write_raw(store, "20260101-widget-cache", ACTIVE_CONTENT).read_text(
        encoding="utf-8"
    )

    result, is_error = rpc.call(
        store,
        name=SUPERSEDE_TOOL,
        arguments=rpc.versioned(
            store, {"id": "20260101-widget-cache", "superseded_by": "../../etc/passwd"}
        ),
    )

    assert is_error
    assert (store / "sessions" / "20260101-widget-cache.md").read_text(
        encoding="utf-8"
    ) == original


# ---------------------------------------------------------------------------
# a document written by these tools is findable by engmem_search in the same
# session — the create-then-search half of the workflow
# ---------------------------------------------------------------------------


def test_a_completed_document_is_findable_by_search_in_the_same_session(store):
    create_msg = rpc.tools_call_msg(
        1, name=CREATE_TOOL, arguments={"id": "20260101-widget-cache", "content": DRAFT_CONTENT}
    )
    complete_msg = rpc.tools_call_msg(
        2,
        name=COMPLETE_TOOL,
        arguments={
            "id": "20260101-widget-cache",
            "content": ACTIVE_CONTENT,
            # the draft the create above writes is DRAFT_CONTENT byte for byte
            "expected_version": version_of(DRAFT_CONTENT.encode("utf-8")),
        },
    )
    search_msg = rpc.tools_call_msg(3, name="engmem_search", arguments={"query": "WidgetCache"})

    _, responses, _ = rpc.run(store, create_msg, complete_msg, search_msg)

    assert not responses[0]["result"].get("isError")
    assert not responses[1]["result"].get("isError")
    search_text = responses[2]["result"]["content"][0]["text"]
    assert "20260101-widget-cache" in search_text


# ---------------------------------------------------------------------------
# tools/list — the write tools are correctly scoped (the inventory itself is
# pinned by test_mcp_server.py's closed five-tool list)
# ---------------------------------------------------------------------------


def test_tools_list_write_tool_schemas_require_the_arguments_each_handler_reads(store):
    _, responses, _ = rpc.run(store, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    by_name = {t["name"]: t for t in responses[0]["result"]["tools"]}
    assert set(by_name[CREATE_TOOL]["inputSchema"]["required"]) == {"id", "content"}
    assert set(by_name[COMPLETE_TOOL]["inputSchema"]["required"]) == {
        "id", "content", "expected_version"
    }
    assert set(by_name[SUPERSEDE_TOOL]["inputSchema"]["required"]) == {
        "id", "superseded_by", "expected_version"
    }


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

    result, is_error = rpc.call(
        store,
        name=SUPERSEDE_TOOL,
        arguments=rpc.versioned(
            store, {"id": "20260101-widget-cache", "superseded_by": "20260201-widget-cache-v2"}
        ),
    )

    assert not is_error, rpc.text(result)
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

    result, is_error = rpc.call(
        store,
        name=SUPERSEDE_TOOL,
        arguments=rpc.versioned(
            store, {"id": "20260101-widget-cache", "superseded_by": "20260201-widget-cache-v2"}
        ),
    )

    assert not is_error, rpc.text(result)
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

    result, is_error = rpc.call(
        store,
        name=SUPERSEDE_TOOL,
        arguments=rpc.versioned(
            store, {"id": "20260101-widget-cache", "superseded_by": "20260201-widget-cache-v2"}
        ),
    )

    assert is_error
    assert "more than one" in rpc.text(result)
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

    result, is_error = rpc.call(
        store,
        name=SUPERSEDE_TOOL,
        arguments=rpc.versioned(
            store, {"id": "20260101-widget-cache", "superseded_by": "20260201-widget-cache-v2"}
        ),
    )

    assert is_error
    assert "spans more than one line" in rpc.text(result)
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

    result, is_error = rpc.call(
        store,
        name=SUPERSEDE_TOOL,
        arguments=rpc.versioned(
            store, {"id": "20260101-widget-cache", "superseded_by": "20260201-widget-cache-v2"}
        ),
    )

    assert is_error
    assert path.read_text(encoding="utf-8") == before
    assert entry_names(store / "sessions") == ["20260101-widget-cache.md"], "no temp file left behind"


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

    _, responses, _ = rpc.run(
        store,
        rpc.tools_call_msg(
            1,
            name=SUPERSEDE_TOOL,
            arguments=rpc.versioned(store, {
                "id": "20260101-widget-cache",
                "superseded_by": "20260201-widget-cache-v2",
            }),
        ),
    )

    assert "error" not in responses[0], responses[0]
    result = responses[0]["result"]
    assert result.get("isError")
    assert "anchor" in rpc.text(result)
    assert path.read_bytes() == before
    assert entry_names(store / "sessions") == ["20260101-widget-cache.md"], "no temp file left behind"


def test_supersede_refuses_a_document_that_changed_after_the_bytes_it_rebuilds(
    store, monkeypatch
):
    """The handler rebuilds the file from the bytes its version check read. A writer landing
    after that read must not have its version replaced by the copy read before it."""
    path = _write_doc(
        store,
        "20260101-widget-cache",
        "id: 20260101-widget-cache\ntitle: Widget cache\ndate: 2026-01-01\nstatus: active\n",
    )
    newer = (
        "---\nid: 20260101-widget-cache\ntitle: Widget cache\ndate: 2026-01-01\n"
        "status: active\n---\n\n## 8. Decision Log\n\nBody rewritten by another writer.\n"
    )

    real_decode_document = mcp_server.decode_document

    def racing_decode(data: bytes):
        # the concurrent write lands after this handler has its bytes, exactly where a
        # staleness check that is not there cannot see it
        path.write_text(newer, encoding="utf-8")
        return real_decode_document(data)

    monkeypatch.setattr(mcp_server, "decode_document", racing_decode)

    result, is_error = rpc.call(
        store,
        name=SUPERSEDE_TOOL,
        arguments=rpc.versioned(
            store, {"id": "20260101-widget-cache", "superseded_by": "20260201-widget-cache-v2"}
        ),
    )

    assert is_error, rpc.text(result)
    assert "changed on disk" in rpc.text(result)
    assert path.read_text(encoding="utf-8") == newer, "the newer version must survive intact"
    assert entry_names(store / "sessions") == ["20260101-widget-cache.md"], "no temp file left behind"


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
    # the client read the corrupt bytes, so the version check passes and the decode meets them
    arguments = {
        "id": "20260101-widget-cache",
        "superseded_by": "20260201-widget-cache-v2",
        "expected_version": version_of(corrupt),
    }

    real_parse_document = mcp_server.parse_document

    def racing_parse(target: Path):
        # the concurrent write lands after the validating parse, so the raw re-read below is
        # the call that meets the undecodable bytes
        doc = real_parse_document(target)
        target.write_bytes(corrupt)
        return doc

    monkeypatch.setattr(mcp_server, "parse_document", racing_parse)

    _, responses, _ = rpc.run(
        store,
        rpc.tools_call_msg(
            1,
            name=SUPERSEDE_TOOL,
            arguments=arguments,
        ),
    )

    assert "error" not in responses[0], responses[0]
    result = responses[0]["result"]
    assert result.get("isError"), rpc.text(result)
    assert "could not be re-read" in rpc.text(result)
    assert path.read_bytes() == corrupt, "nothing may be written over the newer version"
    assert entry_names(store / "sessions") == ["20260101-widget-cache.md"], "no temp file left behind"


def test_a_large_document_round_trips_and_leaves_no_staged_file(store):
    """No size cap is deliberate; at any size the bytes must arrive intact and the staging file
    must never be orphaned."""
    body = "x" * (2 * 1024 * 1024)
    content = (
        "---\nid: 20260101-widget-cache\ntitle: Widget cache\ndate: 2026-01-01\n"
        f"status: draft\nmode: daily\n---\n\n## Pre-reg\n\n{body}\n"
    )

    result, is_error = rpc.call(
        store,
        name=CREATE_TOOL,
        arguments={"id": "20260101-widget-cache", "content": content},
    )

    assert not is_error, rpc.text(result)
    written = (store / "sessions" / "20260101-widget-cache.md").read_text(encoding="utf-8")
    assert written == content, "the document must arrive byte-for-byte"
    assert load_store(store / "sessions").errors == []
    assert entry_names(store / "sessions") == ["20260101-widget-cache.md"], "no staged file left behind"


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(
            {"id": "20260101-widget-cache", "content": DRAFT_CONTENT + "\ud800"},
            id="in_the_content",
        ),
        pytest.param(
            {"id": "20260101-widget-cache\ud800", "content": DRAFT_CONTENT},
            id="in_the_id",
        ),
    ],
)
def test_create_draft_refuses_content_that_cannot_be_encoded_as_utf8(store, arguments):
    """`content.encode("utf-8")` raises on an unpaired surrogate. Refused at the envelope, so it
    never reaches the write path — and never as a -32603 blamed on the server."""
    _, responses, _ = rpc.run(store, rpc.tools_call_msg(1, name=CREATE_TOOL, arguments=arguments))

    assert responses[0]["error"]["code"] == rpc.INVALID_REQUEST
    assert entry_names(store / "sessions") == [], "nothing written, and no staged file left behind"


@requires_posix_modes
def test_completing_a_draft_keeps_the_documents_file_mode(store):
    """The MCP write path replaces the file too, so it inherits the same rule `backfill` does."""
    rpc.call(store, name=CREATE_TOOL,
             arguments={"id": "20260101-widget-cache", "content": DRAFT_CONTENT})
    path = store / "sessions" / "20260101-widget-cache.md"
    os.chmod(path, 0o640)

    rpc.call(store, name=COMPLETE_TOOL,
             arguments=rpc.versioned(store, {"id": "20260101-widget-cache", "content": ACTIVE_CONTENT}))

    assert stat.S_IMODE(os.stat(path).st_mode) == 0o640
    assert load_store(store / "sessions").docs[0].status == "active"


def test_a_documents_line_endings_survive_the_mcp_write(store):
    r"""Regression for the Windows CI leg — `write_text` translated them there. On POSIX
    `os.linesep` is `\n`, so this passes either way; `test_staging.py` pins the rest."""
    content = DRAFT_CONTENT.replace("\n", "\r\n")

    rpc.call(store, name=CREATE_TOOL,
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
    "setup, name, arguments, committer",
    [
        pytest.param(
            lambda store: None,
            CREATE_TOOL,
            {"id": "20260101-widget-cache", "content": DRAFT_CONTENT},
            "commit_new",
            id="create",
        ),
        pytest.param(
            lambda store: rpc.call(
                store,
                name=CREATE_TOOL,
                arguments={"id": "20260101-widget-cache", "content": DRAFT_CONTENT},
            ),
            COMPLETE_TOOL,
            {"id": "20260101-widget-cache", "content": ACTIVE_CONTENT},
            "commit",
            id="complete",
        ),
        pytest.param(
            _make_active,
            SUPERSEDE_TOOL,
            {"id": "20260101-widget-cache", "superseded_by": "20260201-widget-cache-v2"},
            "commit",
            id="supersede",
        ),
    ],
)
def test_a_failing_commit_still_discards_the_staged_file(
    store, monkeypatch, setup, name, arguments, committer
):
    """`commit` stats and chmods before it replaces, so it can raise where the bare `os.replace`
    it succeeded could not — and the staged file must go either way. For create this is also
    AC-03.4's second half: a failed create is a fault, never a success."""
    setup(store)

    def failing_commit(tmp_path, target):
        raise OSError(28, "No space left on device")

    # create and complete write through `drafts`; supersede is MCP-only and stays in mcp_server
    monkeypatch.setattr(mcp_server if name == SUPERSEDE_TOOL else drafts, committer, failing_commit)
    _, responses, _ = rpc.run(
        store, rpc.tools_call_msg(1, name=name, arguments=rpc.versioned(store, arguments))
    )

    # the code and the message together, so a renamed argument or tool cannot leave this green
    # while failing before anything is staged — where "no .tmp survived" is trivially true
    assert responses[0]["error"]["code"] == rpc.INTERNAL_ERROR
    assert "No space left" in responses[0]["error"]["message"]
    assert not [f for f in entry_names(store / "sessions") if f.endswith(".tmp")]


@pytest.mark.parametrize("raw", [
    pytest.param(b"\xef\xbb\xbf", id="bom"),
    pytest.param(b"", id="no-bom"),
])
@pytest.mark.parametrize("nl", [
    pytest.param("\r\n", id="crlf"),
    pytest.param("\r", id="cr"),
    pytest.param("\n", id="lf"),
])
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
        replaced(ACTIVE_CONTENT, "20260101-widget-cache", "20260201-widget-cache-v2"),
        encoding="utf-8",
    )

    result, is_error = rpc.call(store, name=SUPERSEDE_TOOL, arguments=rpc.versioned(store, {
        "id": "20260101-widget-cache", "superseded_by": "20260201-widget-cache-v2",
    }))

    assert not is_error, rpc.text(result)
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
        pytest.param(
            CREATE_TOOL,
            {"id": "20260101-widget-cache", "content": DRAFT_CONTENT},
            id="create",
        ),
        pytest.param(
            COMPLETE_TOOL,
            {"id": "20260101-widget-cache", "content": ACTIVE_CONTENT},
            id="complete",
        ),
        pytest.param(
            SUPERSEDE_TOOL,
            {"id": "20260101-widget-cache",
                              "superseded_by": "20260201-widget-cache-v2"},
            id="supersede",
        ),
    ],
)
def test_a_write_tool_reports_an_unreadable_store_as_a_tool_error(tmp_path, name, arguments):
    """`_resolve_sessions_dir` had the same bare `is_dir()`, so every write tool turned an
    unreadable store into a `-32603 internal error` — the channel for engmem's own bugs."""
    store = tmp_path / "store"
    (store / "sessions").mkdir(parents=True)

    with fs.without_access(store, restore_mode=0o755):
        result, is_error = rpc.call(store, name=name, arguments=arguments)

    assert is_error
    assert "store not readable" in rpc.text(result)


@pytest.mark.parametrize(
    "front_matter",
    [
        pytest.param(
            "{id: doc-a, title: T, date: 2026-01-01, task_date: 2026-01-01, "
            "status: active, tags: [x], entities: [E]}",
            id="flow-one-line",
        ),
        # a root flow mapping can put a key at column 0, so the column test alone missed it
        pytest.param(
            "{\nid: doc-a,\ntitle: T,\ndate: 2026-01-01,\ntask_date: 2026-01-01,\n"
            "status: active,\ntags: [x],\nentities: [E]\n}",
            id="flow-multi-line",
        ),
        pytest.param(
            "  id: doc-a\n  title: T\n  date: 2026-01-01\n  task_date: 2026-01-01\n"
            "  status: active\n  tags: [x]\n  entities: [E]",
            id="indented",
        ),
    ],
)
def test_supersede_refuses_a_key_that_is_not_a_line_of_its_own(store, front_matter):
    """The replacement is written at column 0 alone on its line. `flow-multi-line` pins the
    flow guard and `indented` pins the column guard; `flow-one-line` is refused by either and
    is here to document the shape."""
    sessions = store / "sessions"
    path = sessions / "doc-a.md"
    path.write_text(f"---\n{front_matter}\n---\n\n# Title\n\nbody\n", encoding="utf-8")
    before = path.read_bytes()

    arguments = rpc.versioned(store, {"id": "doc-a", "superseded_by": "20260201-widget-cache-v2"})
    _, responses, _ = rpc.run(store, rpc.tools_call_msg(1, name=SUPERSEDE_TOOL, arguments=arguments))

    # a regression escapes as a protocol fault, so name that rather than showing a KeyError
    assert "error" not in responses[0], responses[0]
    result = responses[0]["result"]
    assert result.get("isError") is True
    assert "not a line of its own" in result["content"][0]["text"]
    assert path.read_bytes() == before


def test_complete_draft_refuses_when_the_draft_changes_between_read_and_commit(store, monkeypatch):
    """Another writer activated the draft after the tool confirmed it was one; committing the
    tool's copy over it would silently replace the newer document."""
    target = _write_raw(store, "20260101-widget-cache", DRAFT_CONTENT)
    external = ACTIVE_CONTENT + "\nFinished elsewhere while the tool was working.\n"
    real_stage = drafts.stage_content

    def stage_after_an_external_write(path, content):
        target.write_bytes(external.encode("utf-8"))
        return real_stage(path, content)

    monkeypatch.setattr(drafts, "stage_content", stage_after_an_external_write)

    result, is_error = rpc.call(
        store, name=COMPLETE_TOOL,
        arguments=rpc.versioned(store, {"id": "20260101-widget-cache", "content": ACTIVE_CONTENT}),
    )

    assert is_error
    assert "changed on disk while it was being read" in rpc.text(result)
    assert target.read_bytes() == external.encode("utf-8")
    assert entry_names(store / "sessions") == ["20260101-widget-cache.md"], "the staged file must be gone"
