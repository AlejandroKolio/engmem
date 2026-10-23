"""The MCP server keeps parsed documents and section entries between searches (spec 002 US-2);
every answer must equal what a fresh process would print (contracts/cache.md, "Kept between
searches")."""

from __future__ import annotations

import os
import shutil
from pathlib import Path, PureWindowsPath

import pytest

from support.docs import DRAFT_CONTENT, FIXTURES, replaced

from engmem import cache, mcp_documents, mcp_server, spine
from engmem.spine import load_store

QUERY = "WidgetCache"


@pytest.fixture
def store(tmp_path):
    shutil.copytree(FIXTURES, tmp_path / "store" / "sessions")
    return tmp_path / "store"


@pytest.fixture
def parses(monkeypatch):
    """Paths `_parse_text` ran for, in order."""
    seen = []
    parse_text = spine._parse_text

    def counting(path, text, identity):
        seen.append(path.name)
        return parse_text(path, text, identity)

    monkeypatch.setattr(spine, "_parse_text", counting)
    return seen


def _search(store, query=QUERY):
    text, is_error = mcp_server._run_search_for_tool(store, query)
    assert not is_error, text
    return text


def _cold(store, new_process, query=QUERY):
    new_process()
    return _search(store, query)


def _rewrite_keeping_stat(path, old, new):
    """Same size, same mtime, different bytes: the edit `(mtime_ns, size)` alone cannot see."""
    st = path.stat()
    # bytes, not text: a checkout with CRLF endings would come back shorter through read_text
    data = path.read_bytes()
    assert len(old) == len(new) and old.encode() in data
    path.write_bytes(data.replace(old.encode(), new.encode()))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert (path.stat().st_size, path.stat().st_mtime_ns) == (st.st_size, st.st_mtime_ns)


def test_an_unchanged_store_is_answered_again_without_parsing_a_document(store, parses):
    """Only a document that failed to parse is parsed again: a failure is never kept."""
    first = _search(store)
    parsed_first = list(parses)

    second = _search(store)

    assert second == first
    assert len(parsed_first) == 8
    assert parses[len(parsed_first):] == ["broken.md"]


def test_an_edit_that_keeps_size_and_mtime_is_still_seen(store, new_process):
    doc = store / "sessions" / "1000001-response-cache.md"
    _search(store, "ResponseCacheController")

    _rewrite_keeping_stat(doc, "ResponseCacheController", "ResponseStoreController")
    warm = _search(store, "ResponseStoreController")

    assert "### 1000001-response-cache " in warm
    assert warm == _cold(store, new_process, "ResponseStoreController")


def test_a_touched_document_is_parsed_again(store, parses):
    """A document stating no `date:` takes it from the file's mtime: same text, new date."""
    doc = store / "sessions" / "1000001-response-cache.md"
    text = doc.read_text(encoding="utf-8")
    doc.write_bytes(replaced(text, "\ndate: 2026-", "\nnodate: 2026-", 1).encode("utf-8"))
    kept = cache.KeptByPath()
    before = next(d for d in load_store(doc.parent, kept).docs if d.path == doc)
    st = doc.stat()

    os.utime(doc, ns=(st.st_atime_ns, st.st_mtime_ns - 400 * 86_400 * 10**9))
    after = next(d for d in load_store(doc.parent, kept).docs if d.path == doc)

    assert parses.count(doc.name) == 2
    assert (before.date - after.date).days == 400


@pytest.mark.parametrize("change", ["add", "remove"])
def test_an_added_or_removed_document_is_reflected(store, new_process, change):
    sessions = store / "sessions"
    assert "docs: 7 " in _search(store)

    if change == "add":
        (sessions / "20260101-widget-cache.md").write_text(DRAFT_CONTENT, encoding="utf-8")
    else:
        (sessions / "ttl-etag-revalidation-v2.md").unlink()
    warm = _search(store)

    assert warm == _cold(store, new_process)
    assert f"docs: {6 if change == 'remove' else 8} " in warm


def test_a_write_tool_served_by_the_process_is_seen_by_the_next_search(store, new_process):
    assert "drafts: 1 " in _search(store)

    _, is_error = mcp_documents.handle_create_draft(
        {"id": "20260101-widget-cache", "content": DRAFT_CONTENT}, store
    )
    warm = _search(store)

    assert not is_error
    assert "drafts: 2 " in warm
    assert warm == _cold(store, new_process)


def test_a_document_that_became_unreadable_is_reported_as_a_cold_load_reports_it(
    store, new_process
):
    doc = store / "sessions" / "1000001-response-cache.md"
    _search(store)

    doc.unlink()
    doc.mkdir()
    warm = _search(store)

    assert "(1 failed to load)" not in warm
    assert "(2 failed to load)" in warm
    assert "### 1000001-response-cache " not in warm
    assert warm == _cold(store, new_process)


def test_a_changed_id_over_the_same_body_is_not_answered_with_the_old_id(store, new_process):
    doc = store / "sessions" / "1000001-response-cache.md"
    _search(store, "ResponseCacheController")

    doc.write_text(
        replaced(doc.read_text(encoding="utf-8"), "id: 1000001-response-cache", "id: renamed-doc"),
        encoding="utf-8",
    )
    warm = _search(store, "ResponseCacheController")

    assert "### renamed-doc " in warm
    assert warm == _cold(store, new_process, "ResponseCacheController")


def test_a_changed_entity_is_matched_by_the_next_search(store, new_process):
    doc = store / "sessions" / "1000001-response-cache.md"
    _search(store, "QuasarGateway")

    text = doc.read_text(encoding="utf-8")
    doc.write_text(replaced(text, "entities: [", "entities: [QuasarGateway, ", 1), encoding="utf-8")
    warm = _search(store, "QuasarGateway")

    assert "### 1000001-response-cache " in warm
    assert warm == _cold(store, new_process, "QuasarGateway")


def test_kept_values_are_served_only_under_their_key_and_forgotten_off_the_store():
    kept = cache.KeptByPath()
    built = []

    def build(value):
        built.append(value)
        return value

    a, b = Path("a.md"), Path("b.md")
    assert kept.get(a, 1, lambda: build("a1")) == "a1"
    assert kept.get(a, 1, lambda: build("again")) == "a1"
    assert kept.get(a, 2, lambda: build("a2")) == "a2"
    kept.get(b, 1, lambda: build("b1"))
    kept.retain([b])

    assert kept.get(a, 2, lambda: build("a2 rebuilt")) == "a2 rebuilt"
    assert kept.get(b, 1, lambda: build("b again")) == "b1"
    assert built == ["a1", "a2", "b1", "a2 rebuilt"]


def test_a_case_only_rename_is_built_again_even_where_paths_compare_equal():
    """Windows paths compare case-insensitively: `Foo.md` renamed `foo.md` keeps its bytes and
    stat, and the kept document would still name the old path."""
    kept = cache.KeptByPath()
    old, new = PureWindowsPath("sessions/Foo.md"), PureWindowsPath("sessions/foo.md")
    assert old == new

    kept.get(old, 1, lambda: "parsed as Foo.md")
    kept.retain([new])

    assert kept.get(new, 1, lambda: "parsed as foo.md") == "parsed as foo.md"


def test_a_failed_build_keeps_nothing():
    kept = cache.KeptByPath()

    def fail():
        raise ValueError("bad front matter")

    with pytest.raises(ValueError):
        kept.get(Path("a.md"), 1, fail)

    assert kept.get(Path("a.md"), 1, lambda: "parsed") == "parsed"


def test_the_cli_keeps_nothing_between_loads(store, parses):
    load_store(store / "sessions")
    first = len(parses)

    load_store(store / "sessions")

    assert len(parses) == 2 * first


def test_the_cache_is_pruned_once_per_set_of_store_paths(tmp_path, monkeypatch):
    """Pruning resolves every store path; a search over the same store does it once."""
    globs = []
    root_glob = Path.glob

    def counting(self, pattern):
        globs.append(pattern)
        return root_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", counting)
    a, b = tmp_path / "a.md", tmp_path / "b.md"

    cache.prune_orphans([a, b])
    once = len(globs)
    cache.prune_orphans([b, a])
    assert len(globs) == once

    cache.prune_orphans([a])
    assert len(globs) == 2 * once

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "other-cache"))
    cache.prune_orphans([a])
    assert len(globs) == 3 * once


def test_a_document_gone_from_the_store_is_forgotten(store):
    """Kept for the life of the server, so a removed document must not stay held."""
    kept = cache.KeptByPath()
    sessions = store / "sessions"
    load_store(sessions, kept)
    gone = sessions / "ttl-etag-revalidation-v2.md"

    gone.unlink()
    load_store(sessions, kept)

    assert str(gone) not in kept._kept
    assert str(sessions / "1000001-response-cache.md") in kept._kept


def test_section_entries_of_a_document_gone_from_the_store_are_forgotten(store):
    from engmem import scoring, section_index

    sessions = store / "sessions"
    _search(store)
    gone = sessions / "ttl-etag-revalidation-v2.md"
    assert str(gone) in section_index._KEPT_ENTRIES._kept
    assert str(gone) in scoring._KEPT_FIELDS._kept

    gone.unlink()
    _search(store)

    assert str(gone) not in section_index._KEPT_ENTRIES._kept
    assert str(gone) not in scoring._KEPT_FIELDS._kept


def test_a_prune_that_could_not_list_the_cache_is_tried_again(tmp_path, monkeypatch):
    globs = []
    root_glob = Path.glob

    def failing_once(self, pattern):
        globs.append(pattern)
        if len(globs) == 1:
            raise OSError("transient")
        return root_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", failing_once)
    paths = [tmp_path / "a.md"]

    cache.prune_orphans(paths)
    cache.prune_orphans(paths)

    assert len(globs) > 1
