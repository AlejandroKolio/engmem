import datetime

from conftest import write_file

from engmem.output import render_scoreboard
from engmem.scoring import search
from engmem.spine import load_store

LEGACY_NO_FRONT_MATTER = """# Knowledge Base — Widget Cache Warmup

- Date:        2026-05-04
- Author:      A. Author
- Status:      Delivered

## 1. Executive Summary

The warmup job preloads WidgetCache on boot so the first request does not pay the
cold-lookup penalty.

## 13. Future LLM Context (cold-start primer)

WidgetCacheWarmer runs once at startup and populates WidgetCache from WidgetRepository.
"""


def test_document_without_front_matter_loads_with_derived_id_and_title(tmp_path):
    write_file(tmp_path, "widget-cache-warmup.md", LEGACY_NO_FRONT_MATTER)

    result = load_store(tmp_path)

    assert result.errors == []
    assert len(result.docs) == 1
    doc = result.docs[0]
    assert doc.id == "widget-cache-warmup"
    assert doc.title == "Widget Cache Warmup"
    assert "## 1. Executive Summary" in doc.body


def test_partial_front_matter_keeps_authored_values_and_defaults_the_rest(tmp_path):
    write_file(
        tmp_path,
        "widget-cache-warmup.md",
        """---
id: widget-cache-warmup
title: Widget Cache Warmup
date: 2026-05-04
tags: [platform, cache]
entities: [WidgetCache, WidgetCacheWarmer]
---

## 1. Executive Summary

Preloads the cache on boot.
""",
    )

    result = load_store(tmp_path)

    assert result.errors == []
    doc = result.docs[0]
    assert doc.tags == ["platform", "cache"]
    assert doc.entities == ["WidgetCache", "WidgetCacheWarmer"]
    assert doc.status == "active"
    assert doc.task_date == doc.date
    assert doc.backfilled is False
    assert doc.capture_minutes is None


def test_date_is_derived_from_preamble_when_front_matter_has_none(tmp_path):
    write_file(tmp_path, "widget-cache-warmup.md", LEGACY_NO_FRONT_MATTER)

    doc = load_store(tmp_path).docs[0]

    assert str(doc.date) == "2026-05-04"
    assert str(doc.task_date) == "2026-05-04"


def test_bold_and_updated_preamble_date_spellings_are_recognised(tmp_path):
    write_file(
        tmp_path,
        "bold-date.md",
        "# Bold Date\n\n- **Date:** 2026-05-05 (last updated) · **Author:** A. Author\n",
    )
    write_file(tmp_path, "updated-date.md", "# Updated Date\n\n- Updated:  2026-05-06\n")

    docs = {d.id: d for d in load_store(tmp_path).docs}

    assert str(docs["bold-date"].date) == "2026-05-05"
    assert str(docs["updated-date"].date) == "2026-05-06"


def test_date_falls_back_to_mtime_when_nothing_else_states_one(tmp_path):
    path = write_file(tmp_path, "no-date-anywhere.md", "# No Date Anywhere\n\nBody.\n")
    expected = datetime.date.fromtimestamp(path.stat().st_mtime)

    doc = load_store(tmp_path).docs[0]

    assert doc.date == expected


def test_degraded_document_defaults_to_active_and_is_searchable(tmp_path):
    write_file(tmp_path, "widget-cache-warmup.md", LEGACY_NO_FRONT_MATTER)

    docs = load_store(tmp_path).docs
    assert docs[0].status == "active"

    outcome = search(docs, "widget cache warmup")
    assert [hit.doc.id for hit in outcome.hits] == ["widget-cache-warmup"]


def test_degraded_fields_are_recorded_on_the_doc(tmp_path):
    write_file(tmp_path, "widget-cache-warmup.md", LEGACY_NO_FRONT_MATTER)

    doc = load_store(tmp_path).docs[0]

    assert doc.spine_complete is False
    assert "entities" in doc.degraded_fields
    assert "status" in doc.degraded_fields
    assert "id" in doc.degraded_fields


def test_complete_document_is_not_marked_degraded(tmp_path):
    write_file(
        tmp_path,
        "widget-cache-warmup.md",
        """---
id: widget-cache-warmup
title: Widget Cache Warmup
date: 2026-05-04
task_date: 2026-05-04
status: active
superseded_by:
backfilled: false
tags: [platform]
entities: [WidgetCache]
related: []
covers_files: []
verified_at_commit: 1a2b3c4
capture_minutes: 7
---

## Pre-reg

Body.
""",
    )

    doc = load_store(tmp_path).docs[0]

    assert doc.spine_complete is True
    assert doc.degraded_fields == []


def test_malformed_yaml_is_still_a_loud_error(tmp_path):
    write_file(
        tmp_path,
        "bad-yaml.md",
        "---\nid: bad-yaml\ntitle: Bad YAML\ntags: [platform\n---\n\n## Pre-reg\n",
    )

    result = load_store(tmp_path)

    assert result.docs == []
    assert len(result.errors) == 1


def test_present_but_empty_fields_degrade_instead_of_failing(tmp_path):
    # `date:` with nothing after it states no date — identical in meaning to omitting the key.
    write_file(
        tmp_path,
        "widget-cache-warmup.md",
        """---
id:
title:
date:
status:
---

# Widget Cache Warmup

- Date: 2026-05-04

## 1. Executive Summary

Body.
""",
    )

    result = load_store(tmp_path)

    assert result.errors == []
    doc = result.docs[0]
    assert doc.id == "widget-cache-warmup"
    assert doc.title == "Widget Cache Warmup"
    assert str(doc.date) == "2026-05-04"
    assert doc.status == "active"
    assert {"id", "title", "date", "status"} <= set(doc.degraded_fields)


def test_utf8_bom_does_not_hide_a_complete_front_matter(tmp_path):
    # a BOM makes the first line "﻿---", which silently demoted an entire
    # hand-written spine to body text
    path = tmp_path / "widget-cache-warmup.md"
    path.write_text(
        """---
id: widget-cache-warmup
title: Widget Cache Warmup
date: 2026-05-04
task_date: 2026-05-04
status: active
superseded_by:
backfilled: false
tags: [platform]
entities: [WidgetCache]
related: []
covers_files: []
verified_at_commit: 1a2b3c4
capture_minutes: 7
---

## Pre-reg

Body.
""",
        encoding="utf-8-sig",
    )

    doc = load_store(tmp_path).docs[0]

    assert doc.spine_complete is True
    assert doc.entities == ["WidgetCache"]
    assert doc.title == "Widget Cache Warmup"


def test_front_matter_that_is_not_a_mapping_is_a_collected_error(tmp_path):
    # a YAML scalar or list between the delimiters is corruption, not incompleteness:
    # it must not take the whole store down with an uncaught exception
    write_file(tmp_path, "scalar-fm.md", "---\njust a bare string\n---\n\n## Pre-reg\n")
    write_file(tmp_path, "list-fm.md", "---\n- one\n- two\n---\n\n## Pre-reg\n")
    write_file(tmp_path, "widget-cache-warmup.md", LEGACY_NO_FRONT_MATTER)

    result = load_store(tmp_path)

    assert len(result.errors) == 2
    assert [d.id for d in result.docs] == ["widget-cache-warmup"]


def test_garbage_date_is_still_a_loud_error(tmp_path):
    write_file(
        tmp_path,
        "bad-date.md",
        "---\nid: bad-date\ntitle: Bad Date\ndate: not-a-date\n---\n\n## Pre-reg\n",
    )

    result = load_store(tmp_path)

    assert result.docs == []
    assert len(result.errors) == 1
    assert "date" in result.errors[0].message


def test_duplicate_id_is_still_a_loud_error_even_when_derived(tmp_path):
    write_file(tmp_path, "widget-cache-warmup.md", "# Widget Cache Warmup\n\nBody.\n")
    write_file(
        tmp_path,
        "other-file.md",
        "---\nid: widget-cache-warmup\ntitle: Clashing Id\n---\n\n## Pre-reg\n",
    )

    result = load_store(tmp_path)

    dup = [e for e in result.errors if "duplicate" in e.message.lower()]
    assert len(dup) == 1
    assert len(result.docs) == 1


def test_scoreboard_reports_the_partial_spine_count(tmp_path):
    write_file(tmp_path, "widget-cache-warmup.md", LEGACY_NO_FRONT_MATTER)
    write_file(
        tmp_path,
        "queue-backpressure.md",
        """---
id: queue-backpressure
title: Queue Backpressure
date: 2026-05-04
task_date: 2026-05-04
status: active
superseded_by:
backfilled: false
tags: [platform]
entities: [QueueConsumer]
related: []
covers_files: []
verified_at_commit: 1a2b3c4
capture_minutes: 7
---

## Pre-reg

Body.
""",
    )

    footer = render_scoreboard(load_store(tmp_path).docs)

    assert "docs: 2 (1 partial spine)" in footer


def test_scoreboard_omits_the_partial_count_when_every_spine_is_complete():
    from pathlib import Path

    fixtures = Path(__file__).parent / "fixtures" / "sessions"
    docs = [d for d in load_store(fixtures).docs if d.spine_complete]

    footer = render_scoreboard(docs)

    assert "partial spine" not in footer


def test_empty_document_is_a_warning_not_a_silent_member_of_the_store(tmp_path):
    """A zero-content file inflates `docs: N`, the number the agent trusts, and can never match
    anything."""
    (tmp_path / "blank.md").write_text("", encoding="utf-8")
    (tmp_path / "whitespace-only.md").write_text("\n\n\n", encoding="utf-8")
    write_file(tmp_path, "widget-cache-warmup.md", LEGACY_NO_FRONT_MATTER)

    result = load_store(tmp_path)

    empty_warnings = [w for w in result.warnings if "document is empty" in w.message]
    named = " ".join(w.message for w in empty_warnings)
    assert "blank.md" in named
    assert "whitespace-only.md" in named
    assert "widget-cache-warmup.md" not in named


DRAFT_AS_THE_SHIPPED_TEMPLATE_WRITES_IT = """---
id: 20260823-widget-cache-eviction
title: Widget cache eviction
date: 2026-08-23
task_date: 2026-08-23
status: draft
superseded_by:
backfilled: false
tags: []
entities: []
related: []
covers_files: []
verified_at_commit:
capture_minutes:
---

## Pre-reg

Naive baseline written before opening the store.
"""


def test_a_draft_written_by_the_shipped_template_is_not_flagged_degraded(tmp_path):
    """Flagging every draft the tool's own command creates trains the reader to ignore the signal."""
    write_file(tmp_path, "20260823-widget-cache-eviction.md", DRAFT_AS_THE_SHIPPED_TEMPLATE_WRITES_IT)

    doc = load_store(tmp_path).docs[0]

    assert doc.spine_complete is True
    assert doc.degraded_fields == []


def test_absent_telemetry_fields_do_not_count_as_spine_damage(tmp_path):
    write_file(
        tmp_path,
        "widget-cache-warmup.md",
        """---
id: widget-cache-warmup
title: Widget Cache Warmup
date: 2026-05-04
task_date: 2026-05-04
status: active
tags: [platform]
entities: [WidgetCache]
---

## Pre-reg

Body.
""",
    )

    doc = load_store(tmp_path).docs[0]

    assert doc.spine_complete is True
    assert doc.capture_minutes is None
    assert doc.verified_at_commit == ""


def test_absent_retrieval_fields_still_count_as_spine_damage(tmp_path):
    """entities and tags carry search weight, so their absence really does degrade ranking, unlike
    telemetry."""
    write_file(
        tmp_path,
        "widget-cache-warmup.md",
        "---\nid: widget-cache-warmup\ntitle: Widget Cache Warmup\ndate: 2026-05-04\n---\n\n## Pre-reg\n",
    )

    doc = load_store(tmp_path).docs[0]

    assert doc.spine_complete is False
    assert {"entities", "tags", "status", "task_date"} <= set(doc.degraded_fields)


def test_shipped_draft_template_does_not_claim_zero_capture_minutes():
    """`0` means measured as instantaneous; a draft has measured nothing, so the value must read
    as null."""
    from importlib import resources

    template = (resources.files("engmem") / "templates" / "engmem.start.md").read_text(
        encoding="utf-8"
    )

    assert "capture_minutes: 0" not in template
    assert "capture_minutes:" in template
