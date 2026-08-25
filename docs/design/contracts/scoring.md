# Contract: scoring internals

Source: `src/engmem/scoring.py`. Base spine-field formula, normalization, and
match rules: `ENGMEM-SPEC.md` §7. This covers what §7 doesn't: BM25 section
ranking (an English addition, approved in §11 "Built after all") and the
ambiguity-clustering algorithm's shape.

## `ID_SLUG_WEIGHT`

An `id` is `<ticket>-<slug>`. A lone slug word (e.g. "cache" from
"widget-cache-warmer") is only as informative as the same word in `title` — the
slug IS the title, tokenized — so it takes the title tier (`FIELD_WEIGHTS["title"]`),
not the full id weight. Full id weight is reserved for a numeric ticket
component or a query naming the whole id (`_id_field_weight`).

## BM25 tuning (`BM25_K1`, `BM25_B`)

`k1 = 1.2` is the textbook default. `b = 0.6`, not the textbook 0.75: the
section model already caps a piece at ~4 KB (`sections.MAX_SECTION_BYTES`), so
section lengths span roughly 4x rather than the ~14x a whole-document BM25
would see. The textbook length penalty, tuned for that wider spread,
over-penalises dense reference sections once length is already this
normalised.

## `DF_CEILING_RATIO`

A term in over half the corpus's sections is dropped from body scoring
entirely — a self-tuning, language-agnostic stopword substitute that also
neutralises honesty tags (`[Verified]`/`[Assumed]`/`[Open]`), which appear in
nearly every section and would otherwise dominate every score.

## Ambiguity clustering (`_cluster_key_for_short_token`)

§7 defines ambiguity by differing full forms behind a shared short token (MQ
→ MessageQueue vs MetricsQuery). The cluster key is the normalized full form
of the entity whose CamelCase acronym equals the matched token. A document
with no expanding entity has an unknown full form — not a *differing* one —
so it is left unclustered (returns `None`) rather than given a spurious
unique key.

## Section index caching (`_entries_for_doc`, `_build_section_index`)

Rebuilding the token index from scratch on every call was fine at the
9-document corpus §8's golden fixtures were measured on, but not once a
shared store grows into the hundreds. `_entries_for_doc` reads a per-document
cache entry keyed on file identity (`cache.py`) and only recomputes what
changed. `_entries_from_cache` treats any payload shape mismatch (e.g. a
future change that forgets to bump `cache.CACHE_FORMAT_VERSION`) as a miss,
never a crash.

## Sharing one parse across ranking and role lookup

`search_with_role_sections` (role-addressed retrieval, `ENGMEM-SPEC.md` §5)
ranks with the same `_search_core` `search()` uses, then builds a
`doc_id -> {role: Section}` map from the *same* section-index build — no
second `split_sections` pass, no second cache read, so a role-filtered query
costs no more than an ordinary one.
