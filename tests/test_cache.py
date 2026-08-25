import os
import sys

import pytest

from conftest import requires_unreadable_paths

from engmem import cache


def _identity(path):
    return cache.identity_for(path)


def test_missing_cache_entry_is_a_miss(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "cache_root", lambda: tmp_path / "cache-home")
    doc = tmp_path / "widget-cache.md"
    doc.write_text("body")

    assert cache.load(doc, _identity(doc)) is None


def test_store_then_load_roundtrips_the_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "cache_root", lambda: tmp_path / "cache-home")
    doc = tmp_path / "widget-cache.md"
    doc.write_text("body")
    payload = {"sections": [{"heading": "Decision Log", "literal_tf": {"cache": 3}}]}

    cache.store(doc, _identity(doc), payload)

    assert cache.load(doc, _identity(doc)) == payload


def test_editing_the_document_invalidates_the_cache_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "cache_root", lambda: tmp_path / "cache-home")
    doc = tmp_path / "widget-cache.md"
    doc.write_text("original body")
    old_identity = _identity(doc)
    cache.store(doc, old_identity, {"sections": ["original"]})

    doc.write_text("edited body, a different length")
    new_identity = _identity(doc)

    assert new_identity != old_identity
    assert cache.load(doc, new_identity) is None
    # the stale entry is still keyed to the old identity, not silently reused
    assert cache.load(doc, old_identity) == {"sections": ["original"]}


def test_two_distinct_paths_never_collide(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "cache_root", lambda: tmp_path / "cache-home")
    doc_a = tmp_path / "widget-cache.md"
    doc_b = tmp_path / "sweeper-job.md"
    doc_a.write_text("a")
    doc_b.write_text("b")

    cache.store(doc_a, _identity(doc_a), {"who": "a"})
    cache.store(doc_b, _identity(doc_b), {"who": "b"})

    assert cache.load(doc_a, _identity(doc_a)) == {"who": "a"}
    assert cache.load(doc_b, _identity(doc_b)) == {"who": "b"}


def test_corrupt_cache_file_degrades_to_a_miss_and_warns_on_stderr(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cache, "cache_root", lambda: tmp_path / "cache-home")
    doc = tmp_path / "widget-cache.md"
    doc.write_text("body")
    identity = _identity(doc)
    cache.store(doc, identity, {"sections": ["fine"]})

    entry_path = cache._entry_path(doc)
    entry_path.write_text("{ not json at all", encoding="utf-8")

    result = cache.load(doc, identity)

    assert result is None
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "widget-cache.md" in captured.err
    assert "cache" in captured.err.lower()


def test_cache_file_missing_the_payload_key_degrades_to_a_miss_and_warns(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(cache, "cache_root", lambda: tmp_path / "cache-home")
    doc = tmp_path / "widget-cache.md"
    doc.write_text("body")
    identity = _identity(doc)

    entry_path = cache._entry_path(doc)
    entry_path.parent.mkdir(parents=True, exist_ok=True)
    entry_path.write_text(
        f'{{"format_version": 1, "key": {list(identity)}}}', encoding="utf-8"
    )

    result = cache.load(doc, identity)

    assert result is None
    assert "cache" in capsys.readouterr().err.lower()


def test_cache_written_by_a_future_format_version_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "cache_root", lambda: tmp_path / "cache-home")
    doc = tmp_path / "widget-cache.md"
    doc.write_text("body")
    identity = _identity(doc)

    entry_path = cache._entry_path(doc)
    entry_path.parent.mkdir(parents=True, exist_ok=True)
    entry_path.write_text(
        f'{{"format_version": 999, "key": {list(identity)}, "payload": {{"x": 1}}}}',
        encoding="utf-8",
    )

    assert cache.load(doc, identity) is None


def test_store_creates_cache_root_directory_when_absent(tmp_path, monkeypatch):
    root = tmp_path / "does" / "not" / "exist-yet"
    monkeypatch.setattr(cache, "cache_root", lambda: root)
    doc = tmp_path / "widget-cache.md"
    doc.write_text("body")

    cache.store(doc, _identity(doc), {"sections": []})

    assert root.exists()
    assert cache.load(doc, _identity(doc)) == {"sections": []}


def test_default_cache_root_respects_xdg_cache_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert cache.cache_root() == tmp_path / "xdg" / "engmem"


def test_default_cache_root_falls_back_to_dot_cache_when_xdg_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(cache.Path, "home", classmethod(lambda cls: tmp_path / "home"))

    assert cache.cache_root() == tmp_path / "home" / ".cache" / "engmem"


def test_identity_for_missing_file_returns_none(tmp_path):
    assert cache.identity_for(tmp_path / "does-not-exist.md") is None


def test_load_with_no_identity_is_always_a_miss(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "cache_root", lambda: tmp_path / "cache-home")
    doc = tmp_path / "widget-cache.md"
    doc.write_text("body")
    cache.store(doc, _identity(doc), {"sections": ["fine"]})

    assert cache.load(doc, None) is None


def test_prune_orphans_removes_entries_for_paths_no_longer_present(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "cache_root", lambda: tmp_path / "cache-home")
    keep = tmp_path / "widget-cache.md"
    gone = tmp_path / "sweeper-job.md"
    keep.write_text("keep")
    gone.write_text("gone")
    cache.store(keep, _identity(keep), {"who": "keep"})
    cache.store(gone, _identity(gone), {"who": "gone"})

    cache.prune_orphans([keep])

    assert cache.load(keep, _identity(keep)) == {"who": "keep"}
    entries = list((tmp_path / "cache-home").glob("*.json"))
    assert len(entries) == 1


def test_storing_many_entries_never_evicts_purely_for_exceeding_a_count(tmp_path, monkeypatch):
    # cache.py used to carry an LRU eviction backstop (MAX_CACHE_ENTRIES) on top of
    # prune_orphans. The cache key is (path, mtime_ns, size), so an edited document
    # overwrites its own entry rather than growing the cache, and prune_orphans already
    # removes entries for renamed/deleted documents every search — nothing legitimate
    # should ever be evicted purely for exceeding a count.
    monkeypatch.setattr(cache, "cache_root", lambda: tmp_path / "cache-home")

    docs = []
    for i in range(5):
        doc = tmp_path / f"doc-{i}.md"
        doc.write_text(f"body {i}")
        docs.append(doc)
        cache.store(doc, _identity(doc), {"i": i})

    for i, doc in enumerate(docs):
        assert cache.load(doc, _identity(doc)) == {"i": i}


@requires_unreadable_paths
def test_unreadable_cache_file_degrades_to_a_miss_and_warns(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cache, "cache_root", lambda: tmp_path / "cache-home")
    doc = tmp_path / "widget-cache.md"
    doc.write_text("body")
    identity = _identity(doc)
    cache.store(doc, identity, {"sections": ["fine"]})

    entry_path = cache._entry_path(doc)
    os.chmod(entry_path, 0o000)
    try:
        result = cache.load(doc, identity)
    finally:
        os.chmod(entry_path, 0o644)

    assert result is None
    assert "cache" in capsys.readouterr().err.lower()


def test_store_write_failure_does_not_raise(tmp_path, monkeypatch, capsys):
    # the cache root itself exists but is a file, not a directory: mkdir(parents=True)
    # raises FileExistsError/NotADirectoryError, which store() must absorb, not propagate
    blocked_root = tmp_path / "cache-home-is-a-file"
    blocked_root.write_text("not a directory")
    monkeypatch.setattr(cache, "cache_root", lambda: blocked_root)
    doc = tmp_path / "widget-cache.md"
    doc.write_text("body")

    cache.store(doc, _identity(doc), {"sections": []})

    assert "cache" in capsys.readouterr().err.lower()
