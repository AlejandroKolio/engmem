# ENGMEM v0.1 - Implementation Specification

`2026-08-18 · single document for development · supersedes all previous documents for implementation purposes`

This document is self-contained. It is meant to be used with Claude Code: read it in full,
then work through the backlog (§9) story by story. Working rules are in §10.

---

## 1. What this is and why (business context)

**Engmem** is personal engineering memory for working with AI. Every closed engineering
task turns into a structured markdown document (an "Engineering Session": the problem,
decisions with their reasons, rejected alternatives, landmines, lessons) and is filed into
a local git-backed store. On a new task, the agent pulls relevant context back out of the
store and works as if it remembered all of the engineer's past work. Mission: no engineering
decision should ever be lost.

**Author and sole user of v0.1** — a senior engineer (Java/Spring, backend platform),
working with AI agents daily.

**Key hypothesis (NOT proven — v0.1 exists to test it):** prior docs pulled into a new task
measurably change its outcome, and the benefit outweighs the cost of writing them. The test
is an experiment: 3–4 weeks of work, every doc carries a Reuse Log (facts about using prior
docs, with quotes), followed by a review (Gate 1).

**Consequence for the code — critical:** telemetry, the Reuse Log, pre-reg, the Search Trace
field, the scoreboard — these are **the experiment's measuring instrument**, not optional
features. They must not be simplified, dropped, or "optimized away" without the author's
explicit permission.

**The project's success metric** is knowledge reused, not document count and not the tool's
feature count.

The work splits in two, and the halves must not be fused:

1. **Record** the lesson so it stays true — solved by the capture templates.
2. **Bring it back** with a payoff on a new task — the unproven half, and the reason this
   tool exists.

By default the hypothesis is false until the Reuse Log shows otherwise. What counts as
showing it:

| | Reuse loop | Convenient archive |
|---|---|---|
| What happened | A prior document supplied something **not remembered**, and it **changed the artifact** of the new task | Pleasant to have; the task would have gone the same way without it |
| Signal | A concrete artifact is named: a class, a contract, a decision, a landmine | "Gave general context"; recognising something familiar |
| Conclusion | Keep building | Valid as a personal tool, but nothing more is justified |

## 2. Hard constraints

1. Local-first, git-native, LLM-agnostic. Markdown is the source of truth. No databases.
2. No server/daemon/cloud. No network calls from the CLI at all.
3. Python ≥3.11. Dependencies: **pyyaml and markdown-it-py only**. CLI scaffolding is
   argparse (stdlib). Forbidden: click, typer, rich, pydantic, and any other packages.
4. The CLI is thin and dumb; all the intelligence lives in the prompt templates.
5. Nothing "for future growth": the list of cut features is §11; build none of it.

Update (author-approved exception): constraint 3 admits a second runtime dependency,
`markdown-it-py`. `backfill` reads link destinations out of a document body to derive
`related`, and a regex over raw markdown cannot tell a real link from one quoted inside
a code fence, an inline code span, or escaped brackets — it invents edges between
documents in a store whose premise is that edges are set deliberately. The parser was
already in the repository as the dev-only oracle for `tests/test_commonmark_parity.py`,
so this promotes a component the suite already exercises rather than adding an unknown
one. `sections.py`'s splitter stays on regexes, held honest by that same oracle. The
rest of the list is unchanged and still in force.

## 3. Architecture (three layers)

```
┌──────────────────────────────────────────────────────────┐
│ CLI `engmem` (Python): install, search. Does no thinking. │
├──────────────────────────────────────────────────────────┤
│ Prompt templates (all the intelligence, agent-agnostic    │
│ markdown): /engmem (start), /engmem.save, /engmem.save.quick │
├──────────────────────────────────────────────────────────┤
│ Store: a folder of markdown docs + git                    │
└──────────────────────────────────────────────────────────┘
```

**Key decision: NO derived index.** `search` parses the front matter of every doc directly
on each invocation (20–200 files ≈ milliseconds). No INDEX.md, no cache, no SQLite.

**Note on `engmem.cache` (English, added when the store grew past ~800 documents — see
`cache.py`'s module docstring for the implementation):** the on-disk cache under
`~/.cache/engmem/` that speeds up body-section tokenisation in `search` is *not* the
derived index this section rules out, and the distinction is load-bearing, not a
technicality. `INDEX.md`/SQLite/a persisted search index is a **second source of truth**:
it is built once, then read instead of the documents, so it can silently drift from them —
exactly the failure this "NO derived index" decision exists to prevent. The cache
is the opposite shape: it is keyed on the source file's own identity (path, `st_mtime_ns`,
`st_size`), so a changed file produces a different key and a cache hit is only ever
possible when the key still matches the read the entry was built from — the stamp is
taken by that read, not by a fresh `stat()` later, which is what would let an entry built
from an old body be filed under an edited file (see `contracts/cache.md`). There is no
invalidation logic to get wrong and no lag between an edit and the miss it causes — a
mismatch is not a bug to fix, it is simply a miss that falls back to recomputing from the
document itself, the very thing `search` already does on every call. Losing the entire cache
directory changes nothing observable except latency; it is disposable, reconstructible,
and never consulted as an authority independent of the markdown files it was derived from.
That is what makes it a pure function of file bytes rather than an index.

**Store** resolves in this order: the `--store PATH` flag → the `ENGMEM_HOME` env var →
default `~/Developer/engmem`. A blank setting is skipped as if it were unset, and the answer is
always absolute — `install --agent claude-desktop` writes it into a config file another process
reads back from a working directory of its own; see `contracts/runtime.md`. Inside:
`sessions/*.md` + `telemetry.jsonl`. No config files.

## 4. Document schema (front matter + sections)

```yaml
---
id: 1000001-response-cache   # <story-id>-<slug> or <YYYYMMDD>-<slug>; = the filename
title: Response Cache API
date: 2026-08-18                  # when the doc was written (CLI/agent)
task_date: 2026-06-10             # when the work happened (for backfill; otherwise = date)
status: draft                     # draft | active | superseded
superseded_by:                    # id of the doc that superseded this one (empty = not superseded)
backfilled: false                 # true for docs written after the fact
tags: [platform, caching]         # domain, lowercase
entities: [ResponseCacheController, ResponseCache, ETAG, TTL]
                                  # real classes/endpoints/terms/synonyms; there is no aliases field
related:
  - ttl-etag-revalidation-v2       # ids of other docs; the reason goes in a comment
covers_files: [ResponseCacheController.java]
verified_at_commit: abc1234
capture_minutes: 12               # duration of the save ritual (delta from draft creation)
author: A. Engineer               # git config user.name
repos: [platform-core]            # repositories the work touched
branch: feature/response-cache
pr: 42                            # number or URL, blank when none exists
---
```

Manually filled fields — **zero**: id/date/verified_at_commit/capture_minutes/author/branch
are computed automatically; title/tags/entities/related/covers_files/repos/pr are drafted by
the agent from the diff and transcript, and the human replies "y".

**No field is required.** A document with no front matter, or with an incomplete one,
always loads. Missing values are derived: `id` from the filename stem, `title` from the
first `# H1` (a `Knowledge Base —` prefix is stripped), `date` from a `- Date:` /
`- Updated:` line in the first 40 lines of the preamble and otherwise from mtime;
`task_date` defaults to `date`, `status` to `active`, lists to empty, and
`capture_minutes` to `null` ("never measured", as distinct from `0` = "measured as
instantaneous"). A key written with no value states nothing and is treated exactly like a
missing key. Such a document is flagged degraded, and **the number of incomplete spines is
printed on stdout** in the search footer. Completeness is measured over the retrieval
fields only (`id`, `title`, `date`, `task_date`, `status`, `tags`, `entities`); ritual
telemetry (`backfilled`, `verified_at_commit`, `capture_minutes`) is excluded, since a
draft cannot know those values yet.

Spine validation: **error** (document excluded) — duplicate id, invalid YAML, front matter
that is not a mapping, an unclosed front-matter block, an unparseable date, an `id`
containing an embedded newline (breaks the `id == filename stem` invariant and, rendered
into a `### <id> (...)` result header, forges a second, fabricated result block), a value
of the wrong type for its field (e.g. `capture_minutes: [1, 2]`, `tags: 5`), an
`OSError` while reading one file itself (permission denied, a dangling symlink, a
directory in place of the file — none of these reach the YAML parser at all), or an
`OSError` while listing `sessions/` itself (e.g. the directory is unreadable). The latter
is an error, and the scan uses `Path.iterdir` rather than a glob so that the `OSError`
`os.scandir` raises propagates instead of reading as an empty directory — unlike one bad
file, whose blast radius is exactly that one document, an unlistable directory hides an
*unknown* count of documents, which makes the whole run untrustworthy the same way a
duplicate id does, not merely degraded; **warning**
— empty entities, id not equal to the filename stem, incomplete spine, a scalar value in
a list-typed field (`tags`, `entities`, `related`, `covers_files`) — degraded to a
one-element list rather than exploded character-by-character by `list(str)`, or rejected
outright, or a `status` value that is not `draft`/`active`/`superseded` once matched
case-insensitively (`Draft`, `SUPERSEDED` are recognized; `wip` is not) — defaults to
`active`, named on stderr, rather than silently ranking or counting as whichever of the
three states its raw string happens to equal.

Rationale: a missing field is incompleteness, not corruption. Rejecting the document hid it
whole while the diagnostic went to stderr, which the consuming agent never reads — a search
then reports `none found` with the answer sitting in the store. Ritual telemetry
(`capture_minutes`, `backfilled`) is even less admissible as a precondition for loading
knowledge: a document whose capture duration was never measured is still true and still
useful.

**Body sections** (thin core; save.quick writes only 2, 5, 6):
1. `## Pre-reg` — 2–3 lines of intent, written BEFORE opening the store
2. `## Decision Log` — decisions + rejected alternatives with reasons
3. `## Lessons Learned` — pitfalls hit along the way
4. `## Future LLM Context (cold-start primer)` — 5–10 lines for a cold-start agent
5. `## Reuse Log` — see §6
6. `## Search Trace` — `shell | paste | miss`

**Content rule:** the doc records only what isn't in the code, or what's expensive to
re-derive (rejected alternatives, incidents, business constraints, agreements, glossary).
Do not duplicate as-is architecture, flows, or contracts that are already in the code.

## 5. CLI — command contracts

A usage error argparse itself catches — a missing argument, an unknown flag, an unknown or
absent subcommand — exits 2 and is named on **stdout as well as stderr**, like every other
terminal state in this section (§10, principle VIII). argparse writes its own errors to
stderr only, which left a mistyped command invisible to a reader that never reads stderr.
`engmem mcp` is the single exception, for the reason its own subsection below gives: on that
invocation stdout is the protocol channel, and no usage error reaches it — including one the
*root* parser reports before the subcommand is dispatched, which is what a misplaced
`engmem --store PATH mcp` produces. An argv naming `mcp` as its command is recognised as
such by the first token that spells a subcommand **and is not itself the value of a
value-taking option** — so `engmem --store search mcp` is the mcp command with a misplaced
`--store`, not a search, while a search whose *query* is the word `mcp`, and a `--store`
path spelled `mcp`, both keep the ordinary stdout mirror. Which options take a value is
read off the actions each parser owns — argument groups included, since a group shares its
parser's action list — so a new one cannot silently reopen the gap.

### `engmem install [--agent claude|copilot|claude-desktop] [--local] [--store PATH]`

Idempotent (re-running = upgrade, nothing breaks):
1. Creates the store (`sessions/`, `git init` if missing).
2. Copies prompt templates into the agent's config: claude → `~/.claude/commands/` (or
   `.claude/commands/` with `--local`); copilot → `.github/prompts/`.
3. Stamps the first line of every installed file: `<!-- engmem-template: <name> v<package version> -->`.
4. Appends the trigger rule to `CLAUDE.md` / `.github/copilot-instructions.md` if not
   already present: "Before proposing a plan, run `engmem search "<key terms for the task>" --session <draft-id>` (the draft `/engmem` just created; without it the search is unattributed)". Presence is detected by the substring `` `engmem search ``, so a
   rule the user reworded is recognised and never duplicated; a skip for this reason
   is reported on stdout, not left silent. A line that is exactly an earlier wording engmem
   itself wrote (with or without the sentinel below) is not skipped but updated in place to
   the current wording and reported as updated — otherwise the marker check would freeze
   the first wording ever installed. *(2026-09-18: `--session` joined the rule; every CLI
   search on the record until then was unattributed.)* The appended line is written after an
   `<!-- engmem-trigger-rule -->` sentinel comment so `uninstall` removes precisely
   the line engmem wrote, not any line that merely contains the same substring — an
   earlier version deleted a user's own unrelated sentence that happened to mention
   `engmem search`; installs from before the sentinel existed are still recognised
   for removal by their exact bare rule text. The rule is appended with the line
   ending the file already uses, and the file it goes into is replaced atomically —
   see `docs/design/contracts/install.md` for why that file, and not the templates.
   Any failure during install (an occupied destination, an undecodable instructions
   file, an unwritable directory, `git` missing, the repo-root guard) exits 2 with a
   named cause on stdout and stderr, never a traceback.
5. `--agent claude-desktop` writes no templates and no trigger rule (Claude Desktop has
   neither a commands directory nor a global instructions file to write them to).
   Instead it writes one entry into Claude Desktop's own
   `claude_desktop_config.json`, merging under the `mcpServers` key without disturbing
   any other server or key already there.

### `engmem mcp [--store PATH]`

Author-approved addition beyond §11, which parks "MCP server" until the author asks for
it explicitly — this is that ask (milestone M3 of the MCP server feature). Runs an MCP
stdio server exposing search as a tool call, so an MCP-capable client (e.g. Claude
Desktop, wired up by `engmem install --agent claude-desktop`) can call it directly
instead of a human pasting `engmem search` output across the paste-bridge.

1. Resolves the store the same way `search` does (`--store`, then `ENGMEM_HOME`, then
   the default) and runs the protocol loop until stdin closes; the exit code is
   whatever the loop returns.
2. On this path, **stdout carries the MCP JSON-RPC protocol and nothing else** — no
   banner, no confirmation, no error line. A single stray line corrupts a frame and
   kills the client session. This is the opposite convention from every other command
   in this section, where a terminal failure must leave a line on stdout because the
   consumer reads only stdout; here the CLI wiring itself must stay silent on stdout no
   matter what, and any diagnostics belong on stderr only. `engmem mcp --help` is the
   one thing that still prints there: argparse's help action, asked for explicitly by a
   human at a terminal and never by a client, which prints the help text and exits 0
   without ever starting the protocol loop.
3. The stdio loop's own protocol behavior (tool schema, request/response shapes,
   error handling within the protocol) is out of scope for this section — it lives with
   `engmem.mcp_server`. Two tools are exposed: `engmem_search` (word search, unchanged)
   and `engmem_search_by_role` (role-addressed retrieval — English addition, see the
   `--role` subsection under `engmem search` below).
4. A tool call writes the same `telemetry.jsonl` line as `search`, with the same
   `session_id` semantics (the tool takes an optional `session_id` argument alongside
   `query`). This path is inside the experiment, not beside it: a search that leaves no
   row makes its session invisible to the Gate 1 analysis, and "the MCP path is quieter"
   would read there as "less work happened" (§1 — the instruments may not be simplified).
   The write-failure note is the one thing that cannot mirror `search`: stdout here
   carries protocol frames only, so the note travels inside the tool result text — the
   same reader, that reader's own channel — and never through `print()`.

### `engmem search "<query>" [--session ID] [--store PATH]`

1. Walks `sessions/*.md` (flat — subdirectories are not scanned) and parses front matter.
   Corrupt files, wrongly-typed field values, and files that cannot even be read (a
   dangling symlink, a directory shadowing the `.md` name, permission denied) → error on
   stderr, the run continues with the rest of the store. Incomplete spines load degraded
   and are counted on stdout (§4). Markdown that engmem will never read — in the store
   root, or nested under `sessions/` — is reported by name on stderr, and on stdout as
   well when the search returns nothing. A missing `sessions/` directory is a terminal
   failure (exit 2), named on both stdout and stderr — every terminal state of this
   command puts at least one line on stdout, since the consumer reads only stdout. An
   *existing* `sessions/` that cannot be listed (permission denied, e.g.) is a different
   case, not a terminal failure and not a phantom-empty store: it is counted as a failed
   load in the stdout scoreboard *and* named on its own stdout line spelling out that the
   real document count is unknown, not zero — the terse scoreboard count alone reads like
   one ordinary bad file. The same distinction applies to the stray-file scan below: a
   subdirectory under `sessions/` (or the store root) that cannot be listed is reported by
   name on both stdout and stderr, never silently counted as "no strays".
2. `status: draft` documents don't appear in results (but do count toward the scoreboard).
   `status: superseded` documents don't appear either; if a superseded doc would have won,
   its line is replaced with `superseded by <id>` plus the successor itself, if it exists.
3. Scoring — §7. Output is the top 3 docs: id, path, score, why-matched (which tokens
   matched in which fields), the first 2 lines of the Cold-start primer, their related docs
   (id + path only), plus the locator and first line of each matched section. Total size
   ≤4 KB (`output.MAX_OUTPUT_BYTES`; the cap was doubled when section locators joined
   the output),
   format is paste-able markdown (for the paste bridge).
3a. If more documents matched than the three shown, a final line states how many were
   withheld.
4. No matches → prints `prior context: none found` (exit code 0).
5. Scoreboard footer, always printed: `docs: N | drafts: N | last doc: Nd ago`. The count
   carries a parenthetical for anything the store is not fully serving —
   `docs: N (M partial spine, K failed to load)` — each note present only when non-zero.
   `N` in `last doc: Nd ago` is clamped to `0` for a future-dated newest document (a typo,
   timezone skew, or a `task_date` copied forward) — the `Nd ago` shape cannot express a
   negative count.
6. Writes a line to `telemetry.jsonl`:
   `{"ts": ISO, "query": raw, "session_id": ID|null, "n_docs": N, "hits": [{"id","score"}...],
   "surfaced": [id...], "result": "hit|miss|ambiguous"}`.
   `session_id` is the draft document id passed as `--session`, written as an explicit
   `null` when the flag was absent — at Gate 1 an unattributed row is a different thing
   from a row written before the field existed, and both differ from "no search happened".
   Blank or whitespace-only is treated as absent; nothing else is validated (the CLI does
   not know which ids exist, and a search refused because a draft was named a moment too
   early would be the ritual gating the answer, §6).
   `surfaced` lists the document ids the reader was actually shown, in render order: the
   top-3 hits plus each superseded redirect and its successor. It is deliberately wider
   than `hits` — a redirect target reaches the agent without ever scoring, so omitting it
   would make an honest Reuse Log row look fabricated at review time. `related` expansion
   is not listed: the search never printed it, and it is reconstructible from front matter.
   A write failure here (store made read-only, `telemetry.jsonl` shadowed by a
   directory) does not crash the command — the search result already printed above is
   correct and complete, and the command still exits 0 — but is reported with its own
   `note: telemetry not recorded (<reason>)` line on stdout, since a silently-stopped
   measurement instrument is a data-integrity problem worth surfacing.
   A search run without `--session` (absent, blank or whitespace-only) ends with one more
   trailing line, `note: unattributed search — pass --session <draft-id>`, after the
   scoreboard and after any telemetry note; the MCP tools append the same line with
   `session_id` in place of the flag. Both are outside the rendered result, so
   `context_bytes` (6a) is unchanged by them. *(Added 2026-09-18: two measurements a week
   apart found 0 of 33 and 2 of 44 searches attributed; the template's instruction alone did
   not hold.)*

6a. Two more fields ride the same line, both English additions not covered above:
    `channel` (`"cli"` or `"mcp"` — which surface ran the search, set by the caller,
    never inferred; distinct from a document's own Search Trace vocabulary, which
    describes how a result reached the agent, not which process ran it) and
    `context_bytes` (exact UTF-8 byte count of the rendered search result for this
    call only — not the scoreboard footer, stray-file note, or telemetry-failure
    note — computed by the same render call on both the CLI and MCP paths so the
    figure means the same thing on either). `context_tokens_estimate` is
    `ceil(context_bytes / 3.5)`: an estimate, not a measurement, since exact
    tokenisation needs the calling model's own tokeniser (a network call, forbidden
    by §2.2). 3.5 bytes/token — denser than the ~4 chars/token usual for prose — is
    calibrated for this store's rendered output (YAML-shaped headers, hyphenated
    ids, table pipes, camelCase identifiers), as the byte-weighted midpoint between
    locator/header lines (~3 bytes/token) and prose-like primer excerpts (~4). Read
    it as a floor: prose-heavy text lands within ~10% of its real cost, while
    identifier/table-dense text commonly runs 15-30% above.

### `engmem search "<query>" --role ROLE` / `engmem roles [--store PATH]` (English
addition — role-addressed retrieval)

Author-approved addition: some questions are about a document's content ("LeaseGuard
sweeper job" — word matching works) and some are about its structure ("what did we
reject and why" — word matching fails, because Decision Log sections across documents
discuss different subjects and share no vocabulary with each other, only their role in
the document). `--role` lets a caller ask for a specific section role — one of the 19
canonical roles `engmem.sections.CANONICAL_ROLES` defines — instead of hoping the right
words are in the query.

- Ranking stays word-based even with `--role` set: `--role decisions` on `"caching"`
  ranks documents by the word "caching" exactly as an ordinary search would, then
  returns the Decision Log from each of the top-ranked documents that has one. A
  document that ranked but lacks the role is skipped outright, never padded with a
  different, mislabeled section.
- Output is always visibly role-filtered (a `role: <ROLE>` line, `[role: <ROLE>]` on
  every per-document header) — a reader must never mistake it for an ordinary result.
  "No document has that role" is stated explicitly on stdout, distinct in wording from
  an ordinary miss (no document matched the query's words at all).
- An unrecognized `--role` value is a terminal failure (exit 2), the invalid value and
  the full list of valid roles named on both stdout and stderr.
- `engmem roles [--store PATH]` lists the full role vocabulary and how many documents in
  the resolved store currently carry each one (0 stated explicitly for a role nobody has
  written yet) — the discoverability mechanism for `--role`'s vocabulary.
- The MCP `engmem_search_by_role` tool (see `engmem mcp` above) exposes the same
  behavior, with the role vocabulary as its `role` parameter's JSON Schema `enum` so a
  calling model picks from the real vocabulary rather than guessing a spelling.

### `engmem telemetry [--store PATH]`

The reading surface for the `telemetry.jsonl` rows §5's `search` subsection writes:
totals, hit rate and context spent, split by channel (`cli`/`mcp`) and overall, plus the
navigation misses recorded in the store's own documents.

- **A missing log is zero rows; a log that cannot be read is not.** No `telemetry.jsonl`
  means no search has run yet, which is honestly reported as a zero-row summary. A log
  that exists but cannot be *read* (permission denied) or *decoded* (a non-UTF-8 byte —
  `UnicodeDecodeError`, which is a `ValueError` and not an `OSError`, so a handler
  catching only `OSError` lets it out as a traceback) is a terminal failure: exit 2, the
  path and the cause named on both stdout and stderr, and never the phrase `0 row(s)`.
  Reporting an unreadable log as an empty one would tell an author their store recorded
  nothing while it recorded everything — the same phantom-empty failure the scoreboard's
  `scan_error` line exists to prevent, and the reason this command reads the store
  through the same loader as every other subcommand rather than reaching into it.
- A single bad *line* is not fatal: it is counted in the summary's `unreadable` tally and
  the remaining rows are still totalled. One truncated append — the usual way a line goes
  bad — must not cost the reader the entire measurement history. "Bad" is every line the
  reader cannot total, not only unparseable JSON: valid JSON that is not an object
  (`123`, `null`), a `context_bytes`/`context_tokens_estimate` that is not a number, a
  `channel` that is not a string, and the shapes that defeat the decoder itself before any
  field is inspected — a number with more digits than CPython will convert, nesting deep
  enough to exhaust the stack. Each of those is one row's defect; charging the whole
  file for it (or letting it out as a traceback) would be the same phantom-empty lie as
  the bullet above, only louder. The distinction the two bullets draw is byte versus row:
  a bad byte invalidates every offset in the file, a bad row does not.

## 6. Prompt templates — behavior specification

### `/engmem <task description>` (start)

1. **Pre-reg:** capture 2–3 lines of "how this would be solved without prior context" —
   BEFORE any search, and without asking the author. Preferably via a separate sub-agent
   that is given only the task statement, with no access to the store and no knowledge of
   engmem; if no sub-agent is available, write it yourself, before opening any doc. The
   source is recorded in a `pre-reg source:` line. Reason: a step that costs the author
   effort and gives them nothing back is the first thing to get dropped (D2).
2. **Draft:** immediately create `sessions/<id>.md` with front matter `status: draft`, a
   Pre-reg section, and a creation timestamp (capture_minutes is measured from it).
3. **Search:** if you can run commands, call `engmem search "<key terms>"`. If you can't
   (no shell), hand the user the ready-to-run command and ask them to paste the output
   back into the chat (the paste bridge).
4. Load ≤3 found docs + their related docs (depth 1, no further).
5. Record the Search Trace (shell | paste | miss) — it goes into the doc at save time.
6. Explicitly list what was pulled in (or "prior context: none found"), and answer the
   actual question IN THE SAME message. The ritual doesn't delay or replace the answer;
   there must be no message that consists only of a ritual report.

### `/engmem.save` (finish)

1. Generate a FULL front matter draft from the diff and transcript (all fields in §4).
   Show the YAML, wait for "y" (or edits). More than one interactive round is a design
   failure.
2. Fill in the thin-core sections. Decision Log must contain rejected alternatives.
3. **Reuse Log** — a table, one row per prior doc that actually influenced the work:
   `| prior-doc | what was taken (artifact + QUOTE from the doc) | how it influenced the
   work | reuse|anti-reuse|harmful |`
   Rules: a row exists only if the doc was opened and had an effect; the artifact must be
   concrete (class/contract/decision/landmine); a row without a quote is invalid; do NOT
   estimate time saved — that's the author's judgment at review time; nothing used →
   exactly the line `Prior docs used: none.`
4. Ask: "does this work supersede a decision from some prior doc?" → if yes, set that
   doc's `status: superseded` and `superseded_by`.
5. Flip `status: draft → active`, compute `capture_minutes` (now − draft creation), fill
   in `verified_at_commit` (current HEAD).

### `/engmem.save.quick` (save without review)

Zero questions: 3 bullets (decisions/pitfalls) from the diff → Decision Log and Lessons Learned
sections, Reuse Log with an "ok" confirmation, front matter filled automatically, status →
active. That's it. This degrades the doc's completeness, not the experiment's ritual.

## 7. Scoring (deterministic, no ML)

**Normalization:** NFKC → casefold → tokenize on non-alphanumerics. CamelCase tokens are
additionally split into fragments + an abbreviation, keeping the original:
`ResponseCacheController` → `responsecachecontroller, response, cache, controller,
rcc`. The same applies to both the query and the doc's fields.

**Match rules:**
- A match = equality of normalized tokens. No substrings, no prefixes.
- Query tokens ≤3 characters long, and purely numeric ones (story IDs), only exact-match
  against the ORIGINAL (unsplit) field tokens.

**Score (additive, not tiered):**
`score(doc) = Σ over query tokens [ max field weight where the token matched ] × (matched
tokens / total query tokens)`
Field weights: id = 5, entities = 3, title = 2, tags = 1. Ties broken by recency (date),
newer first.

**Homonyms:** if a short token (≤3) matches entities across docs whose full forms differ
(MQ → MessageQueue vs MetricsQuery), output one top doc from each cluster, marked
`ambiguous`, with result=ambiguous in telemetry.

**Ground truth is the golden tests (§8): if the formula and a test conflict, the test
wins.**

## 8. Golden fixtures (write BEFORE the scorer — S2 starts with these)

Fixture docs in `tests/fixtures/sessions/` (minimal front matter + a couple of sections):

| File | Key contents |
|---|---|
| `1000001-response-cache.md` | entities: ResponseCacheController, ResponseCache, ETAG, TTL; tags: platform, caching |
| `ttl-etag-revalidation-v2.md` | entities: ETAG, TTL, revalidation, CacheRevalidationController; tags: platform; related: 1000001-response-cache |
| `mq-message-sweeper.md` | entities: MessageQueue, MQ, SweeperJob; tags: platform, sweeper |
| `metrics-query-refactor.md` | entities: MetricsQuery, MQ, QueryService; tags: platform |
| `maze-render-old.md` | status: superseded, superseded_by: maze-render-new; entities: MazeRenderer |
| `maze-render-new.md` | entities: MazeRenderer, Spring Data; tags: tech-debt |
| `wip-something.md` | status: draft |
| `broken.md` | deliberately broken YAML |

Assertions (id → expected outcome):

| # | Query | Expected outcome |
|---|---|---|
| G1 | `1000001` | response-cache first (exact id) — holds at fixture size; see `contracts/scoring.md`, "Spine and body scores are not on one scale" |
| G2 | `482` | response-cache NOT found (numbers are exact-match only) |
| G3 | `ResponseCacheController` | response-cache first |
| G4 | `response cache` | response-cache first (CamelCase fragments) |
| G5 | `MQ` | exactly two results, one from each of the MessageQueue and MetricsQuery clusters, marked ambiguous |
| G6 | `mq` | identical to G5 (casefold) |
| G7 | `MessageQueue sweeper` | mq-message-sweeper first (2-token coverage) |
| G8 | `landmines with MessageQueue sweeper` | mq-message-sweeper first; non-Latin noise doesn't break the run and doesn't match anything |
| G9 | `ETAG` | response-cache and revalidation-v2 rank above the rest |
| G10 | `ET` | empty, `prior context: none found` (≤3 chars — exact only, ET ≠ ETAG) |
| G11 | `etag revalidation` | revalidation-v2 first (2/2 coverage vs 1/2) |
| G12 | `MazeRenderer` | maze-render-new in the output; old appears only as `superseded by maze-render-new` |
| G13 | `wip` / terms from the draft doc | the draft is absent from the output; drafts: 1 in the scoreboard |
| G14 | any query with broken.md present | warning on stderr, results for the remaining docs are still returned |
| G15 | `nonexistent-term-xyz` | `none found`, exit 0, telemetry result=miss |
| G16 | `platform` | total output ≤4 KB (top 3, not everything) |

## 9. Deliberately cut — DO NOT BUILD (parked for v0.2+)

INDEX.md and any derived index · SQLite · vectors/embeddings · doctor/log/upgrade/index
commands (`log` = grep, upgrade lives inside install) · FRESH/STALE checks via git diff
(we write the anchor fields, not the check) · [Decisions]/[Landmines] annotation blocks ·
a symptom layer · an aliases field · transliteration and stemming · config files ·
pre-commit hooks · brew · extraction of de-grounded patterns · any cloud interaction.

**Built after all, and why** — two entries left this list rather than being quietly
ignored, which is the failure this section exists to prevent:

- **BM25 over section bodies.** Spine-field matching alone could not answer a question
  whose words appear only in prose. It is scored in-process from the same token counts
  the search already builds — no derived index file, so the §3 rule still holds.
- **MCP server.** Claude Desktop cannot run shell commands, so without it the tool was
  unreachable from the client the author actually uses. Implemented on the standard
  library alone; the `mcp` SDK stays rejected under §2.3.

**Two new modules in `src/`, and why each earns the exception this section otherwise
refuses.** Neither `src/engmem/gate1.py` (added 2026-08-27) nor `src/engmem/gate1_audit.py`
(added 2026-08-27) was ever on the cut list above — both are new surface, which this section is
deliberately hostile to. Both are admitted for the same reason, applied to two different
questions: each is the analysis layer for a pre-registered §11 concern, not a product feature,
and each removes a duplication this section exists to prevent.

- `gate1.py` is the shared per-row verdict (citation integrity, classification, distance,
  staleness, dogfooding) `tools/verify_citations.py` and `tools/gate1_report.py` both need;
  their exit-code contracts genuinely differ so they cannot merge into one command, and
  duplicating that verdict logic across two files is exactly the drift this section exists to
  prevent (`QUOTE_RE` and the row parser were byte-identical in both before this module
  existed).
- `gate1_audit.py` is the sample-completeness question, not the row-validity one: did a session
  start the ritual, search, and honestly record the outcome, joining `telemetry.jsonl`'s
  `session_id` against the store for the first time. It is a separate module from `gate1.py`
  rather than more functions inside it because the two answer different questions over an
  overlapping input, not because the exception needed spreading thinner.

Full reasoning for both: `docs/design/contracts/gate1.md`.

**Two large modules stay whole, and that is a decision, not a backlog item.**
`mcp_server.py` and `scoring.py` are the biggest files here, and both were examined for a
seam. `scoring.py`'s exists but cutting it would break the thing that makes it fast:
`search`, `search_with_role_sections` and `role_coverage` deliberately share one built
section index, and splitting them would either duplicate that sharing or widen the API
between modules for no reader benefit. `mcp_server.py`'s write tools and path containment
are a genuinely separate concern, but `serve` must stay importable from
`engmem.mcp_server` and the tests reach module internals by name, so a split costs a
re-export layer that defeats its own purpose. Revisit only when a second protocol surface
arrives — not on size alone.

Transliteration and stemming stay cut, and were re-tested rather than assumed: a
measured comparison against `python-slugify` showed stdlib NFKD reproduces its output
on every accented-Latin heading, so the dependency bought nothing these documents need.

These are decisions from three rounds of design review. If something on this list seems
necessary, stop work and ask the author — do not build it.

Update (English, author-approved exception): "MCP server" above is no longer parked.
The author explicitly asked for it — `engmem mcp` and `install/uninstall --agent
claude-desktop`, milestone M3 — see §5. The rest of this list is unchanged and still
in force.

**Amendment, recorded 2026-09-19: the measurement layer is frozen until the tenth counted
story.** The apparatus has outgrown what it measures — about 5 900 lines of Python under
`src/`, 18 000 of tests and 6 200 of contracts, spec, templates and the anatomy page, against a
live store of 10 documents, 27 telemetry rows and no distant reuse event since the first
document on 2026-07-29. §11 already reads this state: "Fewer than 10 counted stories, no
distant event" is undecided and expires at story 10, past which "The measurement superstructure
measures zero. That is a result". Until story 10 no new measurement surface enters the
repository: no new tool under `tools/`, no new audit figure or telemetry field, and no contract
section other than the "why" of a fix. Parked by name: excluding `dsa`-tagged documents from
the endpoint population in code, and a distance census or any change to `gate1._distance`.
Still allowed: fixes to defects that stop a story being collected or counted (the two just
landed: `engmem_complete_draft` refusing a Reuse Log row with no quote or an unreadable
classification, and the `--session` trigger rule with its `note: unattributed search` line) and
the clean-up of existing text, tests and unused surface. A story, so that the freeze has an
end, is §11's cross-repository work story: a session document, status `active` and not
`backfilled`, about work in a repository; one tagged `dsa`, `dsa-mentor` or `algorithms-course`
is practice, not counted. The count is read by hand from the "completed (status: active)"
figure on the "ritual:" line of `tools/gate1_report.py`'s audit block, minus such documents and
minus any whose `repos` names `engmem` (§11, dogfooding); no code encodes this, which is the
point. The freeze lifts at count 10, when §11's table is read, or earlier if a §11 row is met.

## 10. Principles

### Product stance

1. AI assists — the engineer is in control.
2. Local-first, git-native, LLM-agnostic.
3. Structure matters more than raw content.
4. **The metric is reused knowledge, not document count.**
5. The agent writes facts; a human judges value.
6. A stable "dumb" index beats a smart, moving one: edges are set by a human at the moment
   of maximum context.
7. Gates are mandatory: scaling, tooling, and external validation only happen after reuse
   is proven.
8. Nothing is built "for future growth" — every layer appears in response to a documented
   pain point.

### Engineering discipline

Referenced by number from the source as `principle VIII` and so on. Keep the numbering
stable if this section is edited.

**I. Test-first, always.** Red → Green → Refactor. The test that specifies the new
behaviour is written and observed to fail before the implementation exists. A test is never
weakened, skipped, or rewritten to make failing code pass. When a test and the code
disagree, work stops until the contract is resolved.

**II. Definition of done.** Tests written first, suite green, no debug leftovers, no
commented-out code, no TODO markers left behind.

**III. Small, verifiable increments.** One behaviour, one commit, suite green. Commit
messages state intent, not a restatement of the diff.

**IV. Verification before completion claims.** "Done", "fixed", and "passing" are backed by
having run the command and read its output, in the current state of the code.

**V. Surgical changes only.** Touch the minimum required. No drive-by refactoring, no
speculative abstractions, config knobs, or extension points nothing needs yet.

**VI. Plan before code.** Approach, files, and the tests to be written, agreed before
implementation. Ambiguity is resolved, not assumed away.

**VII. Simplicity and dependency discipline.** Standard library first. A new runtime
dependency needs an explicit reason the standard library cannot serve, and the author's
approval. The runtime requires `pyyaml` and `markdown-it-py` and nothing else; CI
enforces that.

**VIII. Errors are loud.** No silent `except: pass`, no fallback values papering over broken
state. A failure names its cause. This one governs the design: the consumer is an agent that
reads stdout and never reads stderr, so a diagnostic written only to stderr is a swallowed
error. Every terminal state leaves a line on stdout.

**IX. Stop when blocked or uncertain.** Ambiguity gets a question, not an invented
assumption. Asking costs one round-trip; a wrong guess compounds through everything built on
it.

## 11. Gate 1 — pre-registered endpoints and interpretation

**Recorded 2026-08-25.** The intent of writing this down now, rather than after results
are in, is that no endpoint is chosen to fit data already seen. If an endpoint below turns
out to be the wrong question, it is replaced by editing this section *and saying so* — never
by reading the result differently.

### Primary endpoint

**At least one distant reuse event across 4–5 cross-repository work stories.**

A reuse event counts when a Reuse Log row cites a prior document, quotes it verbatim, is
classified `reuse` (not `anti-reuse`/`harmful`, and not a blank or misspelled cell — a
classification the count cannot read defaults to excluded, never to `reuse`), and that quote
is traceable to a decision in the new work. `src/engmem/gate1.py` decides the mechanical
half — citation integrity, classification, distance, and whether the citing document is
itself a draft or superseded — shared by `tools/verify_citations.py` (which fails the run on
an unverifiable quote) and `tools/gate1_report.py` (which never fails; it renders every row
and hands the "changed a decision" half to the human). See
`docs/design/contracts/gate1.md`.

*Distant* is the part that matters, so it is defined mechanically rather than felt:
a cited document is **adjacent** if it shares a repository or a tag with the citing
document, or is reachable from it through one `related` edge. Otherwise it is **distant**.
`tools/gate1_report.py` computes this; the human may overrule it per row, in writing.

The reasoning: an adjacent document is one the author would plausibly have remembered or
found anyway — it demonstrates the store works as an archive. A distant one is knowledge
that had genuinely fallen out of reach, which is the only thing the retrieval layer can
claim credit for.

A citation of a `superseded` document is **not** excluded by that fact alone: it is flagged
(`gate1.py`'s staleness axis) and still counts if the row is otherwise `verified`/`reuse`/
`distant` — per `templates/engmem.save.md`, a stale citation becomes `harmful` only when the
human says so at review time, and this endpoint does not pre-empt that judgment. Front matter
this module cannot parse scores **undecidable**, never `distant` — a parse failure must not
work in the endpoint's favour.

### Secondary endpoints, declared now rather than after a miss

1. **Token cost**: `engmem search` versus an agent grepping the store for the same query,
   over 15–20 real queries taken from `telemetry.jsonl`. `tools/baseline_cost.py` automates
   only one side of this — the estimated token cost of engmem's own search output per query
   (`ceil(bytes / 3.5)`, the same estimator `engmem telemetry` uses). The comparison side (a
   fresh agent session per query, file-reading only, counted the same way) is a second,
   deliberately separate measurement the tool does not run.
2. **Navigation hit-rate**: recorded `navigation_miss` entries against total searches. A
   navigation miss is not a search miss — see §4's field table.

### Interpretation, fixed in advance

| Outcome | Reading |
|---|---|
| Primary met | The retrieval layer earns its place. Continue. |
| Primary missed, secondaries met | The measurement layer is unproven; the store as compressed context is proven. Keep the store, stop investing in the measurement superstructure. |
| Primary missed by story 10–12, no distant event at all | The measurement superstructure measures zero. That is a result, not a failure to be retried with softer criteria. |
| Fewer than 10 counted stories, no distant event | **Undecided, and not a miss.** The sample is too small to separate a rare event from an absent one, so no reading is licensed in either direction — including the flattering one that the tool "just needs more time". This row expires at story 10: past that count the row above governs and "not enough data yet" stops being available. |

**Amendment, recorded 2026-09-06, before the numbers for this period were collected.** The
table above had no row for the state the experiment occupies most of the time: too few
completed stories to say anything at all. Without it, a zero at four stories has no
pre-registered reading, and one would have been chosen *after* seeing that zero — the exact
substitution this section exists to prevent. Recorded here rather than read into an existing
row, per this section's own rule. The new row is bounded on purpose so that it cannot become
the escape hatch the third row forbids: it expires at story 10, and it softens none of the
three rows above it.

**Left open, and named rather than resolved:** this section counts "stories" without saying
whether a `backfilled` document is one. It is not, in any reading that matters — a backfilled
document ran no ritual, has no pre-registration, and its Reuse Log was filled from memory
(§4: "docs written after the fact") — but that exclusion is *not* currently written into the
primary endpoint's population, and writing it in now, with the store's composition already
known, would be choosing a rule against visible data. It is flagged here so the gap is on the
record, to be closed by the author deliberately rather than discovered at review time.

**Amendment, recorded 2026-09-12.** A document created before the `## Pre-reg` section existed —
and any document without that section — is not a ritual document for the audit coverage figures
in `tools/gate1_report.py`. Seven such documents sat in the live store on 2026-09-11: written by
an earlier engmem whose save template had no Pre-reg and no Search Trace section, carrying no
`backfilled: true` because that flag postdates them, and counted as ritual starts for want of
anything saying otherwise. The author stamped all seven `backfilled: true` that day, and the
audit now also reads the missing section directly, so the same gap does not depend on somebody
remembering to stamp the next one. The excluded documents are counted and named in the block on
their own line, and a document that is both backfilled and Pre-reg-less is counted once, as
backfilled. See `docs/design/contracts/gate1.md`, "(e) A document with no Pre-reg section never
ran the ritual either."

This changes the audit's population and nothing else. The primary endpoint's population is
untouched: a Reuse Log row from a document the audit now excludes still counts exactly as it did
before. "Left open, and named rather than resolved," above, holds that gap open on purpose, for
the reason given there, and this amendment does not close it from the side — recorded here rather
than read into the endpoint quietly, per this section's own rule.

### What does not count as evidence

- **Dogfooding.** Reuse requires forgetting, and while building engmem the author forgets
  nothing about engmem. Stories about this repository are excluded from the count.
  Mechanism: a citing document whose `repos` front matter names `engmem`
  (case-insensitively) — see `docs/design/contracts/gate1.md`, "Dogfooding identification,"
  for the alternatives considered and why `repos` was chosen over `covers_files` or a
  deny-list.
- **A story still in draft, or superseded.** Search itself excludes `draft` and `superseded`
  documents from its results (§5, search step 2); the count now excludes a Reuse Log row whose *citing*
  document carries either status, so the counted population matches what the retrieval layer
  actually serves.
- **Divergence between the pre-registered plan and the final one.** It is a secondary
  signal at best: a plan written blind diverges for many reasons, reading the repository
  among them. The verdict is carried by quoted Reuse Log rows.
- **A row without a verbatim quote.** `src/engmem/gate1.py` rejects it, and so does the
  count — both `tools/verify_citations.py`'s exit code and `tools/gate1_report.py`'s table
  are computed from that one verdict, not two independent readings of the same rows.
- **A row classified `anti-reuse`.** The prior document was opened, quoted, and the work
  deliberately went the other way because of it — a genuine influence event, not an
  oversight. It is excluded from the primary count regardless: the endpoint is
  conservative by design, crediting the store only when it supplied the answer the work
  used, never when it supplied a foil the work correctly rejected. *(Added 2026-09-03;
  not a change to the endpoint — the exclusion entered the primary endpoint paragraph
  above on 2026-08-28, commit `9663592`, and was not declared at the time. It changes no
  verdict now: the three populated Reuse Log rows in the live store are all classified
  `reuse`. `anti-reuse` had no written definition anywhere in the repository until this
  date — see `templates/engmem.save.md`'s Reuse Log rules and `data-model.md`'s Reuse
  Log Entry table. The count itself is unchanged; `gate1.py` already excluded
  `anti-reuse` under its own name, this bullet only records the exclusion and its
  rationale.)*
