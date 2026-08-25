"""CLI-surface tests for `engmem backfill`: --id/--all, --dry-run, --yes, the
interactive confirmation prompt, and the end-to-end evidence scenarios named
in the task brief (proposals shown, body byte-identical, an already-complete
document left untouched, re-running is a no-op, `load_store` afterwards
reports `spine_complete`, and a search score improves once entities exist).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from engmem.cli import main
from engmem.scoring import search
from engmem.spine import load_store

LEGACY_WIDGET_CACHE = """# Knowledge Base — Widget Cache Warmup

- Date:        2026-05-04
- Status:      Delivered
- Repos:       widget-cache, platform-core

## Executive Summary

The warmup job preloads WidgetCache on boot.

## Search Keywords

- **Classes:** `WidgetCache`, `WidgetCacheWarmer`, CacheWarmer
- **Endpoints:** /api/v1/widgets · /api/v1/widgets/{id}
- **Concepts:** cold start, cache warmup (background job)

## 13. Future LLM Context (cold-start primer)

WidgetCacheWarmer runs once at startup and populates WidgetCache.
"""

LEGACY_MQ_SWEEPER = """# Knowledge Base — MQ Message Sweeper Policy

- Date: 2026-06-10
- Status: In progress

## Executive Summary

SweeperJob deletes messages past their TTL from the MQ store.

## Search Keywords

Classes: SweeperJob; SweeperPolicy
Endpoints: N/A
Notes: sweeper, cleanup job, TTL

See also [Widget Cache Warmup](widget-cache-warmup.md) for the shared
scheduling primitive.
"""

LEGACY_NO_KEYWORDS = """# Cache Invalidation Notes

- Date: 2026-04-01

## Notes

Plain prose only, no keyword list — nothing here is structured enough to
parse mechanically.
"""

COMPLETE_DOC = """---
id: response-cache-complete
title: Response Cache API
date: 2026-07-01
task_date: 2026-07-01
status: active
superseded_by:
backfilled: false
tags: [platform, caching]
entities: [ResponseCacheController, ResponseCache, ETag, TTL]
related: []
covers_files: [ResponseCacheController.java]
verified_at_commit: abc1234
capture_minutes: 12
---

## Decision Log

Chose ETag over Last-Modified for cache validation.
"""


@pytest.fixture
def store(tmp_path):
    store_dir = tmp_path / "store"
    sessions = store_dir / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "widget-cache-warmup.md").write_text(LEGACY_WIDGET_CACHE, encoding="utf-8")
    (sessions / "mq-sweeper-policy.md").write_text(LEGACY_MQ_SWEEPER, encoding="utf-8")
    (sessions / "cache-invalidation-notes.md").write_text(LEGACY_NO_KEYWORDS, encoding="utf-8")
    (sessions / "response-cache-complete.md").write_text(COMPLETE_DOC, encoding="utf-8")
    return store_dir


def _run(args, capsys, monkeypatch=None, answer=None):
    if monkeypatch is not None and answer is not None:
        monkeypatch.setattr("builtins.input", lambda prompt="": answer)
    exit_code = main(["backfill", *args])
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


# ---------------------------------------------------------------------------
# dry-run: proposals are shown, nothing is written
# ---------------------------------------------------------------------------


def test_dry_run_shows_proposal_and_writes_nothing(store, capsys):
    path = store / "sessions" / "widget-cache-warmup.md"
    before = path.read_bytes()

    exit_code, out, err = _run(["--id", "widget-cache-warmup", "--dry-run", "--store", str(store)], capsys)

    assert exit_code == 0
    assert "widget-cache-warmup" in out
    assert "WidgetCache" in out
    assert "<- " in out  # every field's provenance is shown
    assert "dry run" in out
    assert path.read_bytes() == before


def test_dry_run_all_shows_every_degraded_document_and_skips_the_complete_one(store, capsys):
    exit_code, out, err = _run(["--all", "--dry-run", "--store", str(store)], capsys)

    assert exit_code == 0
    assert "widget-cache-warmup" in out
    assert "mq-sweeper-policy" in out
    assert "cache-invalidation-notes" in out
    # the already-complete document is never even proposed for --all
    assert "response-cache-complete" not in out


def test_id_not_found_fails_loud(store, capsys):
    exit_code, out, err = _run(["--id", "does-not-exist", "--dry-run", "--store", str(store)], capsys)

    assert exit_code == 2
    assert "does-not-exist" in out
    assert "error" in out


# ---------------------------------------------------------------------------
# the already-complete document: untouched, said so explicitly
# ---------------------------------------------------------------------------


def test_already_complete_document_reports_nothing_to_backfill_and_is_untouched(store, capsys):
    path = store / "sessions" / "response-cache-complete.md"
    before = path.read_bytes()

    exit_code, out, err = _run(
        ["--id", "response-cache-complete", "--store", str(store)], capsys
    )

    assert exit_code == 0
    assert "spine already complete" in out
    assert "nothing to write" in out
    assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# confirmation interaction: cancel vs confirm vs --yes
# ---------------------------------------------------------------------------


def test_declining_the_confirmation_writes_nothing(store, capsys, monkeypatch):
    path = store / "sessions" / "widget-cache-warmup.md"
    before = path.read_bytes()

    exit_code, out, err = _run(
        ["--id", "widget-cache-warmup", "--store", str(store)],
        capsys,
        monkeypatch=monkeypatch,
        answer="n",
    )

    assert exit_code == 0
    assert "cancelled" in out
    assert path.read_bytes() == before


def test_confirming_writes_the_document(store, capsys, monkeypatch):
    path = store / "sessions" / "widget-cache-warmup.md"
    before = path.read_bytes()

    exit_code, out, err = _run(
        ["--id", "widget-cache-warmup", "--store", str(store)],
        capsys,
        monkeypatch=monkeypatch,
        answer="y",
    )

    assert exit_code == 0
    assert "backfilled" in out
    assert path.read_bytes() != before


def test_yes_flag_skips_the_prompt_entirely(store, capsys, monkeypatch):
    # if the prompt were still consulted, an unset/absent answer would raise
    # or hang — patch input to explode, proving --yes never calls it
    def _boom(prompt=""):
        raise AssertionError("input() must not be called when --yes is passed")

    monkeypatch.setattr("builtins.input", _boom)

    exit_code, out, err = _run(["--id", "widget-cache-warmup", "--yes", "--store", str(store)], capsys)

    assert exit_code == 0
    assert "--yes" in out
    assert "backfilled" in out


def test_eof_on_the_prompt_is_treated_as_decline(store, capsys, monkeypatch):
    def _eof(prompt=""):
        raise EOFError()

    monkeypatch.setattr("builtins.input", _eof)
    path = store / "sessions" / "widget-cache-warmup.md"
    before = path.read_bytes()

    exit_code, out, err = _run(["--id", "widget-cache-warmup", "--store", str(store)], capsys)

    assert exit_code == 0
    assert "cancelled" in out
    assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# full evidence run: --all --yes, then verify every claim in the task brief
# ---------------------------------------------------------------------------


def test_all_yes_backfills_every_degraded_document(store, capsys):
    exit_code, out, err = _run(["--all", "--yes", "--store", str(store)], capsys)

    assert exit_code == 0
    assert "3 document(s) written, 0 failed" in out

    result = load_store(store / "sessions")
    assert result.errors == []
    by_id = {d.id: d for d in result.docs}

    assert by_id["widget-cache-warmup"].spine_complete is True
    assert by_id["mq-sweeper-policy"].spine_complete is True
    assert by_id["cache-invalidation-notes"].spine_complete is True
    # the already-complete document was never touched by --all in the first place
    assert by_id["response-cache-complete"].spine_complete is True

    assert by_id["widget-cache-warmup"].backfilled is True
    assert "WidgetCache" in by_id["widget-cache-warmup"].entities
    assert "SweeperJob" in by_id["mq-sweeper-policy"].entities
    # the document with no Search Keywords section still gets the rest of its
    # spine filled in — only entities is empty
    assert by_id["cache-invalidation-notes"].entities == []
    assert by_id["cache-invalidation-notes"].spine_complete is True

    # related, derived from the markdown link in mq-sweeper-policy's body
    assert "widget-cache-warmup" in by_id["mq-sweeper-policy"].related


def test_body_is_byte_identical_after_the_real_write(store, capsys):
    path = store / "sessions" / "widget-cache-warmup.md"
    original_full_text = path.read_text(encoding="utf-8")

    _run(["--id", "widget-cache-warmup", "--yes", "--store", str(store)], capsys)

    after_text = path.read_text(encoding="utf-8")
    lines = after_text.splitlines(keepends=True)
    closing = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    body_after = "".join(lines[closing + 1:])
    assert body_after == original_full_text


def test_already_complete_document_untouched_across_an_all_yes_run(store, capsys):
    path = store / "sessions" / "response-cache-complete.md"
    before = path.read_bytes()

    _run(["--all", "--yes", "--store", str(store)], capsys)

    assert path.read_bytes() == before


def test_rerunning_all_yes_after_a_backfill_is_a_noop(store, capsys):
    _run(["--all", "--yes", "--store", str(store)], capsys)
    sessions = store / "sessions"
    snapshot = {p.name: p.read_bytes() for p in sessions.iterdir()}

    exit_code, out, err = _run(["--all", "--yes", "--store", str(store)], capsys)

    assert exit_code == 0
    assert "all already have a complete spine" in out
    for p in sessions.iterdir():
        assert p.read_bytes() == snapshot[p.name]


def test_search_score_improves_once_entities_are_backfilled(store, capsys):
    sessions = store / "sessions"

    before_docs = load_store(sessions).docs
    before_outcome = search(before_docs, "WidgetCache")
    before_hit = next(h for h in before_outcome.hits if h.doc.id == "widget-cache-warmup")
    before_score = before_hit.score
    assert "entities" not in before_hit.matched_fields

    _run(["--id", "widget-cache-warmup", "--yes", "--store", str(store)], capsys)

    after_docs = load_store(sessions).docs
    after_outcome = search(after_docs, "WidgetCache")
    after_hit = next(h for h in after_outcome.hits if h.doc.id == "widget-cache-warmup")

    assert "entities" in after_hit.matched_fields
    assert after_hit.score > before_score
