---
description: Start a new engineering task with engmem — recover prior context before planning
argument-hint: <task description>
---

# /engmem — start a task

Task description: "$ARGUMENTS"

Follow these steps in order. **Never make the user wait on the ritual**: they asked for
something, and steps 1–5 exist to serve that request, not to gate it. If any step cannot
be completed, record what happened and keep going — never withhold the answer or the plan
because a step did not work out.

## 1. Pre-reg — generated, never asked of the user

**Do not ask the user anything in this step.** Do not ask them to describe their approach,
do not wait for a reply, do not offer to skip the ritual. They asked a question or gave a
task; answering it is your job, and the baseline below is engmem's bookkeeping, not theirs.

Produce a naive baseline: 2–3 lines describing how this task would be approached with **no
prior context at all**. Two ways, in order of preference:

- **Preferred — a separate pass that cannot see the store.** If you can spawn a sub-agent
  or an isolated task, give it *only* the user's task description. It must not read the
  engmem store, must not run `engmem search`, and must not be told that engmem exists.
  Ask it for 2–3 lines: "how would you approach this?" Use its answer verbatim.
- **Fallback — write it yourself, right now.** If you cannot spawn anything, write those
  2–3 lines yourself **before** running any search and before opening any store document.
  Once you have read a prior document the baseline is contaminated and worthless.

This is a *baseline for comparison*, not a plan you must follow. Do not defend it, do not
refine it, and do not show it to the user for approval — write it down and move on.

Record which way you got it, as the last line of the Pre-reg section:
`pre-reg source: sub-agent` or `pre-reg source: self (no sub-agent available)`.

If the sub-agent's token usage is reported back to you, note it too —
`baseline_tokens: <n>` — this is the one cost of the no-memory path that is genuinely
measured rather than guessed, so never estimate it: omit the line if you cannot see it.

**Why this exists** (so you do not "optimise" it away): the experiment compares this
uncontaminated baseline against the plan produced *after* prior documents are loaded.
That difference is a **secondary** signal, not the verdict — a plan written blind diverges
from the final one for many reasons, and reading the repository is one of them. The primary
evidence that engmem changes outcomes is the quoted rows of the Reuse Log
(`ENGMEM-SPEC.md` §1: the success metric is reused knowledge, and `pre-reg source:` below
is what lets the two baseline populations be told apart at review time). Written after the
documents are read, the baseline measures nothing at all.

## 2. Create the draft immediately

The engmem store resolves in this order: `$ENGMEM_HOME` if set, otherwise
`~/Developer/engmem`. As soon as you have the baseline text, compose the draft: the
**complete** front matter below — placeholder values, but every field present, so the
document is valid to the parser from the moment it exists — followed by a `## Pre-reg`
section containing the baseline from step 1 plus its `pre-reg source:` line. Leave the
other body sections for `/engmem.save`; the empty `tags`/`entities` lists are expected in
a draft (the parser reports empty entities as a warning, not an error — that's normal
until save fills them in).

```yaml
---
id: <YYYYMMDD>-<slug>          # or <story-id>-<slug>; must equal the filename stem
title: <short title from the task description>
date: <today>                   # this is the draft creation anchor for capture_minutes
task_date: <today>
status: draft
superseded_by:
backfilled: false
tags: []
entities: []
related: []
covers_files: []
verified_at_commit:
capture_minutes:                 # left empty: a draft has measured nothing yet
---
```

- If you can write files directly: write `sessions/<id>.md` there with that front
  matter plus the `## Pre-reg` section, exactly as composed above.
- If your runtime exposes the `engmem_create_draft` MCP tool instead of a shell: call
  it with `id` set to `<id>` and `content` set to the exact text above (the front
  matter block, then the `## Pre-reg` section). It never overwrites an existing
  document, so a call that fails because the id is already taken means the id itself
  needs to change, not something to retry as-is.
- If you can do neither: skip creating the draft, say so in your final report, and
  keep going — steps 3–6 below are not blocked by it (a search below with no
  `session_id` is still a search).

## 3. Search for prior context

- If you can run shell commands: run
  `engmem search "<key terms from the task>" --session <the draft id from step 2>` directly.
- If your runtime exposes the `engmem_search` MCP tool instead of a shell: call it with the
  same key terms as `query` and the draft id as `session_id`.
- If you can do neither (no shell access in this environment): print the exact
  `engmem search "..." --session <id>` command for the user to run themselves, and ask them
  to paste the output back into the conversation. Do not silently skip this step just
  because you can't run it yourself.

**Always pass the session id.** It is what ties a search to the document this task will
produce: without it the search is logged, but nothing can ever connect what was retrieved
to what was actually reused, and the row counts as unattributed at review time.

## 4. Load what was found — and measure what it cost

From the search results, load at most 3 documents, plus each of their `related` documents
at depth 1 only (do not follow related-of-related). "Load" means read enough of each
document to actually use it — at minimum its Cold-start primer and Decision Log.

**Read sections, not whole files.** A prior document can be tens of kilobytes; the part
that answers the current task is usually one section. Pulling the whole file in is the
single biggest way engmem can cost more context than it saves.

Then note three numbers for `/engmem.save` — they are the evidence for whether memory is
cheaper than rediscovery:

- `context_bytes: <n>` — bytes of prior-document text you actually pulled into context
  (search output plus the parts of documents you read). Count it; do not estimate.
- `answer_steps: <n>` — how many tool calls you needed **after** the search to reach the
  answer. If the recovered context answered it outright, this is near zero, and that is
  precisely the saving being measured.
- `answer_tokens: <n>` — only if your runtime actually reports it. Omit the line rather
  than guessing: a fabricated number here corrupts the one analysis this project exists
  to produce.

## 5. Record the Search Trace

Note which path step 3 actually took:

- `shell` — you ran the search yourself, whether through the CLI or the `engmem_search`
  MCP tool (regardless of whether it found anything); both write the same telemetry line
- `paste` — the user ran it and pasted the output back (regardless of the result)
- `miss` — the search never ran at all (skipped, blocked, or the user declined the
  paste-bridge)

A search that ran but found nothing is still `shell` or `paste` — the search *result*
(`prior context: none found`) is already captured by telemetry; the Trace records the
*execution path*, which is what the dogfooding smoke (S7) verifies for each agent.

This value gets attached to the session document at save time; you don't need to write it
into the draft file now, just remember it for `/engmem.save`.

## 6. Report, then answer in the same message

Open your reply with what you recovered: name the documents you loaded and, in one line
each, what's relevant about them — or, if nothing came back, say plainly
`prior context: none found`.

Then immediately answer the question or propose the plan, **in that same message**. The
report is a header on your answer, not a checkpoint the user has to clear. Do not send a
message whose only content is the ritual's progress.

If the answer changed because of what you recovered, say so in one line, naming the
document and the specific thing you took from it. Vague credit ("gave useful context") is
worth nothing at review time; the named artifact is what the Reuse Log will have to quote.
