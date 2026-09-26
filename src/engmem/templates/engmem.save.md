---
description: Finish an engmem task — save what was learned as a complete session document
---

# /engmem.save — finish a task

You are closing out the session started by `/engmem`. Find the `sessions/<id>.md` draft
created at the start of this task (`status: draft`) — if you wrote it yourself via a
shell, read it back from disk; if you created it through the `engmem_create_draft` MCP
tool, its content is exactly what you passed as that call's `content`, so reuse that
directly instead of trying to re-read the file — and complete it in the two passes
below.

This template produces the full knowledge-base shape: seventeen numbered sections plus
engmem's own two operational ones, the list `ENGMEM-SPEC.md` §4 fixes. It is deliberately the
long path: use it when the session is worth the ritual. For a low-effort close-out, use
`/engmem.save.quick` instead.

## 1. Front-matter draft (one interactive round, not field-by-field)

Generate a **complete** draft of every front-matter field from the code diff and this
conversation:

`id`, `title`, `date`, `task_date`, `status`, `superseded_by`, `backfilled`, `tags`,
`entities`, `related`, `covers_files`, `verified_at_commit`, `capture_minutes`, `author`,
`repos`, `branch`, `pr`

`related` and the last four are worth a word each — derive them, never guess them:

- `related` — document ids, never filenames: a `.md` suffix will not resolve. It breaks
  both `gate1`'s adjacency check (a citation that should read `adjacent` reads `distant`
  instead) and a search hit's rendered related line (a document that IS in the store
  renders as "not in store").
- `author` — who did the work. Prefer `git config user.name` (and `user.email` if you
  want both); leave blank if neither is available rather than inventing a name.
- `repos` — the repository or repositories this work actually touched, as a list (e.g.
  `[platform-core]`, or `[platform-core, widget-cache-client]` for a cross-repo change).
  Usually just the current repo's name; widen it only where `covers_files` genuinely spans
  more than one checkout.
- `branch` — the branch this work happened on. Run `git rev-parse --abbrev-ref HEAD` if
  you have a shell; leave it blank on detached HEAD or if you have no shell, never
  fabricate a name.
- `pr` — the pull request for this work, if one exists (a number, e.g. `#482`, or a URL).
  Leave it blank if no PR has been opened yet; do not invent a number to fill the field.

Show the full YAML block to the user and ask for `y` or edits. Apply their edits and stop
asking — more than one interactive round for this draft is a design failure. Do not ask
about individual fields one at a time.

## 2. Body sections — the numbered seventeen, plus engmem's own two

Write every section below, in this order, using the numbered headings exactly as shown
(the number is part of the heading text — `## 8. Decision Log`, not `## Decision Log`).
None of the 17 is optional here: this is the point of the full template. A section that
genuinely has nothing to report for this document (see the hub/census note under Decision
Log and Lessons Learned) still keeps its heading — say so in one explicit line, never
delete the heading and never pad it with invented content to look complete.

**Gather ground truth before writing.** Read the actual diff, the actual git history, the
actual ticket, and ask the user directly where something is unclear — do not infer what
"probably" happened. If a source was unreachable (a ticket system you have no access to,
a teammate you can't ask), say so plainly in the relevant section rather than inventing
its contents.

**Tag every factual claim.** `[Verified]` — you read it directly in the code, the git
history, the ticket, or the user confirmed it. `[Assumed]` — a reasonable inference; say
what it rests on. `[Open]` — a real gap; state it, don't paper over it. Never upgrade an
`[Assumed]` to `[Verified]` because it would read better. If a section would end up mostly
`[Assumed]`, that is a signal to go gather more ground truth, not to write prettier
guesses.

**Explain WHY, not only HOW.** For every non-obvious choice, name the problem it solves
and what the alternative would have cost — a reader who only sees the "what" cannot judge
whether the same choice still applies to their situation.

**Define every domain term before using it in a flow**, as if writing for a strong
engineer who is new to this domain. A term used before it is defined forces the reader to
go hunting for the definition mid-flow.

**Diagrams in ASCII** — boxes and arrows, rendering as plain text, matching what the prose
says. **Concrete over abstract** — real scenarios with real values (`request id
req-4471`, not "a request"). **British register, plain, no filler or marketing tone.**

1. **`## 1. Executive Summary`** — 2–4 sentences, the elevator-pitch version of the
   Decision Log. Write it after the Decision Log, never before, so it summarises what
   actually happened rather than what was planned.
2. **`## 2. Business Context`** — the glossary: every domain term this document uses,
   defined — what it is, why it exists, who owns or uses it, where it lives in the
   system. A newcomer should be able to read this section alone and then follow the rest
   of the document without getting lost in vocabulary.
3. **`## 3. End-to-End Business Flow`** — one concrete request or job traced start to
   finish through the system, with an ASCII diagram of the path and real example values
   at each hop (not "the request is processed" — "`POST /widgets/warm` arrives with
   `widgetId: WGT-4471`, hits `WidgetCacheController`, which calls...").
4. **`## 4. Functional Requirements`** — the acceptance-relevant behaviour contract, each
   item tagged `[Verified]` / `[Assumed]` / `[Open]`.
5. **`## 5. Acceptance Criteria Analysis`** — status of the criteria this work was judged
   against, each one tagged `[Verified]` / `[Open]` against what was actually built.
6. **`## 6. System Architecture`** — the shape of the *system* this work touched
   (components, call path, an ASCII box-and-arrow diagram) — not the shape of the
   *change*, which belongs in Code Implementation below.
7. **`## 7. Code Implementation`** — for each changed class (or equivalent unit): its
   purpose, its methods, the rules it enforces, what it depends on, and the risks of
   touching it. Describe it at the level a reviewer would want, not pasted wholesale.
8. **`## 8. Decision Log`** — for each decision: the options considered, why each rejected
   option was rejected, and why the chosen option won. A decision log with no rejected
   alternatives is usually incomplete. If this document genuinely records no decisions (a
   hub or census document surveying other work rather than a bounded task — see below),
   write the single line `No decisions recorded — <why>.` rather than fabricating one or
   dropping the heading.
9. **`## 9. Production Considerations`** — behaviour only visible once deployed: error
   handling, edge cases, monitoring, logging, and the rollback path if this goes wrong.
   Do not skip this one lightly — a real search that motivated this template answered an
   HTTP 400 from a missing request header using exactly this section, in a document that
   had no other place to put it.
10. **`## 10. Testing Knowledge`** — what was actually tested, how, and what a future
    agent should re-run before trusting this area. Tag anything not actually exercised
    `[Assumed]`.
11. **`## 11. Lessons Learned`** — pitfalls actually hit during the work. Also known in
    the wider corpus as "Landmines" — both spellings resolve to the same role, but this
    template uses the skill's own name. If none were hit, say so explicitly (`None hit.`)
    rather than leaving the section blank.
12. **`## 12. Knowledge Graph`** — the entities this work touched and how they relate,
    written as `Entity → Related Entity → Purpose` rows, kept consistent with the front
    matter's `entities:` and `related:`.
13. **`## 13. Future LLM Context (cold-start primer)`** — 5–10 lines a cold-starting agent
    with zero other context could use to get oriented immediately. This is the single
    highest-leverage section: `engmem search` shows its first two lines as the result
    preview, so a document without one is invisible in search output even when it is the
    right answer. For a hub/census document, 2–3 lines pointing at what it surveys
    satisfies this without inflating it.
14. **`## 14. Search Keywords`** — free-text terms and synonyms a future search might use
    that the front-matter `tags`/`entities` do not already cover verbatim. This exists to
    close the wording gap lexical search cannot see on its own.
15. **`## 15. One-Page Cheat Sheet`** — the single densest paragraph in the document: for
    a reader who opens exactly one section, this is it.
16. **`## 16. Reuse Log`** — see the rules below.
17. **`## 17. Status at a Glance`** — a short table or bullets snapshot of where things
    actually stand right now: what is done, what is deployed, what is still open, what is
    blocked and on whom. Sections 1–16 describe the work; this one describes its *state*,
    which is the thing most likely to have changed since. Appended as 17 rather than
    inserted, so the sixteen numbers other documents already cite keep pointing at the
    same sections.

A **hub or census document** — one that indexes or surveys other documents or entities
rather than recording one bounded unit of work (e.g. a corpus-wide glossary, a
cross-ticket status board) — legitimately has nothing substantive for Decision Log or
Lessons Learned. Say so in one line, as above; do not delete the heading and do not pad it
with invented content. The same principle extends to any of the 17 that genuinely does not
apply to this particular document: say so explicitly in one line rather than either
omitting the heading or inventing content to fill it.

Plus engmem's own two operational sections, which every save carries regardless of
document shape:

- **`## Pre-reg`** — carry this over **unedited** from the draft `/engmem` created. Do
  not rewrite it with hindsight. (Place it first, ahead of the numbered sections — it
  documents what was known before this work started.)
- **`## Search Trace`** — the exact `shell` / `paste` / `miss` value `/engmem` recorded
  for this session, alone on its own line: the audit reads a line that is exactly the
  value, so `- shell` or `Trace: shell` counts as no trace at all. (Place it last, after
  Status at a Glance — it documents how this session's own retrieval went.)

  ```
  ## Search Trace

  shell
  ```

### Reuse Log rules

One row per prior document that **actually influenced** this work:

| prior-doc | taken | impact | classification |
|---|---|---|---|

- `prior-doc` is the cited document's id — its front-matter `id`, else its filename
  **stem**. Never the filename as written on disk: a `.md` suffix will not resolve, and
  the row is reported as citing a document that is not in the store and dropped from the
  Gate 1 count.
- `taken` must name a concrete artifact (a class, contract, decision, or pitfall) **and**
  include a direct quote from that prior document. A row without a quote is invalid —
  do not write one. The quote is **verbatim**: copied character for character out of the
  prior document, never paraphrased, tidied, or reconstructed from memory, and it is
  wrapped in double quotes so it can be found. Before finalising, run the quote checker,
  which checks every quote against the cited file. It ships in the engmem checkout, not in
  the installed package, so run it from any directory through the checkout:
  `uv run --project <engmem-checkout> python <engmem-checkout>/tools/verify_citations.py --store <store>`.
  An approximate quote reads as a fabricated one.
- `classification` is one of `reuse`, `anti-reuse`, `harmful` — exactly that spelling,
  lower case, nothing else in the cell.
- `engmem_complete_draft` refuses the whole document when any row has no quoted span or a
  classification outside those three, and returns each offending row; fix the row and call
  it again. A shell save has no such gate, so check the two rules yourself before writing
  the file; the checkout's report shows what the count will do with it:
  `uv run --project <engmem-checkout> python <engmem-checkout>/tools/gate1_report.py --store <store>`.
- A row that passes both rules, cited by id, quote copied out of the prior document:

  | prior-doc | taken | impact | classification |
  |---|---|---|---|
  | 20260101-widget-cache | `CacheWarmer` ordering: "CacheWarmer must run before WidgetCache accepts traffic" | kept the warm-up step in the rollout plan | reuse |
- Classify a row `anti-reuse` if the prior document was opened and quoted, and the
  decision it caused was to deliberately go the other way — the new work departs from
  what the document records, and that document is the reason the departure was a
  decision rather than an oversight. The test is whether the new work followed the
  prior document or went against it: adopting a rejection the document itself records
  is still following it, and stays `reuse`; doing the thing the document rejected,
  because the document made that choice visible, is `anti-reuse`. This differs from
  `harmful`, where the author was actually misled — under `anti-reuse` the outcome is
  better for having read the document.
- Never estimate time saved in this row — that's a judgment for the human at review time,
  not something to guess here. Record only the fact that the prior document was used.
- Never list a merely topically-related document that was not actually opened and used.
- If nothing was reused, the entire section is exactly the sentence
  `Prior docs used: none.` — never leave it blank, never omit it.
- **Before writing a row, check the cited document's `status`.** If it is `superseded`,
  ask the user: *"you cited <id>, which was superseded by <successor>. Did you rely on the
  stale document rather than its successor?"* Record their answer in the row's `impact`
  cell. A row that reuses stale knowledge is not invalid — it is a finding, and one that
  cannot be told apart from honest reuse after the fact. Classify it `harmful` if the
  stale content led somewhere the successor would not have.

## 3. Supersede check

Ask the user: "does this work supersede a decision recorded in an earlier document?" If
yes:

- If you can edit files directly: set that earlier document's `status: superseded` and
  `superseded_by:` to this document's `id`.
- If your runtime exposes the `engmem_mark_superseded` MCP tool instead of a shell: call
  it with `id` set to the earlier document's id and `superseded_by` set to this
  document's id. It touches only those two fields — everything else in the earlier
  document is left exactly as it was — and refuses unless that document's current
  status is `active`.

## 4. Close out

- Set `status: active` (from `draft`).
- Set `capture_minutes` to the minutes the save ritual took only if you know when it really
  started — a time you recorded, not the draft's `date:`, which has day resolution. Otherwise
  leave it blank: blank means "never measured", and a guess corrupts the cost side of the
  experiment.
- Set `verified_at_commit` to the current git `HEAD` — run `git rev-parse HEAD` if you
  have a shell; leave the field blank if you don't (never guess a commit sha).
- Write the finished document:
  - If you can write files directly: replace `sessions/<id>.md` with the complete
    document (finalised front matter from step 1 plus all body sections from step 2).
  - If your runtime exposes the `engmem_complete_draft` MCP tool instead of a shell:
    call it with `id` set to `<id>` and `content` set to that complete document text.
    It refuses to run unless the document on disk is still `status: draft`, and
    refuses the new content unless its own front matter states `status: active` —
    so this is the one call that actually performs the `draft -> active` transition.

## 5. Navigation misses — the other half of the retrieval instrument

If at any point in this session you used a store document that `engmem search` did **not**
surface — you found it by grep, by memory, or by following `related` from somewhere the
search never pointed to — record it as optional front matter, one entry per document:

```yaml
navigation_miss:
  - doc: <id of the document the search failed to surface>
    query: <the exact query that should have found it and did not>
```

Both fields are required in an entry. The id says a miss happened; the query says which
*kind* of miss it was — a wording gap (the document says the same thing in different words,
which lexical matching cannot see) or a ranking gap (it matched but lost to the documents
that were shown). The decision about whether engmem ever needs semantic search is
pre-registered against these entries, and a bare count cannot settle it.

Omit the field entirely if nothing like this happened — never write an empty entry, and
never invent one to look thorough. A miss recorded that did not happen corrupts the same
analysis a missing one does.

## 6. Cost of this session

Carry the numbers `/engmem` collected into the front matter, as optional fields. The
parser ignores fields it does not know, so omitting any of them is safe — and omitting
one is always better than inventing it.

```yaml
baseline_tokens:   # what the no-memory sub-agent actually consumed, if reported
context_bytes:     # prior-document text pulled into context this session
answer_steps:      # tool calls needed after the search to reach the answer
answer_tokens:     # only if the runtime really reports it
```

These answer two different questions, and it is worth keeping them apart:

- **Did memory help?** — the difference between the Pre-reg baseline and what was actually
  done. Qualitative, secondary, and read at review time only where the document records
  `pre-reg source: sub-agent`; a self-written baseline came from something that had already
  seen the code, so its divergence measures nothing. The primary evidence is the Reuse Log.
- **Did memory pay for itself in context?** — `context_bytes` against `answer_steps`.
  Cheap looks like: a few kilobytes recovered, almost no tool calls afterwards. Expensive
  looks like: tens of kilobytes pulled in and the same number of tool calls as without it,
  which means the document was read but did not actually shorten the path.

**What is not measured, and must not be presented as if it were:** the cost of finishing
this task *without* memory. The baseline is a plan, not an execution — nobody ran the
no-memory path to completion. Only a deliberate two-pass comparison on the same task
(same question answered twice, once with the store and once without) yields a real
side-by-side number.
