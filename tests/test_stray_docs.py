"""Markdown sitting in the store root is never searched — engmem only reads `sessions/`.

Found during the first real dogfooding session: the author's documents lived in the store
root, `engmem search` reported `prior context: none found`, and the agent then answered
the question by reading those very files directly. The tool had contributed nothing while
appearing to participate — the worst possible failure, because the session was afterwards
counted as a partial success.

Constitution VIII: this must be loud, not silent.
"""

import os
import shutil
import sys
from pathlib import Path

import pytest

from conftest import requires_unreadable_paths

from engmem.cli import main

FIXTURES = Path(__file__).parent / "fixtures" / "sessions"

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


def test_stray_markdown_in_store_root_is_reported(store, capsys):
    (store / "my-old-notes.md").write_text(STRAY_DOC, encoding="utf-8")

    code, out, err = _run(store, "platform", capsys)

    assert code == 0
    assert "my-old-notes.md" in err, "the stray file must be named, not just counted"
    assert "sessions" in err, "the message must say where documents belong"


def test_none_found_points_at_stray_docs_on_stdout(empty_store, capsys):
    """The misleading case: nothing in sessions/, documents in the root. The agent reads
    stdout; a warning only on stderr is how this went unnoticed the first time."""
    (empty_store / "my-old-notes.md").write_text(STRAY_DOC, encoding="utf-8")
    (empty_store / "another-doc.md").write_text(STRAY_DOC, encoding="utf-8")

    code, out, err = _run(empty_store, "platform", capsys)

    assert code == 0
    assert "prior context: none found" in out
    assert "2 markdown file(s) sit outside the searched set" in out
    assert "sessions" in out


def test_clean_store_says_nothing_about_strays(store, capsys):
    code, out, err = _run(store, "platform", capsys)

    assert code == 0
    assert "store root" not in err.lower()
    assert "not searched" not in out.lower()


def test_readme_in_store_root_is_not_a_stray(store, capsys):
    """`docs/store-readme.md` is meant to be copied to the store root as README.md —
    warning about the file we told the user to put there would be noise."""
    (store / "README.md").write_text("# My store\n", encoding="utf-8")

    code, out, err = _run(store, "platform", capsys)

    assert code == 0
    assert "README" not in err


def test_markdown_nested_under_sessions_is_reported(store, capsys):
    """Filing documents into `sessions/2026-archive/` is the natural move once a store
    grows. `glob("*.md")` does not descend, so those documents vanish — same silent loss
    as the store root, one directory deeper."""
    nested = store / "sessions" / "2026-archive"
    nested.mkdir()
    (nested / "my-old-notes.md").write_text(STRAY_DOC, encoding="utf-8")

    code, out, err = _run(store, "platform", capsys)

    assert code == 0
    assert "my-old-notes.md" in err, "the unreachable file must be named, not just counted"


def test_none_found_points_at_nested_docs_on_stdout(empty_store, capsys):
    nested = empty_store / "sessions" / "2026-archive"
    nested.mkdir()
    (nested / "my-old-notes.md").write_text(STRAY_DOC, encoding="utf-8")

    code, out, err = _run(empty_store, "platform", capsys)

    assert code == 0
    assert "prior context: none found" in out
    assert "1 markdown file(s) sit outside the searched set" in out


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


@requires_unreadable_paths
def test_unreadable_nested_subdirectory_is_reported_not_treated_as_clean(store, capsys):
    """D2, applied one level up: `Path.rglob` swallows the `PermissionError` a locked
    subdirectory under sessions/ raises, so a stray document sitting behind it used to
    be reported as "no strays" — the same silent loss `_stray_documents` itself exists
    to catch, just from a different filesystem call."""
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
