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
  a primary result. A draft is outside the ranking corpus altogether (below); a superseded
  document becomes a `SupersededNote`, with its successor when the store holds it.

## Tokenisation (`tokenize_raw`, `normalize_token`, `camel_fragments`)

One rule set, applied by the same functions to front matter fields, section text and the
query, so CLI and MCP cannot drift (both call `search`):

1. **NFKC** over the whole text (`tokenize_raw`).
2. **Split** into maximal runs of Unicode letters, numbers and combining marks
   (`_token_run_re`). Everything else separates, `_` included, so `snake_case_name` still
   yields three words. So does every apostrophe, including the four modifier letters used as
   one (below).
3. **Casefold** each token after NFKC (`normalize_token`), so `ОТМЕНА` = `отмена`,
   `STRASSE` = `straße`, and a Greek final `ς` = `σ`.
4. **Identifier split** (`camel_fragments`) over Unicode case, not ASCII ranges.

Why NFKC and not NFC. Both compose `e` + U+0301 into `é`, which is what makes canonically
equivalent spellings one token. NFKC also folds compatibility forms: fullwidth `ＭＱ` = `MQ`,
the ligature `ﬁ` = `fi`. Search had NFKC before Unicode support, and
`test_nfkc_runs_before_tokenization` pins that. The cost is that a few distinctions vanish
(`x²` and `x2` are one token), which exact search over prose can afford.

Combining marks stay inside a word. NFKC composes most of them away, but a letter with no
precomposed form keeps its mark: Russian stress (`за́мок`), many Latin transliterations.
Python's `\w` does not cover marks (category `M`), so the token pattern adds them
explicitly. Splitting on a mark cut the word in two and lost the mark. The pattern is built
once per process from `unicodedata`, as code-point ranges, which the regex engine scans about
four times faster than 2,000-odd single characters. The build scans only the planes that hold
marks today (`_MARK_PLANES`: 0, 1 and 14), about 12 ms on first use instead of about 70 ms
for all 17 planes, which every CLI search paid even with a warm cache. Letters and numbers
need no scan: `\w` covers them in every plane. A Unicode version that adds a mark elsewhere
fails `test_the_mark_scan_covers_every_plane_with_marks`, and
`test_the_token_pattern_is_exactly_letters_numbers_and_marks` checks the finished pattern
against a full scan of every code point.

Every apostrophe separates (owner decision, US-02 review). `'` (U+0027) and `’` (U+2019) are
punctuation and always did. U+02BC MODIFIER LETTER APOSTROPHE is a letter (`Lm`), so `\w`
kept it inside the word: Ukrainian `обʼєкт`, its standard spelling, stayed one token while
`об'єкт` split into two, and the two spellings of one word could not find each other.
`_APOSTROPHE_LETTERS` carves out U+02BC and the other modifier letters that are apostrophe
glyphs: U+02BB (turned comma, the `ʻ` of Uzbek `oʻ` and Hawaiian ʻokina), U+02BD (reversed
comma) and U+02EE (double apostrophe). Splitting `oʻzbek` loses nothing that `o'zbek` keeps.
Left as letters: the primes U+02B9/U+02BA and half rings U+02BE/U+02BF, which are
transliteration letters, not apostrophes, and U+A78C saltillo, a lower-case letter.

Identifier splitting keeps its ASCII-era shape: `[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+`,
evaluated over a class string (`A` upper or title case, `a` any other letter, `0` number,
`m` mark following its base character). For ASCII input the fragments are the same as
before (`HTTPServer2Go` → `http, server, 2, go`). For a mixed identifier the accented letter
is a letter, so `CacheMémoire` → `cache, mémoire` with acronym `cm`. The old ASCII ranges
made it `cache, m, moire`, and `moire` matched an unrelated word. Cased scripts split the
same way (`ОтменаЗаказа` → `отмена, заказа`). A letter without case (Hebrew, CJK) counts as
lower case: no rule for it was asked for, and it at least never invents a split inside a
word.

Limits, all deliberate and each its own slice if demand appears:

- Diacritics are significant: `cafe` does not find `café`.
- No morphology: `заказ` does not find `заказа`.
- Case folding is `str.casefold`, with no locale: Turkish `İ` folds to `i̇`, not `i`.
- No segmentation for text written without spaces (Chinese, Japanese, Thai): such a run is
  one token.
- Apostrophes, in every form above, and hyphens separate: `l'annulation` is `l` and
  `annulation`, `обʼєкт` is `об` and `єкт`.

A section heading with no ASCII letters still gets the anchor `section`
(`contracts/sections.md`), so a match there shows as `§N-section`. The index still names
the section. The anchor is section identity, not tokenisation, and is left as it is.

Changing any of this changes the cached `literal_tf`/`derived_tf`, so it bumps
`cache.CACHE_FORMAT_VERSION`. That constant also carries `unicodedata.unidata_version`,
because every step above reads the Unicode database (`contracts/cache.md`).

Golden G8 (`ENGMEM-SPEC.md` §8) changed with this, approved by the owner. Its query mixes
Greek words with `MessageQueue sweeper`. The Greek used to be dropped by the tokenizer, so
the scores equalled those of `MessageQueue sweeper` alone. Now the Greek words are query
terms that match nothing, and coverage counts them. The test pins the same top hit and order
as before, and scores equal to the same query with two unmatched ASCII words in place of the
Greek ones.

## Drafts are outside the ranking corpus (`ranking_corpus`)

`search` and `search_with_role_sections` rank over `ranking_corpus(docs)`, which leaves every
draft out before any statistic is taken: the section index, `n`, `avgdl`, document
frequencies and so `DF_CEILING_RATIO`, `idf`, and the ambiguity clusters. Adding, editing or
deleting a draft therefore cannot change the set, order or score of a published result; only
the scoreboard's `drafts: N` (and the `docs: N` / `last doc` it shares a line with) moves.

The defect this closes: drafts used to be indexed with everything else and dropped only after
scoring. An active document with the term in one of two sections sits at `df / n = 0.5`, inside
the ceiling; a draft repeating the term in three more sections put it at `4 / 5`, the term left
body scoring, and the active document vanished from a search that had found it — a silent
false negative caused by unpublished text. Filtering earlier is the whole fix: a draft is never a
hit and contributes nothing to any score, so excluding it from statistics loses nothing.

`superseded` stays in the corpus. It is not hidden: a match emits its redirect, so it is ranked
like any other result, and its terms are terms a published document once used.

The successor lookup still sees the whole store (`_search_core`'s `store_docs`): a superseded
document's `superseded_by` may name a draft, which `output.md` names but does not render.
Once published (`draft -> active`) a document joins the corpus on the next search and is ranked
exactly as one that was born active.

## A repository scope narrows the corpus, before any statistic (US-09)

`search(docs, query, scope)` and `search_with_role_sections` take an optional `Scope`: one or
more repositories (`Scope(repos=(...))`, `engmem search --repo`, MCP `repo` / `repos`), the
records linked to none (`Scope(unscoped=True)`, `--unscoped`, MCP `unscoped: true`), or every
record (`Scope(all_repos=True)`, `--all-repos`, MCP `all_repos: true`; see below). `None`, the
default, is the whole store and ranks exactly as before the scope existed. A scope is applied in
`ranking_corpus`, the same place a draft is left out, so `n`, `avgdl`, document frequencies,
`DF_CEILING_RATIO` and the ambiguity clusters are all computed over the scope's records only. A
scoped search ranks exactly like a store that holds only those records.

Why before ranking and not after it. Ranking the whole store and dropping the other
repositories' hits afterwards fails in the same two ways a draft did before US-01:

- another repository's text moves a term past the DF ceiling — `flag` in 4 of 5 sections store
  wide, 1 of 2 inside the scope — and the scope's only record that holds it is no longer found;
- another repository's entity forms a second ambiguity cluster (`WidgetCache` here,
  `WorkerCount` there, both `WC`), so a scoped short query is marked ambiguous and collapsed
  over documents the reader never sees.

The cost is that a score is comparable only within one scope; golden tests pin order and
membership, never an absolute score, and nothing compares scores across searches.

The link is the `repos` front-matter field and nothing else (`in_scope`). The repo name also
sits in `tags` by convention (data-model.md), and a title often names it; neither counts, or a
record about the repository would be indistinguishable from a record linked to it (AC-09.3).
Names compare by `repo_key`: surrounding space and case do not count, because the same
repository is written `platform-core` in one session and `Platform-Core` in the next, and
Gate 1's dogfooding check already compares case-insensitively. Nothing else is normalised: a
remote URL, a `.git` suffix or a path is a different name, and the hit's `repos:` line
(`output.md`) shows the reader the spelling that did or did not match.

`repos` is read once, by `spine.parse_document` (`Doc.repos`). A record whose `repos` is
absent, empty or only blank names has no link; one whose value is neither a list nor a string
(`repos: {a: b}`) has `Doc.repos = None` — its links are unknown — and loads with a warning,
never dropped. Both are left out of every repository scope and found under `unscoped`.

The successor of a superseded hit and a hit's `related` documents are still looked up in the
whole store: they are pointers named by a record inside the scope, and the successor's own
`repos:` line shows when it lives elsewhere.

A search filter is not an access control: the tools, the store and every other repository's
records stay readable, and `--repo` narrows only what one search ranks.

## Several repositories are a union, and every record counts once (US-10)

`Scope(repos=(a, b))` keeps a record linked to any of the chosen repositories (`in_scope`), so
the ranking corpus is the union and each record enters it once, however many of the chosen
repositories it names (AC-10.2): the corpus is a filter over the store's documents, never a
concatenation of per-repository lists. Ranking still happens once, over that union, so a union
search ranks exactly like a store holding only those records, for the same reasons a single
repository does. Ranking each repository on its own and merging would give one record two
scores and compare scores across corpora, which nothing here does.

`Scope` collapses the names by `repo_key` when it is built (`_distinct_repos`): `a`, `A` and
` a ` are one repository, kept in the order first given and spelled as first given, so the
`scope:` line and the telemetry row show what the caller typed, once. A blank name among
several is refused by both channels, never dropped: dropping it would search fewer
repositories than the caller listed without saying so. A repository nobody links to is simply
an empty part of the union.

`Scope` also keys its names once, into `repo_keys`, so `in_scope` compares each document's
links against a ready set. Keying them inside `in_scope` cost one `repo_key` per chosen name per
document on every search: 10,000 names over 3,000 records took 1.5–3 s, now about 1 ms.
`repo_keys` is derived from `repos`, so it is left out of `==`, the hash and the repr
(`compare=False`, `repr=False`): two scopes are equal when their names are, as before, and the
repr shows only what the caller chose. It is not an `__init__` argument (`init=False`), so a
caller cannot hand in keys that disagree with the names, and `dataclasses.replace` recomputes
it.

`--unscoped` stays its own scope rather than an addition to a list (no "a plus the unlinked
records"). Nothing asked for it, and one scope per search keeps the `scope:` line a single
statement of what was searched.

`Scope(all_repos=True)` exists for AC-10.3: its corpus is the whole store, exactly the
default's (`in_scope` is always true), so it ranks exactly as a search without a scope. The only
difference is that it is a scope — the result names the mode and every block shows its links
(`output.md`). It is opt-in because the default stays byte-identical to the output before
US-09 (an owner decision recorded there).

A scope belongs to one search. It is an argument of `search`, `compose` and the tool call,
never stored: the section cache is keyed by a document's path and identity, not by a scope, and
the MCP server keeps no state between calls, so the next search without a scope argument
searches the whole store again (AC-10.4).

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
may represent it. A draft never reaches clustering (it is outside the ranking corpus); the
status partition runs after clustering, so a superseded representative would collapse its
cluster-mates and then be dropped itself, taking
an active document out of the results silently. Rejected: narrowing membership instead of
representation — within the ambiguous branch a superseded cluster-mate would survive the
collapse and emit a second entry for one cluster, against §7's "output one top doc from each
cluster". Below the gate (fewer than two clusters with a representative) nothing collapses and
a superseded match emits its ordinary redirect. Accepted consequence: two active documents
sharing a cluster are not ambiguous when the only competing cluster came from a superseded
document; ambiguity between a visible meaning and an invisible one is noise.

## Section index caching (`_entries_for_doc`, `_build_section_index`)

`_entries_for_doc` reads a per-document cache entry (`contracts/cache.md`) and recomputes only
what changed; rebuilding the index on every call was fine at the 9-document fixture corpus and
not at a shared store in the hundreds. The key is `doc.source_identity`, stamped by
`spine.parse_document` before it read the body, never a fresh `stat` at search time
(`contracts/cache.md`, "Identity, not invalidation").

`_build_section_index(docs, store_docs)` indexes the documents it was handed and prunes against
the whole store. The second parameter has no default because a wrongly pruned cache neither
fails nor warns, it only costs a rebuild. Every caller narrows: `role_coverage` to searchable
documents, and pruning on that narrowed list deleted every draft and superseded document's
entry on each `engmem roles`; `search` and `search_with_role_sections` to the ranking corpus,
which would evict every draft's entry on each search.

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
