# Contract: section splitting and role resolution

Source: `src/engmem/sections.py`. Splits a document body into `Section`s on `##`
headings (sub-splitting oversized ones on `###`) and resolves each heading to a
stable canonical role.

## `MAX_SECTION_BYTES`

Measured on a real 9-document corpus: splitting only on `##` leaves 10 sections
over the 4096-byte cap (max 13.53 KB). Sub-splitting those on `###` drops the
over-cap count to 2 (max 4.29 KB) — a couple of dense reference sections with no
`###` boundary at all. See task brief B2 for the full measurement table.

A subsection produced by that sub-split inherits the parent section's canonical role
unless its own heading names one. Splitting is a size decision, not a semantic one: without
inheritance the role survived only on the text before the first `###`, so a section that
opened straight onto its first subheading lost its role entirely — `sections_for_role`
returned nothing and `sections_by_locator` had no entry for it. Because the role's content
is then spread over several `Section`s, a consumer that needs the whole of it (`backfill`
reading `Search Keywords`) must use `sections_for_role`, which returns every piece in
document order; `sections_by_locator` and `scoring._role_index_from_entries` keep returning
one section, which is the right answer for a reader being pointed at a place to look.

Which one is not simply "the first". An inherited role loses to a section whose own heading
names that role, wherever each sits in the document: `CANONICAL_ALIASES` maps several
headings onto one role (`architecture`/`system architecture`, `business context`/`glossary`),
so a document can hold two sections answering for one role, and without this rule an
oversized `## Business Context` splitting into `### Actors` would take `context` away from
the `## Glossary` below it — pointing a `--role` reader at a subheading that does not name
the role at all. `role_is_inherited` is that test, and both indexes make two passes with it.

Changing this changed the `canonical` values written into the section cache, so
`cache.CACHE_FORMAT_VERSION` was bumped to 2 — entries written before it are ignored rather
than served with stale roles for files whose mtime and size never changed.

The same applies to section *boundaries*, not just role values: widening the ATX/setext
heading regexes to tolerate 0-3 leading spaces (see "Heading regexes" and "Setext headings"
below) changes which text lands in which section for a document whose bytes — and therefore
`cache.identity_for` — never change. `CACHE_FORMAT_VERSION` was bumped to 3 for this reason;
without the bump, a document that was silently losing a section to this exact bug would keep
losing it after the fix shipped, until the document happened to be edited again.

`split_sections` deliberately does no tokenisation and is not cached — it measures
well under a millisecond per document even at an 800-document corpus. The expensive
step downstream is BM25 tokenisation, which `engmem.scoring` caches on disk keyed
on file identity (see `cache.py`).

## Heading regexes

`^[ \t]{0,3}` before the hash run mirrors CommonMark's own allowance: an ATX heading
may carry up to 3 leading spaces (or tabs, counted as characters here, not columns —
see the caveat below) before the `#`s; 4+ is an indented code block, not a heading.
`_H2_RE`/`_H3_RE` diverged from this for a stretch, so a heading indented by 1-3
spaces — an easy accident from an editor's auto-indent or an LLM-authored document —
silently lost its own section and its content was folded into whichever section
came before it. `_FENCE_RE` already carried the same `[ \t]{0,3}` allowance, so
this brings headings in line with fences rather than introducing new leniency.

`(?:[ \t]+#+)?` matches ATX's optional closing sequence (`## X ##`) so the trailing
hashes don't survive into the heading text and break canonical lookup. The run must
be space-separated so `C#` in heading text is untouched.

Caveat: CommonMark measures indentation in columns, where a tab advances to the next
4-column stop (so a single leading tab is already "4 spaces" and should read as
indented code). `[ \t]{0,3}` counts characters, not columns, so a tab-indented
heading is recognised here where a strict CommonMark parser would not. This mirrors
`_FENCE_RE`'s pre-existing behaviour and is deliberately not fixed for the same
reason `_FENCE_RE` never was: a leading tab in a markdown heading is not a pattern
this project's documents produce.

## Setext headings

`_rewrite_setext` folds `Title\n-----` to `## Title` so underline-style headings
don't collapse into one anonymous section. Only `---` folds, never `===` (document
title, not a section); the text line's first character may not be a list, table,
quote, or heading marker, and the underline must be 3+ dashes, so horizontal rules
are left alone. The text line also rejects an ordered-list marker (`\d{1,9}[.)]`
followed by whitespace) — CommonMark reads "2. Chose Z" over "---" as a list item
followed by a thematic break, never a heading, and folding it anyway silently
truncated the roled section above it at its last numbered item.

Both the text line and the underline line tolerate the same 0-3 leading spaces as
the ATX regexes, for the same reason — an indented setext heading used to drop out
of a document's section structure entirely, since setext folding runs before `##`
is ever looked for. In a document whose headings are all setext, this collapsed the
whole document into one anonymous `body` section; in a document that mixes setext
with ATX `##` headings, only the indented setext heading's own section was lost, not
the rest of the document.

The tab-vs-column caveat above (`_H2_RE`/`_H3_RE`) applies here too, and in the
opposite direction: a tab-indented underline (`A\n\t---`) or a tab-indented text line
(`\tA\n---`) is read as a heading here where CommonMark reads none, since `[ \t]{0,3}`
counts the tab as one character, not a 4-column indent. Where the ATX caveat is the
section-missing direction (a genuine heading not recognised), this one is
section-inventing — text that is not a heading gets folded into one. Unlike the ATX
caveat above, which mirrors `_FENCE_RE`'s pre-existing behaviour, this divergence is
introduced by the `[ \t]{0,3}` allowance itself: the previous pattern required the
dash run to follow the newline immediately, so a tab blocked the fold, and the
previous text-line class rejected a leading tab outright as whitespace. Accepted
knowingly, for the same reason: not a pattern this project's documents produce.

## `_fold_trailing_parenthetical`

Strips exactly one outermost trailing `(...)` clause and retries the alias table —
e.g. "architecture (the read path)" -> "architecture". Bounded to "prefix + one
trailing bracket", not general prefix matching: an unbounded prefix match would
wrongly fold "Testing Knowledge Gaps" onto "testing knowledge" (a distinct
open-questions section) and "Decision Log Review Notes" onto "decision log" (an
index of *other* documents' logs) — neither has a trailing bracket, so neither
folds.

## `CANONICAL_ALIASES`

A flat, hand-authored literal-match table, not a fuzzy matcher — real documents
spell the same section role differently across authors and revisions, and the
spelling set is small enough to enumerate. Extend it as new spellings turn up.

Measured on the 9-document corpus; each role below is carried by 2+ documents
under 2+ spellings, or is one of engmem's own operational roles (`prereg`/`reuse`/
`trace`, one spelling each, named for stable `--role` addressing).

| canonical | observed spellings | coverage |
|---|---|---|
| `decisions` | Decision Log; Decision Log & Scope | 8/9 |
| `summary` | Executive Summary | 7/9 |
| `testing` | Testing Knowledge | 7/9 |
| `keywords` | Search Keywords | 7/9 |
| `cheatsheet` | One-Page Cheat Sheet | 7/9 |
| `production` | Production Considerations | 6/9 |
| `requirements` | Functional Requirements | 5/9 |
| `graph` | Knowledge Graph | 5/9 |
| `context` | Business Context; Business Context (glossary); Domain & Technical Glossary; Glossary | 4/9 |
| `architecture` | System Architecture; Architecture (the read path) | 4/9 |
| `primer` | Future LLM Context (cold-start primer); Cold-start primer; Future-LLM Cold-Start Primer | 4+2/9 |
| `acceptance` | Acceptance Criteria Analysis; Acceptance Criteria Status; Acceptance Criteria (live status → overview) | 3/9 |
| `lessons` | Lessons Learned; Lessons Learned & Traps; Lessons Learned / Defects Caught; Landmines | 3/9 + 2/9 |
| `flow` | End-to-End Flow; End-to-End Business Flow | 2+/9 |
| `code` | Code Implementation; Code Implementation (the actual change — …) | 2/9 |
| `status` | Status at a Glance; Status Board & TODO | 2/9 |
| `prereg` | Pre-Reg | 2/9 |
| `reuse` | Reuse Log | 2/9 |
| `trace` | Search Trace | 2/9 |

Notes on entries that are not simple 1:1 folds:
- `landmines` merges into `lessons` — both name the pitfalls-surfaced role, split
  only by document era, not by meaning; splitting would fragment recall for the
  same failure a Production Considerations section might also answer.
- `context`, `architecture`, `primer`, `acceptance` each list a bracket-free "base"
  spelling even where it was not independently observed, so `_fold_trailing_parenthetical`
  has something to resolve to. `domain & technical glossary` and bare `glossary` are
  separate spellings, not bracket variants of each other, so each has its own entry.
  `architecture (the read path)` drops "System" entirely, so bare `architecture` is
  its own entry, not just a fold target of `system architecture`.

## `CANONICAL_ROLES`

Derived from `CANONICAL_ALIASES`'s own values, not hand-listed, so the CLI
`--role` validation, the MCP role tool's schema enum, and `engmem roles` share one
source of truth and a role added later cannot be forgotten in a second place. Every
role also resolves to its own bare name (`self.value: value`), since templates and
other agents write the short form directly (e.g. plain `## Testing`).
