# Contract: scoring internals

Source: `src/engmem/scoring.py`. `ENGMEM-SPEC.md` §7 fixes the spine-field formula,
normalisation and match rules, and §8's golden fixtures are the ground truth. This covers what
§7 does not: BM25 over section bodies (`ENGMEM-SPEC.md` §9, "Built after all") and the shape of
the ambiguity clustering. §9 also keeps this module whole: `search`,
`search_with_role_sections` and `role_coverage` deliberately share one built section index.

## Invariants

- The rank order is `(-score, -date, id)`, a full deterministic order. BM25 scores move with
  corpus size, so golden tests pin order and membership, never an absolute score.
- A document scores its single best section, not the sum, or it wins by section count.
- Restricted-ness (`_is_restricted`: three characters or fewer, or purely numeric) is a
  property of the token's text, decided once in `_build_query_tokens` and read from
  `qt.restricted` by `_spine_score` and `_bm25_entry_score` alike; it is never re-derived.
- `_NEVER_PRIMARY` (`draft`, `superseded`; data-model.md "State transitions") never appears as
  a primary result. A draft is dropped; a superseded document becomes a `SupersededNote`, with
  its successor when the store holds it.

## `ID_SLUG_WEIGHT`

An `id` is `<ticket>-<slug>`, and a lone slug word ("cache" from "widget-cache-warmer") is the
same word the title carries, so it takes the title tier (`FIELD_WEIGHTS["title"]`). Full id
weight is paid only for a numeric ticket component or a query naming the whole id
(`_id_field_weight`).

## BM25 tuning (`BM25_K1`, `BM25_B`)

`k1 = 1.2` is the textbook default. `b = 0.6`, not the textbook 0.75: `sections.MAX_SECTION_BYTES`
already caps a piece at ~4 KB, so section lengths span roughly 4x rather than the ~14x a
whole-document BM25 sees, and the textbook length penalty over-penalises dense reference
sections once length is already that normalised.

## `DF_CEILING_RATIO`

A term in over half the corpus's sections is dropped from body scoring: a self-tuning,
language-agnostic stopword substitute that also neutralises the honesty tags
(`[Verified]`/`[Assumed]`/`[Open]`) present in nearly every section. Known limit: with one
section in the whole corpus every matched term has `df / n == 1`, so only the spine ranks. That
is not the new-store state — one saved document is six sections in the fixtures and nineteen
under the full save template.

## One rule per query token (`_build_query_tokens`)

Returns distinct tokens, each carrying `_is_restricted` of its own text.

By text, not by origin: §7's short-token rule is about the token, and the CamelCase expansion
emits short tokens of its own (`mq` from `MQSweeper`, `rc` from `ResponseCache`). Stamped
unrestricted, `mq` matched a document whose only link was `entities: [MetricsQuery]` on that
entity's derived acronym — the collision the rule exists to refuse — while the body index,
which re-derived the flag, correctly left the same document alone. Acronym retrieval is not
what this costs: a restricted `rcc` still matches a field written literally `RCC`, an original
token. Only acronym-to-acronym is dropped, and acronym-to-acronym is the collision generator.

Distinct: `_spine_score` divides by `len(query_tokens)`, and the expansion can emit a token a
neighbouring query word repeats (`cache ResponseCache` yields `cache` once). A duplicate adds
its weight twice and raises the denominator, and those do not cancel, so two documents each
matching one query word at the same tier scored differently.

## Ambiguity clustering (`_cluster_key_for_short_token`)

The cluster key is the normalised full form of the entity whose CamelCase acronym equals the
matched token (§7: differing full forms behind one short token). A document with no expanding
entity has an unknown full form, not a differing one, so it returns `None` and stays
unclustered rather than being given a spurious unique key.

Every matching document collapses into its cluster, but only one that can be a primary result
may represent it (`_NEVER_PRIMARY`). The status partition runs after clustering, so a draft or
superseded representative would collapse its cluster-mates and then be dropped itself, taking
an active document out of the results silently. Rejected: narrowing membership instead of
representation — within the ambiguous branch a superseded cluster-mate would survive the
collapse and emit a second entry for one cluster, against §7's "output one top doc from each
cluster". Below the gate (fewer than two clusters with a representative) nothing collapses and
a superseded match emits its ordinary redirect. Accepted consequence: two active documents
sharing a cluster are not ambiguous when the only competing cluster came from a draft or
superseded document; ambiguity between a visible meaning and an invisible one is noise.

## Section index caching (`_entries_for_doc`, `_build_section_index`)

`_entries_for_doc` reads a per-document cache entry (`contracts/cache.md`) and recomputes only
what changed; rebuilding the index on every call was fine at the 9-document fixture corpus and
not at a shared store in the hundreds. The key is `doc.source_identity`, stamped by
`spine.parse_document` before it read the body, never a fresh `stat` at search time
(`contracts/cache.md`, "Identity, not invalidation").

`_build_section_index(docs, store_docs)` indexes the documents it was handed and prunes against
the whole store. The second parameter has no default because a wrongly pruned cache neither
fails nor warns, it only costs a rebuild. `role_coverage` is the caller that needs the
distinction: it narrows to searchable documents before indexing, and pruning on that narrowed
list deleted every draft and superseded document's entry on each `engmem roles`.

### A shape mismatch is a miss, never a crash

`_entries_from_cache` treats a payload it cannot read — one written before a
`cache.CACHE_FORMAT_VERSION` bump, or by a change that forgot to bump it — as a warned miss.
Warned, unlike the shapes `cache.load` rejects silently, because a payload that survived every
check there and still does not fit is a bug in this repo, not a stale file
(`contracts/cache.md`, "Degrading, and how loudly"). "Never a crash" is what
`_section_entry_from_payload`'s checks exist for; each closes a shape that survives duck
typing:

- Every field by type (`_PAYLOAD_FIELD_TYPES`), with `bool` excluded from the int fields:
  `isinstance(True, int)` holds, and an `index` of `true` renders the locator `§True-notes`.
  A frequency table that arrived as a list would have `Counter` count its elements and rank on
  a plausible index of all-ones; a non-string `body` reaches `output._section_snippet` and
  dies there.
- Table values (`_token_counts`): a dict of the wrong values passes the type entry. A
  fractional count moves `avgdl` and every score by a margin no reader can see; a negative one
  can drive the BM25 denominator, positive for every real count, to zero. Counts are tested
  with `type(count) is not int`, for the `bool` reason above.
- A `canonical` outside `sections.CANONICAL_ROLES`: the one payload field that keys a dict of
  known values — `role_coverage`'s `counts = {role: 0 for role in CANONICAL_ROLES}` has no
  slot for anything else. Renaming or adding a role changes payload content, not shape, so a
  `CACHE_FORMAT_VERSION` bump is not guaranteed to cover it.

Both `_token_counts` rejections raise `TypeError`, the negative count included, though a range
violation would ordinarily be a `ValueError`: `_entries_from_cache` catches `(KeyError,
TypeError)` only, so anything else escapes and crashes the search. A range check added here
must raise `TypeError` too, or widen that handler first.

Known gap: these validate shape, not value. `index`, `level` and `size_bytes` are
range-unchecked — a cached `index` of `-1` renders `§-1-notes`, wrong and silent — but that
neither crashes nor mis-ranks, so it is accepted.

## Spine and body scores are not on one scale

The score is `spine + body`. The spine side is bounded — `FIELD_WEIGHTS` caps a token at 5,
times a coverage ratio of at most 1 — and the BM25 side is not: its `idf` grows as `log(n)` in
sections. So §8's G1 ("an exact id ranks first") holds at fixture size and stops holding as the
corpus grows, and §8's G1 row points here. Measured, query `1000001`, against one document
whose id is `1000001-response-cache` and one whose body merely says "We reverted 1000001
later", with single-section filler documents of one short paragraph:

| filler documents | first two hits |
|---|---|
| 10 | `1000001-response-cache` 5.00, `9999999-other` 3.26 |
| 30 | `1000001-response-cache` 5.00, `9999999-other` 4.50 |
| 120 | `9999999-other` 6.14, `1000001-response-cache` 5.00 |

`n` in the `idf` counts sections, not documents, so the crossover moves with section density
and only the direction is reproducible from the document count.

Recorded, not fixed. Two classes of fix, both rejected for now:

- Tuning — a cap on the body contribution, or a corpus-size-independent `idf` — needs a
  constant, and the 9-document fixture cannot supply one: the crossover moves with corpus
  size, so a constant chosen at fixture size is chosen where the problem does not yet appear.
- Structural — a sort key ahead of `-score` — needs no constant. Keyed on "matched the `id`
  field" it hoists slug words: `_id_field_weight` records `cache` against
  `4242-widget-cache-warmer` as an `id` match while paying it the title tier, so `_spine_score`
  returns `2.0, {'id': ['cache']}` and the tiebreak erases the demotion `ID_SLUG_WEIGHT`
  exists to make. (With `entities: [CacheWarmer]` the same document returns `3.0,
  {'entities': ['cache']}`, since `matched_fields` keeps the best field per token; the
  entity-less case is the common one.) Keyed on the exact-id condition (`whole_id_matched or
  qt.text.isdigit()`) it survives that but makes one field a gate no other signal can
  outweigh: on `http 404 handling`, `404-cache-warmer` scores `1.67` on the digit alone and
  passes the gate, `7001-http-error-handling` scores `2.67` on two slug words and does not —
  and `_is_restricted` classifies a bare digit as too generic to trust while `_id_field_weight`
  pays it the full tier, so the gate would make unbeatable the one signal the tokeniser
  distrusts.

That is a ranking decision to be taken against a corpus, not a bug fix.

## Sharing one parse across ranking and role lookup

`search_with_role_sections` (`ENGMEM-SPEC.md` §5, `--role`) ranks with the same `_search_core`
as `search()` and builds its `doc_id -> {role: Section}` map from the same section-index
build, so a role-filtered query costs no more than an ordinary one. `_role_index_from_entries`
makes two passes, so a role a section's own heading names wins over one inherited from an
oversized parent (`contracts/sections.md`, "Role inheritance across a sub-split"); a role a
document carries twice keeps the first in document order rather than being silently
overwritten.
