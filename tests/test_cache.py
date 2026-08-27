"""Unit tests for `engmem.cache`, the best-effort per-document scoring cache."""

from __future__ import annotations

import json
import os

import pytest

from conftest import requires_symlinks, requires_permission_enforcement

from engmem import cache
from engmem.cache import identity_for


@pytest.fixture
def cache_home(tmp_path, monkeypatch):
    """Redirects the cache out of the developer's real `~/.cache`, and returns where to."""
    root = tmp_path / "cache-home"
    monkeypatch.setattr(cache, "cache_root", lambda: root)
    return root


@pytest.fixture
def doc(tmp_path):
    path = tmp_path / "widget-cache.md"
    path.write_text("body", encoding="utf-8")
    return path


def _write_entry(path, record) -> None:
    entry_path = cache._entry_path(path)[0]
    entry_path.parent.mkdir(parents=True, exist_ok=True)
    entry_path.write_text(json.dumps(record), encoding="utf-8")


def test_missing_cache_entry_is_a_miss(cache_home, doc, capsys):
    assert cache.load(doc, identity_for(doc)) is None
    # the highest-volume degraded path (contracts/cache.md's "entry absent" row): a stray
    # print here would corrupt every cold search, and on the MCP route every JSON-RPC frame
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


def test_store_then_load_roundtrips_the_payload(cache_home, doc):
    payload = {"sections": [{"heading": "Decision Log", "literal_tf": {"cache": 3}}]}

    cache.store(doc, identity_for(doc), payload)

    assert cache.load(doc, identity_for(doc)) == payload


def test_storing_again_replaces_the_entry(cache_home, doc):
    """Recomputing after an edit must not leave the previous payload reachable."""
    cache.store(doc, identity_for(doc), {"round": 1})
    doc.write_text("edited body", encoding="utf-8")
    identity = identity_for(doc)

    cache.store(doc, identity, {"round": 2})

    assert cache.load(doc, identity) == {"round": 2}
    assert len(list(cache_home.glob("*.json"))) == 1


def test_editing_the_document_invalidates_the_cache_entry(cache_home, doc, capsys):
    old_identity = identity_for(doc)
    cache.store(doc, old_identity, {"sections": ["original"]})
    capsys.readouterr()  # scope the silence assertion below to the loads this test names

    doc.write_text("edited body, a different length", encoding="utf-8")
    new_identity = identity_for(doc)

    assert new_identity != old_identity
    assert cache.load(doc, new_identity) is None
    # the stale entry is still keyed to the old identity, not silently reused
    assert cache.load(doc, old_identity) == {"sections": ["original"]}
    # and none of it says anything: this fires once per edited document, so a warning here
    # would be one line of noise per edit, every search
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


def test_two_distinct_paths_never_collide(cache_home, tmp_path, doc):
    other = tmp_path / "sweeper-job.md"
    other.write_text("b", encoding="utf-8")

    cache.store(doc, identity_for(doc), {"who": "a"})
    cache.store(other, identity_for(other), {"who": "b"})

    assert cache.load(doc, identity_for(doc)) == {"who": "a"}
    assert cache.load(other, identity_for(other)) == {"who": "b"}


@requires_symlinks
def test_two_names_for_one_file_share_an_entry(cache_home, tmp_path, doc):
    """`_entry_path` hashes the *resolved* path, so a document reached through a symlink is
    the same document — one entry, and the identity keeps it correct."""
    alias = tmp_path / "alias.md"
    alias.symlink_to(doc)
    cache.store(doc, identity_for(doc), {"who": "the one document"})

    assert cache.load(alias, identity_for(alias)) == {"who": "the one document"}
    assert len(list(cache_home.glob("*.json"))) == 1


# ---------------------------------------------------------------------------
# every degraded path: a miss, a warning on stderr, and stdout untouched
# ---------------------------------------------------------------------------


def test_corrupt_cache_file_degrades_to_a_miss_and_warns(cache_home, doc, capsys):
    identity = identity_for(doc)
    cache.store(doc, identity, {"sections": ["fine"]})
    cache._entry_path(doc)[0].write_text("{ not json at all", encoding="utf-8")

    assert cache.load(doc, identity) is None
    captured = capsys.readouterr()
    assert "widget-cache.md" in captured.err
    assert captured.out == ""


@pytest.mark.parametrize(
    "make_record,expected_err_substring",
    [
        # valid JSON is not enough — a list parses, then `record.get` would raise
        pytest.param(lambda identity: [1, 2, 3], "not a JSON object", id="not_a_json_object"),
        pytest.param(
            lambda identity: {"format_version": cache.CACHE_FORMAT_VERSION, "key": list(identity)},
            "missing or null payload",
            id="payload_key_absent",
        ),
        # unlike the `key` row above, no live case shares this branch: nothing legitimate
        # writes a null payload, so it is free to warn
        pytest.param(
            lambda identity: {
                "format_version": cache.CACHE_FORMAT_VERSION,
                "key": list(identity),
                "payload": None,
            },
            "missing or null payload",
            id="payload_is_null",
        ),
        # damage, like a missing payload — but silent, because it shares its branch with the
        # live once-per-edit mismatch (see the comment on that branch in cache.py)
        pytest.param(
            lambda identity: {"format_version": cache.CACHE_FORMAT_VERSION, "payload": {"x": 1}},
            None,
            id="key_absent_is_a_silent_miss",
        ),
        # `tuple(key)` raises on a scalar, and `load` promises never to raise
        pytest.param(
            lambda identity: {
                "format_version": cache.CACHE_FORMAT_VERSION,
                "key": 5,
                "payload": {"x": 1},
            },
            None,
            id="key_not_a_list_is_a_silent_miss",
        ),
        # bumping CACHE_FORMAT_VERSION must retire old entries quietly, not report a store
        # full of corruption
        pytest.param(
            lambda identity: {
                "format_version": 999,
                "key": list(identity),
                "payload": {"x": 1},
            },
            None,
            id="format_version_mismatch_is_a_silent_miss",
        ),
    ],
)
def test_a_damaged_but_json_valid_cache_record_is_a_miss(
    cache_home, doc, capsys, make_record, expected_err_substring
):
    """Every shape in `contracts/cache.md`'s "Degrading, and how loudly" table that still
    parses as a JSON object: a miss, warned or silent as the table says, stdout never touched."""
    identity = identity_for(doc)
    _write_entry(doc, make_record(identity))

    assert cache.load(doc, identity) is None
    captured = capsys.readouterr()
    if expected_err_substring is None:
        assert captured.err == ""
    else:
        assert expected_err_substring in captured.err
    assert captured.out == ""


@requires_permission_enforcement
def test_unreadable_cache_file_degrades_to_a_miss_and_warns(cache_home, doc, capsys):
    identity = identity_for(doc)
    cache.store(doc, identity, {"sections": ["fine"]})
    entry_path = cache._entry_path(doc)[0]

    os.chmod(entry_path, 0o000)
    try:
        result = cache.load(doc, identity)
    finally:
        os.chmod(entry_path, 0o644)

    assert result is None
    captured = capsys.readouterr()
    assert "widget-cache.md" in captured.err
    assert captured.out == ""


def test_store_write_failure_warns_and_leaves_no_entry(tmp_path, monkeypatch, doc, capsys):
    """The cache root exists but is a file, so `mkdir(parents=True)` raises — `store` must
    absorb that, and a later `load` must simply miss."""
    blocked_root = tmp_path / "cache-home-is-a-file"
    blocked_root.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(cache, "cache_root", lambda: blocked_root)

    cache.store(doc, identity_for(doc), {"sections": []})

    captured = capsys.readouterr()
    assert "widget-cache.md" in captured.err
    assert captured.out == ""
    assert cache.load(doc, identity_for(doc)) is None


def test_a_failed_write_leaves_no_temp_file_behind(cache_home, doc, monkeypatch, capsys):
    """`store` renames a `.tmp` sibling into place. The `except OSError` arm discards it
    promptly on a write failure; `prune_orphans` is only the backstop for a leak that arm
    cannot reach, such as a signal it never sees."""
    def failing_replace(self, target):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cache.Path, "replace", failing_replace)
    cache.store(doc, identity_for(doc), {"sections": []})

    captured = capsys.readouterr()
    assert "widget-cache.md" in captured.err
    assert captured.out == ""
    assert list(cache_home.iterdir()) == []


# ---------------------------------------------------------------------------
# where the cache lives, and what it keeps
# ---------------------------------------------------------------------------


def test_store_creates_cache_root_directory_when_absent(tmp_path, monkeypatch, doc):
    root = tmp_path / "does" / "not" / "exist-yet"
    monkeypatch.setattr(cache, "cache_root", lambda: root)

    cache.store(doc, identity_for(doc), {"sections": []})

    assert root.exists()
    assert cache.load(doc, identity_for(doc)) == {"sections": []}


def test_default_cache_root_respects_xdg_cache_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert cache.cache_root() == tmp_path / "xdg" / "engmem"


def test_default_cache_root_falls_back_to_dot_cache_when_xdg_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(cache.Path, "home", classmethod(lambda cls: tmp_path / "home"))

    assert cache.cache_root() == tmp_path / "home" / ".cache" / "engmem"


def test_identity_for_missing_file_returns_none(tmp_path):
    assert cache.identity_for(tmp_path / "does-not-exist.md") is None


def test_load_with_no_identity_is_always_a_miss(cache_home, doc):
    cache.store(doc, identity_for(doc), {"sections": ["fine"]})

    assert cache.load(doc, None) is None


def test_store_with_no_identity_writes_nothing(cache_home, doc):
    """A file that cannot be stat'd has no staleness key, so an entry for it could never be
    invalidated."""
    cache.store(doc, None, {"sections": ["fine"]})

    assert not cache_home.exists()


def test_prune_orphans_removes_entries_for_paths_no_longer_present(cache_home, tmp_path, doc):
    gone = tmp_path / "sweeper-job.md"
    gone.write_text("gone", encoding="utf-8")
    cache.store(doc, identity_for(doc), {"who": "keep"})
    cache.store(gone, identity_for(gone), {"who": "gone"})
    gone_entry = cache._entry_path(gone)[0]

    cache.prune_orphans([doc])

    assert cache.load(doc, identity_for(doc)) == {"who": "keep"}
    assert not gone_entry.exists()


def test_prune_orphans_collects_a_leaked_temp_file(cache_home, doc):
    """A process killed by a signal between `write_text` and `replace` raises nothing `store`
    can catch, so the temp file it leaves behind is only ever collected here, by name alone."""
    cache.store(doc, identity_for(doc), {"who": "keep"})
    leaked = cache_home / f"{cache._entry_path(doc)[0].name}.deadbeef.tmp"
    leaked.write_text("{}", encoding="utf-8")

    cache.prune_orphans([doc])

    assert not leaked.exists()
    assert cache.load(doc, identity_for(doc)) == {"who": "keep"}


def test_prune_orphans_deleting_an_in_flight_temp_file_is_a_warned_miss_not_a_crash(
    cache_home, doc, monkeypatch, capsys
):
    """`prune_orphans` cannot tell a leaked temp file from one another `store` call is still
    writing — it collects on name alone, with no age or ownership check. If it lands in that
    window, `replace` raises `FileNotFoundError`, which `store`'s own `except OSError` arm
    already turns into a warning, not a crash."""
    real_write_text = cache.Path.write_text

    def write_then_prune(self, data, encoding=None):
        result = real_write_text(self, data, encoding=encoding)
        cache.prune_orphans([doc])
        return result

    monkeypatch.setattr(cache.Path, "write_text", write_then_prune)

    cache.store(doc, identity_for(doc), {"sections": []})

    captured = capsys.readouterr()
    assert "widget-cache.md" in captured.err
    assert captured.out == ""
    assert not cache._entry_path(doc)[0].exists()


def test_prune_orphans_collects_a_legacy_pid_named_temp_file(cache_home, doc):
    """Pre-upgrade residue from the old `{digest}.json.tmp{pid}` name must not be uncollectable
    forever just because it predates the uuid scheme."""
    cache.store(doc, identity_for(doc), {"who": "keep"})
    leaked = cache_home / f"{cache._entry_path(doc)[0].name}.tmp99999"
    leaked.write_text("{}", encoding="utf-8")

    cache.prune_orphans([doc])

    assert not leaked.exists()
    assert cache.load(doc, identity_for(doc)) == {"who": "keep"}


def test_prune_orphans_with_nothing_current_empties_the_cache(cache_home, doc):
    cache.store(doc, identity_for(doc), {"who": "keep"})

    cache.prune_orphans([])

    assert list(cache_home.glob("*.json")) == []


def test_prune_orphans_on_an_absent_cache_root_is_a_no_op(cache_home):
    cache.prune_orphans([])  # must not raise

    assert not cache_home.exists()


@requires_permission_enforcement
def test_prune_orphans_survives_an_entry_it_cannot_delete(cache_home, doc, capsys):
    """Pruning is housekeeping; failing it must not fail the search that triggered it."""
    cache.store(doc, identity_for(doc), {"who": "keep"})
    os.chmod(cache_home, 0o500)
    try:
        cache.prune_orphans([])  # must not raise
    finally:
        os.chmod(cache_home, 0o700)

    assert cache._entry_path(doc)[0].exists()
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


def test_storing_many_entries_never_evicts_purely_for_exceeding_a_count(cache_home, tmp_path):
    """A fossil: `cache.py` used to carry an LRU backstop (`MAX_CACHE_ENTRIES`). No count
    proves the absence of a cap — this only fails loudly if one comes back."""
    docs = []
    for i in range(200):
        path = tmp_path / f"doc-{i}.md"
        path.write_text(f"body {i}", encoding="utf-8")
        docs.append(path)
        cache.store(path, identity_for(path), {"i": i})

    for i, path in enumerate(docs):
        assert cache.load(path, identity_for(path)) == {"i": i}


def test_two_writers_in_one_process_do_not_share_a_temp_name(cache_home, doc, monkeypatch):
    """`os.getpid()` separates processes but not threads; two threads sharing the name would
    interleave into one entry."""
    seen: list[str] = []
    real_write_text = cache.Path.write_text

    def record_then_write(self, data, encoding=None):
        seen.append(self.name)
        return real_write_text(self, data, encoding=encoding)

    monkeypatch.setattr(cache.Path, "write_text", record_then_write)
    cache.store(doc, identity_for(doc), {"round": 1})
    cache.store(doc, identity_for(doc), {"round": 2})

    assert len(set(seen)) == 2, seen
    assert all(name.endswith(".tmp") for name in seen)


def test_a_cache_entry_that_is_not_utf8_degrades_to_a_miss(cache_home, doc, capsys):
    """`UnicodeDecodeError` is a `ValueError`, not an `OSError`, so it walked past the read
    handler and out of a function whose docstring says it never raises."""
    cache.store(doc, identity_for(doc), {"sections": ["fine"]})
    cache._entry_path(doc)[0].write_bytes(b'{"format_version":2,"key":[1,2],"payload":\xff\xfe}')

    assert cache.load(doc, identity_for(doc)) is None
    captured = capsys.readouterr()
    assert "widget-cache.md" in captured.err
    assert captured.out == ""


def test_an_interrupted_write_leaves_no_temp_file(cache_home, doc, monkeypatch):
    """`KeyboardInterrupt` reaches the `except BaseException` arm, which discards the temp
    file promptly rather than leaving it for `prune_orphans` to find later."""
    def interrupted(self, target):
        raise KeyboardInterrupt()

    monkeypatch.setattr(cache.Path, "replace", interrupted)
    with pytest.raises(KeyboardInterrupt):
        cache.store(doc, identity_for(doc), {"sections": []})

    assert list(cache_home.iterdir()) == []


def test_a_payload_json_cannot_serialise_propagates(cache_home, doc):
    """A caller's bug, not a filesystem fault: swallowing it would make a permanently
    uncacheable document look like a full disk. `json.dumps` is the argument to `write_text`,
    so nothing is staged — there is no temp file to assert about."""
    with pytest.raises(TypeError):
        cache.store(doc, identity_for(doc), {"bad": object()})

