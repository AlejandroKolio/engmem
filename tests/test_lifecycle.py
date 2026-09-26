"""Contract test: proves `spine.py` already accepts the full documented `/engmem.save` output
shape."""

import re
from pathlib import Path

from conftest import write_file

from engmem.output import render_scoreboard
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


START_TEMPLATE = (
    Path(__file__).resolve().parent.parent / "src" / "engmem" / "templates" / "engmem.start.md"
)
PLACEHOLDERS = {
    "<YYYYMMDD>-<slug>": "20260819-fresh-draft",
    "<short title from the task description>": "Fresh Draft Task",
    "<today>": "2026-08-19",
}


def _template_draft() -> str:
    """The draft exactly as the start template tells an agent to write it: a hand copy of the
    front matter kept passing while a typo in the template itself published drafts as active."""
    # a Windows checkout may carry CRLF, and the repository pins no line endings
    text = START_TEMPLATE.read_text(encoding="utf-8").replace("\r\n", "\n")
    heading = "## 2. Create the draft immediately"
    assert heading in text, f"start template: {heading!r} is gone — update _template_draft"
    fence = re.search(r"```yaml\n(.*?)```", text[text.index(heading):], re.DOTALL)
    assert fence, "start template: no yaml fence under step 2 — update _template_draft"
    front_matter = fence.group(1)
    for placeholder, value in PLACEHOLDERS.items():
        front_matter = front_matter.replace(placeholder, value)
    return front_matter + "\n## Pre-reg\n\nWarm the cache first; measure the first request.\n"


def _assert_parses_as_a_draft(sessions_dir: Path) -> None:
    result = load_store(sessions_dir)

    assert result.errors == []
    assert len(result.docs) == 1
    doc = result.docs[0]
    assert doc.status == "draft"
    assert doc.id == "20260819-fresh-draft"
    assert "drafts: 1" in render_scoreboard(result.docs)


def test_the_start_templates_draft_parses_as_a_draft(tmp_path):
    write_file(tmp_path, "20260819-fresh-draft.md", _template_draft())

    _assert_parses_as_a_draft(tmp_path)


def test_the_start_templates_draft_parses_with_crlf_and_a_bom(tmp_path):
    crlf = _template_draft().replace("\n", "\r\n")
    (tmp_path / "20260819-fresh-draft.md").write_bytes(b"\xef\xbb\xbf" + crlf.encode("utf-8"))

    _assert_parses_as_a_draft(tmp_path)


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
