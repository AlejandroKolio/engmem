"""Unit tests for `engmem.backfill`: entity/tag/related extraction, the
already-complete no-op gate, and atomic body-preserving writes.

CLI-surface tests (--id/--all/--dry-run/--yes, the confirmation prompt, and
the end-to-end evidence scenarios) live in `tests/test_backfill_cli.py`.
"""

from __future__ import annotations

import hashlib
import datetime

import pytest
import yaml

from engmem.backfill import (
    _strip_bullet,
    _extract_label_and_rest,
    BackfillWriteError,
    apply_backfill,
    propose_backfill,
    _derive_repo_tags,
    _derive_status,
    _extract_entities,
    _extract_related_ids,
    _looks_like_entity,
)
from engmem.spine import load_store, _parse_one


def _write(dir_path, name, text):
    path = dir_path / name
    path.write_text(text, encoding="utf-8")
    return path


LEGACY_WIDGET_CACHE = """# Knowledge Base — Widget Cache Warmup

- Date:        2026-05-04
- Author:      A. Author
- Status:      Delivered
- Repos:       widget-cache, platform-core

## Executive Summary

The warmup job preloads WidgetCache on boot so the first request does not pay
the cold-lookup penalty.

## Search Keywords

- **Classes:** `WidgetCache`, `WidgetCacheWarmer`, CacheWarmer
- **Endpoints:** /api/v1/widgets · /api/v1/widgets/{id}
- **Concepts:** cold start, cache warmup (background job)

## 13. Future LLM Context (cold-start primer)

WidgetCacheWarmer runs once at startup and populates WidgetCache from
WidgetRepository.
"""


# ---------------------------------------------------------------------------
# entity/tag extraction — unit-level, exercising the rule directly
# ---------------------------------------------------------------------------


def test_backticked_terms_are_always_accepted_as_entities():
    entities = _extract_entities(
        "- **Classes:** `WidgetCache`, `WidgetCacheWarmer`, CacheWarmer"
    )
    assert entities == ["WidgetCache", "WidgetCacheWarmer", "CacheWarmer"]


def test_endpoint_paths_are_accepted_via_the_slash_signal():
    entities = _extract_entities(
        "- **Endpoints:** /api/v1/widgets · /api/v1/widgets/{id}"
    )
    assert entities == ["/api/v1/widgets", "/api/v1/widgets/{id}"]


def test_lowercase_descriptive_prose_is_rejected_false_negative_by_design():
    entities = _extract_entities(
        "- **Concepts:** cold start, cache warmup (background job)"
    )
    assert entities == []


def test_na_is_denylisted_despite_matching_the_slash_shape():
    entities = _extract_entities("Endpoints: N/A")
    assert entities == []


def test_plain_non_bold_label_is_recognised_and_discarded():
    """A plain `Label:` still has to be recognised so its terms are read as terms
    and not as part of the label — but the label itself is scaffolding, not a tag."""
    entities = _extract_entities(
        "Classes: SweeperJob; SweeperPolicy"
    )
    assert entities == ["SweeperJob", "SweeperPolicy"]


def test_allcaps_acronym_standing_alone_is_accepted():
    entities = _extract_entities("Notes: sweeper, cleanup job, TTL")
    assert "TTL" in entities
    assert "sweeper" not in entities
    assert "cleanup job" not in entities


def test_facet_labels_are_discarded_not_turned_into_terms():
    """A facet label names the shape of a group ("HTTP Methods", "Classes"), which nearly
    every document in this genre has — a weight-1 tag built from one distinguishes nothing.
    Tags come from the `- Repos:` line alone, so the label is dropped entirely rather than
    surviving as a term."""
    entities = _extract_entities(
        "- **HTTP Methods:** GET, POST\n- **Classes:** `WidgetCache`"
    )
    assert "WidgetCache" in entities
    assert not [e for e in entities if "Classes" in e or "HTTP Methods" in e]


def test_entities_and_tags_are_order_preserving_and_deduplicated():
    entities = _extract_entities(
        "- **Classes:** WidgetCache, CacheWarmer, WidgetCache"
    )
    assert entities == ["WidgetCache", "CacheWarmer"]


def test_looks_like_entity_rejects_a_bare_titlecase_word():
    # a single Title-Case word alone is indistinguishable from ordinary
    # English prose ("Cache" the common noun vs. a class literally named
    # `Cache`) — documented false negative, not a bug
    assert not _looks_like_entity("Cache")


def test_looks_like_entity_accepts_multiword_titlecase_phrase():
    # a second capitalised word signals a proper-noun-shaped phrase rather
    # than a single ordinary noun — documented, deliberately permissive
    assert _looks_like_entity("Response Cache")


def test_looks_like_entity_rejects_a_five_word_phrase():
    assert not _looks_like_entity("this is far too long to be one")


# ---------------------------------------------------------------------------
# status / repos preamble parsing
# ---------------------------------------------------------------------------


def test_status_delivered_maps_to_active():
    status, source = _derive_status("- Status:      Delivered\n")
    assert status == "active"
    assert source == "Delivered"


def test_status_in_progress_maps_to_active():
    status, _ = _derive_status("- Status: In progress\n")
    assert status == "active"


def test_status_mentioning_superseded_maps_to_superseded():
    status, _ = _derive_status("- Status: Superseded by newer doc\n")
    assert status == "superseded"


def test_no_status_line_defaults_active_with_no_source():
    status, source = _derive_status("no status here at all\n")
    assert status == "active"
    assert source is None


def test_repos_preamble_line_becomes_lowercase_tags():
    assert _derive_repo_tags("- Repos: widget-cache, Platform-Core\n") == [
        "widget-cache",
        "platform-core",
    ]


def test_no_repos_line_yields_no_tags():
    assert _derive_repo_tags("nothing here\n") == []


# ---------------------------------------------------------------------------
# related-link extraction
# ---------------------------------------------------------------------------


def test_related_extracts_sibling_md_link_stem():
    ids = _extract_related_ids(
        "See [Widget Cache Warmup](widget-cache-warmup.md) for background.",
        self_id="mq-sweeper-policy",
    )
    assert ids == ["widget-cache-warmup"]


def test_related_skips_external_urls():
    ids = _extract_related_ids(
        "See [external](https://example.com/widget-cache-warmup.md).",
        self_id="mq-sweeper-policy",
    )
    assert ids == []


def test_related_skips_self_link():
    ids = _extract_related_ids(
        "[This very document](widget-cache-warmup.md)",
        self_id="widget-cache-warmup",
    )
    assert ids == []


def test_related_drops_anchor_fragment():
    ids = _extract_related_ids(
        "[See](widget-cache-warmup.md#some-section)",
        self_id="other-doc",
    )
    assert ids == ["widget-cache-warmup"]


# ---------------------------------------------------------------------------
# propose_backfill — end to end over a full synthetic legacy document
# ---------------------------------------------------------------------------


def test_propose_backfill_on_legacy_document_derives_every_field(tmp_path):
    _write(tmp_path, "widget-cache-warmup.md", LEGACY_WIDGET_CACHE)
    doc = load_store(tmp_path).docs[0]

    proposal = propose_backfill(doc)

    assert proposal.already_complete is False
    by_name = {f.name: f for f in proposal.fields}

    assert by_name["id"].value == "widget-cache-warmup"
    assert by_name["title"].value == "Widget Cache Warmup"
    assert by_name["date"].value == datetime.date(2026, 5, 4)
    assert by_name["task_date"].value == datetime.date(2026, 5, 4)
    assert by_name["status"].value == "active"
    assert "Delivered" in by_name["status"].source
    assert by_name["backfilled"].value is True
    assert by_name["entities"].value == ["WidgetCache", "WidgetCacheWarmer", "CacheWarmer", "/api/v1/widgets", "/api/v1/widgets/{id}"]
    assert by_name["tags"].value == ["widget-cache", "platform-core"]
    # facet labels are deliberately absent — tags come from `- Repos:` alone
    assert "classes" not in by_name["tags"].value
    assert proposal.notes == []


def test_propose_backfill_reports_note_when_no_keywords_section(tmp_path):
    _write(
        tmp_path,
        "cache-invalidation-notes.md",
        "# Cache Invalidation Notes\n\n- Date: 2026-04-01\n\n## Notes\n\nPlain prose only, no keyword list.\n",
    )
    doc = load_store(tmp_path).docs[0]

    proposal = propose_backfill(doc)

    by_name = {f.name: f for f in proposal.fields}
    assert by_name["entities"].value == []
    assert len(proposal.notes) == 1
    assert "Search Keywords" in proposal.notes[0]


def test_propose_backfill_on_already_complete_document_proposes_nothing(tmp_path):
    _write(
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

    proposal = propose_backfill(doc)

    assert proposal.already_complete is True
    assert proposal.fields == []


def test_propose_backfill_never_proposes_a_field_already_stated(tmp_path):
    # partial front matter: id/title/date stated, everything else missing —
    # a stated field must never be re-proposed, even though the document as
    # a whole is still degraded
    _write(
        tmp_path,
        "ttl-revalidation.md",
        """---
id: ttl-revalidation
title: TTL / ETag Revalidation
date: 2026-06-01
---

## Search Keywords

- **Classes:** `TtlCache`, `EtagValidator`
""",
    )
    doc = load_store(tmp_path).docs[0]

    proposal = propose_backfill(doc)

    names = {f.name for f in proposal.fields}
    assert "id" not in names
    assert "title" not in names
    assert "date" not in names
    assert "entities" in names
    assert "status" in names


# ---------------------------------------------------------------------------
# apply_backfill — atomicity and byte-identical body preservation
# ---------------------------------------------------------------------------


def _body_hash(path):
    text = path.read_text(encoding="utf-8-sig")
    if text.splitlines()[:1] and text.splitlines()[0].strip() == "---":
        lines = text.splitlines(keepends=True)
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                body = "".join(lines[i + 1:])
                break
        else:
            body = text
    else:
        body = text
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def test_apply_backfill_preserves_body_byte_for_byte_on_a_headerless_document(tmp_path):
    path = _write(tmp_path, "widget-cache-warmup.md", LEGACY_WIDGET_CACHE)
    before_hash = hashlib.sha256(LEGACY_WIDGET_CACHE.encode("utf-8")).hexdigest()

    doc = load_store(tmp_path).docs[0]
    proposal = propose_backfill(doc)
    apply_backfill(doc, proposal)

    after_text = path.read_text(encoding="utf-8")
    assert after_text.startswith("---\n")
    # the body is everything after the newly-written front matter's closing
    # '---' line, and must equal the ENTIRE original file content verbatim —
    # there was no front matter to strip out of it before
    lines = after_text.splitlines(keepends=True)
    closing = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    body_after = "".join(lines[closing + 1:])
    assert body_after == LEGACY_WIDGET_CACHE
    assert hashlib.sha256(body_after.encode("utf-8")).hexdigest() == before_hash


def test_apply_backfill_preserves_body_on_a_partial_front_matter_document(tmp_path):
    original = """---
id: ttl-revalidation
title: TTL / ETag Revalidation
date: 2026-06-01
---

## Search Keywords

- **Classes:** `TtlCache`, `EtagValidator`

## Notes

Some prose the human wrote, with   irregular   spacing preserved.
"""
    path = _write(tmp_path, "ttl-revalidation.md", original)
    doc = load_store(tmp_path).docs[0]
    proposal = propose_backfill(doc)

    apply_backfill(doc, proposal)

    after = path.read_text(encoding="utf-8")
    # everything the human wrote in the original front matter block survives
    # untouched, character for character
    assert "id: ttl-revalidation\n" in after
    assert "title: TTL / ETag Revalidation\n" in after
    assert "date: 2026-06-01\n" in after
    # and the body — including the irregular spacing — is untouched
    assert "Some prose the human wrote, with   irregular   spacing preserved.\n" in after


def test_apply_backfill_result_reparses_with_spine_complete_true(tmp_path):
    path = _write(tmp_path, "widget-cache-warmup.md", LEGACY_WIDGET_CACHE)
    doc = load_store(tmp_path).docs[0]
    proposal = propose_backfill(doc)

    apply_backfill(doc, proposal)

    reloaded = load_store(tmp_path)
    assert reloaded.errors == []
    new_doc = reloaded.docs[0]
    assert new_doc.spine_complete is True
    assert new_doc.backfilled is True
    assert "WidgetCache" in new_doc.entities


def test_apply_backfill_is_atomic_no_temp_file_survives_a_success(tmp_path):
    path = _write(tmp_path, "widget-cache-warmup.md", LEGACY_WIDGET_CACHE)
    doc = load_store(tmp_path).docs[0]
    proposal = propose_backfill(doc)

    apply_backfill(doc, proposal)

    leftovers = [p for p in tmp_path.iterdir() if p.name != "widget-cache-warmup.md"]
    assert leftovers == []


def test_apply_backfill_on_already_complete_proposal_is_a_noop(tmp_path):
    path = _write(
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

Body.
""",
    )
    before = path.read_bytes()
    doc = load_store(tmp_path).docs[0]
    proposal = propose_backfill(doc)

    message = apply_backfill(doc, proposal)

    assert message == "nothing to backfill"
    assert path.read_bytes() == before


def test_reapplying_backfill_after_a_successful_write_is_a_noop(tmp_path):
    path = _write(tmp_path, "widget-cache-warmup.md", LEGACY_WIDGET_CACHE)
    doc = load_store(tmp_path).docs[0]
    proposal = propose_backfill(doc)
    apply_backfill(doc, proposal)

    after_first = path.read_bytes()
    reloaded_doc = load_store(tmp_path).docs[0]
    second_proposal = propose_backfill(reloaded_doc)

    assert second_proposal.already_complete is True
    message = apply_backfill(reloaded_doc, second_proposal)
    assert message == "nothing to backfill"
    assert path.read_bytes() == after_first


def test_punctuation_only_terms_are_not_entities():
    """A bare separator satisfies the hyphen branch of the shape test — `-` is
    exactly the character that lets `widget-rest-clients` through — so a `--`
    left over from splitting reached the proposal and would land in a document
    the author reads by eye."""
    from engmem.backfill import _looks_like_entity

    for junk in ("--", "---", "·", "—", "..", "//", "-.-", "___"):
        assert not _looks_like_entity(junk), f"{junk!r} must not qualify as an entity"


def test_identifiers_containing_punctuation_still_qualify():
    from engmem.backfill import _looks_like_entity

    for real in ("widget-rest-clients", "widget.api.url.v3", "GET /widget-cache/v1",
                 "snake_case_name", "WidgetCache", "SWEEPER_JOB"):
        assert _looks_like_entity(real), f"{real!r} must still qualify"


def test_facet_labels_do_not_become_tags():
    """A `Search Keywords` facet label names the *shape* of a group of terms
    ("endpoints", "classes", "risks"), not what the document is about. Nearly every
    document has an endpoints group, so a tag of weight 1 built from one cannot
    distinguish anything — it is pure dilution."""
    from engmem.backfill import _extract_entities

    section = (
        "- **Endpoints:** `GET /widget-cache/v1`, `GET /widget-cache/v1/{id}`\n"
        "- **Classes:** WidgetCacheController, CacheWarmer\n"
        "- **Risks:** cold-start latency\n"
    )
    entities = _extract_entities(section)

    assert "WidgetCacheController" in entities, "the terms themselves are still entities"
    assert not [e for e in entities if "Endpoints" in e or "Risks" in e], (
        "labels must not survive as entities"
    )


def test_repo_tags_drop_parenthetical_and_prose_suffixes():
    """A `- Repos:` line is prose: entries carry clarifying suffixes. The tag is the
    repository name; `r01234-widgets (legacy monolith)` and `r01234-widgets` must
    not be two different tags."""
    from engmem.backfill import _derive_repo_tags

    body = (
        "# Knowledge Base — Widget cache\n\n"
        "- Repos:        r01234-widgets (legacy monolith) · r01234-widgets-service "
        "(reference only) · r01234-liquibase\n"
    )
    assert _derive_repo_tags(body) == [
        "r01234-widgets", "r01234-widgets-service", "r01234-liquibase",
    ]


def test_repo_line_prose_fragment_is_not_a_tag():
    from engmem.backfill import _derive_repo_tags

    body = (
        "# Knowledge Base — Widget cache\n\n"
        "- Repos:        r01234-widgets — module `widgetrepo`, consumed by "
        "`widgetservice` (domain)\n"
    )
    assert _derive_repo_tags(body) == ["r01234-widgets"]


def test_prose_entries_on_the_repos_line_are_not_repositories():
    """A `- Repos:` line lists repositories, but authors also drop prose entries on
    it ("shared apis (3rd-party)", "external consumer r99999"). Matching a leading
    slug and stopping turns those into the tags `shared` and `external` — words that
    name nothing. A real entry is the slug and nothing else, bar a parenthetical."""
    from engmem.backfill import _derive_repo_tags

    body = (
        "# Knowledge Base — Widget cache\n\n"
        "- Repos: r01234-widgets (legacy monolith) · shared apis (3rd-party) "
        "· external consumer r99999 (some-tools) · widget-cache\n"
    )
    assert _derive_repo_tags(body) == ["r01234-widgets", "widget-cache"]


# ---------------------------------------------------------------------------
# repos — the declared field, alongside the ranked copy in tags
# ---------------------------------------------------------------------------


def test_backfill_proposes_repos_as_its_own_field(tmp_path):
    """Repository names reach `tags` because tags are scored and `repos` is not — that
    copy is what makes a repo name findable. But leaving `repos` itself empty means the
    field a reader looks at to answer "which code is this about" stays blank on every
    backfilled document."""
    _write(tmp_path, "widget-cache-warmup.md", LEGACY_WIDGET_CACHE)
    doc = load_store(tmp_path).docs[0]

    by_name = {f.name: f for f in propose_backfill(doc).fields}

    assert "repos" in by_name, "repos must be proposed, not left for the human to retype"
    assert by_name["repos"].value == by_name["tags"].value or set(
        by_name["repos"].value
    ) <= set(by_name["tags"].value), "repos must be the repo subset of tags, not something new"
    assert by_name["repos"].value, "the fixture declares repositories — they must appear"


def test_a_bold_label_without_a_bullet_keeps_both_asterisks():
    """`**Classes:** WidgetCache` is the documented shape of a Search Keywords line and
    needs no leading dash. `*` is also a list marker, so stripping bullets used to eat the
    first asterisk of the bold marker; the label then went unrecognised and `*Classes:**`
    was proposed as an entity — into the field ranking actually reads."""
    assert _strip_bullet("**Classes:** WidgetCache") == "**Classes:** WidgetCache"
    assert _extract_label_and_rest("**Classes:** WidgetCache") == ("Classes", "WidgetCache")


def test_real_bullets_are_still_stripped():
    for line in ("- **Classes:** X", "* **Classes:** X", "  - **Classes:** X", "+ **Classes:** X"):
        assert _strip_bullet(line) == "**Classes:** X", line
