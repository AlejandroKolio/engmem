# Contract: `engmem backfill`

Source: `ENGMEM-SPEC.md` §4, data-model.md's "No field is a load gate". Implementation:
`src/engmem/backfill.py` (`propose_backfill` / `apply_backfill`); CLI surface (the
confirmation prompt, `--id`/`--all`/`--dry-run`/`--yes`) is `cli.py`'s `_cmd_backfill`.

Nothing in `backfill.py` writes without a proposal being shown first — that gate lives in
`_cmd_backfill`, not in this module. `propose_backfill` only computes what would be
written; `apply_backfill` writes it, once the caller has decided to, atomically.

## What is derived, and from what

| field | source |
|---|---|
| `id` | filename stem (`doc.id`, already how `spine._parse_one` derives it) |
| `title` | first `# H1`, `Knowledge Base — ` prefix stripped (`doc.title`, ditto) |
| `date` | a `- Date:` / `- Updated:` preamble line, else file mtime (`doc.date`, ditto) |
| `task_date` | = `date` |
| `status` | a `- Status:` preamble line — `superseded` if it says so, `active` otherwise (matches every observed spelling: "Delivered", "In progress", and the common case of no line at all) |
| `backfilled` | always `true` — that is what the field is for |
| `entities` | the document's `Search Keywords` section (`sections.split_sections` resolves it to `canonical == "keywords"`) — see the extraction rule below |
| `tags` | repo names from a `- Repos:` preamble line. Facet labels ("Classes", "Endpoints") are deliberately *not* tags: nearly every document in this genre has them, so a weight-1 tag built from one distinguishes nothing and only dilutes |
| `repos` | the same repo names, on their own. Not a redundant copy: `tags` is a scored spine field, so a repo name has to be there to be findable, while `repos` is the declared answer to "which code is this about" and is never ranked on |
| `related` | markdown links to sibling `.md` files found in the body |

Fields are proposed in the order above. Each is proposed only if it is not already
present in the document's own front matter (`spine._stated`) — a field a human, or an
earlier `backfill` run, already set is never proposed for overwriting.

## The entity-extraction rule, and what it gets wrong on purpose

A `Search Keywords` section is hand-written prose, not a list to parse mechanically:
bulleted, grouped by facet (`**Classes:** WidgetCache, CacheWarmer`), separated by `,` `;`
`·` or `|`, sometimes with backtick-quoted terms, sometimes with a plain (non-bold)
`Label:` instead of a bold one.

Per candidate term (after label-stripping and delimiter-splitting), a term qualifies as an
entity if:
  - it is wrapped in backticks in the source (`` `WidgetCache` ``) — the author's own
    explicit "this is a literal identifier" signal, always accepted regardless of shape; or
  - it contains a digit, or one of `/ _ . -` (endpoint paths, file names, hyphenated
    identifiers), and is not on the small denylist of tokens that also match this shape but
    are never entities (`n/a`, `tbd`, `todo`, …); or
  - it has an uppercase letter anywhere after its first character — CamelCase
    (`CacheWarmer`), an ALLCAPS acronym (`TTL`), or a multi-word Title Case phrase
    (`Response Cache`).

A term longer than 4 words or 60 characters is never accepted — that is prose, not a
keyword.

**Known false positives**: a generic capitalized phrase the author happened to
title-case for emphasis, not because it names a system entity (e.g. "Read Path" as a
facet body, not a label), and any deliberately backtick-quoted non-identifier the author
quoted for a different reason (rare in practice — backticks are a strong, deliberate
signal in this genre of document).

**Known false negatives**: a lowercase, single- or two-word term with no digit or
punctuation (`sweeper`, `cache warmup`) — the rule requires *some* identifier-shaped
signal, and plain English prose describing a concept has none. This is deliberate: the
alternative (accepting every lowercase noun phrase) would flood `entities` with the
section's connective prose and defeat its purpose. A "one term per bullet, with an
em-dash description" layout (`- **WidgetCache** — the request-scoped cache class`) is
also not handled: the leading term is read as a facet *label*, not a candidate, so it is
dropped rather than promoted to `entities`. Recognising that shape needs a different rule
(distinguishing "this bold span names the row" from "this bold span groups the row") that
starts to look like judgement rather than parsing an already-structured list, and is left
for a future revision rather than guessed at here — see `cli.py`'s `_cmd_backfill`
docstring for the same scope note applied to the CLI surface.

## The document with no `Search Keywords` section

Every other field above is still proposed — `entities` is the only field this section
feeds. Rather than guess entities from free-form body prose (which is exactly the
"close to judgement" line `ENGMEM-SPEC.md` §2.4 draws around the CLI), `entities` is
proposed empty and a note is attached telling the human so, so a silently-empty list is
never mistaken for "checked, found none".

## Atomic, body-preserving writes

`apply_backfill` stages the new content to a sibling temp file, re-splits it, and
confirms the body is byte-identical to the original and that the staged file re-parses
before `os.replace` commits it over the original — matching `mcp_server.py`'s write-tool
staging pattern, for the same reason: a crash between staging and committing must never
leave a half-written document, and a bug in this function must never be the thing that
corrupts a human's own hand-written document. Re-running `backfill` on a document it has
already completed is a no-op (`doc.spine_complete` is checked again from the file on
disk, not from a proposal computed a moment earlier).
