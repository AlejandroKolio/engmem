"""Round-trips a document shaped exactly as contracts/prompt-templates.md's "finish"
section and data-model.md's Front matter fields / Body sections require /engmem.save to
produce, through spine.py's parser.

This is a contract test, not a red-before-green TDD test in the usual sense: /engmem.save
is a markdown prompt template (Phase 5), not Python code, so there is no implementation
task that makes this test transition from failing to passing. Its purpose is (a) to prove
spine.py already accepts the full documented save-output shape, and (b) to serve as the
executable ground truth the save-template checklist (checklists/save-template.md) is
verified against.
"""

from engmem.spine import load_store

SAVE_SHAPED_DOC = """---
id: 20260819-lifecycle-check
title: Lifecycle Round-Trip Check
date: 2026-08-19
task_date: 2026-08-19
status: active
superseded_by:
backfilled: false
tags: [testing]
entities: [LifecycleCheck]
related:
  - 1000001-response-cache
covers_files: [test_lifecycle.py]
verified_at_commit: cafe123
capture_minutes: 7
---

## Pre-reg

Verify that a document shaped exactly like /engmem.save's output parses cleanly.

## Decision Log

Hand-built this fixture instead of invoking a live template; rejected waiting for a live
Claude Code run because the harness can't self-invoke a just-written slash command.

## Landmines

None hit.

## Cold-start primer

This document exists purely to exercise spine.py's parser against the full documented
save-output shape ahead of the save template's own live verification.

## Reuse Log

| prior-doc | taken | impact | classification |
|---|---|---|---|
| 1000001-response-cache | Reused its ETAG/TTL header names as the naming example — "Reused the ETAG and TTL headers the upstream service already sends" | Confirmed the field-naming convention to follow | reuse |

## Search Trace

shell
"""


DRAFT_SHAPED_DOC = """---
id: 20260819-fresh-draft
title: Fresh Draft Task
date: 2026-08-19
task_date: 2026-08-19
status: draft
superseded_by:
backfilled: false
tags: []
entities: []
related: []
covers_files: []
verified_at_commit:
capture_minutes: 0
---

## Pre-reg

Two or three lines of intended approach, written before any search.
"""


def test_draft_shaped_document_parses_and_counts_in_scoreboard(tmp_path):
    """The exact placeholder front matter engmem.start.md instructs the agent to write
    (per the C3 review decision: full front matter with placeholders, spine stays strict)
    must parse with zero errors and register as a draft."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "20260819-fresh-draft.md").write_text(DRAFT_SHAPED_DOC)

    result = load_store(sessions_dir)

    assert result.errors == []
    assert len(result.docs) == 1
    doc = result.docs[0]
    assert doc.status == "draft"
    assert doc.capture_minutes == 0
    assert doc.verified_at_commit == ""

    from engmem.output import render_scoreboard

    assert "drafts: 1" in render_scoreboard(result.docs)


def test_save_shaped_document_round_trips_through_spine(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "20260819-lifecycle-check.md").write_text(SAVE_SHAPED_DOC, encoding="utf-8")

    result = load_store(sessions_dir)

    assert result.errors == []
    assert result.warnings == []
    assert len(result.docs) == 1

    doc = result.docs[0]
    assert doc.status == "active"
    assert doc.related == ["1000001-response-cache"]
    assert "## Reuse Log" in doc.body
    assert "## Search Trace" in doc.body
