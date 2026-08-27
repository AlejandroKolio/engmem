"""Unit tests for `engmem.backfill`; the CLI surface lives in `tests/test_backfill_cli.py`."""

from __future__ import annotations

import os
import re
from pathlib import Path
import stat
import hashlib
import datetime

import pytest
import yaml

from conftest import (
    requires_posix_modes,
    requires_symlinks,
    write_file,
)

from engmem import backfill

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
    _split_terms,
    _drop_declared_keys,
)
from engmem.spine import load_store, parse_document, split_front_matter


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

# the only shape that proposes every field: `superseded_by` is offered only when the
# `- Status:` line both says superseded and names the successor
SUPERSEDED_WIDGET_CACHE = LEGACY_WIDGET_CACHE.replace(
    "- Status:      Delivered", "- Status:      Superseded by [v2](widget-cache-v2.md)"
)


# ---------------------------------------------------------------------------
# entity/tag extraction — unit-level, exercising the rule directly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "section_body, expected",
    [
        pytest.param(
            "- **Classes:** `WidgetCache`, `WidgetCacheWarmer`, CacheWarmer",
            ["WidgetCache", "WidgetCacheWarmer", "CacheWarmer"],
            id="backticked-terms-always-accepted",
        ),
        pytest.param(
            "- **Endpoints:** /api/v1/widgets · /api/v1/widgets/{id}",
            ["/api/v1/widgets", "/api/v1/widgets/{id}"],
            id="endpoint-paths-accepted-via-the-slash-signal",
        ),
        pytest.param(
            "- **Concepts:** cold start, cache warmup (background job)",
            [],
            id="lowercase-descriptive-prose-rejected-false-negative-by-design",
        ),
        pytest.param(
            "Endpoints: N/A", [], id="na-denylisted-despite-matching-the-slash-shape",
        ),
        pytest.param(
            "Classes: SweeperJob; SweeperPolicy",
            ["SweeperJob", "SweeperPolicy"],
            # the label must be recognised so its terms read as terms, but the label
            # itself is scaffolding
            id="plain-non-bold-label-recognised-and-discarded",
        ),
        pytest.param(
            "Notes: sweeper, cleanup job, TTL",
            ["TTL"],
            id="allcaps-acronym-standing-alone-is-accepted",
        ),
        pytest.param(
            "- **Classes:** WidgetCache, CacheWarmer, WidgetCache",
            ["WidgetCache", "CacheWarmer"],
            id="order-preserving-and-deduplicated",
        ),
        pytest.param(
            "- **Endpoints:** OrderAPI (v1, v2), PaymentAPI",
            ["OrderAPI", "PaymentAPI"],
            # `OrderAPI (v1, v2)` split through the parenthetical produced `OrderAPI (v1`
            # and `v2)`, both of which passed the shape test on their digits
            id="parenthetical-is-not-split-into-non-terms",
        ),
        pytest.param(
            "- **Classes:** `WidgetCache` (thread-safe, LRU); CacheWarmer",
            ["WidgetCache", "CacheWarmer"],
            # the worse half of the same bug: the fragment no longer matches the
            # backtick test, losing the author's own explicit identifier signal
            id="backticked-term-survives-a-parenthetical-beside-it",
        ),
        pytest.param(
            "- **Classes:** A (x, B",
            [],
            # an unclosed paren must not turn the rest of the line into one term that
            # then passes the shape test on some capital further along
            id="unbalanced-open-paren-protects-nothing",
        ),
    ],
)
def test_extract_entities_shape_and_dedup_rules(section_body, expected):
    assert _extract_entities(section_body) == expected


@pytest.mark.parametrize(
    "section, must_contain, forbidden_label_fragments",
    [
        pytest.param(
            "- **HTTP Methods:** GET, POST\n- **Classes:** `WidgetCache`",
            ["WidgetCache"],
            ["Classes", "HTTP Methods"],
            id="facet-labels-discarded-not-turned-into-terms",
        ),
        pytest.param(
            "- **Endpoints:** `GET /widget-cache/v1`, `GET /widget-cache/v1/{id}`\n"
            "- **Classes:** WidgetCacheController, CacheWarmer\n"
            "- **Risks:** cold-start latency\n",
            ["WidgetCacheController"],
            ["Endpoints", "Risks"],
            id="facet-labels-do-not-become-tags",
        ),
    ],
)
def test_facet_labels_never_survive_as_entities(section, must_contain, forbidden_label_fragments):
    """Nearly every document has an endpoints group, so a weight-1 tag built from a facet label
    distinguishes nothing."""
    entities = _extract_entities(section)
    for term in must_contain:
        assert term in entities, "the terms themselves are still entities"
    assert not [
        e for e in entities if any(frag in e for frag in forbidden_label_fragments)
    ], "labels must not survive as entities"


@pytest.mark.parametrize(
    "text, expected",
    [
        # a single Title-Case word alone is indistinguishable from ordinary English prose
        # ("Cache" the common noun vs. a class literally named `Cache`) — documented
        # false negative, not a bug
        pytest.param("Cache", False, id="bare-titlecase-word-rejected"),
        # a second capitalised word signals a proper-noun-shaped phrase rather than a
        # single ordinary noun — documented, deliberately permissive
        pytest.param("Response Cache", True, id="multiword-titlecase-phrase-accepted"),
        pytest.param("this is far too long to be one", False, id="five-word-phrase-rejected"),
        # the contract states two caps ("a term longer than 4 words or 60 characters is
        # never accepted"); this row is title-case so it would pass every other rule —
        # the word-count cap is the only thing rejecting it
        pytest.param("This Is Far Too Long", False, id="over-four-words"),
        # 61 characters, title-case, no digits/punctuation — rejected on length alone;
        # the identical string one character shorter is accepted (verified by hand)
        pytest.param("A" + "a" * 59 + "B", False, id="over-sixty-characters"),
        # `-` is exactly the character that lets `widget-rest-clients` through, so a
        # leftover run of punctuation must not itself qualify
        pytest.param("--", False, id="double-dash"),
        pytest.param("---", False, id="triple-dash"),
        pytest.param("·", False, id="middle-dot"),
        pytest.param("—", False, id="em-dash"),
        pytest.param("..", False, id="double-dot"),
        pytest.param("//", False, id="double-slash"),
        pytest.param("-.-", False, id="dash-dot-dash"),
        pytest.param("___", False, id="triple-underscore"),
        pytest.param("widget-rest-clients", True, id="hyphenated-identifier"),
        pytest.param("widget.api.url.v3", True, id="dotted-identifier"),
        pytest.param("GET /widget-cache/v1", True, id="endpoint-path"),
        pytest.param("snake_case_name", True, id="snake-case-identifier"),
        pytest.param("WidgetCache", True, id="camel-case"),
        pytest.param("SWEEPER_JOB", True, id="allcaps-with-underscore"),
    ],
)
def test_looks_like_entity_shape_rule(text, expected):
    assert _looks_like_entity(text) is expected


# ---------------------------------------------------------------------------
# status / repos preamble parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body, expected_status, expected_source",
    [
        pytest.param("- Status:      Delivered\n", "active", "Delivered", id="delivered-maps-to-active"),
        pytest.param("- Status: In progress\n", "active", "In progress", id="in-progress-maps-to-active"),
        pytest.param(
            "- Status: Superseded by newer doc\n", "superseded", "Superseded by newer doc",
            id="mentioning-superseded-maps-to-superseded",
        ),
        pytest.param(
            "no status here at all\n", "active", None,
            id="no-status-line-defaults-active-with-no-source",
        ),
        pytest.param(
            # the opposite relationship: this document replaced another one, so it is the
            # live one. A stem test ("supersed") read it as the replaced one
            "- Status: Active — supersedes [v1](widget-cache-v1.md)\n",
            "active", "Active — supersedes [v1](widget-cache-v1.md)",
            id="supersedes-another-document-is-still-active",
        ),
        pytest.param(
            "- Status: Current; superseding the v1 note\n",
            "active", "Current; superseding the v1 note",
            id="superseding-another-document-is-still-active",
        ),
        pytest.param(
            # `\s` after the colon spanned the line break, so an empty label line reached
            # over it and read the *next* preamble line as its own value — here that meant
            # `superseded` plus a successor aimed at the document this one replaced
            "- Status:\n- Notes: superseded by [old](old-flow.md)\n",
            "active", None,
            id="empty-status-line-does-not-adopt-the-next-lines-value",
        ),
        pytest.param(
            # and the shadowing it caused: the empty line consumed the real one below it
            "- Status:\n- Status: Superseded by [v2](widget-cache-v2.md)\n",
            "superseded", "Superseded by [v2](widget-cache-v2.md)",
            id="an-empty-status-line-does-not-shadow-the-real-one-below-it",
        ),
        pytest.param(
            # an unfilled label is far more often typed with a trailing space than bare —
            # two of them is CommonMark's own hard line break
            "- Status: \n- Status: Superseded by [v2](widget-cache-v2.md)\n",
            "superseded", "Superseded by [v2](widget-cache-v2.md)",
            id="a-trailing-space-status-line-does-not-shadow-the-real-one-below-it",
        ),
        pytest.param(
            "- Status:\t\n- Status: Superseded by [v2](widget-cache-v2.md)\n",
            "superseded", "Superseded by [v2](widget-cache-v2.md)",
            id="a-trailing-tab-status-line-does-not-shadow-the-real-one-below-it",
        ),
        pytest.param(
            "- **Status:** \n- Status: Superseded by [v2](widget-cache-v2.md)\n",
            "superseded", "Superseded by [v2](widget-cache-v2.md)",
            id="a-bold-blank-status-line-does-not-shadow-the-real-one-below-it",
        ),
        pytest.param(
            # nothing but the emphasis markers is still no value
            "- Status: **\n- Status: Superseded by [v2](widget-cache-v2.md)\n",
            "superseded", "Superseded by [v2](widget-cache-v2.md)",
            id="a-status-line-holding-only-emphasis-markers-shadows-nothing",
        ),
        pytest.param(
            # the source is the evidence a human approves the write on: a blank line is
            # not a value, so it must not be reported as one
            "- Status: \n", "active", None,
            id="a-blank-status-line-alone-reports-no-source",
        ),
        pytest.param(
            # the line rule is "not across the line break", not "ASCII space only": a
            # Confluence/Notion export writes a non-breaking space after the colon, and
            # narrowing the gap to space-and-tab dropped the whole line
            "- Status:\xa0Superseded by [v2](widget-cache-v2.md)\n",
            "superseded", "Superseded by [v2](widget-cache-v2.md)",
            id="a-non-breaking-space-after-the-colon-still-carries-the-value",
        ),
        pytest.param(
            "- **Status**\u202f: Superseded by [v2](widget-cache-v2.md)\n",
            "superseded", "Superseded by [v2](widget-cache-v2.md)",
            id="a-narrow-non-breaking-space-around-the-label-still-carries-the-value",
        ),
        pytest.param(
            "-\xa0Status: Superseded by [v2](widget-cache-v2.md)\n",
            "superseded", "Superseded by [v2](widget-cache-v2.md)",
            id="a-non-breaking-space-after-the-bullet-still-carries-the-value",
        ),
        pytest.param(
            # the other edge of the same class: whitespace, but never the line break. A
            # bullet whose label sits on the line below it is a lazy continuation to
            # CommonMark, and reading it would be the cross-line read the rule forbids —
            # the same cost `spine._PREAMBLE_DATE_RE` accepts for that shape
            "-\nStatus: Superseded by [v2](widget-cache-v2.md)\n",
            "active", None,
            id="a-status-label-on-the-line-below-its-bullet-is-not-read",
        ),
        pytest.param(
            # and the other half of the rule survives the wider class: `str.strip()` strips
            # Unicode whitespace, so a value that is only a non-breaking space is still blank
            # and still must not shadow the real line below it
            "- Status:\xa0\n- Status: Superseded by [v2](widget-cache-v2.md)\n",
            "superseded", "Superseded by [v2](widget-cache-v2.md)",
            id="a-non-breaking-space-only-status-line-shadows-nothing",
        ),
        pytest.param(
            "- Status:\u3000\n", "active", None,
            id="a-non-breaking-space-only-status-line-alone-reports-no-source",
        ),
    ],
)
def test_derive_status_maps_preamble_line_to_status(body, expected_status, expected_source):
    status, source = _derive_status(body)
    assert status == expected_status
    assert source == expected_source


@pytest.mark.parametrize(
    "body, expected_tags",
    [
        pytest.param(
            "- Repos: widget-cache, Platform-Core\n",
            ["widget-cache", "platform-core"],
            id="repos-preamble-line-becomes-lowercase-tags",
        ),
        pytest.param("nothing here\n", [], id="no-repos-line-yields-no-tags"),
        pytest.param(
            "# Knowledge Base — Widget cache\n\n"
            "- Repos:        r01234-widgets (legacy monolith) · r01234-widgets-service "
            "(reference only) · r01234-liquibase\n",
            ["r01234-widgets", "r01234-widgets-service", "r01234-liquibase"],
            # `r01234-widgets (legacy monolith)` and `r01234-widgets-service` must not
            # collapse into two different tags
            id="parenthetical-and-prose-suffixes-are-dropped",
        ),
        pytest.param(
            "# Knowledge Base — Widget cache\n\n"
            "- Repos:        r01234-widgets — module `widgetrepo`, consumed by "
            "`widgetservice` (domain)\n",
            ["r01234-widgets"],
            # an em-dash opens a trailing aside about the entry before it; its own
            # commas must not split into phantom entries
            id="em-dash-aside-is-not-a-tag",
        ),
        pytest.param(
            "# Knowledge Base — Widget cache\n\n"
            "- Repos: r01234-widgets (legacy monolith) · shared apis (3rd-party) "
            "· external consumer r99999 (some-tools) · widget-cache\n",
            ["r01234-widgets", "widget-cache"],
            # matching a leading slug would otherwise turn prose entries into the tags
            # "shared" and "external", words that name nothing
            id="prose-entries-are-not-repositories",
        ),
        pytest.param(
            "- **Repos:** **widget-cache, platform-core**\n",
            ["widget-cache", "platform-core"],
            # the emphasis markers are not part of the name; leaving them in the candidate
            # made the whole line contribute nothing, and `tags: []` is written either way
            id="emphasised-entries-are-still-repositories",
        ),
        pytest.param(
            "- Repos: *widget-cache*\n", ["widget-cache"], id="italicised-entry-is-a-repository",
        ),
        pytest.param(
            # `_` is CommonMark's other emphasis delimiter; stripping only `*` left this
            # entry rejected by the slug test and wrote the permanent `tags: []`
            "- Repos: _widget-cache_\n", ["widget-cache"],
            id="underscore-italicised-entry-is-a-repository",
        ),
        pytest.param(
            "- Repos: __widget-cache__\n", ["widget-cache"],
            id="underscore-bolded-entry-is-a-repository",
        ),
        pytest.param(
            # worse than a wholly-rejected line: one name survives, so the proposal reads
            # as successful while the other entry is silently gone
            "- Repos: *widget-cache*, _platform-core_\n", ["widget-cache", "platform-core"],
            id="mixed-emphasis-delimiters-keep-both-entries",
        ),
        pytest.param(
            # the empty line used to consume the real one below it and yield no tag at all,
            # which writes the permanent `tags: []`
            "- Repos:\n- Repos: widget-cache, platform-core\n",
            ["widget-cache", "platform-core"],
            id="an-empty-repos-line-does-not-shadow-the-real-one-below-it",
        ),
        pytest.param(
            "- Repos: \n- Repos: widget-cache, platform-core\n",
            ["widget-cache", "platform-core"],
            id="a-trailing-space-repos-line-does-not-shadow-the-real-one-below-it",
        ),
        pytest.param(
            "- Repos:\t\n- Repos: widget-cache, platform-core\n",
            ["widget-cache", "platform-core"],
            id="a-trailing-tab-repos-line-does-not-shadow-the-real-one-below-it",
        ),
        pytest.param(
            "- **Repos:** \n- Repos: widget-cache, platform-core\n",
            ["widget-cache", "platform-core"],
            id="a-bold-blank-repos-line-does-not-shadow-the-real-one-below-it",
        ),
        pytest.param(
            # an export's non-breaking spaces, after the colon and after the comma: the gap
            # class carries the line, and `part.strip()` strips the one inside the value
            "- Repos:\xa0widget-cache,\xa0platform-core\n",
            ["widget-cache", "platform-core"],
            id="non-breaking-spaces-inside-the-value-still-parse-as-entries",
        ),
        pytest.param(
            # the gaps the class actually governs: bullet to label, label to colon
            "-\xa0**Repos**\u202f: widget-cache, platform-core\n",
            ["widget-cache", "platform-core"],
            id="non-breaking-spaces-around-the-repos-label-still-carry-the-line",
        ),
        pytest.param(
            "-\nRepos: widget-cache, platform-core\n", [],
            id="a-repos-label-on-the-line-below-its-bullet-is-not-read",
        ),
        pytest.param(
            # a value of nothing but a non-breaking space is still blank, so the real line
            # below it is still reached — losing it writes the permanent `tags: []`
            "- Repos:\xa0\n- Repos: widget-cache, platform-core\n",
            ["widget-cache", "platform-core"],
            id="a-non-breaking-space-only-repos-line-does-not-shadow-the-real-one-below-it",
        ),
    ],
)
def test_derive_repo_tags_shape_rule(body, expected_tags):
    assert _derive_repo_tags(body) == expected_tags


# ---------------------------------------------------------------------------
# related-link extraction — `related` follows CommonMark, not a regex over raw text
# ---------------------------------------------------------------------------

_SELF = "this-document"

_RELATED_CASES = [
    # basic sibling recognition
    pytest.param(
        "See [Widget Cache Warmup](widget-cache-warmup.md) for background.",
        "mq-sweeper-policy", ["widget-cache-warmup"], id="extracts-sibling-md-link-stem",
    ),
    pytest.param(
        "See [external](https://example.com/widget-cache-warmup.md).",
        "mq-sweeper-policy", [], id="skips-external-urls",
    ),
    pytest.param(
        "[This very document](widget-cache-warmup.md)",
        "widget-cache-warmup", [], id="skips-self-link",
    ),
    pytest.param(
        "[See](widget-cache-warmup.md#some-section)",
        "other-doc", ["widget-cache-warmup"], id="drops-anchor-fragment",
    ),
    # CommonMark link forms
    pytest.param(
        "Real [a](widget-cache.md)\n\n```markdown\nExample: [b](not-a-sibling.md)\n```\n",
        _SELF, ["widget-cache"],
        # a document that shows markdown as an example was growing an edge to whatever
        # that example linked to
        id="link-inside-a-code-fence-is-not-an-edge",
    ),
    pytest.param(
        "Write it as `[label](not-a-sibling.md)` in the body.\n",
        _SELF, [], id="link-inside-an-inline-code-span-is-not-an-edge",
    ),
    pytest.param(
        "Escaped: \\[label\\](not-a-sibling.md)\n",
        _SELF, [], id="escaped-brackets-are-not-an-edge",
    ),
    pytest.param(
        "See [the cache doc][ref].\n\n[ref]: widget-cache.md\n",
        _SELF, ["widget-cache"],
        # `[label][ref]` has no `](`, so the old regex could not see it at all
        id="reference-style-link-is-an-edge",
    ),
    pytest.param(
        "See [the notes](<widget cache notes.md>).\n",
        _SELF, ["widget cache notes"],
        # CommonMark writes such a destination in angle brackets and percent-encodes it;
        # `related` holds filename stems, so it has to be read back as written
        id="a-destination-with-a-space-keeps-it",
    ),
    pytest.param(
        'See [the cache doc](widget-cache.md "The Cache").\n',
        _SELF, ["widget-cache"], id="a-link-title-is-not-part-of-the-destination",
    ),
    pytest.param(
        "See [the notes](weird%23name.md).\n",
        _SELF, ["weird#name"],
        # decoding before cutting the anchor read `weird%23name.md` as the fragment
        # `name.md` on a document called `weird`, and dropped the edge
        id="a-percent-encoded-hash-is-part-of-the-filename",
    ),
    pytest.param(
        "See [the notes](<weird#name.md>).\n",
        _SELF, [],
        # a `#` typed literally is indistinguishable from an anchor — a known boundary,
        # not a bug
        id="an-unencoded-hash-reads-as-an-anchor",
    ),
    pytest.param(
        "See [that section](widget-cache.md#read-path).\n",
        _SELF, ["widget-cache"], id="an-anchor-is-still-stripped",
    ),
    pytest.param(
        "See [upstream](https://example.com/notes.md#section).\n",
        _SELF, [], id="an-external-url-with-an-anchor-is-still-not-a-sibling",
    ),
    # block quotes hold another document's text, so their links are that document's edges
    pytest.param(
        "Real [a](widget-cache.md)\n\n> Quoted: [b](someone-elses.md)\n",
        _SELF, ["widget-cache"], id="a-link-inside-a-block-quote-is-not-an-edge",
    ),
    pytest.param(
        "> > Deeply quoted: [b](someone-elses.md)\n\n> - Listed: [c](also-theirs.md)\n",
        _SELF, [], id="a-link-nested-deeper-inside-a-block-quote-is-not-an-edge",
    ),
    pytest.param(
        "> Quoted: [b](someone-elses.md)\n\nOurs again: [a](widget-cache.md)\n",
        _SELF, ["widget-cache"], id="a-link-after-the-quote-closes-is-still-an-edge",
    ),
    # a link that leaves the document's own directory contributes no id: its stem is
    # some other file's name, and taking it invents an edge to a document that may not
    # even exist
    pytest.param(
        "see [x](../../docs/design/contracts/backfill.md)", _SELF, [],
        id="a-project-doc-is-not-a-session-document",
    ),
    pytest.param("see [x](sub/dir/other.md)", _SELF, [], id="a-nested-subdirectory-is-not-a-sibling"),
    pytest.param("see [x](/etc/passwd.md)", _SELF, [], id="an-absolute-path-is-not-a-sibling"),
    pytest.param(
        "see [x](..\\other.md)", _SELF, [],
        # a Windows-style path is not a sibling either, and `\` is not a separator here
        id="a-windows-style-path-is-not-a-sibling",
    ),
    pytest.param(
        "see [x](mailto:alex@example.md)", _SELF, [],
        id="mailto-ends-in-md-but-is-not-a-path-at-all",
    ),
    # a sibling is recognised however it is spelled
    pytest.param("see [x](sibling-doc.md)", _SELF, ["sibling-doc"], id="a-plain-sibling-name"),
    pytest.param("see [x](./sibling-doc.md)", _SELF, ["sibling-doc"], id="a-dot-slash-sibling-name"),
    pytest.param(
        "see [x](a/../sibling-doc.md)", _SELF, ["sibling-doc"],
        id="a-normalised-relative-sibling-name",
    ),
    # `validate_doc_id`'s shape half is declined here; its safety half is not — a dot-only
    # or NUL-bearing stem names no document and cannot be a filename either
    pytest.param("see [x](.md)", _SELF, [], id="a-dot-md-stem-is-not-an-id"),
    pytest.param("see [x](..md)", _SELF, [], id="a-dot-dot-md-stem-is-not-an-id"),
    pytest.param("see [x](...md)", _SELF, [], id="a-dot-dot-dot-md-stem-is-not-an-id"),
    pytest.param("see [x](a%00b.md)", _SELF, [], id="a-nul-bearing-stem-is-not-an-id"),
]


@pytest.mark.parametrize("body, self_id, expected", _RELATED_CASES)
def test_extract_related_ids_behavior(body, self_id, expected):
    assert _extract_related_ids(body, self_id) == expected


# ---------------------------------------------------------------------------
# propose_backfill — end to end over a full synthetic legacy document
# ---------------------------------------------------------------------------


CONTRACT = Path(__file__).resolve().parent.parent / "docs" / "design" / "contracts" / "backfill.md"


def _contract_field_order() -> list[str]:
    table = CONTRACT.read_text(encoding="utf-8").split("## What is derived, and from what", 1)[1]
    return re.findall(r"^\| `([a-z_]+)` \|", table.split("\n\n", 2)[1], re.MULTILINE)


def test_fields_are_proposed_in_the_order_the_contract_states(tmp_path):
    """`backfill.md` says "Fields are proposed in the order above", and that order reaches both
    the confirmation preview and the key order written into the author's front matter."""
    write_file(tmp_path, "widget-cache-warmup.md", SUPERSEDED_WIDGET_CACHE)
    doc = load_store(tmp_path).docs[0]

    proposed = [f.name for f in propose_backfill(doc).fields]

    assert proposed == _contract_field_order(), (
        "the contract's table and propose_backfill disagree about field order; one of them "
        "has to move, and the front matter engmem writes keeps whichever order the code uses"
    )


def test_related_is_pinned_at_the_public_boundary_too(tmp_path):
    """29 cases sit on the private `_extract_related_ids`; this is the one at
    `propose_backfill` itself, over a body mixing a real sibling link with a fenced-off
    and a block-quoted one that must not count, asserting both the derived value and
    both of the field's evidence strings (found / not found)."""
    write_file(
        tmp_path,
        "with-sibling.md",
        "# With Sibling\n\n- Date: 2026-01-01\n\n"
        "See [it](sibling-doc.md).\n\n"
        "```markdown\nExample: [x](not-a-sibling.md)\n```\n\n"
        "> Quoted: [y](someone-elses.md)\n",
    )
    write_file(tmp_path, "sibling-doc.md", "# Sibling Doc\n\n- Date: 2026-01-01\n")
    write_file(
        tmp_path,
        "no-sibling.md",
        "# No Sibling\n\n- Date: 2026-01-01\n\n"
        "```markdown\nExample: [x](not-a-sibling.md)\n```\n\n"
        "> Quoted: [y](someone-elses.md)\n",
    )
    docs = {d.id: d for d in load_store(tmp_path).docs}

    with_sibling = {f.name: f for f in propose_backfill(docs["with-sibling"]).fields}
    no_sibling = {f.name: f for f in propose_backfill(docs["no-sibling"]).fields}

    assert with_sibling["related"].value == ["sibling-doc"]
    assert with_sibling["related"].source == (
        "markdown link(s) to sibling .md file(s) in the body"
    )
    assert no_sibling["related"].value == []
    assert no_sibling["related"].source == "no markdown links to sibling .md files found"


def test_propose_backfill_on_legacy_document_derives_every_field(tmp_path):
    write_file(tmp_path, "widget-cache-warmup.md", LEGACY_WIDGET_CACHE)
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
    """`entities` is left unset, not written empty: `spine.stated` counts `[]` as answered, so
    writing it would take the document spine-complete and make the note's "re-run" impossible."""
    write_file(
        tmp_path,
        "cache-invalidation-notes.md",
        "# Cache Invalidation Notes\n\n- Date: 2026-04-01\n\n## Notes\n\nPlain prose only, no keyword list.\n",
    )
    doc = load_store(tmp_path).docs[0]

    proposal = propose_backfill(doc)

    by_name = {f.name: f for f in proposal.fields}
    assert "entities" not in by_name
    assert len(proposal.notes) == 1
    assert "Search Keywords" in proposal.notes[0]


def test_no_entities_note_when_the_author_already_stated_entities(tmp_path):
    """The note claims *this run* left entities unset. A document that states entities and is
    degraded on some other field would be told to go do work that changes nothing."""
    write_file(
        tmp_path,
        "cache-invalidation-notes.md",
        """---
entities: [CacheInvalidator]
---

# Cache Invalidation Notes

Prose only, no keyword list.
""",
    )
    doc = load_store(tmp_path).docs[0]

    proposal = propose_backfill(doc)

    assert doc.spine_complete is False
    assert "entities" not in {f.name for f in proposal.fields}
    assert proposal.notes == []


def test_a_rejected_repos_line_is_not_reported_as_a_missing_one(tmp_path):
    """"none found" read as "no line was found" — false when the author wrote one and the
    parser rejected every entry."""
    write_file(
        tmp_path,
        "cache-invalidation-notes.md",
        "# Cache Invalidation Notes\n\n- Date: 2026-04-01\n- Repos: Shared APIs (3rd-party)\n",
    )
    doc = load_store(tmp_path).docs[0]

    by_name = {f.name: f for f in propose_backfill(doc).fields}

    assert by_name["tags"].value == []
    assert by_name["tags"].source == (
        "'- Repos:' preamble line found, but no entry parsed as a repo name"
    )


def test_propose_backfill_leaves_entities_unset_when_the_section_yields_nothing(tmp_path):
    """A section that names no identifier is the same epistemic state as no section at all —
    entities were never determined, so the field stays unset and says so."""
    write_file(
        tmp_path,
        "cache-invalidation-notes.md",
        "# Cache Invalidation Notes\n\n- Date: 2026-04-01\n\n"
        "## Search Keywords\n\ncache invalidation, cold start, warmup\n",
    )
    doc = load_store(tmp_path).docs[0]

    proposal = propose_backfill(doc)

    assert "entities" not in {f.name for f in proposal.fields}
    assert len(proposal.notes) == 1
    assert "yielded no term" in proposal.notes[0]


def test_adding_a_keywords_section_after_a_backfill_still_fills_entities(tmp_path):
    """The whole point of leaving `entities` unset: the note tells the author to add a section
    and re-run, and that has to actually work. Writing `[]` made it a dead end."""
    write_file(
        tmp_path,
        "cache-invalidation-notes.md",
        "# Cache Invalidation Notes\n\n- Date: 2026-04-01\n\n## Notes\n\nPlain prose.\n",
    )
    path = tmp_path / "cache-invalidation-notes.md"

    doc = load_store(tmp_path).docs[0]
    apply_backfill(doc, propose_backfill(doc))
    assert load_store(tmp_path).docs[0].spine_complete is False

    path.write_text(
        path.read_text(encoding="utf-8")
        + "\n## Search Keywords\n\n**Classes:** `WidgetCache`, `CacheWarmer`\n",
        encoding="utf-8",
    )

    doc = load_store(tmp_path).docs[0]
    apply_backfill(doc, propose_backfill(doc))

    reloaded = load_store(tmp_path).docs[0]
    assert reloaded.entities == ["WidgetCache", "CacheWarmer"]
    assert reloaded.spine_complete is True


def test_propose_backfill_on_already_complete_document_proposes_nothing(tmp_path):
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

    proposal = propose_backfill(doc)

    assert proposal.already_complete is True
    assert proposal.fields == []


def test_propose_backfill_never_proposes_a_field_already_stated(tmp_path):
    # partial front matter: id/title/date stated, everything else missing — a stated field must
    # never be re-proposed, even though the document as a whole is still degraded
    write_file(
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
    path = write_file(tmp_path, "widget-cache-warmup.md", LEGACY_WIDGET_CACHE)
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
    path = write_file(tmp_path, "ttl-revalidation.md", original)
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
    write_file(tmp_path, "widget-cache-warmup.md", LEGACY_WIDGET_CACHE)
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
    write_file(tmp_path, "widget-cache-warmup.md", LEGACY_WIDGET_CACHE)
    doc = load_store(tmp_path).docs[0]
    proposal = propose_backfill(doc)

    apply_backfill(doc, proposal)

    leftovers = [p for p in tmp_path.iterdir() if p.name != "widget-cache-warmup.md"]
    assert leftovers == []


def test_a_write_that_states_none_of_its_fields_is_refused_not_reported_as_written(
    tmp_path, monkeypatch
):
    """The returned message is a claim about the file. A dumper that emits nothing still stages
    a document that parses and whose body matches, so nothing else here would notice — while
    `_drop_declared_keys` has already taken the author's own `tags:` line out."""
    path = write_file(
        tmp_path,
        "widget-cache-warmup.md",
        "---\ntitle: Widget Cache Warmup\ntags:\n---\n\n# Widget Cache Warmup\n\n"
        "- Date: 2026-05-04\n- Repos: widget-cache\n",
    )
    before = path.read_bytes()
    doc = load_store(tmp_path).docs[0]
    monkeypatch.setattr(backfill, "_dump_front_matter", lambda data: "")

    with pytest.raises(BackfillWriteError, match="does not state"):
        apply_backfill(doc, propose_backfill(doc))

    assert path.read_bytes() == before
    assert [p for p in tmp_path.iterdir() if p.name != "widget-cache-warmup.md"] == []


def test_apply_backfill_on_already_complete_proposal_is_a_noop(tmp_path):
    path = write_file(
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
    path = write_file(tmp_path, "widget-cache-warmup.md", LEGACY_WIDGET_CACHE)
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


# ---------------------------------------------------------------------------
# repos — the declared field, alongside the ranked copy in tags
# ---------------------------------------------------------------------------


def test_backfill_proposes_repos_as_its_own_field(tmp_path):
    """Repo names reach `tags` because tags are scored, but leaving `repos` blank empties the
    field a reader looks at."""
    write_file(tmp_path, "widget-cache-warmup.md", LEGACY_WIDGET_CACHE)
    doc = load_store(tmp_path).docs[0]

    by_name = {f.name: f for f in propose_backfill(doc).fields}

    assert "repos" in by_name, "repos must be proposed, not left for the human to retype"
    assert by_name["repos"].value == by_name["tags"].value or set(
        by_name["repos"].value
    ) <= set(by_name["tags"].value), "repos must be the repo subset of tags, not something new"
    assert by_name["repos"].value, "the fixture declares repositories — they must appear"


def test_a_bold_label_without_a_bullet_keeps_both_asterisks():
    """`*` is also a list marker, so stripping bullets ate the first asterisk and proposed
    `*Classes:**` as an entity."""
    assert _strip_bullet("**Classes:** WidgetCache") == "**Classes:** WidgetCache"
    assert _extract_label_and_rest("**Classes:** WidgetCache") == ("Classes", "WidgetCache")


@pytest.mark.parametrize(
    "line",
    ["- **Classes:** X", "* **Classes:** X", "  - **Classes:** X", "+ **Classes:** X"],
)
def test_real_bullets_are_still_stripped(line):
    assert _strip_bullet(line) == "**Classes:** X"


def test_apply_backfill_preserves_crlf_line_endings_byte_for_byte(tmp_path):
    """Both `read_text` and `write_text` translate line endings, invisibly to the body check."""
    body = (
        "# Widget Cache Warmup\r\n\r\n- Status: Delivered\r\n- Repos: widget-cache\r\n"
        "\r\n## Search Keywords\r\n\r\n**Classes:** WidgetCache\r\n\r\nProse line.\r\n"
    )
    path = tmp_path / "widget-cache-warmup.md"
    path.write_bytes(body.encode("utf-8"))

    doc = load_store(tmp_path).docs[0]
    apply_backfill(doc, propose_backfill(doc))

    after = path.read_bytes()
    assert body.encode("utf-8") in after, "the original body bytes must survive verbatim"
    # every line ending in the file, the created front matter's included — a document with
    # both is a whole-file diff under `core.autocrlf` or a `.gitattributes` eol
    assert after.count(b"\n") == after.count(b"\r\n")


def test_apply_backfill_preserves_a_byte_order_mark(tmp_path):
    """`utf-8-sig` strips a BOM on the way in and nothing put it back, so a document authored
    on Windows silently lost its encoding signature — invisible in a diff, and not ours to drop."""
    body = "# Widget Cache Warmup\n\n- Status: Delivered\n\nProse line.\n"
    path = tmp_path / "widget-cache-warmup.md"
    path.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))

    doc = load_store(tmp_path).docs[0]
    apply_backfill(doc, propose_backfill(doc))

    after = path.read_bytes()
    assert after.startswith(b"\xef\xbb\xbf---\n")
    assert after.endswith(body.encode("utf-8"))


def test_entities_are_extracted_from_every_piece_of_an_oversized_keywords_section(tmp_path):
    """An oversized section splits across its `###` pieces; the terms live in all of them."""
    filler = ", ".join(f"`Widget{i}`" for i in range(400))
    write_file(
        tmp_path,
        "widget-cache-warmup.md",
        "# Widget Cache Warmup\n\n- Date: 2026-05-04\n\n## Search Keywords\n\n"
        f"### Classes\n\n{filler}\n\n### Endpoints\n\n`/api/v1/widgets`\n",
    )
    doc = load_store(tmp_path).docs[0]

    proposal = propose_backfill(doc)

    by_name = {f.name: f for f in proposal.fields}
    assert proposal.notes == []
    assert by_name["entities"].value[0] == "Widget0"
    # the term from the LAST piece, which the first-piece-only read never reached
    assert "/api/v1/widgets" in by_name["entities"].value
    assert "§2-classes, §3-endpoints" in by_name["entities"].source  # §1 is the preamble


# ---------------------------------------------------------------------------
# apply_backfill — a key the author declared but left empty
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spelling",
    ["tags:", "tags: ", "tags: null", "tags: ~", "tags: !!null", "tags:  # tbd"],
)
def test_an_empty_key_is_replaced_not_duplicated(tmp_path, spelling):
    """New fields are appended to the author's own front matter rather than re-dumped over it,
    so a key they declared and left empty would end up in the document twice."""
    write_file(
        tmp_path,
        "widget-cache-warmup.md",
        f"""---
title: Widget Cache Warmup
{spelling}
---

# Widget Cache Warmup

- Date: 2026-05-04
- Repos: widget-cache
""",
    )
    path = tmp_path / "widget-cache-warmup.md"
    doc = load_store(tmp_path).docs[0]

    apply_backfill(doc, propose_backfill(doc))

    front_matter, _ = split_front_matter(path.read_text(encoding="utf-8"))
    assert [ln for ln in front_matter.splitlines() if ln.startswith("tags")] == ["tags: [widget-cache]"]
    assert yaml.safe_load(front_matter)["tags"] == ["widget-cache"]
    assert parse_document(path).tags == ["widget-cache"]


def test_an_empty_key_is_dropped_on_a_crlf_document(tmp_path):
    """`splitlines(keepends=True)` hands back the `\r`, which a `$`-anchored test would miss."""
    path = tmp_path / "widget-cache-warmup.md"
    path.write_bytes(
        "---\r\ntitle: Widget Cache Warmup\r\ntags:\r\n---\r\n"
        "# Widget Cache Warmup\r\n\r\n- Repos: widget-cache\r\n".encode("utf-8")
    )
    doc = load_store(tmp_path).docs[0]

    apply_backfill(doc, propose_backfill(doc))

    front_matter, _ = split_front_matter(path.read_text(encoding="utf-8"))
    assert [ln for ln in front_matter.splitlines() if ln.startswith("tags")] == ["tags: [widget-cache]"]


def test_the_authors_other_front_matter_lines_and_comments_survive(tmp_path):
    write_file(
        tmp_path,
        "widget-cache-warmup.md",
        """---
# hand-written header comment
title: Widget Cache Warmup
capture_minutes: 7
tags:
---

# Widget Cache Warmup

- Repos: widget-cache
""",
    )
    path = tmp_path / "widget-cache-warmup.md"
    doc = load_store(tmp_path).docs[0]

    apply_backfill(doc, propose_backfill(doc))

    front_matter, _ = split_front_matter(path.read_text(encoding="utf-8"))
    assert "# hand-written header comment" in front_matter
    assert "capture_minutes: 7" in front_matter
    assert parse_document(path).capture_minutes == 7


def test_a_key_with_a_real_value_is_never_dropped(tmp_path):
    """A stated field is never among the ones written, so its line is never a candidate for
    removal — that guard, not the line test, is what keeps the author's value."""
    write_file(
        tmp_path,
        "widget-cache-warmup.md",
        """---
title: Widget Cache Warmup
tags: [platform-core]
entities:
---

# Widget Cache Warmup

- Repos: widget-cache

## Search Keywords

`WidgetCache`
""",
    )
    path = tmp_path / "widget-cache-warmup.md"
    doc = load_store(tmp_path).docs[0]

    apply_backfill(doc, propose_backfill(doc))

    front_matter, _ = split_front_matter(path.read_text(encoding="utf-8"))
    assert "tags: [platform-core]" in front_matter
    assert parse_document(path).tags == ["platform-core"]
    assert parse_document(path).entities == ["WidgetCache"]


def test_an_indented_line_that_looks_like_a_key_is_left_alone(tmp_path):
    """Only a top-level key can collide; a `tags:` inside a block scalar is the author's text."""
    write_file(
        tmp_path,
        "widget-cache-warmup.md",
        """---
title: Widget Cache Warmup
superseded_by: |
  tags:
tags:
---

# Widget Cache Warmup

- Repos: widget-cache
""",
    )
    path = tmp_path / "widget-cache-warmup.md"
    doc = load_store(tmp_path).docs[0]

    apply_backfill(doc, propose_backfill(doc))

    front_matter, _ = split_front_matter(path.read_text(encoding="utf-8"))
    assert "  tags:" in front_matter
    assert yaml.safe_load(front_matter)["superseded_by"].strip() == "tags:"
    assert yaml.safe_load(front_matter)["tags"] == ["widget-cache"]


def test_an_already_duplicated_key_keeps_the_authors_valued_line(tmp_path):
    """Only the empty declaration is removed; dropping the valued one would lose `[alpha,
    beta]`."""
    write_file(
        tmp_path,
        "widget-cache-warmup.md",
        """---
title: Widget Cache Warmup
tags: [alpha, beta]
tags:
---

# Widget Cache Warmup

- Repos: widget-cache
""",
    )
    path = tmp_path / "widget-cache-warmup.md"
    doc = load_store(tmp_path).docs[0]

    apply_backfill(doc, propose_backfill(doc))

    front_matter, _ = split_front_matter(path.read_text(encoding="utf-8"))
    assert "tags: [alpha, beta]" in front_matter
    # the append still wins for what engmem reads, exactly as before this rule existed
    assert parse_document(path).tags == ["widget-cache"]


@pytest.mark.parametrize("spelling", ['"tags":', "'tags':"])
def test_a_quoted_empty_key_is_replaced_not_duplicated(tmp_path, spelling):
    """PyYAML accepts a quoted key, so the document engmem reads has `tags` — a line test that
    only matched bare keys would leave exactly the duplicate this rule exists to prevent."""
    write_file(
        tmp_path,
        "widget-cache-warmup.md",
        f"""---
title: Widget Cache Warmup
{spelling}
---

# Widget Cache Warmup

- Repos: widget-cache
""",
    )
    path = tmp_path / "widget-cache-warmup.md"
    doc = load_store(tmp_path).docs[0]

    apply_backfill(doc, propose_backfill(doc))

    front_matter, _ = split_front_matter(path.read_text(encoding="utf-8"))
    assert [ln for ln in front_matter.splitlines() if "tags" in ln] == ["tags: [widget-cache]"]
    assert parse_document(path).tags == ["widget-cache"]


def test_a_cr_only_document_round_trips(tmp_path):
    """CR is a YAML line break too, so no fixup is needed — but the fields must still land."""
    path = tmp_path / "widget-cache-warmup.md"
    path.write_bytes(
        "---\rtitle: Widget Cache Warmup\r---\r# Widget Cache Warmup\r\r- Repos: widget-cache\r"
        .encode("utf-8")
    )
    doc = load_store(tmp_path).docs[0]

    apply_backfill(doc, propose_backfill(doc))

    reloaded = parse_document(path)
    assert reloaded.title == "Widget Cache Warmup"
    assert reloaded.tags == ["widget-cache"]


def test_appended_front_matter_takes_the_documents_own_line_ending(tmp_path):
    """The body is preserved byte-for-byte either way, but emitting LF into a CRLF document
    left it with both — exactly the whole-file diff this command exists to avoid."""
    path = tmp_path / "widget-cache-warmup.md"
    path.write_bytes(
        "---\r\ntitle: Widget Cache Warmup\r\n---\r\n"
        "# Widget Cache Warmup\r\n\r\n- Repos: widget-cache\r\n".encode("utf-8")
    )
    doc = load_store(tmp_path).docs[0]

    apply_backfill(doc, propose_backfill(doc))

    after = path.read_bytes()
    assert after.count(b"\n") == after.count(b"\r\n")
    assert after.startswith(b"---\r\n")
    assert parse_document(path).tags == ["widget-cache"]


def test_an_lf_document_is_untouched_by_the_line_ending_rule(tmp_path):
    write_file(
        tmp_path,
        "widget-cache-warmup.md",
        "---\ntitle: Widget Cache Warmup\n---\n\n# Widget Cache Warmup\n\n- Repos: widget-cache\n",
    )
    path = tmp_path / "widget-cache-warmup.md"
    doc = load_store(tmp_path).docs[0]

    apply_backfill(doc, propose_backfill(doc))

    assert b"\r" not in path.read_bytes()


def test_the_front_matters_own_line_ending_wins_over_the_bodys(tmp_path):
    """The block being extended is the front matter; a body that disagrees is not this
    command's to normalise, in either direction."""
    path = tmp_path / "widget-cache-warmup.md"
    path.write_bytes(
        "---\ntitle: Widget Cache Warmup\n---\n"
        "# Widget Cache Warmup\r\n\r\n- Repos: widget-cache\r\n".encode("utf-8")
    )
    doc = load_store(tmp_path).docs[0]

    apply_backfill(doc, propose_backfill(doc))

    after = path.read_bytes()
    front_matter = after.split(b"---")[1]
    assert b"\r" not in front_matter
    # the body keeps its own, untouched
    assert after.endswith(b"- Repos: widget-cache\r\n")


def test_a_zero_indented_line_inside_a_flow_collection_is_not_a_declaration(tmp_path):
    """A column-0 `status:` can belong to the value above it; removing it destroys the author's
    data."""
    write_file(
        tmp_path,
        "widget-cache-warmup.md",
        """---
title: Widget Cache Warmup
navigation_miss: {
status: missed,
query: cache}
---

# Widget Cache Warmup

- Repos: widget-cache
""",
    )
    path = tmp_path / "widget-cache-warmup.md"
    doc = load_store(tmp_path).docs[0]
    assert "status" in {f.name for f in propose_backfill(doc).fields}

    apply_backfill(doc, propose_backfill(doc))

    front_matter, _ = split_front_matter(path.read_text(encoding="utf-8"))
    assert "status: missed," in front_matter
    assert yaml.safe_load(front_matter)["navigation_miss"] == {"status": "missed", "query": "cache"}
    assert parse_document(path).status == "active"


def test_a_proposal_of_only_scalar_fields_writes(tmp_path):
    """A mapping with no collection dumps flow, and a flow mapping in block front matter is a
    syntax error."""
    write_file(
        tmp_path,
        "widget-cache-warmup.md",
        """---
id: widget-cache-warmup
title: Widget Cache Warmup
date: 2026-01-01
task_date: 2026-01-01
tags: [widget-cache]
entities: [WidgetCache]
related: [other-doc]
---

# Widget Cache Warmup

Body.
""",
    )
    path = tmp_path / "widget-cache-warmup.md"
    doc = load_store(tmp_path).docs[0]
    proposal = propose_backfill(doc)
    assert [f.name for f in proposal.fields] == ["status", "backfilled"]

    apply_backfill(doc, proposal)

    reloaded = parse_document(path)
    assert reloaded.status == "active"
    assert reloaded.backfilled is True
    assert reloaded.spine_complete is True


def test_a_full_proposal_still_renders_lists_inline(tmp_path):
    """Forcing block style for the mapping must not turn `tags: [a]` into a block sequence —
    that is a formatting change to every document backfill touches."""
    write_file(tmp_path, "widget-cache-warmup.md", LEGACY_WIDGET_CACHE)
    path = tmp_path / "widget-cache-warmup.md"
    doc = load_store(tmp_path).docs[0]

    apply_backfill(doc, propose_backfill(doc))

    front_matter, _ = split_front_matter(path.read_text(encoding="utf-8"))
    assert "tags: [widget-cache, platform-core]" in front_matter


# ---------------------------------------------------------------------------
# term splitting — a parenthetical is an aside, not a term boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        pytest.param("A ((x, y)), B", ["A ((x, y))", "B"], id="nested-parentheses-are-tracked"),
        pytest.param(
            "A) , B", ["A)", "B"],
            # the stray `)` must not leave depth negative
            id="a-stray-closing-paren-swallows-nothing",
        ),
        pytest.param(
            "A) (x, y), B", ["A) (x, y)", "B"],
            # ... or the `(` after it would read as depth 0 and the separator inside the
            # real parenthetical would split
            id="a-stray-close-does-not-corrupt-a-later-real-parenthetical",
        ),
        pytest.param(
            "A (x, B", ["A (x", "B"],
            # otherwise one unclosed `(` turns the rest of the line into a single term
            id="an-unbalanced-open-paren-protects-nothing",
        ),
    ],
)
def test_split_terms_paren_tracking(text, expected):
    """Compared with each part's own surrounding whitespace stripped: that whitespace is
    incidental (`_clean_candidate` strips it immediately downstream), and pinning it exactly
    would redden every case the day someone adds a behaviour-neutral `.strip()` here."""
    assert [part.strip() for part in _split_terms(text)] == expected


def test_a_flow_collection_entry_with_a_null_value_is_not_a_declaration(tmp_path):
    """The line-shape half of the rule, not just the value half."""
    write_file(
        tmp_path,
        "widget-cache-warmup.md",
        """---
title: Widget Cache Warmup
navigation_miss: {
status: null
}
---

# Widget Cache Warmup

- Repos: widget-cache
""",
    )
    path = tmp_path / "widget-cache-warmup.md"
    doc = load_store(tmp_path).docs[0]
    assert "status" in {f.name for f in propose_backfill(doc).fields}

    apply_backfill(doc, propose_backfill(doc))

    front_matter, _ = split_front_matter(path.read_text(encoding="utf-8"))
    assert yaml.safe_load(front_matter)["navigation_miss"] == {"status": None}
    assert parse_document(path).status == "active"


def test_an_unclosed_paren_does_not_cost_the_balanced_one_before_it():
    """The fallback that discarded the whole line brought the bug back for its balanced part:
    one unterminated aside further along resurrected `OrderAPI (v1` and `v2)`."""
    line = "- **Endpoints:** OrderAPI (v1, v2), PaymentAPI (see ticket #42"
    assert _split_terms(line)[0].endswith("OrderAPI (v1, v2)")
    assert "OrderAPI" in _extract_entities(line)
    assert "v2)" not in _extract_entities(line)


def test_a_backticked_term_survives_an_unclosed_paren_later_on():
    entities = _extract_entities("- **Classes:** `WidgetCache` (thread-safe, LRU), CacheWarmer (TODO")
    assert "WidgetCache" in entities


@pytest.mark.parametrize("declaration", ["tags:\n  null", "? tags\n: null"])
def test_a_null_written_below_its_key_leaves_the_line_alone(declaration):
    """Removing the key line alone would orphan the `null`, or leave a bare `: null`."""
    front_matter = f"title: Widget Cache Warmup\n{declaration}\n"

    assert _drop_declared_keys(front_matter, {"tags"}) == front_matter


# ---------------------------------------------------------------------------
# apply_backfill — what the replaced file inherits
# ---------------------------------------------------------------------------

def test_a_flow_style_root_refuses_the_write_rather_than_eating_a_key(tmp_path):
    """A flow-style root (`{title: T, tags: null}`) shares every pair on one line, so removing
    just `tags:` would take `title` with it too — contracts/backfill.md leaves it alone and
    refuses the write instead of guessing."""
    path = write_file(
        tmp_path,
        "widget-cache-warmup.md",
        "---\n{title: Widget Cache Warmup, tags: null}\n---\n\n"
        "# Widget Cache Warmup\n\n- Repos: widget-cache\n",
    )
    before = path.read_bytes()
    doc = load_store(tmp_path).docs[0]

    with pytest.raises(BackfillWriteError, match="does not parse"):
        apply_backfill(doc, propose_backfill(doc))

    assert path.read_bytes() == before
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


@requires_posix_modes
@pytest.mark.parametrize("mode", [0o600, 0o644, 0o664])
def test_the_documents_file_mode_survives_the_write(tmp_path, mode):
    """`os.replace` puts a new file there: left to the umask it widens 0600, staged at 0600 it
    narrows 0644."""
    write_file(tmp_path, "widget-cache-warmup.md", "# Widget Cache Warmup\n\n- Repos: widget-cache\n")
    path = tmp_path / "widget-cache-warmup.md"
    os.chmod(path, mode)
    doc = load_store(tmp_path).docs[0]

    apply_backfill(doc, propose_backfill(doc))

    assert stat.S_IMODE(os.stat(path).st_mode) == mode
    assert parse_document(path).tags == ["widget-cache"]


@requires_posix_modes
def test_the_staged_file_is_never_wider_than_the_document(tmp_path, monkeypatch):
    """Narrowing after staging is not enough: the temp file holds the whole body, and the
    read-back, the split and a full parse all happen before any chmod could run."""
    write_file(tmp_path, "widget-cache-warmup.md", "# Widget Cache Warmup\n\n- Repos: widget-cache\n")
    path = tmp_path / "widget-cache-warmup.md"
    os.chmod(path, 0o600)
    doc = load_store(tmp_path).docs[0]

    seen: list[int] = []
    real_parse_one = backfill.parse_document

    def record_then_parse(tmp_path_arg):
        seen.append(stat.S_IMODE(os.stat(tmp_path_arg).st_mode))
        return real_parse_one(tmp_path_arg)

    monkeypatch.setattr(backfill, "parse_document", record_then_parse)
    apply_backfill(doc, propose_backfill(doc))

    assert seen and all(mode <= 0o600 for mode in seen), seen


@requires_symlinks
def test_a_symlink_out_of_the_store_is_refused(tmp_path):
    """Writing through would make this the only path here that writes outside the store."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    outside = elsewhere / "shellrc"
    outside.write_text("# Widget Cache Warmup\n\nexport PATH=/usr/bin\n", encoding="utf-8")
    (sessions / "widget-cache-warmup.md").symlink_to(outside)
    before = outside.read_bytes()

    doc = load_store(sessions).docs[0]
    with pytest.raises(BackfillWriteError, match="is a symlink"):
        apply_backfill(doc, propose_backfill(doc))

    assert outside.read_bytes() == before
    assert not [p for p in elsewhere.iterdir() if p.name.endswith(".tmp")]


@requires_symlinks
def test_a_symlink_inside_the_store_is_refused_too(tmp_path):
    """The two names share an inode, so the alias's `id` lands in the real document too."""
    real = tmp_path / "widget-cache-real.md"
    real.write_text("# Widget Cache Warmup\n\n- Repos: widget-cache\n", encoding="utf-8")
    link = tmp_path / "widget-cache-alias.md"
    link.symlink_to(real)
    before = real.read_bytes()

    doc = next(d for d in load_store(tmp_path).docs if d.path == link)
    with pytest.raises(BackfillWriteError, match="is a symlink"):
        apply_backfill(doc, propose_backfill(doc))

    assert real.read_bytes() == before
    assert link.is_symlink()
    assert load_store(tmp_path).errors == []


# ---------------------------------------------------------------------------
# apply_backfill — the proposal has to still describe the document
# ---------------------------------------------------------------------------


def test_a_document_edited_since_the_proposal_is_not_written(tmp_path):
    """The body is re-read at write time, so the edit survives the byte check — the values go
    stale silently."""
    write_file(
        tmp_path,
        "widget-cache-warmup.md",
        "# Widget Cache Warmup\n\n- Date: 2026-05-04\n\n## Notes\n\nProse.\n",
    )
    path = tmp_path / "widget-cache-warmup.md"
    doc = load_store(tmp_path).docs[0]
    proposal = propose_backfill(doc)

    # the author adds the section the note asked for while the prompt is waiting
    path.write_text(
        path.read_text(encoding="utf-8") + "\n## Search Keywords\n\n`WidgetCache`\n",
        encoding="utf-8",
    )
    before = path.read_bytes()

    with pytest.raises(BackfillWriteError, match="changed on disk"):
        apply_backfill(doc, proposal)

    assert path.read_bytes() == before


def test_an_edit_between_the_store_load_and_the_proposal_is_not_overwritten(tmp_path):
    """The values come from `doc.body`, read by `load_store` — a whole directory scan before the
    proposal."""
    write_file(
        tmp_path,
        "widget-cache-warmup.md",
        "# Widget Cache Warmup\n\n- Date: 2026-05-04\n\n## Search Keywords\n\n`WidgetCache`\n",
    )
    path = tmp_path / "widget-cache-warmup.md"
    doc = load_store(tmp_path).docs[0]

    path.write_text(
        path.read_text(encoding="utf-8").replace("`WidgetCache`", "`CacheWarmer`, `EvictionPolicy`"),
        encoding="utf-8",
    )
    before = path.read_bytes()
    proposal = propose_backfill(doc)

    # the proposal still describes the file as it was, which is the point
    assert [f.value for f in proposal.fields if f.name == "entities"] == [["WidgetCache"]]

    with pytest.raises(BackfillWriteError, match="changed on disk"):
        apply_backfill(doc, proposal)

    assert path.read_bytes() == before


def test_an_edit_landing_between_the_staleness_check_and_the_read_is_not_written(
    tmp_path, monkeypatch
):
    """The stamp is compared after the re-read, so it covers that read too — taken before it,
    an edit landing in between was certified by a check that had already run."""
    write_file(
        tmp_path,
        "widget-cache-warmup.md",
        "# Widget Cache Warmup\n\n- Date: 2026-05-04\n\n## Search Keywords\n\n`WidgetCache`\n",
    )
    path = tmp_path / "widget-cache-warmup.md"
    doc = load_store(tmp_path).docs[0]
    proposal = propose_backfill(doc)

    real_read_document = backfill.read_document

    def edit_then_read(target):
        # `apply_backfill` reads the document once and the staged file after it, so only the
        # first call is the window being simulated. The edit changes the file's size, since the
        # residual this key knowingly leaves is a size-preserving edit inside one mtime tick
        if target == path:
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "`WidgetCache`", "`CacheWarmer`, `EvictionPolicy`"
                ),
                encoding="utf-8",
            )
        return real_read_document(target)

    monkeypatch.setattr(backfill, "read_document", edit_then_read)

    with pytest.raises(BackfillWriteError, match="changed on disk"):
        apply_backfill(doc, proposal)

    # the stale `entities: [WidgetCache]` never reached the edited document
    assert "entities" not in path.read_text(encoding="utf-8")


def test_an_unstattable_document_is_not_read_as_unchanged(tmp_path, monkeypatch):
    """`None != None` is False, so a proposal carrying no stamp, against a file that cannot be
    stat'd, passed the staleness check on no evidence at all."""
    write_file(tmp_path, "widget-cache-warmup.md", LEGACY_WIDGET_CACHE)
    path = tmp_path / "widget-cache-warmup.md"
    doc = load_store(tmp_path).docs[0]
    proposal = propose_backfill(doc)
    proposal.source_identity = None
    before = path.read_bytes()

    monkeypatch.setattr(backfill, "identity_for", lambda _path: None)

    with pytest.raises(BackfillWriteError, match="changed on disk"):
        apply_backfill(doc, proposal)

    assert path.read_bytes() == before


def test_a_proposal_for_another_document_is_not_written(tmp_path):
    """Writing one document's derived values into another would put a foreign `id` in its front
    matter, and `load_store` would then report a duplicate id across the two."""
    write_file(tmp_path, "widget-cache-warmup.md", LEGACY_WIDGET_CACHE)
    write_file(tmp_path, "mq-sweeper-policy.md", "# MQ Sweeper Policy\n\n- Date: 2026-06-10\n")
    docs = {d.id: d for d in load_store(tmp_path).docs}
    other_proposal = propose_backfill(docs["mq-sweeper-policy"])
    target = tmp_path / "widget-cache-warmup.md"
    before = target.read_bytes()

    with pytest.raises(BackfillWriteError, match="proposal is for mq-sweeper-policy"):
        apply_backfill(docs["widget-cache-warmup"], other_proposal)

    assert target.read_bytes() == before


# ---------------------------------------------------------------------------
# superseded_by — a superseded document that points nowhere is a dead end
# ---------------------------------------------------------------------------


def test_the_successor_named_on_the_status_line_becomes_superseded_by(tmp_path):
    """A superseded document with no successor is a dead end for both the renderer and the
    search."""
    write_file(
        tmp_path,
        "widget-cache-warmup.md",
        "# Widget Cache Warmup\n\n- Date: 2026-05-04\n"
        "- Status: Superseded by [Widget Cache v2](widget-cache-v2.md)\n",
    )
    doc = load_store(tmp_path).docs[0]

    by_name = {f.name: f for f in propose_backfill(doc).fields}

    assert by_name["status"].value == "superseded"
    assert by_name["superseded_by"].value == "widget-cache-v2"
    assert "Status:" in by_name["superseded_by"].source


def test_an_active_document_never_gets_a_superseded_by(tmp_path):
    write_file(
        tmp_path,
        "widget-cache-warmup.md",
        "# Widget Cache Warmup\n\n- Status: Delivered — see [v2](widget-cache-v2.md)\n",
    )
    doc = load_store(tmp_path).docs[0]

    assert "superseded_by" not in {f.name for f in propose_backfill(doc).fields}


def test_a_document_that_supersedes_another_is_not_marked_as_the_superseded_one(tmp_path):
    """The worst shape of the stem test: the live document was written `status: superseded` and
    pointed at the document it had itself replaced, so the reader was sent backwards."""
    write_file(
        tmp_path,
        "widget-cache-v2.md",
        "# Widget Cache v2\n\n- Date: 2026-05-04\n"
        "- Status: Active — supersedes [Widget Cache](widget-cache-v1.md)\n",
    )
    doc = load_store(tmp_path).docs[0]

    by_name = {f.name: f for f in propose_backfill(doc).fields}

    assert by_name["status"].value == "active"
    assert "superseded_by" not in by_name
    # the link is still an edge, just not a successor
    assert by_name["related"].value == ["widget-cache-v1"]


def test_a_superseded_line_naming_no_sibling_proposes_no_successor(tmp_path):
    """Nothing is guessed from prose: `- Status: superseded` alone names no document, and an
    invented id would render as `(not in store)` forever."""
    write_file(
        tmp_path,
        "widget-cache-warmup.md",
        "# Widget Cache Warmup\n\n- Status: superseded, replaced by the new cache\n",
    )
    doc = load_store(tmp_path).docs[0]

    by_name = {f.name: f for f in propose_backfill(doc).fields}
    assert by_name["status"].value == "superseded"
    assert "superseded_by" not in by_name


def test_a_stated_status_the_line_contradicts_gets_no_successor(tmp_path):
    """The author declared this document active; `superseded_by` would then be a
    replaced-by claim written onto the live document, aimed at the one it replaced —
    and `status` is not proposed at all, so nothing else on the write would say so."""
    write_file(
        tmp_path,
        "widget-cache-v2.md",
        "---\nstatus: active\n---\n# Widget Cache v2\n\n- Date: 2026-05-04\n"
        "- Status: Active — this superseded [the old flow](widget-cache-v1.md)\n",
    )
    doc = load_store(tmp_path).docs[0]

    by_name = {f.name: f for f in propose_backfill(doc).fields}

    assert "status" not in by_name
    assert "superseded_by" not in by_name
    # the link is still an edge, just not a successor
    assert by_name["related"].value == ["widget-cache-v1"]


def test_a_stated_superseded_status_still_takes_the_successor_from_the_line(tmp_path):
    write_file(
        tmp_path,
        "widget-cache-warmup.md",
        "---\nstatus: superseded\n---\n# Widget Cache Warmup\n\n- Date: 2026-05-04\n"
        "- Status: Superseded by [v2](widget-cache-v2.md)\n",
    )
    doc = load_store(tmp_path).docs[0]

    by_name = {f.name: f for f in propose_backfill(doc).fields}

    assert "status" not in by_name
    assert by_name["superseded_by"].value == "widget-cache-v2"


def test_an_empty_status_line_never_supersedes_the_document(tmp_path):
    """End to end: the preamble line below the empty one is another field entirely, so the
    document is active and has no successor."""
    write_file(
        tmp_path,
        "widget-cache-warmup.md",
        "# Widget Cache Warmup\n\n- Date: 2026-05-04\n- Status:\n"
        "- Notes: superseded by [the old flow](widget-cache-v1.md)\n",
    )
    doc = load_store(tmp_path).docs[0]

    by_name = {f.name: f for f in propose_backfill(doc).fields}

    assert by_name["status"].value == "active"
    assert "superseded_by" not in by_name


def test_unfilled_labels_do_not_shadow_the_real_ones_below_them(tmp_path):
    """The permanent dead end: a blank `- Repos:` above the real one proposed `tags: []`,
    which counts as answered, so `--all` would never offer the document again and both repo
    names would be lost for good — while the blank `- Status:` above the real one dropped
    the successor and left a replaced document rankable with nothing to redirect to."""
    write_file(
        tmp_path,
        "widget-cache-warmup.md",
        "# Widget Cache Warmup\n\n- Date: 2026-05-04\n- Status: \n"
        "- Status: Superseded by [v2](widget-cache-v2.md)\n- Repos: \n"
        "- Repos: widget-cache, platform-core\n",
    )
    doc = load_store(tmp_path).docs[0]

    by_name = {f.name: f for f in propose_backfill(doc).fields}

    assert by_name["status"].value == "superseded"
    assert by_name["superseded_by"].value == "widget-cache-v2"
    assert by_name["tags"].value == ["widget-cache", "platform-core"]
    assert by_name["repos"].value == ["widget-cache", "platform-core"]


def test_a_blank_label_line_is_not_reported_as_a_value(tmp_path):
    """The human approves the write on these strings. A document carrying two blank labels
    was told a value had been read off them, and — before that — that no such line existed."""
    write_file(
        tmp_path,
        "widget-cache-warmup.md",
        "# Widget Cache Warmup\n\n- Date: 2026-05-04\n- Status: \n- Repos: \n",
    )
    doc = load_store(tmp_path).docs[0]

    by_name = {f.name: f for f in propose_backfill(doc).fields}

    assert by_name["status"].value == "active"
    assert by_name["status"].source == (
        "no '- Status:' preamble line with a value found — defaulted to active"
    )
    assert by_name["tags"].source == "no '- Repos:' preamble line with a value found"


def test_superseded_by_survives_the_write_and_reloads(tmp_path):
    write_file(
        tmp_path,
        "widget-cache-warmup.md",
        "# Widget Cache Warmup\n\n- Date: 2026-05-04\n"
        "- Status: Superseded by [v2](widget-cache-v2.md)\n",
    )
    path = tmp_path / "widget-cache-warmup.md"
    doc = load_store(tmp_path).docs[0]

    apply_backfill(doc, propose_backfill(doc))

    reloaded = parse_document(path)
    assert reloaded.status == "superseded"
    assert reloaded.superseded_by == "widget-cache-v2"
