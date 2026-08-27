"""Markdown in the store root is never searched; the first dogfooding session reported `none
found` while the answer sat on disk."""

import os
import shutil

import pytest

from conftest import (
    FIXTURES,
    requires_permission_enforcement,
)

from engmem.cli import main


STRAY_DOC = """---
id: my-old-notes
title: Notes I wrote before installing engmem
date: 2026-05-01
task_date: 2026-05-01
status: active
superseded_by:
backfilled: true
tags: [platform]
entities: [SweeperJob]
related: []
covers_files: []
verified_at_commit: abc1234
capture_minutes: 0
---

## Cold-start primer

Notes that live in the wrong directory.
"""


@pytest.fixture
def store(tmp_path):
    store_dir = tmp_path / "store"
    shutil.copytree(FIXTURES, store_dir / "sessions")
    return store_dir


@pytest.fixture
def empty_store(tmp_path):
    store_dir = tmp_path / "store"
    (store_dir / "sessions").mkdir(parents=True)
    return store_dir


def _run(store, query, capsys):
    code = main(["search", query, "--store", str(store)])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.parametrize(
    "relative_path",
    [
        pytest.param("my-old-notes.md", id="store-root"),
        pytest.param("sessions/2026-archive/my-old-notes.md", id="nested-under-sessions"),
    ],
)
def test_stray_markdown_is_reported(store, capsys, relative_path):
    """The store scan is flat — `iterdir`, no descent — so a document foldered one directory
    deeper is loaded no more than a stray sitting in the store root is."""
    path = store / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(STRAY_DOC, encoding="utf-8")

    code, out, err = _run(store, "platform", capsys)

    assert code == 0
    assert "my-old-notes.md" in err, "the stray file must be named, not just counted"
    assert "sessions" in err, "the message must say where documents belong"


@pytest.mark.parametrize(
    "relative_paths, expected_count",
    [
        pytest.param(["my-old-notes.md", "another-doc.md"], 2, id="store-root"),
        pytest.param(["sessions/2026-archive/my-old-notes.md"], 1, id="nested-under-sessions"),
    ],
)
def test_none_found_points_at_strays_on_stdout(empty_store, capsys, relative_paths, expected_count):
    """The agent reads stdout, and a warning only on stderr is how this went unnoticed the first
    time — true whether the stray sits in the store root or nested under `sessions/`."""
    for relative_path in relative_paths:
        path = empty_store / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(STRAY_DOC, encoding="utf-8")

    code, out, err = _run(empty_store, "platform", capsys)

    assert code == 0
    assert "prior context: none found" in out
    assert f"{expected_count} markdown file(s) sit outside the searched set" in out
    assert "sessions" in out


def test_clean_store_says_nothing_about_strays(store, capsys):
    code, out, err = _run(store, "platform", capsys)

    assert code == 0
    assert "store root" not in err.lower()
    assert "not searched" not in out.lower()


def test_readme_in_store_root_is_not_a_stray(store, capsys):
    """`docs/store-readme.md` is meant to be copied there, so warning about it would be noise."""
    (store / "README.md").write_text("# My store\n", encoding="utf-8")

    code, out, err = _run(store, "platform", capsys)

    assert code == 0
    assert "README" not in err


def test_root_strays_and_nested_docs_are_counted_together(empty_store, capsys):
    (empty_store / "root-note.md").write_text(STRAY_DOC, encoding="utf-8")
    nested = empty_store / "sessions" / "2026-archive"
    nested.mkdir()
    (nested / "nested-note.md").write_text(STRAY_DOC, encoding="utf-8")

    code, out, err = _run(empty_store, "platform", capsys)

    assert code == 0
    assert "root-note.md" in err
    assert "nested-note.md" in err
    assert "2 markdown file(s) sit outside the searched set" in out


def test_nested_non_markdown_is_not_reported(store, capsys):
    """Attachments and images filed beside a store are not lost documents."""
    nested = store / "sessions" / "assets"
    nested.mkdir()
    (nested / "diagram.png").write_bytes(b"\x89PNG\r\n")

    code, out, err = _run(store, "platform", capsys)

    assert code == 0
    assert "diagram" not in err


def test_no_match_without_strays_keeps_the_bare_message(empty_store, capsys):
    code, out, err = _run(empty_store, "anything", capsys)

    assert code == 0
    assert out.strip().splitlines()[0] == "prior context: none found"
    assert "not searched" not in out.lower()


@requires_permission_enforcement
def test_unreadable_nested_subdirectory_is_reported_not_treated_as_clean(store, capsys):
    """D2 one level up: `os.walk` skips an unreadable directory in silence, so without its
    `onerror` callback a locked subdirectory would read as "no strays"."""
    locked = store / "sessions" / "locked-archive"
    locked.mkdir()
    (locked / "my-old-notes.md").write_text(STRAY_DOC, encoding="utf-8")
    os.chmod(locked, 0o000)
    try:
        code, out, err = _run(store, "platform", capsys)
    finally:
        os.chmod(locked, 0o755)

    assert code == 0
    assert "cannot list directory for stray documents" in out
    assert "cannot list directory for stray documents" in err
