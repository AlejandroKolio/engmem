"""Ranking, ambiguity clustering and body-cache behaviour of `engmem.scoring` — the gN
golden tables of ENGMEM-SPEC.md §8 included."""

import pytest

from conftest import fixture_docs

import dataclasses
import json

from engmem import cache
from engmem.scoring import (
    _NULLABLE_PAYLOAD_FIELDS,
    _PAYLOAD_FIELD_TYPES,
    _build_section_index,
    _entries_for_doc,
    _section_entry_from_payload,
    _section_to_payload,
    _tokenize_counts,
    role_coverage,
    search,
    search_with_role_sections,
)
from engmem.sections import Section
from engmem.spine import load_store


@pytest.fixture(autouse=True)
def _isolated_body_cache(tmp_path, monkeypatch):
    """Every test gets its own cache directory, isolated from `~/.cache/engmem` and from every
    other test."""
    monkeypatch.setattr(cache, "cache_root", lambda: tmp_path / "engmem-cache-dir")


def _top_id(outcome):
    return outcome.hits[0].doc.id


@pytest.mark.parametrize(
    "query,expected_top_id",
    [
        pytest.param("1000001", "1000001-response-cache", id="g1_exact_id_ranks_first"),
        pytest.param(
            "ResponseCacheController",
            "1000001-response-cache",
            id="g3_class_name_ranks_first",
        ),
        pytest.param(
            "response cache",
            "1000001-response-cache",
            id="g4_camelcase_fragments_rank_first",
        ),
        pytest.param(
            "MessageQueue sweeper",
            "mq-message-sweeper",
            id="g7_class_plus_tag_two_token_coverage_ranks_first",
        ),
        pytest.param(
            "etag revalidation",
            "ttl-etag-revalidation-v2",
            id="g11_two_token_coverage_ranks_revalidation_first",
        ),
    ],
)
def test_gN_query_ranks_expected_document_first(query, expected_top_id):
    outcome = search(fixture_docs(), query)
    assert _top_id(outcome) == expected_top_id


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("482", id="g2_numeric_prefix_does_not_match"),
        pytest.param("ET", id="g10_short_token_et_does_not_match_etag"),
    ],
)
def test_gN_query_yields_no_hits(query):
    outcome = search(fixture_docs(), query)
    assert outcome.hits == []


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("MQ", id="g5_short_ambiguous_token_returns_two_clusters"),
        pytest.param("mq", id="g6_lowercase_short_token_identical_to_g5"),
    ],
)
def test_gN_short_token_is_ambiguous_across_two_clusters(query):
    outcome = search(fixture_docs(), query)
    assert outcome.ambiguous is True
    ids = {h.doc.id for h in outcome.hits}
    assert ids == {"mq-message-sweeper", "metrics-query-refactor"}


def test_g8_non_latin_noise_does_not_break_and_does_not_match():
    outcome = search(fixture_docs(), "σφάλματα και MessageQueue sweeper")
    baseline = search(fixture_docs(), "MessageQueue sweeper")

    assert _top_id(outcome) == "mq-message-sweeper"

    # "does not match": the non-Latin tokens must contribute nothing to scoring —
    # same hit count and same scores as the query with them stripped out, and no
    # non-Latin token appears anywhere in any hit's matched fields.
    assert [h.doc.id for h in outcome.hits] == [h.doc.id for h in baseline.hits]
    assert [h.score for h in outcome.hits] == [h.score for h in baseline.hits]
    matched_tokens = {
        token
        for hit in outcome.hits
        for tokens in hit.matched_fields.values()
        for token in tokens
    }
    assert all(token.isascii() for token in matched_tokens), (
        f"a non-Latin token leaked into matched fields: {matched_tokens!r}"
    )


def test_g9_etag_ranks_response_and_revalidation_above_rest():
    outcome = search(fixture_docs(), "ETAG")
    # the corpus produces exactly these two hits for "ETAG" — the query returns no third
    # hit for the old `hits[2:]` loop to compare against, and scoring sorts by `-score`
    # so a comparison against `hits[0]` would hold by construction regardless
    assert len(outcome.hits) == 2
    top_ids = {h.doc.id for h in outcome.hits}
    assert top_ids == {"1000001-response-cache", "ttl-etag-revalidation-v2"}
    assert outcome.hits[1].score < outcome.hits[0].score


def test_nfkc_runs_before_tokenization(tmp_path):
    """Splitting on ASCII first dropped fullwidth characters entirely and mangled a ligature
    into a plausible but wrong token."""
    (tmp_path / "filter-doc.md").write_text("""---
id: filter-doc
title: Filter Chain Notes
date: 2026-08-01
task_date: 2026-08-01
status: active
superseded_by:
backfilled: false
tags: [filter]
entities: [FilterChain]
related: []
covers_files: []
verified_at_commit: abc
capture_minutes: 1
---

## Cold-start primer

Filter.
""")
    docs = load_store(tmp_path).docs

    ligature_outcome = search(docs, "ﬁlter")  # 'ﬁlter'
    assert [h.doc.id for h in ligature_outcome.hits] == ["filter-doc"]

    fullwidth_outcome = search(fixture_docs(), "ＭＱ")  # 'ＭＱ'
    plain_outcome = search(fixture_docs(), "MQ")
    assert {h.doc.id for h in fullwidth_outcome.hits} == {
        h.doc.id for h in plain_outcome.hits
    }
    assert fullwidth_outcome.ambiguous == plain_outcome.ambiguous


def test_shared_acronym_with_same_meaning_is_not_ambiguous():
    """Ambiguity requires the full forms to differ, and no CamelCase entity here expands to
    `ttl`."""
    outcome = search(fixture_docs(), "TTL")
    assert outcome.ambiguous is False
    ids = [h.doc.id for h in outcome.hits]
    assert "1000001-response-cache" in ids
    assert "ttl-etag-revalidation-v2" in ids


AMBIG_DOC = """---
id: {id}
title: {title}
date: 2026-07-0{n}
task_date: 2026-07-0{n}
status: active
superseded_by:
backfilled: false
tags: {tags}
entities: {entities}
related: []
covers_files: []
verified_at_commit: eee000{n}
capture_minutes: 1
---

## Cold-start primer

Doc {id}.
"""


def _write_ambig_store(tmp_path):
    specs = [
        ("queue-doc", "Message Queues", "[platform]", "[MessageQueue, MQ]", 1),
        ("metrics-doc", "Metrics Queries", "[platform]", "[MetricsQuery, MQ]", 2),
        ("runbook-doc", "MQ Runbook", "[mq]", "[RunbookThing]", 3),
    ]
    for id_, title, tags, entities, n in specs:
        content = (
            AMBIG_DOC.replace("{id}", id_)
            .replace("{title}", title)
            .replace("{tags}", tags)
            .replace("{entities}", entities)
            .replace("{n}", str(n))
        )
        (tmp_path / f"{id_}.md").write_text(content)


def test_the_cluster_filter_keeps_non_entity_matches_and_flags_only_representatives(tmp_path):
    """A document matching via title or tags must not be deleted when the entity-cluster rule
    triggers for others — nor carry the [ambiguous] mark that belongs to the cluster."""
    _write_ambig_store(tmp_path)
    docs = load_store(tmp_path).docs

    outcome = search(docs, "MQ")

    assert outcome.ambiguous is True
    ids = {h.doc.id for h in outcome.hits}
    assert {"queue-doc", "metrics-doc"}.issubset(ids)
    assert "runbook-doc" in ids, "title/tag match must survive the cluster filter"

    flags = {h.doc.id: h.ambiguous for h in outcome.hits}
    assert flags["queue-doc"] is True
    assert flags["metrics-doc"] is True
    assert flags["runbook-doc"] is False


# ---------------------------------------------------------------------------
# id weight — a slug word alone is only as informative as a title word (weight 2); weight 5 is
# reserved for a whole-id match or the numeric ticket component.
# ---------------------------------------------------------------------------

ID_WEIGHT_DOC = """---
id: {id}
title: {title}
date: 2026-07-0{n}
task_date: 2026-07-0{n}
status: active
superseded_by:
backfilled: false
tags: []
entities: []
related: []
covers_files: []
verified_at_commit: fff000{n}
capture_minutes: 1
---

## Cold-start primer

Session capture pending review.
"""


def _write_id_weight_doc(tmp_path, filename, id_, title, n):
    content = (
        ID_WEIGHT_DOC.replace("{id}", id_).replace("{title}", title).replace("{n}", str(n))
    )
    (tmp_path / filename).write_text(content)


def test_id_slug_word_alone_scores_like_a_title_word_not_full_id_weight(tmp_path):
    _write_id_weight_doc(tmp_path, "a.md", "widget-cache-warmer", "Something Unrelated", 1)
    _write_id_weight_doc(tmp_path, "b.md", "other-doc", "Cache Notes", 2)
    docs = load_store(tmp_path).docs

    outcome_id = search(docs, "warmer")  # only in doc a's id, as one slug word
    outcome_title = search(docs, "notes")  # only in doc b's title, one word

    id_hit = next(h for h in outcome_id.hits if h.doc.id == "widget-cache-warmer")
    title_hit = next(h for h in outcome_title.hits if h.doc.id == "other-doc")

    assert id_hit.score == title_hit.score, (
        "an id slug word alone must score at the title tier (weight 2), not the "
        "old flat weight 5 that let every slug word rank as strongly as the id itself"
    )


def test_id_whole_match_still_scores_at_full_id_weight(tmp_path):
    _write_id_weight_doc(tmp_path, "a.md", "widget-cache-warmer", "Unrelated Title Text", 1)
    docs = load_store(tmp_path).docs

    outcome = search(docs, "widget cache warmer")  # covers every slug component of the id
    hit = outcome.hits[0]

    assert hit.doc.id == "widget-cache-warmer"
    assert set(hit.matched_fields.get("id", [])) == {"widget", "cache", "warmer"}
    # 3 tokens matched at the full id weight (5), full coverage (3/3)
    assert hit.score == 15.0


def test_id_numeric_ticket_component_is_weight_five_even_without_full_slug_coverage(tmp_path):
    _write_id_weight_doc(tmp_path, "a.md", "9001-pricing-engine", "Unrelated Title", 1)
    docs = load_store(tmp_path).docs

    outcome = search(docs, "9001")
    hit = outcome.hits[0]

    assert hit.doc.id == "9001-pricing-engine"
    assert hit.score == 5.0  # 1 token matched, weight 5, full coverage (1/1)


# ---------------------------------------------------------------------------
# body index and BM25 ranking
# ---------------------------------------------------------------------------

BODY_DOC = """---
id: {id}
title: {title}
date: 2026-07-0{n}
task_date: 2026-07-0{n}
status: active
superseded_by:
backfilled: false
tags: {tags}
entities: {entities}
related: []
covers_files: []
verified_at_commit: bbb000{n}
capture_minutes: 5
---

{body}
"""


def _write_body_doc(tmp_path, filename, id_, title, n, body, tags="[]", entities="[]"):
    assert 1 <= n <= 9, f"BODY_DOC renders 2026-07-0{{n}}; n must be 1..9, got {n}"
    content = (
        BODY_DOC.replace("{id}", id_)
        .replace("{title}", title)
        .replace("{n}", str(n))
        .replace("{body}", body)
        .replace("{tags}", tags)
        .replace("{entities}", entities)
    )
    (tmp_path / filename).write_text(content, encoding="utf-8")


def test_a_body_only_match_names_its_section_and_no_spine_field(tmp_path):
    """A concept explained only in the body must still be found, and the line must name which
    section it lives in — and only that: a spine field it never matched must not be listed."""
    body = """## Pre-reg

Unrelated setup notes.

## Production Considerations

LeaseGuard guards the job so only one node runs it at a time.
"""
    _write_body_doc(tmp_path, "a.md", "sweeper-job-doc", "Sweeper Job", 1, body)
    docs = load_store(tmp_path).docs

    outcome = search(docs, "LeaseGuard")

    assert len(outcome.hits) == 1
    hit = outcome.hits[0]
    assert hit.doc.id == "sweeper-job-doc"
    assert hit.score > 0
    section_keys = [k for k in hit.matched_fields if k.startswith("§")]
    assert len(section_keys) == 1
    assert "production-considerations" in section_keys[0]
    assert "leaseguard" in hit.matched_fields[section_keys[0]]

    for spine_field in ("id", "title", "tags", "entities"):
        assert spine_field not in hit.matched_fields


def test_repeating_a_matching_section_does_not_multiply_the_documents_score(tmp_path):
    """`_body_scores_from_entries` takes a document's single best-scoring section, not the
    sum of every matching one — else a document would win purely by repeating one section
    many times over. That promise is about the *multiplier*, not about which of two
    differently-shaped documents ranks first: corpus-wide statistics (avgdl, df) still let
    the 8-section document nudge narrowly ahead of the 1-section one with the identical
    text (measured ~1.02x). What the 1.5x bound below actually catches is a regression from
    max to sum, which multiplies the score by the section count (measured ~8.15x for 8
    sections)."""
    explainer = (
        "WidgetCache preloads the registry on boot so the first request never "
        "pays a cold-start penalty for the lookup."
    )
    long_body = "\n\n".join(f"## Section {i}\n\n{explainer}" for i in range(1, 9))
    short_body = f"## Cold-start primer\n\n{explainer}"

    _write_body_doc(tmp_path, "long.md", "long-doc", "Long Reference Document", 1, long_body)
    _write_body_doc(tmp_path, "short.md", "short-doc", "Short Note", 2, short_body)

    # filler docs unrelated to the query, purely to dilute the corpus so the query
    # terms stay under the df/N ceiling (otherwise, with only 2 docs, the repeated
    # explainer text alone would already exceed 50% of all sections and be dropped
    # from body scoring entirely — a different effect than the one under test here)
    filler_sections = [
        f"## Filler {i}\n\nNothing about the query terms in this filler section."
        for i in range(1, 5)
    ]
    filler_body = "\n\n".join(filler_sections)
    for k in (3, 4, 5):
        _write_body_doc(tmp_path, f"filler-{k}.md", f"filler-doc-{k}", f"Filler Doc {k}", k, filler_body)

    docs = load_store(tmp_path).docs

    outcome = search(docs, "WidgetCache preloads registry")

    hits_by_id = {h.doc.id: h for h in outcome.hits}
    long_score = hits_by_id["long-doc"].score
    short_score = hits_by_id["short-doc"].score

    assert long_score < short_score * 1.5, (
        f"long doc ({long_score}) must not blow past the short doc ({short_score}) "
        f"just for repeating the same section 8 times over"
    )


def test_restricted_query_token_matches_body_literal_only(tmp_path):
    """A short or numeric token may match only text as literally written, or `MQ` would match
    every CamelCase word so initialled."""
    literal_body = "## Notes\n\nThe MQ backlog was cleared manually.\n"
    camel_only_body = "## Notes\n\nMessageQueue backlog was cleared manually.\n"

    _write_body_doc(tmp_path, "literal.md", "literal-doc", "Backlog Cleanup Note", 1, literal_body)
    _write_body_doc(tmp_path, "camel.md", "camel-doc", "Backlog Cleanup Log", 2, camel_only_body)
    docs = load_store(tmp_path).docs

    outcome = search(docs, "MQ")
    ids = {h.doc.id for h in outcome.hits}

    assert "literal-doc" in ids
    assert "camel-doc" not in ids, (
        "a restricted token must not match the body via a CamelCase-derived "
        "fragment/acronym, only via literal text"
    )


def test_a_short_camelcase_fragment_matches_only_literal_spine_text(tmp_path):
    """§7's short-token rule is about the query token, not about where it came from. A `mq`
    split out of `MQSweeper` used to be exempt from it in the spine index while the body index
    applied it, so one query token was restricted in one half of the score and not the other."""
    body = "## Notes\n\nNothing else here.\n"
    _write_body_doc(tmp_path, "a.md", "alpha-one", "Queue Notes", 1, body, entities="[MQ]")
    _write_body_doc(
        tmp_path, "b.md", "beta-two", "Query Notes", 2, body, entities="[MetricsQuery]"
    )
    docs = load_store(tmp_path).docs

    outcome = search(docs, "MQSweeper")

    assert [h.doc.id for h in outcome.hits] == ["alpha-one"], (
        "`mq` may match the entity written literally as MQ, and must not reach "
        "MetricsQuery through its CamelCase acronym"
    )
    assert outcome.hits[0].matched_fields == {"entities": ["mq"]}


def test_a_query_token_the_expansion_repeats_is_counted_once(tmp_path):
    """`cache ResponseCache` expands to `cache` twice. Counted twice it inflated both the weight
    sum and the coverage denominator, so a document matching that one word outranked a document
    matching a different query word just as fully."""
    body = "## Notes\n\nNothing else here.\n"
    _write_body_doc(tmp_path, "a.md", "alpha-one", "Cache Notes", 1, body)
    _write_body_doc(tmp_path, "b.md", "beta-two", "Response Times", 2, body)
    docs = load_store(tmp_path).docs

    scores = {h.doc.id: h.score for h in search(docs, "cache ResponseCache").hits}

    assert set(scores) == {"alpha-one", "beta-two"}
    assert scores["alpha-one"] == scores["beta-two"], (
        "both documents match exactly one distinct query token at the title tier"
    )


def test_ubiquitous_body_term_is_dropped_from_body_scoring(tmp_path):
    """A term in over half the corpus's sections is dropped — the self-tuning stand-in for a
    stopword list."""
    # 3 docs x 4 sections; "ubiquitous" appears in 3 of every 4 sections per doc,
    # well over the 50% df/N ceiling corpus-wide, and nowhere in any spine field
    for n in (1, 2, 3):
        body = "\n\n".join(
            f"## Section {i}\n\nubiquitous filler text here." for i in range(1, 4)
        ) + f"\n\n## Section 4\n\nNothing special in doc {n}."
        _write_body_doc(tmp_path, f"doc-{n}.md", f"doc-{n}", f"Doc {n}", n, body)
    docs = load_store(tmp_path).docs

    outcome = search(docs, "ubiquitous")

    assert outcome.hits == [], (
        "a term over the df/N ceiling must contribute nothing to body scoring, and "
        "with no spine field mentioning it either, no document should match at all"
    )


# ---------------------------------------------------------------------------
# `_build_section_index` (via `engmem.cache`) must be transparent — identical results
# whether the body-token index came from a cold cache, a warm cache, or a cache invalidated by an
# edit — and must actually avoid re-tokenizing an unchanged document on a warm run.
# ---------------------------------------------------------------------------


def _outcome_signature(outcome):
    """One equality check over everything a reader of `SearchOutcome` sees, rather than several
    that could each miss a field."""
    return [
        (
            h.doc.id,
            h.score,
            h.ambiguous,
            {k: list(v) for k, v in h.matched_fields.items()},
            [(sh.section.locator, sh.score, list(sh.matched_tokens)) for sh in h.section_hits],
        )
        for h in outcome.hits
    ]


def _write_cache_bench_docs(tmp_path, n=6):
    body = """## Problem

The WidgetCache eviction policy dropped hot keys too aggressively.

## Decision Log

Reused the existing SweeperJob scheduler instead of building a new one.

## Lessons Learned

CacheWarmer must run before traffic is shifted, not after.
"""
    # `BODY_DOC` renders the date as `2026-07-0{n}`, so the day number starts at 1: passing the
    # 0-based index wrote `2026-07-00` into `doc-0.md`, which never loaded and left the helper
    # silently one document short of what every caller asked for
    for i in range(n):
        _write_body_doc(
            tmp_path, f"doc-{i}.md", f"widget-{i}", f"Widget Session {i}", i + 1, body
        )
    store = load_store(tmp_path)
    assert not store.errors, store.errors
    return store.docs


def test_warm_cache_produces_identical_results_to_cold_cache(tmp_path):
    docs = _write_cache_bench_docs(tmp_path)

    cold = search(docs, "WidgetCache eviction")
    warm = search(docs, "WidgetCache eviction")

    assert _outcome_signature(cold) == _outcome_signature(warm)
    assert len(cold.hits) > 0


def test_second_search_does_not_retokenize_unchanged_documents(tmp_path, monkeypatch):
    import engmem.scoring as scoring_module

    docs = _write_cache_bench_docs(tmp_path)
    search(docs, "WidgetCache eviction")  # populates the cache for every doc

    calls = []
    real_tokenize = scoring_module._tokenize_counts

    def _spy(text):
        calls.append(text)
        return real_tokenize(text)

    monkeypatch.setattr(scoring_module, "_tokenize_counts", _spy)
    search(docs, "WidgetCache eviction")

    assert calls == [], "a warm cache must skip re-tokenizing every unchanged document"


def test_editing_one_document_only_retokenizes_that_document(tmp_path, monkeypatch):
    import engmem.scoring as scoring_module

    docs = _write_cache_bench_docs(tmp_path, n=6)
    search(docs, "WidgetCache eviction")

    edited_path = docs[0].path
    edited_path.write_text(
        edited_path.read_text().replace("dropped hot keys", "dropped warm keys and cold keys")
    )
    docs = load_store(tmp_path).docs

    calls = []
    real_tokenize = scoring_module._tokenize_counts

    def _spy(text):
        calls.append(text)
        return real_tokenize(text)

    monkeypatch.setattr(scoring_module, "_tokenize_counts", _spy)
    search(docs, "WidgetCache eviction")

    # by count and by content: every untouched sibling shares two of its three section texts
    # with the edited document, so "every call contains one of these phrases" passes just as
    # happily when the work was done on the wrong file — or when the one section that
    # actually changed was the one served from the stale entry
    assert len(calls) == 3, (
        f"exactly the edited document's three sections may be retokenized, got {calls!r}"
    )
    assert sum("dropped warm keys and cold keys" in text for text in calls) == 1, (
        "the section that actually changed must be among them"
    )
    assert not any("dropped hot keys" in text for text in calls), (
        "no untouched sibling's own Problem section may be retokenized"
    )


def test_stale_cache_after_edit_does_not_change_the_answer(tmp_path, monkeypatch):
    docs = _write_cache_bench_docs(tmp_path, n=3)
    search(docs, "eviction")  # warm the cache

    edited_path = docs[0].path
    edited_path.write_text(
        edited_path.read_text().replace(
            "CacheWarmer must run before traffic is shifted, not after.",
            "CacheWarmer must run before traffic is shifted, not after. "
            "A brand-new phrase: quokka migration checklist.",
        )
    )
    docs = load_store(tmp_path).docs

    with_cache = search(docs, "quokka migration checklist")

    # recompute from a completely fresh, empty cache directory to get the
    # ground truth this cached run must match
    fresh_dir = tmp_path / "fresh-cache"
    monkeypatch.setattr(cache, "cache_root", lambda: fresh_dir)
    without_cache = search(docs, "quokka migration checklist")

    assert _outcome_signature(with_cache) == _outcome_signature(without_cache)
    assert len(with_cache.hits) == 1
    assert with_cache.hits[0].doc.id == docs[0].id


# ---------------------------------------------------------------------------
# Role-addressed retrieval: `search_with_role_sections` and `role_coverage`.
# ---------------------------------------------------------------------------


def _write_role_doc(tmp_path, filename, id_, title, n, body, tags="[]", entities="[]"):
    _write_body_doc(tmp_path, filename, id_, title, n, body, tags=tags, entities=entities)


def test_search_with_role_sections_ranks_identically_to_plain_search(tmp_path):
    body_a = """## Pre-reg

Unrelated setup notes.

## Decision Log

Reused the existing SweeperJob scheduler instead of building a new one.
"""
    body_b = """## Decision Log

Rejected a second cache tier; the read path was already fast enough.

## Lessons Learned

CacheWarmer must run before traffic is shifted.
"""
    _write_role_doc(tmp_path, "a.md", "sweeper-job-doc", "Sweeper Job", 1, body_a)
    _write_role_doc(tmp_path, "b.md", "cache-tier-doc", "Cache Tier", 2, body_b)
    docs = load_store(tmp_path).docs

    plain = search(docs, "caching")
    role_outcome, role_map = search_with_role_sections(docs, "caching")

    assert [h.doc.id for h in role_outcome.hits] == [h.doc.id for h in plain.hits]
    assert [h.score for h in role_outcome.hits] == [h.score for h in plain.hits]


def test_search_with_role_sections_maps_each_document_to_its_role_sections(tmp_path):
    body_a = """## Decision Log

Reused the existing SweeperJob scheduler instead of building a new one.

## Production Considerations

SweeperJob is guarded by LeaseGuard so only one node runs it.
"""
    _write_role_doc(tmp_path, "a.md", "sweeper-job-doc", "Sweeper Job", 1, body_a)
    docs = load_store(tmp_path).docs

    _, role_map = search_with_role_sections(docs, "SweeperJob")

    doc_roles = role_map["sweeper-job-doc"]
    assert set(doc_roles) == {"decisions", "production"}
    assert "SweeperJob scheduler" in doc_roles["decisions"].body
    assert "LeaseGuard" in doc_roles["production"].body


def test_search_with_role_sections_omits_role_for_a_document_lacking_it(tmp_path):
    body_a = """## Decision Log

Reused the existing SweeperJob scheduler instead of building a new one.
"""
    _write_role_doc(tmp_path, "a.md", "sweeper-job-doc", "Sweeper Job", 1, body_a)
    docs = load_store(tmp_path).docs

    _, role_map = search_with_role_sections(docs, "SweeperJob")

    assert "production" not in role_map["sweeper-job-doc"]


def test_role_coverage_counts_active_documents_and_zero_fills_absent_roles(tmp_path):
    from engmem.sections import CANONICAL_ROLES

    body_with_decisions = "## Decision Log\n\nRejected the naive approach.\n"
    body_without = "## Pre-reg\n\nNothing structural here.\n"
    _write_role_doc(tmp_path, "a.md", "doc-a", "Doc A", 1, body_with_decisions)
    _write_role_doc(tmp_path, "b.md", "doc-b", "Doc B", 2, body_with_decisions)
    _write_role_doc(tmp_path, "c.md", "doc-c", "Doc C", 3, body_without)
    docs = load_store(tmp_path).docs

    coverage = role_coverage(docs)

    assert set(coverage) == set(CANONICAL_ROLES)
    assert coverage["decisions"] == 2
    assert coverage["graph"] == 0


def test_role_coverage_excludes_draft_and_superseded_documents(tmp_path):
    active_body = "## Decision Log\n\nActive doc's own decision.\n"
    _write_role_doc(tmp_path, "a.md", "active-doc", "Active Doc", 1, active_body)

    draft_content = """---
id: draft-doc
title: Draft Doc
date: 2026-07-09
task_date: 2026-07-09
status: draft
superseded_by:
backfilled: false
tags: []
entities: []
related: []
covers_files: []
verified_at_commit: eee0009
capture_minutes: 1
---

## Decision Log

A draft's decision must not count toward coverage.
"""
    (tmp_path / "draft.md").write_text(draft_content)

    superseded_content = """---
id: superseded-doc
title: Superseded Doc
date: 2026-07-09
task_date: 2026-07-09
status: superseded
superseded_by: active-doc
backfilled: false
tags: []
entities: []
related: []
covers_files: []
verified_at_commit: eee0010
capture_minutes: 1
---

## Decision Log

A superseded decision must not count toward coverage either.
"""
    (tmp_path / "superseded.md").write_text(superseded_content)

    docs = load_store(tmp_path).docs
    coverage = role_coverage(docs)

    assert coverage["decisions"] == 1


# ---------------------------------------------------------------------------
# cache payloads whose shape does not match what scoring expects
# ---------------------------------------------------------------------------


def _doc_with_body(path, doc_id, body):
    path.write_text(
        f"---\nid: {doc_id}\ntitle: Widget cache\ndate: 2026-01-01\nstatus: active\n"
        f"tags: [platform]\nentities: [WidgetCache]\n---\n\n{body}",
        encoding="utf-8",
    )
    return {d.id: d for d in load_store(path.parent).docs}[doc_id]


@pytest.mark.parametrize(
    "payload,label",
    [
        ({}, "no sections key at all"),
        ({"sections": None}, "sections is not iterable"),
        ({"sections": [{"heading": "Decision Log"}]}, "a section entry missing its counts"),
        ({"sections": ["not a mapping"]}, "a section entry of the wrong type"),
    ],
)
def test_a_cache_payload_of_the_wrong_shape_recomputes_instead_of_crashing(
    tmp_path, capsys, payload, label
):
    """A payload of an older shape is a miss, not a crash, and the warning goes to stderr only."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    doc = _doc_with_body(
        sessions / "20260101-widget-cache.md",
        "20260101-widget-cache",
        "## 8. Decision Log\n\nChose write-through for WidgetCache.\n",
    )
    cache.store(doc.path, cache.identity_for(doc.path), payload)
    capsys.readouterr()

    entries = _entries_for_doc(doc)

    assert entries, f"{label}: the document must still be indexed"
    assert any(e.section.canonical == "decisions" for e in entries)
    captured = capsys.readouterr()
    assert captured.out == "", "a cache warning must never reach stdout"
    assert "unexpected shape" in captured.err


def test_a_recomputed_entry_replaces_the_bad_payload(tmp_path, capsys):
    """The bad entry has to be overwritten, or every later search pays the same cost and prints
    the same warning."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    doc = _doc_with_body(
        sessions / "20260101-widget-cache.md",
        "20260101-widget-cache",
        "## 8. Decision Log\n\nChose write-through for WidgetCache.\n",
    )
    cache.store(doc.path, cache.identity_for(doc.path), {"sections": None})

    _entries_for_doc(doc)
    capsys.readouterr()
    _entries_for_doc(doc)

    assert "unexpected shape" not in capsys.readouterr().err


def test_a_stale_format_version_entry_is_ignored_even_when_the_file_is_unchanged(tmp_path):
    """`CACHE_FORMAT_VERSION` gates section-boundary changes, not just `canonical`-value
    changes. A cache entry written under an older version must be recomputed with the
    current splitter rather than served as-is, or a fix to `split_sections` never reaches an
    already-cached document until it happens to be edited again."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    doc = _doc_with_body(
        sessions / "20260101-widget-cache.md",
        "20260101-widget-cache",
        "## Architecture\n\nProse.\n\n  ## Testing Knowledge\n\nHow this is covered.\n",
    )

    # a stale (pre-fix) cache entry: only the section an older `split_sections` could see,
    # under a format_version this install no longer trusts
    cache.store(doc.path, cache.identity_for(doc.path), {
        "sections": [{
            "anchor": "architecture", "heading": "Architecture",
            "body": "Prose.\n\n  ## Testing Knowledge\n\nHow this is covered.",
            "size_bytes": 60, "level": 2, "canonical": "architecture", "index": 1,
            "literal_tf": {}, "derived_tf": {},
        }],
    })
    entry_path = cache._entry_path(doc.path)[0]
    record = json.loads(entry_path.read_text(encoding="utf-8"))
    # 2, not "current minus one": a real pre-fix cache entry was written under the
    # literal version this fix bumped past, not under some version relative to today's
    record["format_version"] = 2
    entry_path.write_text(json.dumps(record), encoding="utf-8")

    entries = _entries_for_doc(doc)

    assert any(e.section.canonical == "testing" for e in entries), (
        "the stale entry must be recomputed with the current splitter, not served as-is"
    )


def test_role_search_prefers_the_section_that_names_the_role_over_an_inherited_one(tmp_path):
    """`--role` hands back a place to read. An oversized `## Business Context` splitting into
    `### Actors` must not take `context` away from the document's own `## Glossary`."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    filler = ", ".join(f"`Term{i}`" for i in range(400))
    (sessions / "widget-cache-warmup.md").write_text(
        "---\nid: widget-cache-warmup\ntitle: Widget Cache Warmup\ndate: 2026-05-04\n"
        "task_date: 2026-05-04\nstatus: active\ntags: [platform]\nentities: [WidgetCache]\n---\n\n"
        f"## Business Context\n\n### Actors\n\n{filler}\n\n### Flows\n\nflows\n\n"
        "## Glossary\n\nWidgetCache is the request-scoped cache.\n",
        encoding="utf-8",
    )

    docs = load_store(sessions).docs
    _outcome, role_map = search_with_role_sections(docs, "WidgetCache")

    assert role_map["widget-cache-warmup"]["context"].heading == "Glossary"


def test_an_edit_during_a_search_does_not_poison_the_section_cache(tmp_path):
    """The cached sections come from `doc.body`; keying them to the file as it is *now* files
    the old body under the edited file's identity, and that entry never invalidates."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    path = sessions / "widget-cache-warmup.md"
    front_matter = (
        "---\nid: widget-cache-warmup\ntitle: Widget Cache Warmup\ndate: 2026-05-04\n"
        "task_date: 2026-05-04\nstatus: active\ntags: [platform]\nentities: [WidgetCache]\n---\n\n"
    )
    path.write_text(front_matter + "## Notes\n\nThe oldword path.\n", encoding="utf-8")

    doc = load_store(sessions).docs[0]
    # a same-size edit is invisible to the key inside one filesystem tick — contracts/cache.md
    path.write_text(front_matter + "## Notes\n\nThe newword path, rewritten.\n", encoding="utf-8")
    _entries_for_doc(doc)  # caches the sections of the body that was read

    reloaded = load_store(sessions).docs[0]
    assert reloaded.source_identity != doc.source_identity, "the edit must be visible in the key"
    bodies = " ".join(e.section.body for e in _entries_for_doc(reloaded))

    assert "newword" in bodies
    assert "oldword" not in bodies


# ---------------------------------------------------------------------------
# what indexing a subset is, and is not, allowed to do
# ---------------------------------------------------------------------------


def _store_with_each_status(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    for name, status in (("alpha-doc", "active"), ("beta-draft", "draft"),
                         ("gamma-old", "superseded")):
        (sessions / f"{name}.md").write_text(
            f"---\nid: {name}\ntitle: T\ndate: 2026-01-01\ntask_date: 2026-01-01\n"
            f"status: {status}\ntags: [x]\nentities: [WidgetCache]\n---\n\n"
            "## Notes\n\nWidgetCache prose here.\n",
            encoding="utf-8",
        )
    return sessions


def test_role_coverage_does_not_evict_the_documents_it_skips(tmp_path, monkeypatch):
    """`prune_orphans` means "no longer in the store". `role_coverage` narrows to searchable
    documents first, so passing it that list deletes every draft and superseded entry."""
    monkeypatch.setattr(cache, "cache_root", lambda: tmp_path / "cache-home")
    sessions = _store_with_each_status(tmp_path)
    docs = load_store(sessions).docs
    search(docs, "WidgetCache")
    assert len(list((tmp_path / "cache-home").glob("*.json"))) == 3

    role_coverage(docs)

    assert len(list((tmp_path / "cache-home").glob("*.json"))) == 3


def test_a_document_gone_from_the_store_still_loses_its_entry(tmp_path, monkeypatch):
    """The narrowing fix must not turn pruning off."""
    monkeypatch.setattr(cache, "cache_root", lambda: tmp_path / "cache-home")
    sessions = _store_with_each_status(tmp_path)
    docs = load_store(sessions).docs
    search(docs, "WidgetCache")

    (sessions / "gamma-old.md").unlink()
    search(load_store(sessions).docs, "WidgetCache")

    assert len(list((tmp_path / "cache-home").glob("*.json"))) == 2


def _recomputed_literal_tf(entry):
    return _tokenize_counts(f"{entry.section.heading}\n{entry.section.body}")[0]


@pytest.mark.parametrize(
    "field_path, bad_value, believed_it_would_be",
    [
        pytest.param(
            ("literal_tf",),
            ["widgetcache", "prose"],
            # `Counter` accepts any iterable, so a table that arrived as a list would count
            # its elements and rank on frequencies of 1
            lambda entry: entry.literal_tf != _recomputed_literal_tf(entry),
            id="a-frequency-table-that-is-a-list",
        ),
        pytest.param(
            ("literal_tf", "widgetcache"),
            2.5,
            # `dict` is only half the check: a value that is not a token count still shifts
            # every section length and every BM25 score
            lambda entry: entry.literal_tf["widgetcache"] != 1,
            id="a-fractional-token-count",
        ),
        pytest.param(
            ("literal_tf", "widgetcache"),
            -4,
            # and a negative one can drive the BM25 denominator to zero
            lambda entry: entry.literal_tf["widgetcache"] != 1,
            id="a-negative-token-count",
        ),
        pytest.param(
            ("body",),
            42,
            # a non-string `body` reaches `output._section_snippet` and dies there
            lambda entry: not isinstance(entry.section.body, str),
            id="a-body-that-is-not-a-string",
        ),
        pytest.param(
            ("index",),
            True,
            # `isinstance(True, int)` is True, so an `index` of `true` rendered the locator
            # `§True-notes` — plausible, wrong, silent, and cached for the life of the entry
            lambda entry: entry.section.index is True,
            id="a-boolean-where-an-int-is-wanted",
        ),
    ],
)
def test_a_payload_field_that_fails_the_type_table_is_recomputed_not_believed(
    tmp_path, capsys, field_path, bad_value, believed_it_would_be
):
    """"A shape mismatch is a miss, never a crash" has to cover the shapes that would not crash
    on their own: each of these is wrong, silent, and cached for as long as the entry lives."""
    sessions = _store_with_each_status(tmp_path)
    doc = next(d for d in load_store(sessions).docs if d.id == "alpha-doc")
    _entries_for_doc(doc)

    entry_path = cache._entry_path(doc.path)[0]
    record = json.loads(entry_path.read_text(encoding="utf-8"))
    target = record["payload"]["sections"][0]
    for key in field_path[:-1]:
        target = target[key]
    target[field_path[-1]] = bad_value
    entry_path.write_text(json.dumps(record), encoding="utf-8")
    capsys.readouterr()

    entries = _entries_for_doc(doc)

    assert not believed_it_would_be(entries[0]), (
        f"the damaged {'.'.join(field_path)} was believed, not recomputed"
    )
    captured = capsys.readouterr()
    assert "unexpected shape" in captured.err
    assert captured.out == ""


def test_building_an_index_requires_the_store_it_prunes_against():
    """The default was a trap: a wrongly pruned cache does not fail, does not warn, and only
    costs a rebuild — which is why the original defect survived until entries were counted."""
    with pytest.raises(TypeError):
        _build_section_index([])


def _cluster_store(tmp_path, first_status):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    for name, status, title, entities in (
        ("aaa-queue-hidden", first_status, "MQ rollout", "[MessageQueue, MQ]"),
        ("bbb-queue-active", "active", "T", "[MessageQueue, MQ]"),
        ("ccc-metrics-doc", "active", "T", "[MetricsQuery, MQ]"),
    ):
        extra = "superseded_by: ccc-metrics-doc\n" if status == "superseded" else ""
        (sessions / f"{name}.md").write_text(
            f"---\nid: {name}\ntitle: {title}\ndate: 2026-01-01\ntask_date: 2026-01-01\n"
            f"status: {status}\ntags: [x]\nentities: {entities}\n{extra}---\n\n"
            "## Notes\n\nProse.\n",
            encoding="utf-8",
        )
    return load_store(sessions).docs


@pytest.mark.parametrize("hidden_status", ["draft", "superseded"])
def test_a_hidden_document_cannot_evict_an_active_one_from_its_cluster(tmp_path, hidden_status):
    """The status partition runs after clustering, so a representative that cannot be output
    collapsed its cluster-mates and was then dropped itself."""
    docs = _cluster_store(tmp_path, hidden_status)

    hits = [h.doc.id for h in search(docs, "MQ").hits]

    assert "bbb-queue-active" in hits
    assert "aaa-queue-hidden" not in hits


def test_a_superseded_cluster_mate_is_still_collapsed(tmp_path):
    """Only representative *choice* is narrowed. Collapsing every member was never the defect,
    and letting a superseded member through would emit a second entry for one cluster.
    Superseded only: a draft has no observable through `SearchOutcome` either way."""
    docs = _cluster_store(tmp_path, "superseded")

    outcome = search(docs, "MQ")

    assert outcome.ambiguous is True
    assert "aaa-queue-hidden" not in [n.doc.id for n in outcome.superseded_notes]


def test_a_role_the_payload_does_not_recognise_is_recomputed_not_believed(tmp_path, capsys):
    """`canonical` keys `role_coverage`'s `{role: 0 for role in CANONICAL_ROLES}`. A payload
    written by a future install that added a role without bumping `cache.CACHE_FORMAT_VERSION`
    must be a miss here too, or that dict comprehension raises `KeyError` on a role it has never
    heard of."""
    _write_role_doc(
        tmp_path, "a.md", "alpha-doc", "Alpha Doc", 1,
        "## Decision Log\n\nChose write-through.\n",
    )
    docs = load_store(tmp_path).docs
    role_coverage(docs)

    doc = next(d for d in docs if d.id == "alpha-doc")
    entry_path = cache._entry_path(doc.path)[0]
    record = json.loads(entry_path.read_text(encoding="utf-8"))
    record["payload"]["sections"][0]["canonical"] = "runbook-from-a-newer-install"
    entry_path.write_text(json.dumps(record), encoding="utf-8")
    capsys.readouterr()

    coverage = role_coverage(docs)

    assert coverage["decisions"] == 1
    captured = capsys.readouterr()
    assert "unexpected shape" in captured.err
    assert captured.out == ""


def test_a_section_survives_the_payload_round_trip_field_by_field():
    """Three hand-maintained lists say the same thing: the payload, its type table, and the
    `Section(...)` kwargs. A field added to two of them and forgotten in the third is silent."""
    section = Section(
        anchor="notes", heading="Notes", body="Body text.", size_bytes=10,
        level=3, canonical="lessons", index=7,
    )
    literal, derived = _tokenize_counts("Body text.")

    payload = _section_to_payload(section, literal, derived)
    entry = _section_entry_from_payload("alpha-doc", payload)

    assert entry.section == section
    assert entry.literal_tf == literal and entry.derived_tf == derived
    assert set(payload) == set(_PAYLOAD_FIELD_TYPES) | set(_NULLABLE_PAYLOAD_FIELDS)
    # the equality above cannot see a *defaulted* field forgotten in the `Section(...)`
    # kwargs — both sides would take the default — so the field list is checked directly
    assert set(payload) - {"literal_tf", "derived_tf"} == {
        f.name for f in dataclasses.fields(Section)
    }
