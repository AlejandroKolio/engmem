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

`split_sections` deliberately does no tokenisation and is not cached — it measures
well under a millisecond per document even at an 800-document corpus. The expensive
step downstream is BM25 tokenisation, which `engmem.scoring` caches on disk keyed
on file identity (see `cache.py`).

## Heading regexes

`(?:[ \t]+#+)?` matches ATX's optional closing sequence (`## X ##`) so the trailing
hashes don't survive into the heading text and break canonical lookup. The run must
be space-separated so `C#` in heading text is untouched.

## Setext headings

`_rewrite_setext` folds `Title\n-----` to `## Title` so underline-style headings
don't collapse into one anonymous section. Only `---` folds, never `===` (document
title, not a section); the text line's first character may not be a list, table,
quote, or heading marker, and the underline must be 3+ dashes, so horizontal rules
are left alone.

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
