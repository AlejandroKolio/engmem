---
description: Save the useful core of a decision in one preview — the decision, its reason, the rejected alternative, the source and a short context for the next task
---

# /engmem.save.quick — save the useful core

For a decision worth keeping when a full report is not. Close out the session `/engmem`
started with a **short record**: five components, one preview, one confirmation, one
published document. Ask the user nothing else.

## The five components

1. **Decision** — what was decided, in one sentence.
2. **Reason** — why, as the conversation stated it.
3. **Rejected alternative** — the option that was turned down, and why, if one was named.
4. **Source** — where the decision can be checked: a PR, commit, ticket, file path or link.
5. **Context for the next task** — 2–4 lines a cold-starting agent can act on.

<!-- engmem-branch: research -->
The first four go in `## Decision Log`, one labelled line each. The fifth is
`## Future LLM Context (cold-start primer)`: `engmem search` shows its first two lines as the
result preview, cut at about 240 characters, so make those two lines short and able to stand
alone. Besides Reuse Log and Search Trace (steps 3–4), which every save carries, and the draft's
Pre-reg (step 5) when it has one, no other section needs filling.

<!-- engmem-branch: end -->
<!-- engmem-branch: daily -->
In daily mode the first four go in `## Decision Log`, one labelled line each, and the fifth is
`## Future LLM Context (cold-start primer)`; no other section is needed, but Lessons Learned
for a pitfall hit.

<!-- engmem-branch: end -->
## Rules

- **Never invent.** Take every component from what this session can see: the conversation,
  the diff, the git history. Copy a Source exactly as it appears there; never build a link, PR
  number or ticket id from a pattern. Never supply a reason the conversation did not give.
- **Mark what is absent.** When the Reason, the Rejected alternative, the Source, or a
  lesson's `Failed because:`, `Fixed by:` or `Files:` is not in that material, its line
  keeps its label and says so in exactly these words, and is never dropped or left blank:

  ```
  - Source: not stated in the available material.
  - Rejected alternative: not stated in the available material.
  ```

<!-- engmem-branch: research -->
- **No decision, no short record.** If the session reached no decision, say so, save
  nothing, and leave the draft as it is.
<!-- engmem-branch: end -->
<!-- engmem-branch: daily -->
- **No decision, no short record.** In daily mode, say so and save nothing.
<!-- engmem-branch: end -->
- **One confirmation, no questions.** No field-by-field prompting, no YAML review round, no
  supersede question.
<!-- engmem-branch: research -->
- The record may be less *complete*, never less *valid*: it must parse cleanly, with valid
  YAML front matter and every required field present.
<!-- engmem-branch: end -->
<!-- engmem-branch: research -->
- **What it skips:** the supersede question and a Reuse Log review with the user. Use
  `/engmem.save` when either matters.
<!-- engmem-branch: end -->

## Steps

<!-- engmem-branch: daily -->
Daily mode reads no clock.

<!-- engmem-branch: end -->
<!-- engmem-branch: research -->
**Read the clock first.** With a shell, read the current UTC time before anything else:
`date -u +%Y-%m-%dT%H:%M:%SZ` in a POSIX shell, `(Get-Date).ToUniversalTime().ToString('o')`
in PowerShell. Keep the value for `capture_minutes`. Without a shell, leave `capture_minutes` blank.

<!-- engmem-branch: end -->
1. From the conversation and the diff, take the decision and its reason. If more than one
   decision is worth keeping, repeat the four lines for each, at most three. Write them in
   `## Decision Log`:

   ```
   - Decision: <one sentence>
   - Reason: <why>
   - Rejected alternative: <the option, and why it lost>
   - Source: <PR, commit, ticket, path or link>
   ```

   If a pitfall was actually hit, add it as one lesson in this shape under
   `## Lessons Learned`; otherwise leave that section out.

   ```
   - Tried: <what was done, one sentence ending with a full stop>
     Failed because: <the cause, not the symptom>
     Fixed by: <what made it work>
     Files: <paths as git ls-files prints them, comma-separated>
   ```

   Nothing precedes the first bullet in the section: search shows its first sentence. `Tried:`
   is always filled.
2. Write `## Future LLM Context (cold-start primer)`: 2–4 lines. The first two are the
   search preview: short, about 240 characters together, and readable on their own.
<!-- engmem-branch: daily -->
3. *Daily mode only (research: the next step 3).* Generate the front matter:
   the example's fields, `status: active`, `mode: daily`, `id` as `<YYYYMMDD>-<slug>`
   (lowercase letters, digits, hyphens), `date` and `task_date` today. With a shell, run
   `git rev-parse --show-toplevel HEAD --abbrev-ref HEAD; git config user.name` once: the
   checkout's directory name is `repos`, the sha `verified_at` (`{platform-core: <sha>}`), the
   branch `branch` (blank for `HEAD`), the name `author`; without a shell they stay blank, never
   guessed. `covers_files` as `git ls-files` prints them. Name the decision in `title` and the
   classes, services or components it is about in `entities`: search weighs both above the
   body. `related` lists the ids (before `@`) of prior documents this work used. Headings carry
   no numbers.
4. **The preview.** Show the finished document in one message and ask one thing: save it?
   Confirmed: publish it as shown. Edits: apply them and publish without showing it again.
   Declined, or a reply you cannot read as either: publish nothing.
5. **Publish** in two calls, never by writing `sessions/<id>.md` yourself. First the front
   matter alone with `status: draft`: `engmem_create_draft` with `id` and it as `content`
   (preferred), or pipe it to `engmem draft create <id>`. Then the whole document:
   `engmem_complete_draft` with `id`, `content` and the version create named as
   `expected_version`, or pipe it to `engmem draft complete <id> --expected-version <version>`.
   Pipe with a quoted delimiter, so nothing is expanded or run: `<<'EOF'` … `EOF` in a POSIX
   shell; in PowerShell, first `$OutputEncoding = [System.Text.UTF8Encoding]::new($false)`, then
   `@'` … `'@ |` the command. A taken id needs another id. If a fix to a refusal changes the
   document, show it again and publish only after a new confirmation. If complete fails after
   create, the draft `<id>` stays; `engmem draft complete <id> --expected-version <version>`
   (MCP: `engmem_complete_draft`) finishes it later. If you can do neither, or a call fails for a
   reason no change fixes, publish nothing, say why and keep the document in your reply. Write
   the file by hand only if the user asks, in the store `engmem store show` names, saying it
   bypassed engmem's checks. The daily save ends here.

<!-- engmem-branch: end -->
<!-- engmem-branch: research -->
3. Write `## Reuse Log` from what this session found: with a shell, run
   `engmem feedback summary --session <id>` first; through MCP only, the `cite as` lines you
   kept are the list. Write it as exactly `Prior docs used: none.` unless prior docs obviously
   influenced the work, in which case list them as rows of a
   `| prior-doc | taken | impact | classification |` table (with quotes, same validity rule
   as `/engmem.save`: `prior-doc` is the `<id>@<version>` the `cite as` line of the search or
   read the quote came from gave, a quoted span in `taken`, classification exactly `reuse` /
   `anti-reuse` / `harmful`; `engmem draft complete` and `engmem_complete_draft` refuse a row
   that breaks one and return it), but do not interrogate the user about it.
4. Write `## Search Trace` with the `shell` / `paste` / `miss` value alone on its own line.
   With a shell, run `engmem cost summary --session <id>`: a `search responses` count above 0
   means `shell`, or `paste` if the user pasted the output. Otherwise write the value you still
   know, else `not stated in the available material.`
   `- shell` or `Trace: shell` counts as no trace:

   ```
   ## Search Trace

   shell
   ```
5. Carry `## Pre-reg` over unedited from the draft, first in the body. Skip it when the
   draft has none: a daily draft has none, and none is written now.

   Headings here carry the same names as `/engmem.save`'s but **no numbers**: numbered
   sections would read as a full record with gaps.
6. Generate the complete front matter automatically (same fields as `/engmem.save`, with
   `verified_at` holding, for each repository in `repos`, the sha from
   `git -C <its checkout> rev-parse HEAD` (`verified_at: {platform-core: <sha>}`; a repository
   with no checkout or no shell at hand is left out, never guessed), `covers_files` as paths
   from the repository's top-level directory the way `git ls-files` prints them, and `mode`,
   and `baseline_unavailable` if the draft has it, copied from the draft unchanged), and set
   `status: active`. Name the decision in `title`, and put the classes, services or components
   it is about in `entities`: search weighs both above the body. Fill `capture_minutes` last:
   read the UTC time again with the same command, and write the whole minutes between the two
   readings. Take this reading here, not at publishing: the document must not change after the
   user confirms it. Without both readings, leave it blank; the draft's `date:` is no start time.
7. **The preview.** Show the user the finished document in one message and ask exactly one
   thing: save it? Then act on the reply once:
   - **Confirmed** (`ok`, `yes`): publish that document as shown.
   - **Edits**: apply them and publish. Do not show it again and do not ask again.
   - **Declined** (`no`, `cancel`, `don't save`), or a reply you cannot read as either of
     the above: publish nothing. Do not run `engmem draft complete`, do not call
     `engmem_complete_draft`, and do not set
     `status: active`. The draft stays as it is, `status: draft`, out of search. Do not
     delete it: this session's searches are attributed to it. Tell the user it stays a draft
     that `/engmem.save` or `/engmem.save.quick` can finish later.

   To publish, call the `engmem_complete_draft` MCP tool with `id`, the document as
   `content`, and the version create gave as `expected_version`; prefer it to the shell when
   you have both. Otherwise, with a shell, pipe the document to `engmem draft complete <id>
   --expected-version <the version create gave>` with a quoted delimiter, so nothing in it is
   expanded or run: in a POSIX shell, `<<'EOF'` … `EOF` (a delimiter no line of the document
   is); in PowerShell, first `$OutputEncoding = [System.Text.UTF8Encoding]::new($false)`, since
   Windows PowerShell 5.1 otherwise sends every non-ASCII character as `?`, then `@'` … `'@ |`
   the command. Never write `sessions/<id>.md` yourself, and never under a second name.

   Publish only the document the user confirmed. If the call refuses, whatever the reason, and
   what fixes it changes the document — a Reuse Log row, the mode, a field — show the changed
   document to the user again and publish only after a new confirmation: it is a different
   document from the one confirmed. A refusal because the draft changed since you read it
   carries the current draft and its version: carry what changed into the document, show it
   again, and call with that version. If the published result warns that the Reuse Log says
   none while this session read documents, tell the user, and do not edit the published file.

   If you can do neither, or the call fails for a reason no change to the document fixes,
   publish nothing and tell the user it was not saved and why. Keep the confirmed document in
   your reply, and say the draft stays a draft that `/engmem.save.quick` can finish once
   engmem can write. Write the file by hand only if the user then explicitly asks: as
   `sessions/<id>.md` in the store `engmem store show` names (without a shell, the order
   `/engmem.save` gives), and tell them it was saved outside engmem's checks.

<!-- engmem-branch: end -->
## Example
<!-- engmem-branch: research -->

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
<!-- engmem-branch: end -->
<!-- engmem-branch: daily -->

The daily shape, not content to copy: a rejected alternative and no source.

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
````
<!-- engmem-branch: end -->
