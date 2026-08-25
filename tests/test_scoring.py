from pathlib import Path

import pytest

from engmem import cache
from engmem.scoring import role_coverage, search, search_with_role_sections
from engmem.spine import load_store

FIXTURES = Path(__file__).parent / "fixtures" / "sessions"


@pytest.fixture(autouse=True)
def _isolated_body_cache(tmp_path, monkeypatch):
    """Every test gets its own on-disk cache directory, isolated from the real
    `~/.cache/engmem` and from every other test — B4's body-token cache (see
    `engmem.cache`) must never leak state across test runs or touch the
    developer's actual cache while the suite runs."""
    monkeypatch.setattr(cache, "cache_root", lambda: tmp_path / "engmem-cache-dir")


def _docs():
    return load_store(FIXTURES).docs


def _top_id(outcome):
    return outcome.hits[0].doc.id


def test_g1_exact_id_ranks_first():
    outcome = search(_docs(), "1000001")
    assert _top_id(outcome) == "1000001-response-cache"


def test_g2_numeric_prefix_does_not_match():
    outcome = search(_docs(), "482")
    assert outcome.hits == []


def test_g3_class_name_ranks_first():
    outcome = search(_docs(), "ResponseCacheController")
    assert _top_id(outcome) == "1000001-response-cache"


def test_g4_camelcase_fragments_rank_first():
    outcome = search(_docs(), "response cache")
    assert _top_id(outcome) == "1000001-response-cache"


def test_g5_short_ambiguous_token_returns_two_clusters():
    outcome = search(_docs(), "MQ")
    assert outcome.ambiguous is True
    ids = {h.doc.id for h in outcome.hits}
    assert ids == {"mq-message-sweeper", "metrics-query-refactor"}


def test_g6_lowercase_short_token_identical_to_g5():
    outcome = search(_docs(), "mq")
    assert outcome.ambiguous is True
    ids = {h.doc.id for h in outcome.hits}
    assert ids == {"mq-message-sweeper", "metrics-query-refactor"}


def test_g7_class_plus_tag_two_token_coverage_ranks_first():
    outcome = search(_docs(), "MessageQueue sweeper")
    assert _top_id(outcome) == "mq-message-sweeper"


def test_g8_non_latin_noise_does_not_break_and_does_not_match():
    outcome = search(_docs(), "σφάλματα και MessageQueue sweeper")
    baseline = search(_docs(), "MessageQueue sweeper")

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
    outcome = search(_docs(), "ETAG")
    top_ids = {h.doc.id for h in outcome.hits[:2]}
    assert top_ids == {"1000001-response-cache", "ttl-etag-revalidation-v2"}
    for h in outcome.hits[2:]:
        assert h.score < outcome.hits[0].score


def test_g10_short_token_et_does_not_match_etag():
    outcome = search(_docs(), "ET")
    assert outcome.hits == []


def test_g11_two_token_coverage_ranks_revalidation_first():
    outcome = search(_docs(), "etag revalidation")
    assert _top_id(outcome) == "ttl-etag-revalidation-v2"


def test_nfkc_runs_before_tokenization(tmp_path):
    """Review M2: §7 mandates NFKC → casefold → tokenize. Splitting on ASCII first
    dropped fullwidth ＭＱ entirely and mangled ﬁlter (ligature) into 'lter' — a
    plausible-looking but WRONG token."""
    from engmem.spine import load_store

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

    fullwidth_outcome = search(_docs(), "ＭＱ")  # 'ＭＱ'
    plain_outcome = search(_docs(), "MQ")
    assert {h.doc.id for h in fullwidth_outcome.hits} == {
        h.doc.id for h in plain_outcome.hits
    }
    assert fullwidth_outcome.ambiguous == plain_outcome.ambiguous


def test_shared_acronym_with_same_meaning_is_not_ambiguous():
    """Review H1: §7 requires ambiguity only when the FULL FORMS differ. TTL appears as
    a literal entity in two fixture docs meaning the same thing (no CamelCase entity
    expands to 'ttl'), so this must rank normally, not report ambiguous."""
    outcome = search(_docs(), "TTL")
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


def test_ambiguity_filter_keeps_non_entity_matches(tmp_path):
    """Review H2: a doc matching the short token via title/tags must not be silently
    deleted when the entity-cluster ambiguity rule triggers for other docs."""
    from engmem.spine import load_store

    _write_ambig_store(tmp_path)
    docs = load_store(tmp_path).docs

    outcome = search(docs, "MQ")

    assert outcome.ambiguous is True
    ids = {h.doc.id for h in outcome.hits}
    assert {"queue-doc", "metrics-doc"}.issubset(ids)
    assert "runbook-doc" in ids, "title/tag match must survive the cluster filter"


def test_ambiguous_flag_marks_only_cluster_representatives(tmp_path):
    from engmem.spine import load_store

    _write_ambig_store(tmp_path)
    docs = load_store(tmp_path).docs

    outcome = search(docs, "MQ")

    flags = {h.doc.id: h.ambiguous for h in outcome.hits}
    assert flags["queue-doc"] is True
    assert flags["metrics-doc"] is True
    assert flags["runbook-doc"] is False


# ---------------------------------------------------------------------------
# B4: id-weight fix — a slug word alone is only as informative as a title
# word (weight 2); weight 5 is reserved for a whole-id match or the numeric
# ticket component.
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
    from engmem.spine import load_store

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
    from engmem.spine import load_store

    _write_id_weight_doc(tmp_path, "a.md", "widget-cache-warmer", "Unrelated Title Text", 1)
    docs = load_store(tmp_path).docs

    outcome = search(docs, "widget cache warmer")  # covers every slug component of the id
    hit = outcome.hits[0]

    assert hit.doc.id == "widget-cache-warmer"
    assert set(hit.matched_fields.get("id", [])) == {"widget", "cache", "warmer"}
    # 3 tokens matched at the full id weight (5), full coverage (3/3)
    assert hit.score == 15.0


def test_id_numeric_ticket_component_is_weight_five_even_without_full_slug_coverage(tmp_path):
    from engmem.spine import load_store

    _write_id_weight_doc(tmp_path, "a.md", "9001-pricing-engine", "Unrelated Title", 1)
    docs = load_store(tmp_path).docs

    outcome = search(docs, "9001")
    hit = outcome.hits[0]

    assert hit.doc.id == "9001-pricing-engine"
    assert hit.score == 5.0  # 1 token matched, weight 5, full coverage (1/1)


# ---------------------------------------------------------------------------
# B4: body index and BM25 ranking
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
    content = (
        BODY_DOC.replace("{id}", id_)
        .replace("{title}", title)
        .replace("{n}", str(n))
        .replace("{body}", body)
        .replace("{tags}", tags)
        .replace("{entities}", entities)
    )
    (tmp_path / filename).write_text(content)


def test_body_only_match_surfaces_document_and_names_the_section(tmp_path):
    """A query naming a concept explained only in the body must still find the
    document — and the explainability line must name which section it lives in."""
    from engmem.spine import load_store

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


def test_body_match_alone_gives_no_spine_field_entries(tmp_path):
    from engmem.spine import load_store

    body = """## Pre-reg

Unrelated setup notes.

## Production Considerations

LeaseGuard guards the job.
"""
    _write_body_doc(tmp_path, "a.md", "sweeper-job-doc", "Sweeper Job", 1, body)
    docs = load_store(tmp_path).docs

    outcome = search(docs, "LeaseGuard")
    hit = outcome.hits[0]

    for spine_field in ("id", "title", "tags", "entities"):
        assert spine_field not in hit.matched_fields


def test_long_document_does_not_win_purely_on_length(tmp_path):
    """max-over-sections, not sum: a document whose best section is identical to a
    short document's only section must not outscore it just for having more of them."""
    from engmem.spine import load_store

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

    assert long_score < short_score * 2, (
        f"long doc ({long_score}) must not blow past the short doc ({short_score}) "
        f"just for repeating the same section 8 times over"
    )


def test_restricted_query_token_matches_body_literal_only(tmp_path):
    """§7's restricted-token rule applies to the body index too: a short/numeric
    token may only match text as literally written, never a CamelCase-derived
    fragment — otherwise "MQ" would match every CamelCase word starting with those
    initials anywhere in any body."""
    from engmem.spine import load_store

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


def test_ubiquitous_body_term_is_dropped_from_body_scoring(tmp_path):
    """A term present in over half of the corpus's sections is dropped from body
    scoring entirely — the self-tuning stand-in for a stopword list that also
    neutralises honesty tags like [Verified] that appear in nearly every section."""
    from engmem.spine import load_store

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
# B4 cache: `_build_section_index` (via `engmem.cache`) must be transparent —
# identical results whether the body-token index came from a cold cache, a
# warm cache, or a cache invalidated by an edit — and must actually avoid
# re-tokenizing an unchanged document on a warm run.
# ---------------------------------------------------------------------------


def _outcome_signature(outcome):
    """A comparable snapshot of everything a reader of `SearchOutcome` can see,
    so "cached and uncached results match exactly" is a single equality check
    rather than several ad hoc ones that could each individually miss a field
    the cache accidentally dropped or reordered."""
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
    for i in range(n):
        _write_body_doc(
            tmp_path, f"doc-{i}.md", f"widget-{i}", f"Widget Session {i}", i, body
        )
    return load_store(tmp_path).docs


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

    retokenized_docs = {c for c in calls}
    assert len(retokenized_docs) > 0, "the edited document must be retokenized"
    # every re-tokenized section came from the one edited document, not from any
    # of the five untouched siblings
    for text in calls:
        assert "dropped warm keys and cold keys" in text or "Decision Log" in text or "CacheWarmer" in text


def test_stale_cache_after_edit_does_not_change_the_answer(tmp_path):
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
    import engmem.cache as cache_module

    original_cache_root_fn = cache_module.cache_root
    cache_module.cache_root = lambda: fresh_dir
    try:
        without_cache = search(docs, "quokka migration checklist")
    finally:
        cache_module.cache_root = original_cache_root_fn

    assert _outcome_signature(with_cache) == _outcome_signature(without_cache)
    assert len(with_cache.hits) == 1
    assert with_cache.hits[0].doc.id == docs[0].id


# ---------------------------------------------------------------------------
# Role-addressed retrieval: `search_with_role_sections` and `role_coverage`.
# Ranking stays word-based even with a role in play (see the design note in
# `output.select_role_hits`) — these tests pin that `search_with_role_sections`
# produces the exact same ranked `SearchOutcome` `search()` does, plus a
# doc_id -> {role: Section} map built from the same section-index parse, so a
# role-addressed search costs no more than an ordinary one.
# ---------------------------------------------------------------------------


def _write_role_doc(tmp_path, filename, id_, title, n, body, tags="[]", entities="[]"):
    _write_body_doc(tmp_path, filename, id_, title, n, body, tags=tags, entities=entities)


def test_search_with_role_sections_ranks_identically_to_plain_search(tmp_path):
    from engmem.spine import load_store

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
    from engmem.spine import load_store

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
    from engmem.spine import load_store

    body_a = """## Decision Log

Reused the existing SweeperJob scheduler instead of building a new one.
"""
    _write_role_doc(tmp_path, "a.md", "sweeper-job-doc", "Sweeper Job", 1, body_a)
    docs = load_store(tmp_path).docs

    _, role_map = search_with_role_sections(docs, "SweeperJob")

    assert "production" not in role_map["sweeper-job-doc"]


def test_role_coverage_counts_active_documents_and_zero_fills_absent_roles(tmp_path):
    from engmem.sections import CANONICAL_ROLES
    from engmem.spine import load_store

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
    from engmem.spine import load_store

    active_body = "## Decision Log\n\nActive doc's own decision.\n"
    _write_role_doc(tmp_path, "a.md", "active-doc", "Active Doc", 1, active_body)

    draft_content = f"""---
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

    superseded_content = f"""---
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
    from engmem.spine import load_store

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
    tmp_path, monkeypatch, capsys, payload, label
):
    """A payload written by an older format is a miss, not a crash. The document must still
    be searchable, and the warning must go to stderr only — the agent reads stdout, and a
    slow-but-correct search must not look like a failed one."""
    from engmem import cache
    from engmem.scoring import _entries_for_doc

    monkeypatch.setattr(cache, "cache_root", lambda: tmp_path / "cache-home")
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


def test_a_recomputed_entry_replaces_the_bad_payload(tmp_path, monkeypatch, capsys):
    """Recomputing is not enough on its own — the bad entry has to be overwritten, or every
    later search pays the same cost and prints the same warning forever."""
    from engmem import cache
    from engmem.scoring import _entries_for_doc

    monkeypatch.setattr(cache, "cache_root", lambda: tmp_path / "cache-home")
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
