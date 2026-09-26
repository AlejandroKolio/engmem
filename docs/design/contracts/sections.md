# Contract: section splitting and role resolution

Source: `src/engmem/sections.py`. Splits a document body into `Section`s on `##` headings,
sub-splitting oversized ones on `###`, and resolves each heading to a stable canonical role;
`scoring` ranks over the pieces and `--role` addresses them.

## Invariants

- `split_sections` tokenises nothing and is not cached: it measures well under a millisecond
  per document at an 800-document corpus. The expensive step is the tokenisation downstream,
  which `scoring` caches on disk (`contracts/cache.md`).
- A change here that alters what a cached payload holds for a document whose bytes did not
  change — the `canonical` a heading resolves to, or the section boundaries themselves — needs
  a `cache.CACHE_FORMAT_VERSION` bump, because `cache.identity_for` cannot see it. Role
  inheritance took the bump to 2; tolerating 0-3 leading spaces before a heading took it to
  3, without which a document that had been losing a section to that defect would keep losing
  it until it happened to be edited.
- The anchor is the heading's slug with any ordinal prefix stripped (`_ORDINAL_PREFIX_RE`),
  so it survives renumbering that touches no content; `index` is a display convenience only.
  Slugs go through NFKD and drop combining marks, so `Müller` slugs to `muller`, not `m-ller`;
  a heading with no sluggable characters gets `section`.

## `MAX_SECTION_BYTES`

Measured on a 9-document corpus: splitting only on `##` leaves 10 sections over the 4096-byte
cap (max 13.53 KB); sub-splitting those on `###` drops the over-cap count to 2 (max 4.29 KB),
both dense reference sections with no `###` boundary. A section with no `###` is returned
whole rather than chunked at invented boundaries; text before the first `###` keeps the parent
heading, since it is lead-in, not a subsection.

## Role inheritance across a sub-split

A `###` piece inherits the parent's role unless its own heading names one. Splitting is a size
decision, not a semantic one: without inheritance the role survived only on the lead-in, so a
section opening straight onto its first `###` lost its role entirely. The role's content is
then spread over several `Section`s, so a consumer that needs all of it (`backfill` reading
`keywords`) uses `sections_for_role`, which returns every piece in document order;
`scoring._role_index_from_entries` returns one section, the right answer for a reader being
pointed at a place to look.

Which one is not simply the first. `CANONICAL_ALIASES` maps several headings onto one role, so
a document can hold two sections answering for it, and an inherited role loses to a section
whose own heading names the role wherever each sits in the document: otherwise an oversized
`## Business Context` splitting into `### Actors` takes `context` away from the `## Glossary`
below it and points a `--role` reader at a subheading that does not name the role.
`role_is_inherited` is that test, and `scoring._role_index_from_entries` makes two passes with
it; an inherited role still answers when nothing else claims it.

## Heading regexes

`_H2_RE`/`_H3_RE` allow up to 3 leading spaces or tabs before the hashes — CommonMark's own
ATX allowance (4+ is an indented code block), and the same `[ \t]{0,3}` `_FENCE_RE` already
carried. Without it a heading indented by an editor's auto-indent or an LLM silently folded
its content into the section before it. `(?:[ \t]+#+)?` eats the optional closing hashes of
`## X ##` so they do not break canonical lookup; the run must be space-separated, so `C#` in
heading text is untouched.

Headings inside a matched fence pair are sample output, not structure, and are skipped. A
pair is backtick or tilde, and its closer is the same character, no shorter, with no info
string.

## Setext headings

`_rewrite_setext` folds `Title\n---` to `## Title` before `##` is looked for, so
underline-style headings do not collapse into one anonymous section. Only `---` folds, never
`===` (the document's own title); the underline is 3+ dashes; and the text line cannot start
with a list, table, quote or heading marker, nor be an ordered-list marker (`\d{1,9}[.)]` then
whitespace) — CommonMark reads "2. Chose Z" over "---" as a list item followed by a thematic
break, and folding it truncated the roled section above at its last numbered item — so this
project's horizontal rules stay rules. Both lines tolerate the same 0-3 leading spaces as the
ATX regexes, for the same reason.

## Where the splitter diverges from CommonMark on purpose

`tests/test_commonmark_parity.py` keeps the regexes beside a real parser. The agreements it
pins are the rules above; three divergences are deliberate, and each has its own test there,
named `diverges_on_purpose`, asserting both halves — a section from `split_sections`, no `h2`
from the parser:

- An unclosed fence does not run to end of document: only matched pairs count, or one stray
  fence hides every heading after it.
- A blockquoted heading (`> ## ...`) opens no section: a quoted heading is another document's,
  and opening a section there files our prose under someone else's title.
- Tabs count as characters, not 4-column stops. A tab-indented ATX heading is recognised where
  CommonMark reads indented code (mirroring `_FENCE_RE`); a tab-indented setext text line or
  underline is folded where CommonMark reads no heading (introduced by the `[ \t]{0,3}`
  allowance itself). Both accepted because a leading tab in a heading is not a pattern this
  project's documents produce.

## `_fold_trailing_parenthetical`

Strips exactly one outermost trailing `(...)` and retries the alias table — "architecture (the
read path)" -> "architecture". Rejected: general prefix matching, which would fold "Testing
Knowledge Gaps" onto "testing knowledge" (a distinct open-questions section) and "Decision Log
Review Notes" onto "decision log" (an index of *other* documents' logs); neither has a trailing
bracket, so neither folds.

## `CANONICAL_ALIASES`

A flat, hand-authored literal-match table, not a fuzzy matcher: real documents spell one role
differently across authors and revisions, and the set is small enough to enumerate. Extend it
as new spellings turn up. Lookup runs on the heading with its ordinal stripped,
NFKC-normalised, whitespace-collapsed and casefolded.

Measured on the 9-document corpus; each role below is carried by 2+ documents under 2+
spellings, or is one of engmem's own operational roles (`prereg`/`reuse`/`trace`, one spelling
each, named for stable `--role` addressing).

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

Entries that are not simple 1:1 folds:
- `landmines` merges into `lessons`: both name the pitfalls-surfaced role, split by document
  era, not by meaning, and splitting them would fragment recall.
- `context`, `architecture`, `primer`, `acceptance` each list a bracket-free base spelling
  even where it was not independently observed, so `_fold_trailing_parenthetical` has
  something to resolve to. `domain & technical glossary` and bare `glossary` are separate
  spellings, not bracket variants of each other; `architecture (the read path)` drops
  "System" entirely, so bare `architecture` is its own entry.

## `CANONICAL_ROLES`

Derived from `CANONICAL_ALIASES`'s own values, not hand-listed, so CLI `--role` validation, the
MCP role tool's schema enum and `engmem roles` share one source of truth and a role added
later cannot be forgotten in a second place. Every role also resolves to its own bare name,
derived the same way, since templates and other agents write the short form directly (plain
`## Testing`).
