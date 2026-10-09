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

## 0. Mode — daily or research

Find this session's mode before anything else. With a shell, run `engmem mode show`: its first
line is `mode: daily` or `mode: research`. Through the engmem MCP prompt, the line above this
template names it. Otherwise read the one word in `$XDG_CONFIG_HOME/engmem/mode` (default
`~/.config/engmem/mode`; `%APPDATA%\engmem\mode` on Windows); when that file does not exist,
the mode is `daily`. If the mode cannot be read (an `error:` line), tell the user and continue
as `daily`: a daily session is never counted in the experiment, so it cannot corrupt it.

- **daily** — skip step 1: no Pre-reg, no baseline sub-agent. The draft records `mode: daily`
  and has no `## Pre-reg` section.
- **research** — step 1 is the Gate 1 protocol, unchanged. The draft records `mode: research`.

The mode is written once, into the draft's front matter in step 2, and never changed after:
if the user switches mode during the task, the switch applies to the next session.

## 1. Pre-reg — generated, never asked of the user (research mode only)

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

**When no baseline can be had.** If neither way works before the search — the sub-agent
failed and you have already read a store document in this conversation, so anything you wrote
now would be contaminated — do not write a baseline. Record the reason instead, in one line, as
`baseline_unavailable: <reason>` in the draft's front matter (step 2). Search and save go on as
usual; the session is reported as an incomplete observation, apart from the valid experimental
group and from every baseline comparison.

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

The engmem store is the path on the first line of `engmem store show` (`store: <path>`);
run it once before you write. When the first line is `store:`, use that path, and report any
`error:` line further down to the user. When the first line is `error:`, report it and write
the draft nowhere — another directory is another store, and the draft would be lost to every
later search. Without a shell, the store resolves in this order: `$ENGMEM_HOME` if
set, otherwise the one path saved in `$XDG_CONFIG_HOME/engmem/store` (default
`~/.config/engmem/store`; `%APPDATA%\engmem\store` on Windows) if that file exists, otherwise
`~/Developer/engmem`. Compose the draft as soon as step 0 (and, in research mode, step 1) is
done: the **complete** front matter below — placeholder values, but every field present, so
the document is valid to the parser from the moment it exists — with `mode` from step 0. In
research mode it is followed by a `## Pre-reg` section containing the baseline from step 1
plus its `pre-reg source:` line, or, when step 1 found no baseline, the front matter carries
`baseline_unavailable: <reason>` and there is no Pre-reg section. In daily mode the draft is
the front matter alone. Leave the other body sections for `/engmem.save`; the empty
`tags`/`entities` lists are expected in a draft (the parser reports empty entities as a warning,
not an error — that's normal until save fills them in), and an empty daily draft draws no
warning at all.

```yaml
---
id: <YYYYMMDD>-<slug>          # or <story-id>-<slug>; must equal the filename stem
title: <short title from the task description>
date: <today>                   # day resolution: not an anchor for capture_minutes
task_date: <today>
status: draft
superseded_by:
backfilled: false
tags: []
entities: []
related: []
covers_files: []
verified_at:                     # left empty: filled per repository at save
capture_minutes:                 # left empty: a draft has measured nothing yet
mode: <daily|research from step 0>   # write the one word; left as is, it counts nowhere
---
```

- If you can write files directly: write `sessions/<id>.md` there with that front
  matter plus, in research mode, the `## Pre-reg` section, exactly as composed above. Write it only if no
  file by that name exists yet; if one does, pick a new id — never overwrite it.
- If your runtime exposes the `engmem_create_draft` MCP tool instead of a shell: call
  it with `id` set to `<id>` and `content` set to the exact text above (the front
  matter block, then any `## Pre-reg` section). It refuses a `mode` other than the
  configured one and names the configured mode; take that one and compose again. It never
  overwrites an existing
  document, so a call that fails because the id is already taken means the id itself
  needs to change, not something to retry as-is. Its result names the draft's
  `version`; keep it, because `/engmem.save` passes it back when it completes the draft.
- If you can do neither: skip creating the draft, say so in your final report, and
  keep going — steps 3–6 below are not blocked by it.

## 3. Search for prior context

- If step 2 could not create the draft, there is no id to pass: run the search without
  `--session` / `session_id`, and say so in your final report — an unattributed row is the
  cost of a missing draft, not a free pass. Never pass an id whose document was never
  written: that row is attributed to a document that does not exist, and the Gate 1 audit
  reports it as an orphan. Every bullet below assumes step 2 produced a draft.
- If you can run shell commands: run
  `engmem search "<key terms from the task>" --session <the draft id from step 2>` directly.
  **Always pass `--session`** — on every search you run in this task, not just the first.
  It is what ties a search to the document this task will produce; without it the row is
  logged unattributed and nothing can connect what was retrieved to what was reused.
- If your runtime exposes the `engmem_search` MCP tool instead of a shell: call it with the
  same key terms as `query`. **Always pass the draft id from step 2 as `session_id`** — on
  every call, `engmem_search_by_role` included, for the same reason as `--session` above.
- If you can do neither (no shell access in this environment): print the exact
  `engmem search "..." --session <id>` command for the user to run themselves, and ask them
  to paste the output back into the conversation. Do not silently skip this step just
  because you can't run it yourself.

Search the whole store unless the user asks you to limit it to one repository: a story from
another repository is often the one worth finding. When they do, add `--repo <name>` (MCP:
`repo`) with the name their documents write in `repos`; `--unscoped` (MCP: `unscoped: true`)
finds the records linked to no repository. If they name several repositories, repeat
`--repo` once per name (MCP: `repos: [<name>, <name>]`); if they want the whole store with
each result's repository shown, add `--all-repos` (MCP: `all_repos: true`). Use these only
when the user asks, for that one search; the next search goes back to the plain default. A
scoped result opens with a `scope:` line, and a miss under it says nothing about the rest of
the store.

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
into the draft file now, just remember it for `/engmem.save`. It is saved alone on its own
line, because the audit reads a line that is exactly the value:

```
## Search Trace

shell
```

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
