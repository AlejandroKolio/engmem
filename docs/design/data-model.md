# Phase 1 Data Model: Engmem — Personal Engineering Memory

Source of truth for field-level detail: `ENGMEM-SPEC.md` §4. This document restates it as
an entity model for implementation, per spec.md's Key Entities section.

## Entity: Engineering Session Document

A single completed piece of engineering work, stored as one `sessions/<id>.md` file:
YAML front matter + a fixed set of markdown body sections.

### Front matter fields

| Field | Type | Required | Set by | Notes |
|---|---|---|---|---|
| `id` | string | derived | automatic | `<story-id>-<slug>` or `<YYYYMMDD>-<slug>`; MUST equal the filename stem |
| `title` | string | derived | agent draft, human confirms | short human-readable title |
| `date` | date | derived | automatic | when the document was written |
| `task_date` | date | derived (defaults to `date`) | automatic | when the underlying work happened; differs from `date` only for backfilled docs |
| `status` | enum | derived (defaults to `active`) | automatic / human (save flow) | `draft` \| `active` \| `superseded` |
| `superseded_by` | string (id) | no | human (save flow) | id of the document that superseded this one; empty if not superseded |
| `backfilled` | bool | no (defaults to `false`) | automatic | `true` for documents written after the fact (seeding) |
| `tags` | list[string] | no (empty — warning) | agent draft, human confirms | domain tags, lowercase |
| `entities` | list[string] | no (empty — warning) | agent draft, human confirms | real classes/endpoints/terms/synonyms this doc concerns; no separate `aliases` field |
| `related` | list[string] (ids) | no | agent draft, human confirms | ids of other documents this one relates to |
| `covers_files` | list[string] | no | agent draft, human confirms | files touched by the underlying work |
| `verified_at_commit` | string (git sha) | no | automatic | HEAD at the moment of save |
| `author` | string | no | automatic (`git config user.name`) | who did the work; constant in a single-author store, kept for parity with hand-written knowledge-base documents |
| `repos` | list[string] | no | agent draft, human confirms / backfill | repositories the work touched. The same names also appear in `tags`, deliberately: `tags` is scored and `repos` is not, so the tag copy is what makes a repo findable while this field is what a reader looks at |
| `branch` | string | no | automatic (`git rev-parse --abbrev-ref HEAD`) | branch the work was done on |
| `pr` | string \| number | no | agent draft | pull request number or URL, blank when none exists |
| `navigation_miss` | list[{doc, query}] | no | agent, at save | documents the search failed to surface that were used anyway. Both keys required per entry; a half-written entry is dropped with a warning, never the document. Counted by `engmem telemetry` separately from search misses — a search that found nothing and a search that missed something present are different failures |
| `capture_minutes` | number \| null | no | automatic | elapsed time from draft creation to save |
| `baseline_tokens` | number | no | automatic | tokens the no-memory sub-agent consumed producing the Pre-reg baseline; omitted when the runtime does not report it |
| `context_bytes` | number | no | automatic | bytes of prior-document text pulled into context this session |
| `answer_steps` | number | no | automatic | tool calls needed *after* the search to reach the answer |
| `answer_tokens` | number | no | automatic | tokens for the answering pass, only when the runtime really reports it |

**No field is a load gate.** `derived` means the parser computes the value when the front
matter omits it (`id` ← filename stem, `title` ← first `# H1`, `date` ← a `- Date:` /
`- Updated:` preamble line then mtime, `task_date` ← `date`, `status` ← `active`). A
document loads with a partial or entirely absent spine and is flagged `spine_complete:
false`; the count of such documents is reported on **stdout** in the search footer, e.g.
`docs: 9 (7 partial spine) | drafts: 1 | last doc: 0d ago`. Only corruption excludes a
document: duplicate `id`, invalid YAML, an unclosed front-matter block, an unparseable
date, an `id` containing an embedded newline, a value of the wrong type for its field, or
the file being unreadable in the first place (permission denied, a dangling symlink, a
directory shadowing the `.md` name). A scalar in a list-typed field (`tags: platform`
instead of `tags: [platform]`) is *not* corruption — it degrades to a one-element list
with a warning, the same way a missing value degrades.

`status` is normalized case-insensitively (`.strip().casefold()`) before being compared
against `draft`/`active`/`superseded`, so `Draft` and `SUPERSEDED` behave exactly like
their lowercase forms rather than silently failing every equality check search.py and the
scoreboard perform against the lowercase literals. A stated value that still is not one
of the three recognized states (`status: wip`) is *not* a load gate either — it degrades
to `active` with a warning naming the rejected value, the same "never hide the document"
reasoning that governs every other field here.

Spine completeness (`spine_complete` / the `partial spine` count) is measured over the
fields that affect retrieval only — `id`, `title`, `date`, `task_date`, `status`, `tags`,
`entities`. `backfilled`, `verified_at_commit`, `capture_minutes`, `author`, `repos`,
`branch` and `pr` are excluded: nothing ranks, orders or filters on them, and a draft cannot know the commit it will be verified
at nor how long its own capture will take. Counting them flagged every draft the `/engmem`
template creates as a partial spine, which trains the reader to ignore the signal.

`capture_minutes: null` means "never measured" and is distinct from `0`, which means
"measured as instantaneous". Ritual telemetry is never a precondition for loading
knowledge.

**Zero manually-typed fields**: every field is either fully automatic or a one-round
agent draft that the human approves with `y` or edits — never asked for field-by-field
(spec FR-002; `ENGMEM-SPEC.md` §4 "Manually filled fields — zero").

The four cost fields are optional by design: they are omitted whenever the number is not
actually available, because a guessed figure would corrupt the only analysis this project
exists to produce. The parser ignores fields it does not know, so they require no schema
enforcement — they are read at review time, not validated at write time.

### Body sections (fixed order; `save.quick` writes only sections 2, 5, 6)

1. `## Pre-reg` — 2–3 line naive baseline generated before any search, never asked of the
   human, plus a `pre-reg source:` line naming how it was obtained (spec FR-011)
2. `## Decision Log` — decisions made + alternatives rejected, with reasons
3. `## Lessons Learned` — pitfalls hit during the work
4. `## Future LLM Context (cold-start primer)` — 5–10 lines for a cold-starting agent
5. `## Reuse Log` — see **Reuse Log Entry** below
6. `## Search Trace` — one of `shell | paste | miss` (spec FR-014)

**Content rule**: only what isn't in the code, or is expensive to re-derive (rejected
alternatives, incidents, business constraints, agreements, glossary) belongs in the body.
As-is architecture, flows, and contracts already visible in code are not duplicated here.

### State transitions

```
draft --(save: human confirms full draft)--> active
active --(save of a LATER doc: human confirms "yes, this supersedes it")--> superseded
```

- `draft`: created immediately when `/engmem` starts; excluded from search results but
  counted in the scoreboard's `drafts: N`.
- `active`: the only state search actually returns as a primary result.
- `superseded`: excluded from primary results; a query that would otherwise rank it
  returns `superseded by <id>` plus the successor document, if the successor itself
  exists in the store and can be output. Two successors cannot: a `draft` one is
  named but not rendered (a draft is excluded from search results, and a redirect
  is a search result), and one that is *itself* superseded is named along with what
  supersedes it, since handing over a known-stale document is the harm the redirect
  exists to prevent. A superseded document with no `superseded_by` recorded says so
  rather than naming an absent id.
- No other transitions exist (a superseded document does not return to `active`; this is
  intentionally out of scope — reversing supersession is a manual front-matter edit, not
  a supported CLI/template operation, per principle V).

`superseded_by` is coerced to `str` like every other id-shaped field. Unquoted, YAML reads
`2026-01-01` as a `date`, and the MCP `mark_superseded` tool writes the value unquoted,
re-reads it and compares it against the string it was given — so that round trip used to
refuse its own write.

### Reuse Log Entry (row of the Reuse Log table)

| Field | Description |
|---|---|
| `prior-doc` | id of the prior document that influenced this work |
| taken | concrete artifact reused (class / contract / decision / pitfall) + a direct quote from the prior document |
| impact | how it influenced this work |
| classification | `reuse` \| `anti-reuse` \| `harmful` |

**Validity rule**: a row exists only if the prior document was actually opened and
influenced the work; the artifact must be concrete (not "the doc was helpful"); a row
without a quote is invalid. Time saved is never estimated in this row — that judgment
belongs to the human at review time, outside this document. If nothing was reused, the
section is exactly the sentence `Prior docs used: none.` — never blank, never omitted
(spec FR-001, User Story 2 acceptance scenario 3).

### Validation rules

| Severity | Condition | Effect |
|---|---|---|
| error | duplicate `id` across documents | fails loud; the offending document is named in the error (principle VIII) |
| error | invalid YAML front matter | fails loud for that document |
| error | the file cannot be read (`OSError`: permission denied, a dangling symlink, a directory shadowing the `.md` name) | fails loud for that document; never an uncaught exception that aborts the whole load |
| error | `sessions/` itself cannot be listed (`OSError`: permission denied) | distinct from a single unreadable file above — the scan uses `Path.iterdir`, which propagates the `os.scandir` failure rather than swallowing it as a glob would and rendering an unlistable directory byte-identical, on stdout, to a genuinely empty store; reported as an error (the true document count is unknown, not zero) with a dedicated stdout line, not merely folded into the generic failed-to-load count |
| error | a front-matter value of the wrong type for its field (e.g. `capture_minutes: [1, 2]`, `tags: 5`) | fails loud for that document, named by field |
| error | `id` contains an embedded newline | fails loud for that document; a multi-line `id` can never equal the filename stem, and rendered into a `### <id> (score: ...)` header it forges a second, fabricated result block |
| warning | `entities` is empty | surfaced on stderr; document still processed |
| warning | `id` ≠ filename stem | surfaced on stderr; document still processed |
| warning | a scalar value in a list-typed field (`tags`, `entities`, `related`, `covers_files`), e.g. `entities: WidgetCache` | coerced to a one-element list and named on stderr — never silently exploded into single characters by `list(str)` |
| warning | `status` is not `draft`/`active`/`superseded` after case-insensitive normalization, e.g. `status: wip` | degraded to `active` and named on stderr — never silently ranks or counts as whichever of the three states its raw string happens to equal |
| warning | `backfilled` is not a YAML boolean, e.g. `backfilled: "false"` | treated as `false` and named on stderr — `bool("false")` is `True`, so a quoted value would read as the opposite of what it says |
| warning | `capture_minutes` coerces but was not written as an integer, e.g. `"12"` or `3.9` | read and named on stderr, so the same quoting slip is reported for this field as for `backfilled` |
| warning | `superseded_by` is not a string, e.g. an unquoted `2026-01-01` | read as its `str` and named — a non-string would otherwise print as a fabricated id |
| error | `capture_minutes` is `.inf`/`.nan` | the document is skipped, the rest of the store still returns. `int(float("inf"))` raises `OverflowError` and `.nan` raises `ValueError`; `_coerce_int` catches both and re-raises as the `ValueError` `load_store` handles, so one document's infinity does not take the whole load down |
| error | `capture_minutes` is a boolean | rejected: `int(True)` is `1`, so a boolean would arrive as one measured minute rather than as the wrong type it is |

Per spec FR-008 / Edge Cases: an **error** on one document during a `search` call MUST
NOT abort the whole call — the malformed document is skipped (with the error/warning
reported), and results from the rest of the store are still returned. "Fails loud" means
the failure is visible and named, not that it takes down the whole operation.

## Entity: Document Store

The engineer's full collection of Engineering Session Documents.

| Attribute | Description |
|---|---|
| location | resolved via `--store PATH` flag → `ENGMEM_HOME` env var → default `~/Developer/engmem` |
| contents | `sessions/*.md` (the documents) + `telemetry.jsonl` (append-only search log) |
| persistence | a git repository (created by `install` if it doesn't exist); no other database or cache |
| scope | machine-wide default, or single-project if `--local` was used at install time (spec FR-017) |

There is no derived/generated artifact belonging to the store (no index file, no cache) —
this is a hard invariant, not an optimization detail (spec FR-019; research.md "Storage &
indexing strategy").

## Entity: Search Result Set

The ephemeral output of one `search` call — not persisted, only rendered and logged to
telemetry.

| Attribute | Description |
|---|---|
| results | top 3 matching **active** documents, ranked by score (research.md "Search ranking algorithm") |
| per-result fields | id, path, score, why-matched (which query tokens matched which fields), first 2 lines of Cold-start primer, its `related` (id + path only, depth 1) |
| ambiguous flag | set when a short/numeric query token matches entities across distinct clusters; one representative per cluster is returned instead of a single top result |
| scoreboard footer | always present: `docs: N | drafts: N | last doc: Nd ago`; `N` in `last doc` is clamped to `0` for a future-dated newest document (typo, timezone skew, a `task_date` copied forward), since `Nd ago` cannot express a negative count |
| size constraint | total rendered output ≤ 4 KB (`output.MAX_OUTPUT_BYTES`; spec SC-002, FR-004) |
| no-match output | exactly `prior context: none found`, exit code 0 |

Every call also produces one `telemetry.jsonl` line (see research.md "Telemetry &
measurement instrumentation") — this is a side effect of the Search Result Set being
produced, not a separate entity. The line carries two join keys beyond the result itself:
`session_id` (the draft document this search was run for, `null` when unattributed) and
`surfaced` (the ids actually shown, including redirect targets that never scored).
Together they let the Gate 1 review connect what was retrieved to what a session's Reuse
Log claims to have reused. A tool call over the MCP path writes the identical line.
