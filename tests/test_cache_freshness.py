"""US-21: search answers from the Markdown as it is now, whatever the section cache holds
(contracts/cache.md, "Keyed by content")."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from support import fs
from support.docs import write_file
from support.platform import requires_permission_enforcement, requires_symlinks
from support.search import cli_search, mcp_search

from engmem import cache

# an even number of seconds: FAT keeps mtime in 2-second steps, NTFS in 100ns ones
FIXED_MTIME_NS = 1_700_000_000 * 10**9


def _note(word: str) -> str:
    return (
        "---\nid: 0001-note\ntitle: Widget Note\ndate: 2026-01-01\ntask_date: 2026-01-01\n"
        "status: active\ntags: [platform]\nentities: [WidgetCache]\n---\n\n"
        f"## Decision Log\n\nWe kept the {word} path for WidgetCache.\n\n"
        "## Landmines\n\nThe gateway times out under load.\n"
    )


def _write_note(store: Path, word: str) -> Path:
    sessions = store / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    path = write_file(sessions, "0001-note.md", _note(word))
    os.utime(path, ns=(FIXED_MTIME_NS, FIXED_MTIME_NS))
    return path


@pytest.mark.parametrize("run", [
    pytest.param(cli_search, id="cli"),
    pytest.param(mcp_search, id="mcp"),
])
def test_ac_21_1_a_same_size_edit_under_the_old_mtime_is_searched_as_written(
    tmp_path, run, capsys
):
    path = _write_note(tmp_path, "alphaword")
    before = cache.identity_for(path)
    assert "### 0001-note " in run(tmp_path, "alphaword", capsys)  # warms the cache

    _write_note(tmp_path, "bravoword")
    assert cache.identity_for(path) == before, "the edit must be invisible to (mtime, size)"

    found = run(tmp_path, "bravoword", capsys)
    assert "### 0001-note " in found
    assert "§1-decision-log=bravoword" in found
    assert "### 0001-note " not in run(tmp_path, "alphaword", capsys)


QUERIES = ["ResponseCacheController", "ETAG revalidation", "latency", "sweeper", "nothingmatches"]


def _outputs(store: Path, capsys) -> list[str]:
    return [cli_search(store, query, capsys) for query in QUERIES]


def test_ac_21_2_cold_warm_and_no_cache_render_the_same_results(
    fixture_store, tmp_path, monkeypatch, capsys
):
    cold = _outputs(fixture_store, capsys)
    warm = _outputs(fixture_store, capsys)
    assert any(entry.suffix == ".json" for entry in cache.cache_root().iterdir())

    blocked = tmp_path / "cache-root-is-a-file"
    blocked.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(cache, "cache_root", lambda: blocked)
    without = _outputs(fixture_store, capsys)

    assert "### 1000001-response-cache " in cold[0]
    assert warm == cold
    assert without == cold


def _truncated(entry: Path) -> None:
    raw = entry.read_bytes()
    entry.write_bytes(raw[: len(raw) // 2])


def _garbage(entry: Path) -> None:
    entry.write_bytes(b"\xff\xfe\x00 not a cache entry \x81")


def _not_an_object(entry: Path) -> None:
    entry.write_text("[1, 2, 3]", encoding="utf-8")


def _hash_of_another_body(entry: Path) -> None:
    record = json.loads(entry.read_text(encoding="utf-8"))
    record["body_sha256"] = cache.content_key("a body this document never had")
    record["payload"] = {"sections": []}
    entry.write_text(json.dumps(record), encoding="utf-8")


def _hash_missing(entry: Path) -> None:
    record = json.loads(entry.read_text(encoding="utf-8"))
    del record["body_sha256"]
    record["payload"] = {"sections": []}
    entry.write_text(json.dumps(record), encoding="utf-8")


def _payload_of_the_wrong_shape(entry: Path) -> None:
    record = json.loads(entry.read_text(encoding="utf-8"))
    record["payload"] = {"sections": [{"heading": "Decision Log"}]}
    entry.write_text(json.dumps(record), encoding="utf-8")


def _a_directory(entry: Path) -> None:
    entry.unlink()
    entry.mkdir()


@pytest.mark.parametrize(
    "damage",
    [
        _truncated,
        _garbage,
        _not_an_object,
        _hash_of_another_body,
        _hash_missing,
        _payload_of_the_wrong_shape,
        _a_directory,
    ],
)
def test_ac_21_3_a_damaged_entry_is_rebuilt_from_the_source(tmp_path, damage, capsys):
    path = _write_note(tmp_path, "alphaword")
    expected = cli_search(tmp_path, "alphaword", capsys)
    assert "### 0001-note " in expected

    damage(cache._entry_path(path)[0])

    assert cli_search(tmp_path, "alphaword", capsys) == expected


@requires_permission_enforcement
def test_ac_21_3_an_unreadable_entry_is_rebuilt_from_the_source(tmp_path, capsys):
    path = _write_note(tmp_path, "alphaword")
    expected = cli_search(tmp_path, "alphaword", capsys)
    entry = cache._entry_path(path)[0]

    with fs.without_access(entry, restore_mode=0o644):
        found = cli_search(tmp_path, "alphaword", capsys)

    assert found == expected
    assert "### 0001-note " in found


def _separate_paths(root: Path) -> tuple[Path, Path, Callable[[Path], Path]]:
    return root / "store-a", root / "store-b", lambda store: store


def _one_path_swapped(root: Path) -> tuple[Path, Path, Callable[[Path], Path]]:
    """One store path whose directory is swapped for the other store's, names and times kept."""
    live = root / "live"

    def reach(store: Path) -> Path:
        if live.exists():
            live.rename(root / f"parked-{(live / 'owner').read_text(encoding='utf-8')}")
        (root / f"parked-{store.name}").rename(live)
        return live

    return root / "store-a", root / "store-b", reach


def _one_symlink_retargeted(root: Path) -> tuple[Path, Path, Callable[[Path], Path]]:
    link = root / "store-link"

    def reach(store: Path) -> Path:
        if link.is_symlink():
            link.unlink()
        link.symlink_to(store, target_is_directory=True)
        return link

    return root / "store-a", root / "store-b", reach


@pytest.mark.parametrize(
    "layout",
    [
        pytest.param(_separate_paths, id="separate_paths"),
        pytest.param(_one_path_swapped, id="one_path_swapped"),
        pytest.param(_one_symlink_retargeted, id="symlink", marks=requires_symlinks),
    ],
)
def test_ac_21_4_alternating_stores_never_serve_each_others_sections(tmp_path, layout, capsys):
    store_a, store_b, reach = layout(tmp_path)
    words = {store_a: "alphaword", store_b: "bravoword"}
    for store, word in words.items():
        _write_note(store, word)
        (store / "owner").write_text(store.name, encoding="utf-8")
    assert cache.identity_for(store_a / "sessions" / "0001-note.md") == cache.identity_for(
        store_b / "sessions" / "0001-note.md"
    ), "the two notes must be indistinguishable by (mtime, size)"
    if layout is _one_path_swapped:
        for store in words:
            store.rename(tmp_path / f"parked-{store.name}")

    for store in [store_a, store_b, store_a, store_b]:
        path = reach(store)
        own, other = words[store], words[store_a if store is store_b else store_b]
        assert "§1-decision-log=" + own in cli_search(path, own, capsys)
        assert "### 0001-note " not in cli_search(path, other, capsys)
