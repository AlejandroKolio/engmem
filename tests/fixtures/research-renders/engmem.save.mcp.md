---
description: Finish an engmem task — save what was learned as a complete session document
---

# /engmem.save — finish a task

Close out the session `/engmem` started. Its draft is `sessions/<id>.md`, `status: draft`,
holding exactly the text you passed to `engmem draft create` or `engmem_create_draft`. Reuse
that text and the `version` the call returned, and complete the draft in the passes below.
This is the full shape; for a low-effort close-out, use `/engmem.save.quick`.

**Read the clock first.** With a shell, read the current UTC time before anything else:
`date -u +%Y-%m-%dT%H:%M:%SZ` in a POSIX shell, `(Get-Date).ToUniversalTime().ToString('o')`
in PowerShell. Keep the value for `capture_minutes`. Without a shell, leave `capture_minutes` blank.

## 1. Front-matter draft (one interactive round, not field-by-field)

Draft **every** front-matter field from the diff and this conversation:

`id`, `title`, `date`, `task_date`, `status`, `superseded_by`, `backfilled`, `tags`,
`entities`, `related`, `covers_files`, `verified_at`, `capture_minutes`, `author`,
`repos`, `branch`, `pr`, `mode`

Copy `mode`, and `baseline_unavailable` when the draft has it, from the draft unchanged. Never
add, edit or drop them, even if the user has switched mode since: they record how this session
started. A draft without `mode` stays without one.

Derive these fields; never guess them:

- `related` — document ids, never filenames: a `.md` suffix does not resolve.
- `covers_files` — the files the decision rests on, as `git ls-files` prints them from the
  repository's top-level directory (`src/export/ExportJob.java`, not `ExportJob.java` or
  `./src/...`). Search compares them between `verified_at` and `HEAD`, only when `repos` has
  one entry.
- `author` — `git config user.name` (and `user.email` if you want both); blank if neither is
  available. Never invent a name.
- `repos` — the repositories this work touched, as a list: `[platform-core]`, or
  `[platform-core, widget-cache-client]` only where `covers_files` spans more than one checkout.
  Spell each as its directory name, every time: `engmem search --repo` filters on it.
- `branch` — `git rev-parse --abbrev-ref HEAD`; blank on a detached HEAD or without a shell.
  Never fabricate a name.
- `pr` — the pull request number (`#482`) or URL; blank if no PR exists yet. Never invent one.

Show the full YAML block and ask for `y` or edits. Apply the edits, then stop asking: one
interactive round only.

## 2. Body sections — the numbered seventeen, plus engmem's own two

Write every section below, in this order, with the numbered heading exactly as shown
(`## 8. Decision Log`, not `## Decision Log`). None of the 17 is optional. A section with
nothing to report keeps its heading and says so in one line; never delete the heading or pad
it with invented content. A **hub or census document** (one that surveys other documents, such
as a glossary or a status board) often has nothing for Decision Log or Lessons Learned.

**Gather ground truth before writing.** Read the actual diff, git history and ticket, and ask
the user where something is unclear; never infer what "probably" happened. Name a source you
could not reach; never invent its contents.

**Tag every factual claim.** `[Verified]` — read in the code, history or ticket, or confirmed
by the user. `[Assumed]` — an inference; say what it rests on. `[Open]` — a real gap. Never
upgrade `[Assumed]` to `[Verified]` to read better. A mostly `[Assumed]` section means: gather
more ground truth.

**Explain WHY, not only HOW**: for each non-obvious choice, name the problem it solves and what
the alternative would have cost. **Define every domain term before using it in a flow.**

**Diagrams in ASCII** — boxes and arrows, matching the prose. **Concrete over abstract** —
real scenarios with real values (`request id req-4471`, not "a request"). **Write in plain
technical English, following ASD-STE100 principles: one idea per sentence; sentences of 20
words or fewer; active voice; one term for one concept, used the same way throughout. No
filler and no marketing tone.**

1. **`## 1. Executive Summary`** — 2–4 sentences. Write it after the Decision Log, so it
   summarises what happened, not what was planned.
2. **`## 2. Business Context`** — the glossary: each domain term this document uses, with what
   it is, why it exists, who owns it and where it lives. A newcomer can follow the rest after it.
3. **`## 3. End-to-End Business Flow`** — one concrete request or job traced start to finish,
   with an ASCII diagram and real values at each hop (`widgetId: WGT-4471` hits
   `WidgetCacheController`, which calls...).
4. **`## 4. Functional Requirements`** — the acceptance-relevant behaviour contract, each item
   tagged `[Verified]` / `[Assumed]` / `[Open]`.
5. **`## 5. Acceptance Criteria Analysis`** — each criterion this work was judged against,
   tagged `[Verified]` / `[Open]` against what was built.
6. **`## 6. System Architecture`** — the shape of the *system* this work touched (components,
   call path, an ASCII diagram), not of the *change*.
7. **`## 7. Code Implementation`** — per changed class or unit: purpose, methods, rules it
   enforces, dependencies, and the risks of touching it. Do not paste it wholesale.
8. **`## 8. Decision Log`** — per decision: the options considered, why each rejected one lost,
   and why the chosen one won. A log with no rejected alternative is usually incomplete. With
   no decisions (a hub or census document), write the single line
   `No decisions recorded — <why>.`; never fabricate one.
9. **`## 9. Production Considerations`** — what shows only once deployed: error handling, edge
   cases, monitoring, logging, the rollback path. Do not skip it lightly: a later search may
   need it.
10. **`## 10. Testing Knowledge`** — what was tested, how, and what to re-run before trusting
    this area. Tag anything not exercised `[Assumed]`.
11. **`## 11. Lessons Learned`** — pitfalls actually hit. If none, say so (`None hit.`); never
    leave it blank. Write each pitfall as one lesson in this shape:

    ```
    - Tried: <what was done, one sentence ending with a full stop>
      Failed because: <the cause, not the symptom>
      Fixed by: <what made it work>
      Files: <paths as git ls-files prints them, comma-separated>
    ```

    Nothing precedes the first bullet in the section: search shows its first sentence. `Tried:`
    is always filled; a `Failed because:`, `Fixed by:` or `Files:` the session cannot state
    keeps its label and reads `not stated in the available material.`, in place of `[Open]`.
    `Files:` is not copied into `covers_files`.
12. **`## 12. Knowledge Graph`** — `Entity → Related Entity → Purpose` rows, consistent with
    the front matter's `entities:` and `related:`.
13. **`## 13. Future LLM Context (cold-start primer)`** — 5–10 lines a cold-starting agent can
    act on (2–3 for a hub or census document). `engmem search` shows its first two lines as
    the result preview: without them the document is invisible there.
14. **`## 14. Search Keywords`** — terms and synonyms a future search might use that `tags` and
    `entities` do not already cover verbatim.
15. **`## 15. One-Page Cheat Sheet`** — the densest paragraph, for a reader who opens one section.
16. **`## 16. Reuse Log`** — see the rules below.
17. **`## 17. Status at a Glance`** — short table or bullets: what is done, deployed, open, and
    blocked on whom. It records the work's *state*, which is what most likely changed since.

Plus engmem's own two sections, in every save:

- **`## Pre-reg`** — first, before the numbered sections. Carry it over **unedited** from the
  draft, if it has one; never rewrite it with hindsight. Skip it otherwise:
  a daily draft has none, and none is written now.
- **`## Search Trace`** — last: `shell`, `paste` or `miss`, alone on its own line. The audit
  reads only that exact line; `- shell` or `Trace: shell` counts as no trace. With a shell, run
  `engmem cost summary --session <id>`: a `search responses` count above 0 means `shell`, or
  `paste` if the user pasted the output. Otherwise write the value you still know, else
  `not stated in the available material.`

  ```
  ## Search Trace

  shell
  ```

### Reuse Log rules

Start from what this session found. With a shell, run `engmem feedback summary --session <id>`
before writing the Reuse Log: every find marked `[read]` and every id on its
`read, not surfaced:` line either gets a row below or is left out on purpose. Through MCP only,
the `cite as` lines you kept are the list. If completing the draft warns that the Reuse Log says
none while this session read documents, tell the user, and do not edit the published file.

One row per prior document that **actually influenced** this work:

| prior-doc | taken | impact | classification |
|---|---|---|---|

- `prior-doc` is `<id>@<version>`, copied exactly from the `cite as (Reuse Log prior-doc):`
  line of the response the quote came from: the `engmem read` result's when you quote a read,
  the search result's when you quote the search. The id is the front-matter `id`, else the
  filename **stem**; a `.md` suffix does not resolve. Never compute or guess a version: the
  quote is checked against engmem's copy of that version. If no search showed you the
  document, write the bare id; the row is then checked against the current text as legacy.
- `taken` names a concrete artifact (a class, contract, decision, or pitfall) **and** a direct
  quote from that document, in double quotes. A row without a quote is invalid — do not write
  one. The quote is **verbatim**: never paraphrased, tidied, or reconstructed from memory; an
  approximate quote reads as a fabricated one. Completion checks every quote and returns
  refusals and warnings; read them, each one names the defect and the row. A warning comes
  after publishing, so tell the user and do not edit the published file.
- `classification` is one of `reuse`, `anti-reuse`, `harmful` — exactly that spelling,
  lower case, nothing else in the cell.
- `engmem draft complete` and `engmem_complete_draft` refuse the whole document when any row
  has no quoted span, a classification outside those three, or a `prior-doc` version that is
  not one a search showed, when the table's columns are not in the order above, or when the
  section has neither a row nor `Prior docs used: none.`, and return each defect; fix it and
  complete again.
- A valid row:

  | prior-doc | taken | impact | classification |
  |---|---|---|---|
  | 20260101-widget-cache@3f9a0c1b2d4e5f60 | `CacheWarmer` ordering: "CacheWarmer must run before WidgetCache accepts traffic" | kept the warm-up step in the rollout plan | reuse |
- `anti-reuse`: the document was quoted, and the work chose to deliberately go the other way
  because of it. The test is whether the work followed the prior document or went against it:
  adopting a rejection the document records is `reuse`; doing what it rejected is
  `anti-reuse`. Under `harmful` the document misled the author; under `anti-reuse` the outcome
  is better for having read it.
- Never estimate time saved in a row; the human judges it at review. Never list a related
  document that was not opened and used.
- If nothing was reused, the entire section is exactly the sentence
  `Prior docs used: none.` — never leave it blank, never omit it.
- **Before writing a row, check the cited document's `status`.** If it is `superseded`,
  ask the user: *"you cited <id>, which was superseded by <successor>. Did you rely on the
  stale document rather than its successor?"* Record their answer in the row's `impact`
  cell. Such a row is a finding, not invalid. Classify it `harmful` if the stale content led
  somewhere the successor would not have.

## 3. Supersede check

Ask the user: "does this work supersede a decision recorded in an earlier document?" If
yes:

- If your runtime exposes the `engmem_mark_superseded` MCP tool: prefer it to editing the file
  when you have both. Call it with `id` set to the earlier document's id, `superseded_by` set
  to this document's id, and `expected_version` set to the version of the earlier document
  this document was built on: the `<version>` of its `<id>@<version>` row in your Reuse Log.
  Do not read the earlier document again to get a newer version: the check catches changes
  made since you built this document. Only if you have no such version, read it now with
  `engmem_read`, check that this document really replaces it, and pass that read's version.
  The tool changes only those two fields and refuses unless the earlier document is `active`.
  If it refuses because the earlier document changed since you read it, the refusal carries
  its current text and version. Read what changed, tell the user, and call again with that
  version only if this document still replaces it.
- Otherwise, if you can edit files directly: set that earlier document's `status: superseded`
  and `superseded_by:` to this document's `id`. Nothing checks this edit, so read the document
  again just before you edit it.

## 4. Close out

- Set `status: active` (from `draft`).
- Set `capture_minutes`. Just before you publish, read the UTC time again with the same command.
  Write the whole minutes between the two readings. Without both readings, leave it blank: blank
  means "never measured", and a guess corrupts the cost data. The draft's `date:` has day
  resolution, so it is no start time.
- Set `verified_at` to the commit each repository in `repos` was checked at, one entry per
  repository: `verified_at: {platform-core: <sha>}`, the sha from
  `git -C <that repository's checkout> rev-parse HEAD`. Leave a repository out when you have
  no shell or no checkout of it, and leave the field blank when you have neither; never guess
  a sha. Do not write the older single `verified_at_commit`.
- Write the finished document (front matter from step 1, body sections from step 2) through
  engmem, never as a plain file write:
  - If your runtime exposes the `engmem_complete_draft` MCP tool: call it with `id` set to
    `<id>`, `content` set to the document, and `expected_version` set to the version create
    gave. Prefer it to the shell when you have both.
  - Otherwise, with a shell: pipe it to
    `engmem draft complete <id> --expected-version <the version create gave>` with a quoted
    delimiter, so nothing in the text is expanded or run. In a POSIX shell:
    `engmem draft complete <id> --expected-version <version> <<'EOF'` … `EOF`, with a
    delimiter no line of the text is. In PowerShell, first
    `$OutputEncoding = [System.Text.UTF8Encoding]::new($false)`, since Windows PowerShell 5.1
    otherwise sends every non-ASCII character as `?`, then
    `@'` … `'@ | engmem draft complete <id> --expected-version <version>`.
  - Either way, it performs the `draft -> active` transition. It refuses unless the document
    on disk is still `status: draft` and your content states `status: active`. If the draft
    changed since you read it, the refusal carries the current draft and its version: read
    it, carry what changed into your document and call again with that version. Never retry
    the same content with the new version without reading what changed. If you no longer have
    the version, pass an empty `expected_version` (or `--expected-version ""`): nothing is
    written, and the refusal carries the current draft and its version.
  - If you can do neither, or it fails for a reason you cannot fix in the document: do not
    write `sessions/<id>.md` yourself. Tell the user the document was not saved and why, and
    keep the finished text in your reply; the draft stays a draft, out of search. Only if the
    user then explicitly asks, write it by hand as `sessions/<id>.md` in the store, and tell
    them it was saved outside engmem's checks: no version check, no Reuse Log check, no lock.
    The store is the first line of `engmem store show`. Without a shell: `$ENGMEM_HOME` if
    set, else the path saved in `$XDG_CONFIG_HOME/engmem/store` (default
    `~/.config/engmem/store`; `%APPDATA%\engmem\store` on Windows) if that file exists, else
    `~/Developer/engmem`.

### Usefulness feedback (optional)

After the document is written, ask the user once, naming the documents this session's
searches showed: *"did any of these help, not apply, or do harm?"* `engmem feedback summary
--session <id>` lists them. For each document the user assesses, record exactly what they said:

- With a shell: `engmem feedback record <id> <doc-id> helped|not-applicable|harmful`, adding
  `--decision "<the decision it changed>"` and `--source "<commit, PR or review>"` when the
  user names them.
- Through MCP: call `engmem_record_feedback` with `session_id`, `doc_id`, `assessment`, and
  `decision` / `source` when given.

The assessment is the user's: never record your own judgement, never fill in a document the
user did not assess, and record nothing if the user does not answer. This changes nothing in
the Reuse Log.

## 5. Navigation misses — the other half of the retrieval instrument

If you used a store document that `engmem search` did **not** surface (found by grep, by
memory, or through `related` of a document the search never showed), record it as optional
front matter, one entry per document:

```yaml
navigation_miss:
  - doc: <id of the document the search failed to surface>
    query: <the exact query that should have found it and did not>
```

Both fields are required in an entry: the query shows what kind of miss it was. Omit the
field if nothing like this happened. Never write an empty entry or invent one.

## 6. Cost of this session

Optional fields. With a shell, take the first two from `engmem cost summary --session <id>`;
without one, omit them. Write the last two only if you still have `/engmem`'s number: omit a
lost one, never reconstruct it.

```yaml
baseline_tokens:   # research mode: the sum of the tokens on the lines under `baseline call`
context_bytes:     # the bytes its `total:` line shows
answer_steps:      # tool calls needed after the search to reach the answer
answer_tokens:     # only if the runtime really reports it
```

Never present the gap between the Pre-reg and the final plan as proof that memory helped: the
Reuse Log is the evidence. Never present the cost of this task *without* memory as measured:
nobody ran it.
