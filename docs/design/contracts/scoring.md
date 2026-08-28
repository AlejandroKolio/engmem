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

The ratio has a floor at `n == 1`: with a single section in the whole corpus every matched
term has `df / n == 1 > 0.5`, so body scoring returns nothing and only the spine ranks. That
floor is not the new-store state — one saved session document is already six sections in the
test fixtures and nineteen under the full save template, and a store holding just that one
document scores its body normally.

## One rule per query token (`_build_query_tokens`)

`_build_query_tokens` returns *distinct* tokens, each carrying the restricted-ness
`_is_restricted` gives its own text. Both halves of that sentence were once false, and each
cost something different.

§7's short-token rule — query tokens of three characters or fewer, and purely numeric ones,
only exact-match against the ORIGINAL (unsplit) field tokens — is about the token, not about
where it came from. The expansion emits short tokens of its own (`rcc` from
`ResponseCacheController`, `mq` from `MQSweeper`), and those used to be stamped unrestricted.
`_bm25_entry_score` never read the flag: it re-derived restricted-ness from the text, so the
*body* index applied the rule while the *spine* index did not, for the same token of the same
query. Query `MQSweeper` against a document whose only link is `entities: [MetricsQuery]`
matched on `entities: ['mq']` — precisely the acronym collision (MQ → MessageQueue vs
MetricsQuery) the rule exists to refuse — while that document's body was correctly left
alone. Acronym retrieval is not what this costs: a restricted `rcc` still matches a field
written literally `RCC`, because that is an original token. Only acronym-to-acronym is
dropped, and acronym-to-acronym is the collision generator. `_bm25_entry_score` now reads
`qt.restricted` instead of re-deriving it, so the rule is applied in one place.

Distinctness matters for the coverage ratio. `_spine_score` divides by
`len(query_tokens)`, and the expansion can emit a token a neighbouring query word repeats:
`cache ResponseCache` yielded `cache` twice. A token counted twice adds its weight twice
*and* raises the denominator, and those do not cancel — `Cache Notes` against
`Response Times`, each matching one query word at the title tier, scored `1.6` against
`0.4`. The body side already deduplicated, in a `seen` set of its own; that set is gone,
because the list it was guarding is now distinct where it is built.

## Ambiguity clustering (`_cluster_key_for_short_token`)

Every matching document is collapsed into its cluster, but only one that can
be a primary result may *represent* one (`_NEVER_PRIMARY`). The status
partition runs after clustering, so a draft or superseded representative
collapsed its cluster-mates away and was then dropped itself — taking an
active document out of the results with it, silently. Narrowing membership
instead of representation would be the other error: *within the ambiguous
branch*, a superseded cluster-mate would survive the collapse and emit a second
entry for one cluster, against §7's "output one top doc from each cluster".
Below that gate — fewer than two clusters with a representative — nothing
collapses at all and a superseded match emits its ordinary redirect.

One consequence, accepted: two active documents sharing a cluster no longer
count as ambiguous when the *only* competing cluster came from a draft or
superseded document. Ambiguity between a visible meaning and an invisible one
is noise.

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
changed.

Pruning is separate from indexing, and the two take different sets, so
`_build_section_index` requires both: it indexes the documents it was handed
and prunes against the whole store. The parameter has no default on purpose —
a wrongly pruned cache does not fail and does not warn, it only costs a
rebuild, which is why the original defect survived until entries were counted.
`role_coverage` is the caller that needs the distinction: it narrows to
searchable documents before indexing, and pruning on that narrowed list deleted
the cache entry of every draft and superseded document on each `engmem roles`.

`_entries_from_cache` treats any payload shape mismatch (e.g. a future change
that forgets to bump `cache.CACHE_FORMAT_VERSION`) as a miss, never a crash.
"Never a crash" is what makes the field-by-field type check necessary, because
the two dangerous shapes both survive duck typing: a frequency table that
arrived as a list would have `Counter` count its *elements* and rank on a
plausible index of all-ones, and a non-string `body` reaches
`output._section_snippet` and dies there.

Declaring those two tables `dict` is only half of that check, and `_token_counts` is the
other half: a table that is a dict of the wrong *values* survives the type entry just as
readily. A fractional count moves `avgdl` and every BM25 score by a margin no reader can
see. A negative one moves `entry.length` the same way, and can drive the BM25 denominator —
`tf + k1(1 - b + b·length/avgdl)`, positive for every real count — to zero, which is the
crash this section promises not to have. Counts are tested with `type(count) is not int`
rather than `isinstance`, for the reason `bool` is excluded from the int fields above. It
costs one pass over each table's distinct keys, measured at about a fifth of the
`json.loads` that produced them, against a cache read that is already the cheap path.

Both rejections raise `TypeError`, including the negative count, which is a range violation
and would ordinarily be a `ValueError`. The exception type is load-bearing rather than
descriptive: `_entries_from_cache` catches `(KeyError, TypeError)`, so a `ValueError` would
escape that handler and crash the search — the exact failure this check exists to close. A
second range check added here must raise `TypeError` too, or widen that handler first.

It warns on the mismatch, unlike the
shapes `cache.load` rejects silently, because a payload that survived every
check there and still does not fit is a bug in this repo rather than a stale
file. See `contracts/cache.md`, "Degrading, and how loudly".

## Spine and body scores are not on one scale

A document's score is `spine + body`, and the two grow differently. The spine
side is bounded: `FIELD_WEIGHTS` caps a token at 5, times a coverage ratio of
at most 1. The BM25 side is not — its `idf` grows as `log(n)` in *sections*,
so a body match keeps climbing as the store does.

So §8's G1 ("an exact id ranks first") is true at fixture size and stops being
true as the corpus grows. Measured, query `1000001`, against one document whose
*id* is `1000001-response-cache` and one whose body merely says "We reverted
1000001 later":

| filler documents | first two hits |
|---|---|
| 10 | `1000001-response-cache` 5.00, `9999999-other` 3.26 |
| 30 | `1000001-response-cache` 5.00, `9999999-other` 4.50 |
| 120 | `9999999-other` 6.14, `1000001-response-cache` 5.00 |

The fillers above are single-section documents of one short paragraph; `n` in
the `idf` counts *sections*, not documents, so the crossover moves with section
density and the exact scores are not reproducible from the document count
alone. The direction is what the table is for.

This is recorded, not fixed, and there are two classes of fix rather than one.
The *tuning* class — a cap on the body contribution, or a corpus-size-
independent `idf` — needs a constant, and the 9-document fixture cannot
supply one: the whole defect is that the crossover moves with corpus size, so a
constant chosen at fixture size is chosen where the problem does not yet
appear. (Not an appeal to how `BM25_K1`/`DF_CEILING_RATIO` were picked — as
the tuning section above says, `k1` is the textbook default and `b` rests on a
structural argument about section-length spread.)

The *structural* class needs no constant: a sort key ahead of `-score` would
hold G1 at every corpus size.

Two variants, and the broad one is wrong on its own terms. Sorting on "matched
the `id` field" hoists slug-word matches too: `_id_field_weight` records
`cache` against `4242-widget-cache-warmer` as an `id` match while deliberately
paying it the *title* tier. Query `cache`, that document with no entities:
`_spine_score` returns `2.0, {'id': ['cache']}`. Under either keying — the
`matched_fields` entry the explainability line already prints, or
`_id_field_weight` returning non-`None` — the tiebreak hoists it, erasing the
demotion `ID_SLUG_WEIGHT` exists to make and putting documents with `cache` in
their slug above the document that is about caching.

Give the same document `entities: [CacheWarmer]` and it returns `3.0,
{'entities': ['cache']}` instead, because `matched_fields` keeps only the best
field per token — so that shape escapes a `matched_fields`-keyed tiebreak
while still tripping an `_id_field_weight`-keyed one. It is the shadowed case,
not the common one: most documents have no entity expanding to their own slug
word, and the objection rests on the plain case above.

The narrow variant survives that. The exact-id condition is `qt.text in
candidates` and `whole_id_matched or qt.text.isdigit()` — the pair that earns
`FIELD_WEIGHTS["id"]` past the membership test — and a tiebreak keyed on that
is immune to the slug objection and needs no constant. It is still not adopted,
because it makes one field a gate no other signal can outweigh. Query
`http 404 handling`: `404-cache-warmer` scores `1.67` on the digit alone
(`{'id': ['404']}`) and passes the gate, while `7001-http-error-handling`
scores `2.67` on two slug words (`{'id': ['http', 'handling']}`) and does
not. Worse, `_is_restricted` classifies a bare digit as too generic to trust
past an exact match, while `_id_field_weight` pays that same digit the *full*
id tier — the tiebreak would make unbeatable the one signal the tokenizer
distrusts.

That is a ranking decision rather than a bug fix, and it should be taken
against a corpus. What is not acceptable is
the spec asserting G1 unconditionally while the code holds it only below some
corpus size, so §8's G1 row now points here.

## Sharing one parse across ranking and role lookup

`search_with_role_sections` (role-addressed retrieval, `ENGMEM-SPEC.md` §5)
ranks with the same `_search_core` `search()` uses, then builds a
`doc_id -> {role: Section}` map from the *same* section-index build — no
second `split_sections` pass, no second cache read, so a role-filtered query
costs no more than an ordinary one.
