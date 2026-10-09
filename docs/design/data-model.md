# Phase 1 Data Model: Engmem — Personal Engineering Memory

Source of truth for field-level detail: `ENGMEM-SPEC.md` §4. This document restates it as
an entity model for implementation.

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
| `covers_files` | list[string] | no | agent draft, human confirms | files the decision rests on, each a path from the repository's top-level directory as `git ls-files` prints it. A search compares exactly these between the `verified_at` commit and `HEAD` when the two differ (US-12): unchanged, changed / deleted, or not checked with a reason. They are read as files of the record's one repository; a record linked to several is not compared (contracts/provenance.md) |
| `verified_at` | mapping repository → commit | no | automatic at save (`git -C <checkout> rev-parse HEAD` per repository) | the commit each repository was checked at (US-11). Names spelled like `repos`; a value is 7–64 hex characters. A search shows each entry next to the session's checkout `HEAD` as same / differs / unknown with a reason, never as proof the text is true. A malformed value or field is a warning, never a load failure (contracts/provenance.md) |
| `verified_at_commit` | string (git sha) | no | legacy | the single commit older templates recorded. Read as the anchor of the record's one linked repository; with no link or several it stays an unattributed legacy anchor shown as unknown, never guessed onto one. Ignored when `verified_at` has entries; no longer written |
| `author` | string | no | automatic (`git config user.name`) | who did the work; constant in a single-author store, kept for parity with hand-written knowledge-base documents |
| `repos` | list[string] | no | agent draft, human confirms / backfill | repositories the work touched, by name (the repository's directory name, e.g. `platform-core`). The same names also appear in `tags`, deliberately: `tags` is scored and `repos` is not, so the tag copy is what makes a repo findable. `repos` is the explicit link a `--repo` search filters on (US-09; contracts/scoring.md) — the tag copy and the title never stand in for it. A scalar degrades to a one-element list with a warning; a value of another type (`repos: {a: b}`) does not cost the document — it loads with a warning, its links unknown, and only an `--unscoped` search finds it |
| `branch` | string | no | automatic (`git rev-parse --abbrev-ref HEAD`) | branch the work was done on |
| `pr` | string \| number | no | agent draft | pull request number or URL, blank when none exists |
| `navigation_miss` | list[{doc, query}] | no | agent, at save | documents the search failed to surface that were used anyway. Both keys required per entry; a half-written entry is dropped with a warning, never the document. Counted by `engmem telemetry` separately from search misses — a search that found nothing and a search that missed something present are different failures |
| `capture_minutes` | number \| null | no | agent at save | minutes the save ritual took; blank unless a real start time is known (`null` = never measured) |
| `mode` | `daily` \| `research` | no | automatic, when the draft is created | the mode the session was started in (US-08), from `engmem mode show`; copied unchanged at save. Absent = written before modes existed (legacy), counted by Gate 1 exactly as before. `daily` keeps the document outside the experiment; another value loads with a warning and is excluded from the count until corrected (contracts/gate1.md, "Modes") |
| `baseline_unavailable` | string | no | agent, before the first search | research mode only: why no uncontaminated baseline could be had. Present, the session is an incomplete observation, kept apart from the valid group and the baseline comparison; a value that is not a reason (`true`, `''`) still marks it, with a warning |
| `baseline_tokens` | number | no | automatic | tokens the no-memory sub-agent consumed producing the Pre-reg baseline; omitted when the runtime does not report it |
| `context_bytes` | number | no | automatic | bytes of prior-document text pulled into context this session |
| `answer_steps` | number | no | automatic | tool calls needed *after* the search to reach the answer |
| `answer_tokens` | number | no | automatic | tokens for the answering pass, only when the runtime really reports it |

**No field is a load gate.** `derived` means the parser computes the value when the front
matter omits it (`id` ← filename stem, `title` ← first `# H1`, `date` ← a `- Date:` /
`- Updated:` preamble line then mtime, `task_date` ← `date`, `status` ← `active`). A
preamble label is read only as far as the end of its own line — never across the line break,
whatever whitespace sits on the line itself — so an unfilled `- Date:` takes no date from the
text below it (contracts/backfill.md, "A preamble line's value is on the line"). A document
loads with a partial or entirely absent spine and is flagged
`spine_complete: false`; the count of such documents is reported on **stdout** in the search
footer, e.g. `docs: 9 (7 partial spine) | drafts: 1 | last doc: 0d ago`. Only corruption
excludes a document: duplicate `id`, invalid YAML, an unclosed front-matter block, an
unparseable date, an `id` containing an embedded newline, a value of the wrong type for its
field, or the file being unreadable in the first place (permission denied, a dangling symlink, a
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
`entities`. `backfilled`, `verified_at`, `verified_at_commit`, `capture_minutes`, `author`, `repos`,
`branch`, `pr`, `mode` and `baseline_unavailable` are excluded: nothing ranks or orders on them, `repos` filters only a search that asks for a scope (a record about no repository is complete without it), and a draft cannot know the commit it will be verified
at nor how long its own capture will take. Counting them flagged every draft the `/engmem`
template creates as a partial spine, which trains the reader to ignore the signal.

`capture_minutes: null` means "never measured" and is distinct from `0`, which means
"measured as instantaneous". Ritual telemetry is never a precondition for loading
knowledge.

**Zero manually-typed fields**: every field is either fully automatic or a one-round
agent draft that the human approves with `y` or edits — never asked for field-by-field
(`ENGMEM-SPEC.md` §4 "Manually filled fields — zero").

The four cost fields are optional by design: they are omitted whenever the number is not
actually available, because a guessed figure would corrupt the only analysis this project
exists to produce. The parser ignores fields it does not know, so they require no schema
enforcement — they are read at review time, not validated at write time.

### Body sections (the thin core; which template writes which sections is the list in `ENGMEM-SPEC.md` §4)

1. `## Pre-reg` — 2–3 line naive baseline generated before any search, never asked of the
   human, plus a `pre-reg source:` line naming how it was obtained (`ENGMEM-SPEC.md` §6, start step 1).
   Research mode only: a `mode: daily` session has none, and a research session that could not
   get a baseline records `baseline_unavailable` instead
2. `## Decision Log` — decisions made + alternatives rejected, with reasons
3. `## Lessons Learned` — pitfalls hit during the work
4. `## Future LLM Context (cold-start primer)` — 5–10 lines for a cold-starting agent
5. `## Reuse Log` — see **Reuse Log Entry** below
6. `## Search Trace` — one of `shell | paste | miss`, alone on its line (`ENGMEM-SPEC.md` §6, start step 5)

**Content rule**: only what isn't in the code, or is expensive to re-derive (rejected
alternatives, incidents, business constraints, agreements, glossary) belongs in the body.
As-is architecture, flows, and contracts already visible in code are not duplicated here.

### State transitions

```
draft --(save: human confirms full draft)--> active
active --(save of a LATER doc: human confirms "yes, this supersedes it")--> superseded
```

- `draft`: created immediately when `/engmem` starts; excluded from search results and
  from the statistics that rank them, but counted in the scoreboard's `drafts: N`.
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

A document has no stored version field. Its version is derived from its bytes on disk
(`staging.version_of`), so every existing document has one; `engmem_complete_draft` uses it
to refuse completing a draft that changed after the client read it (`contracts/mcp-server.md`,
"Rewriting a document: the version read").

`superseded_by` is coerced to `str` like every other id-shaped field. Unquoted, YAML reads
`2026-01-01` as a `date`, and the MCP `mark_superseded` tool writes the value unquoted,
re-reads it and compares it against the string it was given — so that round trip used to
refuse its own write.

### Reuse Log Entry (row of the Reuse Log table)

| Field | Description |
|---|---|
| `prior-doc` | id of the prior document that influenced this work, as `<id>@<version>` — the reference a search result's `cite as` line gives, naming the exact version quoted (US-13); a bare `<id>` is a legacy row, checked against the document's current text |
| taken | concrete artifact reused (class / contract / decision / pitfall) + a direct quote from the prior document |
| impact | how it influenced this work |
| classification | `reuse` \| `anti-reuse` (quoted, then deliberately departed from) \| `harmful` |

**Validity rule**: a row exists only if the prior document was actually opened and
influenced the work; the artifact must be concrete (not "the doc was helpful"); a row
without a quote is invalid. Time saved is never estimated in this row — that judgment
belongs to the human at review time, outside this document. If nothing was reused, the
section is exactly the sentence `Prior docs used: none.` — never blank, never omitted
(`ENGMEM-SPEC.md` §6, `/engmem.save` step 3).

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
| warning | the body is empty | surfaced on stderr; document still processed — except a `status: draft` with `mode: daily`, which is front matter alone by design (US-08) and would otherwise print the warning on every search until it is saved |
| warning | `id` ≠ filename stem | surfaced on stderr; document still processed |
| warning | a scalar value in a list-typed field (`tags`, `entities`, `related`, `covers_files`), e.g. `entities: WidgetCache` | coerced to a one-element list and named on stderr — never silently exploded into single characters by `list(str)` |
| warning | `status` is not `draft`/`active`/`superseded` after case-insensitive normalization, e.g. `status: wip` | degraded to `active` and named on stderr — never silently ranks or counts as whichever of the three states its raw string happens to equal |
| warning | `backfilled` is not a YAML boolean, e.g. `backfilled: "false"` | treated as `false` and named on stderr — `bool("false")` is `True`, so a quoted value would read as the opposite of what it says |
| warning | `capture_minutes` coerces but was not written as an integer, e.g. `"12"` or `3.9` | read and named on stderr, so the same quoting slip is reported for this field as for `backfilled` |
| warning | `superseded_by` is not a string, e.g. an unquoted `2026-01-01` | read as its `str` and named — a non-string would otherwise print as a fabricated id |
| warning | `mode` is not `daily`/`research` after case-insensitive normalization, e.g. `mode: reserch` | kept as written and named on stderr; the document loads and is searched, and Gate 1 excludes it under its own reason rather than counting it as legacy |
| warning | `baseline_unavailable` is stated but is not a reason, e.g. `true` or `''` | read as unavailable, "(no reason stated)", and named — reading it as "the baseline was there" would put a session with no baseline into the valid group |
| error | `capture_minutes` is `.inf`/`.nan` | the document is skipped, the rest of the store still returns. `int(float("inf"))` raises `OverflowError` and `.nan` raises `ValueError`; `_coerce_int` catches both and re-raises as the `ValueError` `load_store` handles, so one document's infinity does not take the whole load down |
| error | `capture_minutes` is a boolean | rejected: `int(True)` is `1`, so a boolean would arrive as one measured minute rather than as the wrong type it is |

Per `ENGMEM-SPEC.md` §5 (`engmem search` step 1): an **error** on one document during a `search` call MUST
NOT abort the whole call — the malformed document is skipped (with the error/warning
reported), and results from the rest of the store are still returned. "Fails loud" means
the failure is visible and named, not that it takes down the whole operation.

## Entity: Document Store

The engineer's full collection of Engineering Session Documents.

| Attribute | Description |
|---|---|
| location | resolved via `--store PATH` flag → `ENGMEM_HOME` env var → the choice saved by `engmem store set` → default `~/Developer/engmem` |
| contents | `sessions/*.md` (the documents) + `telemetry.jsonl` (append-only search log) + `feedback.jsonl` (append-only user assessments of finds — US-15, see **Usefulness Assessment** below) + `versions/<id>/<version>.md` (the exact bytes of each document version a search showed, kept so a Reuse Log quote can be checked against the version it cites — US-13) |
| persistence | a git repository (created by `install` if it doesn't exist); no other database or cache |
| scope | machine-wide default, or single-project if `--local` was used at install time (`ENGMEM-SPEC.md` §5, `engmem install`) |

There is no derived/generated artifact belonging to the store (no index file, no cache) —
this is a hard invariant, not an optimization detail (`ENGMEM-SPEC.md` §3).
`versions/` does not break it: a retained copy cannot be rebuilt from anything else in the
store — once its document is edited, the copy is the only record of the text a quote was
taken from — so it is evidence, like `telemetry.jsonl`, not a derived index. It is never
read as a document and never searched (`contracts/gate1.md`, "Versioned citations").
`feedback.jsonl` is not derived either: it holds assessments nothing else in the store records.

## Entity: Usefulness Assessment

One line of `feedback.jsonl`: the user's assessment of a find, a document a search attributed
to a session showed (US-15; `contracts/gate1.md`, "Usefulness feedback").

| Field | Description |
|---|---|
| `ts` | when it was recorded, UTC |
| `session_id` | the session document whose search showed the document; must name a document in the store |
| `doc_id` | the document shown (in `surfaced`, never only in `hits`); must be in that session's finds when recorded, and is never the session itself |
| `assessment` | `helped` \| `not-applicable` \| `harmful` |
| `assessed_by` | always `user`: the line claims to be the user's assessment; a line written through MCP is an agent's claim that the user said it, which is why `channel` is shown beside it |
| `decision` | optional, the decision the document changed, ≤ 1000 characters, `null` when not given |
| `source` | optional, where that can be seen (commit, PR, review), ≤ 1000 characters, `null` when not given |
| `channel` | `cli` or `mcp` |

A pair can be re-recorded: the latest line for a (session, document) pair is the one every figure reads. A find with no
line has unknown influence, which is neither a success nor a failure. No session document and
no Gate 1 figure reads this file.

## Entity: Search Result Set

The ephemeral output of one `search` call — not persisted, only rendered and logged to
telemetry.

| Attribute | Description |
|---|---|
| results | top 3 matching **active** documents, reliable finds first, then ranked by score (`ENGMEM-SPEC.md` §7) |
| weak candidate | a result that matched fewer than two distinct query words (all of a shorter query; a word of three characters or fewer counts only as an identifier) and no identifier; marked `[weak candidate]` with the words it matched, and when every result shown is one the set opens with `prior context: no reliable match — only weak candidates below` (US-14; contracts/scoring.md, "Weak candidates") |
| per-result fields | id, path, score, why-matched (which query tokens matched which fields), first 2 lines of Cold-start primer, its `related` (id + path only, depth 1), and a `snapshot:` line per record with an anchor or a repository link (US-11; contracts/provenance.md) |
| ambiguous flag | set when a short/numeric query token matches entities across distinct clusters; one representative per cluster is returned instead of a single top result |
| scoreboard footer | always present: `docs: N | drafts: N | last doc: Nd ago`; `N` in `last doc` is clamped to `0` for a future-dated newest document (typo, timezone skew, a `task_date` copied forward), since `Nd ago` cannot express a negative count |
| size constraint | total rendered output ≤ 4 KB (`output.MAX_OUTPUT_BYTES`; `ENGMEM-SPEC.md` §5, `engmem search`) |
| no-match output | exactly `prior context: none found`, exit code 0 |

Every call also produces one `telemetry.jsonl` line (`ENGMEM-SPEC.md` §5, `engmem search` step 6) — this is a side effect of the Search Result Set being
produced, not a separate entity. The line carries two join keys beyond the result itself:
`session_id` (the draft document this search was run for, `null` when unattributed) and
`surfaced` (the ids actually shown, including redirect targets that never scored).
`"weak_only": true`, present only then, says everything shown was a weak candidate; it never changes `result`
(contracts/output.md, "Weak candidates are marked, never dropped").
Together they let the Gate 1 review connect what was retrieved to what a session's Reuse
Log claims to have reused. A tool call over the MCP path writes the identical line.
