---
description: Start an engineering task on a codebase with engmem — recover prior context before planning
argument-hint: <task description>
---

# /engmem — start a task

Task description: "$ARGUMENTS"

Follow these steps in order. **Never make the user wait on the ritual**: they asked for
something, and steps 0–5 exist to serve that request, not to gate it. If any step cannot
be completed, record what happened and keep going — never withhold the answer or the plan
because a step did not work out.

## Scope

From the task description alone, before step 0: if it is not engineering work on a codebase or
system (changing, debugging or investigating code, configuration or infrastructure, or choosing
an approach), reply `engmem: skipped — not an engineering task` on one line and answer; no
draft, no search. Out of scope: study or practice (algorithms, interviews), writing, marketing
or product reviews, personal tasks. When unsure, continue.

## 0. Mode — daily or research

Find this session's mode before anything else.
With a shell, run `engmem mode show`: its first line is `mode: daily` or `mode: research`.
Otherwise read the one word in `$XDG_CONFIG_HOME/engmem/mode` (default `~/.config/engmem/mode`;
`%APPDATA%\engmem\mode` on Windows); when that file does not exist, the mode is `daily`.
If the mode cannot be read (an `error:` line), tell the user and continue as `daily`: a daily
session is never counted in the experiment, so it cannot corrupt it.

- **daily** — skip step 1: no Pre-reg, no baseline sub-agent. The draft records `mode: daily`
  and has no `## Pre-reg` section.
- **research** — run step 1: the research comparison needs its baseline. The draft records
  `mode: research`.

The mode is written once, into the draft's front matter in step 2, and never changed after:
if the user switches mode during the task, the switch applies to the next session.

## 1. Pre-reg — generated, never asked of the user (research mode only)

**Do not ask the user anything in this step.** Do not ask them to describe their approach,
do not wait for a reply, do not offer to skip the ritual. The baseline is engmem's
bookkeeping, not theirs.

Produce a naive baseline: 2–3 lines describing how this task would be approached with **no
prior context at all**. Two ways, in order of preference:

- **Preferred — a separate pass that cannot see the store.** If you can spawn a sub-agent
  or an isolated task, give it *only* the user's task description. It must not read the
  engmem store, must not run `engmem search`, and must not be told that engmem exists.
  Ask it for 2–3 lines: "how would you approach this?" Use its answer verbatim.
- **Fallback — write it yourself, right now.** If you cannot spawn anything, write those
  2–3 lines yourself **before** running any search and before opening any store document.
  Write them from the task description only. After you read a prior document, the baseline
  is worthless.

This is a *baseline for comparison*, not a plan you must follow. Do not defend it, do not
refine it, and do not show it to the user for approval — write it down and move on.

Record which way you got it, as the last line of the Pre-reg section:
`pre-reg source: sub-agent` or `pre-reg source: self (no sub-agent available)`. After the self
line's closing bracket, say on the same line whether project instructions or memory files were
already in your context: `pre-reg source: self (no sub-agent available); memory in context`.

**When no baseline can be had.** If neither way works before the search — the sub-agent
failed and you have already read a store document in this conversation, so anything you wrote
now would be contaminated — do not write a baseline. Record the reason instead, in one line, as
`baseline_unavailable: <reason>` in the draft's front matter (step 2). Search and save go on as
usual.

If the runtime reports the sub-agent's tokens or seconds, record them once the draft exists
(step 2): `engmem cost record-baseline <draft id> --tokens <n> --seconds <s> --call-id <the
runtime's id for the call, if it shows one>` (MCP: `engmem_record_baseline`). Leave out what was
not reported, and never estimate. Nothing reported means nothing to record.

**Why this exists** (so you do not "optimise" it away): the experiment compares this
uncontaminated baseline with the plan made *after* prior documents are loaded. Written after
the documents are read, it measures nothing.

## 2. Create the draft immediately

engmem writes the draft itself, so you never write a file in the store. With a shell, run
`engmem store show` once first. When the first line is `store:`, that path is the store engmem
writes to, and report any `error:` line further down to the user. When the first line is
`error:`, report it and create no draft: a draft in another directory is lost to every later
search. Compose the draft as soon as step 0 (and, in research mode, step 1) is done: the
**complete** front matter below, with placeholder values but every field present, and `mode`
from step 0. In research mode it is followed by a `## Pre-reg` section containing the baseline
from step 1 plus its `pre-reg source:` line, or, when step 1 found no baseline, the front matter
carries `baseline_unavailable: <reason>` and there is no Pre-reg section. In daily mode the
draft is the front matter alone. Leave the other body sections for `/engmem.save`. Empty
`tags`/`entities` lists are expected in a draft; a warning about empty entities is normal.

```yaml
---
id: <YYYYMMDD>-<slug>          # lowercase letters, digits and hyphens
title: <short title from the task description>
date: <today>                   # day resolution: not an anchor for capture_minutes
task_date: <today>              # equals date; differs only in a backfilled record
status: draft
superseded_by:
backfilled: false
tags: []
entities: []
related: []
covers_files: []
repos: [<repository>]            # this checkout's top-level directory name; [] if unknown
verified_at:                     # left empty: filled per repository at save
capture_minutes:                 # left empty: a draft has measured nothing yet
mode: <daily|research from step 0>   # the one word; engmem refuses this placeholder
---
```

- If your runtime exposes the `engmem_create_draft` MCP tool: call it with `id` set to `<id>`
  and `content` set to the exact text above (the front matter block, then any `## Pre-reg`
  section). Prefer it to the shell when you have both.
- Otherwise, with a shell: pipe that same text to `engmem draft create <id>` with a quoted
  delimiter, so nothing in the text is expanded or run. In a POSIX shell:
  `engmem draft create <id> <<'EOF'` … `EOF`, with a delimiter no line of the text is. In
  PowerShell, first `$OutputEncoding = [System.Text.UTF8Encoding]::new($false)`, since Windows
  PowerShell 5.1 otherwise sends every non-ASCII character as `?`, then
  `@'` … `'@ | engmem draft create <id>`.
- Do not write `sessions/<id>.md` yourself, even though you could: these make the checks a plain
  file write skips.
- Either way: it refuses a `mode` other than the configured one and names the configured mode;
  take that one and compose again. It never overwrites an existing document, so a refusal
  because the id is already taken means the id itself needs to change, not something to retry
  as-is. Its output names the draft's `version`; keep it, because `/engmem.save` passes it back
  when it completes the draft.
- If you can do neither, or it fails for any reason other than the two above: create no draft
  and write no file yourself. Say so in your final report, and keep going — steps 3–6 below are
  not blocked by it.

## 3. Search for prior context

- If step 2 could not create the draft, there is no id to pass: run the search without
  `--session` / `session_id`, and say so in your final report — an unattributed row is the
  cost of a missing draft, not a free pass. Never pass an id whose document was never
  written: that row is attributed to a document that does not exist, and engmem's audit
  reports it as an orphan. Every bullet below assumes step 2 produced a draft.
- If you can run shell commands: run
  `engmem search "<key terms from the task>" --session <the draft id from step 2>` directly.
  **Always pass `--session`** — on every search you run in this task, not just the first.
  Without it, nothing connects what was retrieved to what was reused.
- If your runtime exposes the `engmem_search` MCP tool instead of a shell: call it with the
  same key terms as `query`. **Always pass the draft id from step 2 as `session_id`** — on
  every call, `engmem_search_by_role` included, for the same reason as `--session` above.
- If you can do neither (no shell access in this environment): print the exact
  `engmem search "..." --session <id>` command for the user to run themselves, and ask them
  to paste the output back into the conversation. Do not silently skip this step.

If no result is relevant, run one more search with other terms: synonyms, or the names of the
components and files involved. Pass the same session id. Run at most two searches before step 4.

Search the whole store unless the user asks you to limit it: the document worth finding often
comes from another repository. Only on that request, and for that one search, add `--repo <name>`
(repeatable), `--unscoped` or `--all-repos` (MCP: `repo`, `repos`, `unscoped`, `all_repos`).
`engmem search --help` and the MCP tool's parameters say what each one does. A scoped result
opens with a `scope:` line, and a miss under it says nothing about the rest of the store.

## 4. Load what was found — and measure what it cost

From the search results, load at most 5 documents in all, `related` documents included. Take
the found documents first. Then take their `related` documents at depth 1, in the order the
found documents list them; do not follow related-of-related. "Load" means read enough of each
document to actually use it — at minimum its Cold-start primer and Decision Log.

Read them through engmem, so the read is counted toward this session's cost: run
`engmem read <id> --role <role> --session <the draft id from step 2>` (for example
`--role primer`, then `--role decisions`; leave `--role` out for the whole document), or call
the `engmem_read` MCP tool with `id`, `role` and `session_id`. If step 2 could not create the
draft, leave out `--session` / `session_id`, as in step 3: never pass an id whose document was
never written. A document you open as a file instead is not observed.

Keep the `cite as (Reuse Log prior-doc):` line of every search and every read: `/engmem.save`
cites each quote by the `<id>@<version>` of the response the quoted text came from — the read's
line when you quote what a read returned, the search's when you quote the search result. A
document edited between the search and the read has a different version.

**Read sections, not whole files.** A whole file is the biggest way engmem can cost more
context than it saves.

engmem measures the bytes each search and read delivers; do not count them. Note two numbers
for `/engmem.save` yourself:

- `answer_steps: <n>` — how many tool calls you needed **after** the search to reach the
  answer.
- `answer_tokens: <n>` — only if your runtime actually reports it.

Nothing writes them down for you. `/engmem.save` writes each one only if you still have it.
If you lost one, omit it: never reconstruct it.

## 5. Record the Search Trace

Note which path step 3 actually took:

- `shell` — you ran the search yourself, whatever it found: `shell` is the value for both the
  CLI and the `engmem_search` MCP tool
- `paste` — the user ran it and pasted the output back (regardless of the result)
- `miss` — the search never ran at all (skipped, blocked, or the user declined the
  paste-bridge)

You need not write it into the draft now. With a shell, `/engmem.save` reads `shell` back from
engmem if a search was recorded. Otherwise it writes the value only if you still have it; never
reconstruct it. It is saved alone on its own line, because engmem's audit reads a line that is
exactly the value:

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
document and the specific thing you took from it. Never give vague credit ("gave useful
context"): the Reuse Log will have to quote the named artifact.
