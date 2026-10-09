---
description: Save the useful core of a decision in one preview — the decision, its reason, the rejected alternative, the source and a short context for the next task
---

# /engmem.save.quick — save the useful core

For when the session produced a decision worth keeping and a full report is not. Close out
the session started by `/engmem` with a **short record**: five components in one preview,
one confirmation, and one published document. Nothing else is asked of the user.

## The five components

1. **Decision** — what was decided, in one sentence.
2. **Reason** — why, as the conversation stated it.
3. **Rejected alternative** — the option that was turned down, and why, if one was named.
4. **Source** — where the decision can be checked: a PR, commit, ticket, file path or link.
5. **Context for the next task** — 2–4 lines a cold-starting agent can act on.

The first four go in `## Decision Log`, one labelled line each. The fifth is
`## Future LLM Context (cold-start primer)`: `engmem search` shows its first two lines as the
result preview, cut at about 240 characters, so make those two lines short and able to stand
alone. Besides Reuse Log and Search Trace (steps 3–4), which every save carries, and the draft's
Pre-reg (step 5) when it has one, no other section needs filling.

## Rules

- **Never invent.** Take every component from what this session can see: the conversation,
  the diff, the git history. A Source is copied exactly as it appears there. Never build a
  link, PR number or ticket id from a pattern. A reason the conversation did not give is not
  supplied by you.
- **Mark what is absent.** When the Reason, the Rejected alternative or the Source is not in
  that material, its line keeps its label and says so in exactly these words, and is never
  dropped or left blank:

  ```
  - Source: not stated in the available material.
  - Rejected alternative: not stated in the available material.
  ```

- **No decision, no short record.** If the session reached no decision, say so, save
  nothing, and leave the draft as it is.
- **One confirmation, no questions.** No field-by-field prompting, no YAML review round, no
  supersede question.
- This path degrades document *completeness*, never document *validity*: the result must
  still parse cleanly, with valid YAML front matter and every required field present.
- **What it skips.** There is no supersede question, so a document this decision overturns
  keeps surfacing as current until someone marks it. The Reuse Log is not reviewed with the
  user, so reuse evidence the experiment needs may be missing. Use `/engmem.save` when either
  matters.

## Steps

1. From the conversation and the diff, take the decision and its reason. If more than one
   decision is worth keeping, repeat the four lines for each, at most three. Write them in
   `## Decision Log`:

   ```
   - Decision: <one sentence>
   - Reason: <why>
   - Rejected alternative: <the option, and why it lost>
   - Source: <PR, commit, ticket, path or link>
   ```

   If a pitfall was actually hit, add it as one bullet under `## Lessons Learned`;
   otherwise leave that section out.
2. Write `## Future LLM Context (cold-start primer)`: 2–4 lines. The first two are the
   search preview: short, about 240 characters together, and readable on their own.
3. Write `## Reuse Log` as exactly `Prior docs used: none.` unless prior docs obviously
   influenced the work, in which case list them as rows (with quotes, same validity rule
   as `/engmem.save`: a quoted span in `taken`, classification exactly `reuse` /
   `anti-reuse` / `harmful`; `engmem_complete_draft` refuses a row that breaks either and
   returns it), but do not interrogate the user about it.
4. Write `## Search Trace` with the `shell` / `paste` / `miss` value recorded by
   `/engmem`, alone on its own line. `- shell` or `Trace: shell` counts as no trace:

   ```
   ## Search Trace

   shell
   ```
5. Carry `## Pre-reg` over unedited from the draft, first in the body. Skip it when the
   draft has none: a daily draft has none, and none is written now.

   Headings here carry the same names as `/engmem.save`'s but **no numbers**. The numbers
   there index a fixed set of seventeen; a short record numbered 8 and 13 would read as one
   with gaps where sections were dropped, which is exactly what this path does not claim.
   The sections this path writes are listed in `ENGMEM-SPEC.md` §4.
6. Generate the complete front matter automatically (same fields as `/engmem.save`, with
   `verified_at` holding, for each repository in `repos`, the sha from
   `git -C <its checkout> rev-parse HEAD` (`verified_at: {platform-core: <sha>}`; a repository
   with no checkout or no shell at hand is left out, never guessed), `covers_files` as paths
   from the repository's top-level directory the way `git ls-files` prints them, and
   `capture_minutes` blank unless you know when the save really started; the draft's
   `date:` has day resolution and is no anchor; `mode`, and `baseline_unavailable` if the
   draft has it, copied from the draft unchanged), and set `status: active`. Name the
   decision in `title`, and put the classes, services or components it is about in
   `entities`: search weighs both above the body.
7. **The preview.** Show the user the finished document in one message and ask exactly one
   thing: save it? Then act on the reply once:
   - **Confirmed** (`ok`, `yes`): publish that document as shown.
   - **Edits**: apply them and publish. Do not show it again and do not ask again.
   - **Declined** (`no`, `cancel`, `don't save`), or a reply you cannot read as either of
     the above: publish nothing. Do not call `engmem_complete_draft` and do not set
     `status: active`. The draft stays as it is, `status: draft`, which keeps it out of
     search, and it still holds the Pre-reg this session's searches are attributed to:
     deleting it would leave them pointing at no document. Tell the user it stays a draft
     that `/engmem.save` or `/engmem.save.quick` can finish later.

   To publish, replace `sessions/<id>.md` with the document if you have file access; never
   write it under a second name. Otherwise call the `engmem_complete_draft` MCP tool with
   `id`, the document as `content`, and the version `engmem_create_draft` returned as
   `expected_version`. If it refuses because the draft changed since you read it, the
   refusal carries the current draft and its version: carry what changed into the document,
   show it to the user again, since that is a different document from the one confirmed,
   and call with that version.

## Example

The shape, not content to copy. This record, from a daily session, had a rejected alternative
and no source:

````markdown
---
id: 20260101-export-retry
title: Export job retries with a fixed backoff
date: 2026-01-01
task_date: 2026-01-01
status: active
superseded_by:
backfilled: false
tags: [export]
entities: [ExportJob, RetryPolicy]
related: []
covers_files: [src/export/ExportJob.java]
verified_at:
capture_minutes:
author:
repos: [platform-core]
branch:
pr:
mode: daily
---

## Decision Log

- Decision: ExportJob retries a failed batch three times with a fixed 30-second backoff.
- Reason: the downstream store rejects bursts, and exponential backoff overran the nightly window.
- Rejected alternative: exponential backoff, because its last retry landed after the window closed.
- Source: not stated in the available material.

## Future LLM Context (cold-start primer)

ExportJob retries a failed batch three times, 30 seconds apart, because the store rejects bursts.
Change the retry count only together with the nightly window it has to fit.

## Reuse Log

Prior docs used: none.

## Search Trace

shell
````
