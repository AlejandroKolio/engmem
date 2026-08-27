"""CLI-surface tests for `engmem backfill`: flags, the confirmation prompt, and the end-to-end
evidence scenarios."""

from __future__ import annotations


import pytest

from conftest import requires_symlinks

from engmem import cli
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


def test_end_of_input_at_the_prompt_is_a_decline(store, capsys, monkeypatch):
    """stdin closed — `backfill --all` behind a pipe, or a cron job with no terminal. An
    unanswered [y/N] is a "no", not a failure, so it exits 0 like typing `n` does."""

    def _raise(prompt=""):
        raise EOFError()

    monkeypatch.setattr("builtins.input", _raise)
    path = store / "sessions" / "widget-cache-warmup.md"
    before = path.read_bytes()

    exit_code, out, err = _run(["--id", "widget-cache-warmup", "--store", str(store)], capsys)

    assert exit_code == 0
    assert "cancelled" in out
    assert path.read_bytes() == before


def test_an_interrupt_at_the_prompt_exits_130_and_writes_nothing(store, capsys, monkeypatch):
    """Ctrl-C is not a decline: exit 0 would run the next command of
    `engmem backfill --all && deploy.sh` because the user pressed Ctrl-C."""

    def _raise(prompt=""):
        raise KeyboardInterrupt()

    monkeypatch.setattr("builtins.input", _raise)
    path = store / "sessions" / "widget-cache-warmup.md"
    before = path.read_bytes()

    exit_code, out, err = _run(["--id", "widget-cache-warmup", "--store", str(store)], capsys)

    assert exit_code == 130
    # a traceback here reads as a crash partway through a write — the one outcome this
    # command's staging exists to make impossible
    assert "interrupted" in out and "interrupted" in err
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
    # the already-complete document was never touched by --all in the first place
    assert by_id["response-cache-complete"].spine_complete is True

    assert by_id["widget-cache-warmup"].backfilled is True
    assert "WidgetCache" in by_id["widget-cache-warmup"].entities
    assert "SweeperJob" in by_id["mq-sweeper-policy"].entities
    # the document with no Search Keywords section still gets the rest of its spine filled
    # in, but `entities` is left unset, not written empty — it stays degraded until a human
    # names its identifiers, which is what the printed note asks for
    assert by_id["cache-invalidation-notes"].entities == []
    assert by_id["cache-invalidation-notes"].spine_complete is False
    assert by_id["cache-invalidation-notes"].degraded_fields == ["entities"]
    assert by_id["cache-invalidation-notes"].backfilled is True

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
    assert "nothing to write" in out
    for p in sessions.iterdir():
        assert p.read_bytes() == snapshot[p.name]


def test_the_entities_note_keeps_being_reported_until_a_human_acts(store, capsys):
    """The document with no keywords section stays on the `--all` list with its note, rather
    than being silently declared complete with an empty `entities`."""
    _run(["--all", "--yes", "--store", str(store)], capsys)

    exit_code, out, err = _run(["--all", "--dry-run", "--store", str(store)], capsys)

    assert exit_code == 0
    assert "cache-invalidation-notes" in out
    assert "no 'Search Keywords' section" in out
    assert "nothing left to write — see the note below" in out


def test_adding_the_missing_section_makes_the_rerun_fill_entities(store, capsys):
    """End to end over the CLI: the note's own instructions have to work."""
    _run(["--all", "--yes", "--store", str(store)], capsys)
    path = store / "sessions" / "cache-invalidation-notes.md"
    path.write_text(
        path.read_text(encoding="utf-8")
        + "\n## Search Keywords\n\n**Classes:** `CacheInvalidator`\n",
        encoding="utf-8",
    )

    exit_code, out, err = _run(["--all", "--yes", "--store", str(store)], capsys)

    assert exit_code == 0
    doc = next(d for d in load_store(store / "sessions").docs if d.id == "cache-invalidation-notes")
    assert doc.entities == ["CacheInvalidator"]
    assert doc.spine_complete is True

    # and with nothing degraded left, `--all` takes the "no targets at all" branch
    exit_code, out, err = _run(["--all", "--yes", "--store", str(store)], capsys)
    assert exit_code == 0
    assert "all already have a complete spine" in out


def test_the_prompt_counts_writable_documents_not_printed_blocks(store, capsys, monkeypatch):
    """A document can be listed with only a note and no field to write, so the count of what
    would be written is a subset of what was printed — the prompt must not claim otherwise."""
    _run(["--all", "--yes", "--store", str(store)], capsys)
    path = store / "sessions" / "widget-cache-warmup.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("id: widget-cache-warmup\n", ""),
        encoding="utf-8",
    )

    prompts: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "n")
    exit_code, out, err = _run(["--all", "--store", str(store)], capsys)

    assert exit_code == 0
    # two documents printed, only one of them writable
    assert "cache-invalidation-notes" in out and "widget-cache-warmup" in out
    assert prompts == ["Write the 1 document(s) with fields to write? [y/N]: "]


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


def test_a_document_edited_while_the_prompt_waits_is_reported_not_written(store, capsys, monkeypatch):
    """The window the staleness check exists for: the author is free to edit while the prompt
    waits."""
    path = store / "sessions" / "cache-invalidation-notes.md"

    def answer_and_edit(prompt=""):
        path.write_text(
            path.read_text(encoding="utf-8") + "\n## Search Keywords\n\n`CacheInvalidator`\n",
            encoding="utf-8",
        )
        return "y"

    monkeypatch.setattr("builtins.input", answer_and_edit)
    exit_code, out, err = _run(["--id", "cache-invalidation-notes", "--store", str(store)], capsys)

    assert exit_code == 2
    assert "changed on disk" in err
    assert "0 document(s) written, 1 failed" in out
    # the author's edit is still there, and the document still has no front matter
    assert "CacheInvalidator" in path.read_text(encoding="utf-8")
    assert not path.read_text(encoding="utf-8").startswith("---")


@requires_symlinks
def test_a_symlinked_sessions_directory_is_refused(tmp_path, capsys):
    """A git checkout carries a symlinked directory as readily as a symlinked file."""
    store = tmp_path / "store"
    store.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    doc = outside / "widget-cache-warmup.md"
    doc.write_text("# Widget Cache Warmup\n\n- Repos: widget-cache\n", encoding="utf-8")
    (store / "sessions").symlink_to(outside)
    before = doc.read_bytes()

    exit_code, out, err = _run(["--all", "--yes", "--store", str(store)], capsys)

    assert exit_code == 2
    assert "does not resolve to" in err and "refusing to write" in err
    assert doc.read_bytes() == before


def test_a_target_unreadable_at_proposal_time_is_reported_not_raised(store, capsys, monkeypatch):
    """`propose_backfill` reads the file again, after `load_store` already did."""
    real_propose = cli.propose_backfill

    def propose_or_fail(doc):
        if doc.id == "widget-cache-warmup":
            raise FileNotFoundError(2, "No such file or directory", str(doc.path))
        return real_propose(doc)

    monkeypatch.setattr(cli, "propose_backfill", propose_or_fail)
    exit_code, out, err = _run(["--all", "--yes", "--store", str(store)], capsys)

    assert exit_code == 2
    assert "widget-cache-warmup: could not read" in err
    # the other degraded documents were still written
    assert "2 document(s) written, 1 failed" in out


def test_an_unreadable_target_costs_the_dry_run_its_exit_code(store, capsys, monkeypatch):
    """`--dry-run` is what a `--dry-run && --yes` script gates on."""
    real_propose = cli.propose_backfill

    def propose_or_fail(doc):
        if doc.id == "widget-cache-warmup":
            raise PermissionError(13, "Permission denied", str(doc.path))
        return real_propose(doc)

    monkeypatch.setattr(cli, "propose_backfill", propose_or_fail)
    exit_code, out, err = _run(["--all", "--dry-run", "--store", str(store)], capsys)

    assert exit_code == 2
    assert "widget-cache-warmup: could not read" in err


def test_a_declined_confirmation_still_reports_a_read_failure(store, capsys, monkeypatch):
    """The read failed before the prompt; it is not what the human declined."""
    real_propose = cli.propose_backfill

    def propose_or_fail(doc):
        if doc.id == "widget-cache-warmup":
            raise PermissionError(13, "Permission denied", str(doc.path))
        return real_propose(doc)

    monkeypatch.setattr(cli, "propose_backfill", propose_or_fail)
    exit_code, out, err = _run(["--all", "--store", str(store)], capsys, monkeypatch, "n")

    assert "cancelled" in out
    assert exit_code == 2
