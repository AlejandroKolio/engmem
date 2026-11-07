# Constraints on the prompt templates

The templates — `src/engmem/templates/engmem.start.md`, `engmem.save.md`,
`engmem.save.quick.md` — are the instructions, and they are plain markdown you can read. This
file records the rules they must satisfy, which are not visible from inside any one of them.
Command-level behaviour lives in `ENGMEM-SPEC.md` §6.

## The Pre-reg is evidence, and must not be overclaimed

The gap between the naive pre-registered plan and the final one is a **secondary** signal,
never the verdict. A plan written blind diverges from the final one for many reasons, and
reading the repository is one of them. The verdict is carried by the quoted rows of the
Reuse Log (`ENGMEM-SPEC.md` §1).

**No template may present that divergence as evidence the tool works.** At review time it
is read only from sessions recording `pre-reg source: sub-agent`.

## The Pre-reg must never be asked of the human, and must never gate the answer

In research mode it is obtained before any search, from a sub-agent that knows only the task description —
or, failing that, written by the agent itself before it opens a single prior document.

A template that prompts the user for it, waits on them, or refuses to proceed without it
has broken the experiment twice over: it contaminates the baseline with the user's own
thinking, and it turns the capture ritual into a precondition for getting an answer.
Recording *which* source was used is what keeps the two cases distinguishable later.

A self-written baseline is not blind either: project instructions, memory files and the
conversation can already be in the agent's context before `/engmem` runs. The protocol stays as
it is (2026-11-07). The agent writes the baseline from the task description only, and says on
the `pre-reg source: self (no sub-agent available)` line, after its closing bracket, whether
project instructions or memory were already in context. The reviewer then knows how blind the
baseline was. No code parses the line: `gate1_audit` looks for the `## Pre-reg` section, and the
review reads the line by its prefix, so the prefix stays exactly as it was.

## The mode is read first, and recorded once (US-08)

`engmem.start.md` opens with step 0, before the Pre-reg it decides: the mode comes from the first
line of `engmem mode show`, from the first line of the `engmem` MCP prompt, or, with neither, from
the one word in the mode file, `daily` when there is none. Numbered 0 so steps 1–6 keep the
numbers other documents cite.

- **daily** skips step 1 entirely: no Pre-reg, no baseline sub-agent, and the draft is the front
  matter alone, `mode: daily`. AC-08.1 asks that ordinary work neither waits on nor pays for the
  experiment, and a daily session is outside the count anyway (`contracts/gate1.md`, "Modes").
- **research** runs step 1 exactly as before. When no uncontaminated baseline can be had before
  the search, the agent writes none and records `baseline_unavailable: <reason>` instead. Search
  and save continue; the report keeps the session apart (AC-08.5). A baseline written after a
  store document was read would be counted as a real one, and that is worse than a recorded gap.
- **unreadable** is told to the user and run as daily: a daily session cannot put a wrong
  observation into the experiment, so it is the one safe guess.

The draft's YAML shows `mode: <daily|research from step 0>`, not a real value: a placeholder
that is a valid word would be copied as a claim. `engmem draft create` and
`engmem_create_draft` refuse this one and name the configured mode, and the comment beside it
says so. It said a copied placeholder "counts nowhere" until 2026-11-07, which is now true only
of a document written by hand. That reaches no such check; there it is an unrecognized mode and
counts nowhere (`contracts/mcp-server.md`, "A hand-written document is unguarded").

The mode is written once, into the draft, and the save templates copy `mode` and
`baseline_unavailable` unchanged — never added, edited or dropped, whatever the saved mode is by
then. `engmem draft complete` and `engmem_complete_draft` both enforce that, as they enforce the
Reuse Log rules: one writer serves both surfaces (`contracts/mcp-server.md`, "One writer for
every surface"). So the full save states the refusals once, for both, and asks no self-check
on the shell. Both save templates carry the Pre-reg over only when
the draft has one. The short capture's example record is a daily one, with no Pre-reg, since a
short save is the daily path the story is about.

## Each target gets the branches it can take (2026-11-07)

A template is written once and delivered two ways. `engmem install` writes it as a file for
claude, copilot-ide, copilot-cli and codex. The MCP server returns it from `prompts/get`, to any
client that connects, Claude Desktop, ChatGPT and Codex among them. Both pass it through
`template_render.render` first. That function leaves out a marked branch only when the target
can never take it. A branch is dropped only when it is provably never used. Otherwise the cost is
an agent that has no instruction for the situation it is in.

**What each target cannot take.**

| Branch | File copy | MCP prompt |
|---|---|---|
| shell, POSIX and PowerShell | kept | kept |
| MCP tools | kept | kept |
| paste bridge, no-shell fallbacks | kept | kept |
| step 0, "the line above this template" (`mcp`) | dropped | kept |
| step 0, `engmem mode show` and the mode file (`files`) | kept | dropped |
| step 1, the Pre-reg (`research`) | kept | dropped from a daily start prompt |

- **A file copy is never the MCP prompt**, so the sentence about the line above it is never
  true there. It cannot know the mode either: `engmem mode set` changes the mode after install,
  and nothing rewrites the copy then. So it keeps step 0's ways to find the mode, and step 1.
- **A file copy keeps the MCP branch.** Claude Code, VS Code and Codex can all have engmem's MCP
  server as well as a shell. The templates prefer the MCP tool for writes when both are there.
- **No copy drops a shell branch by the install-time OS.** One `.github/prompts/` is committed and
  read on every teammate's OS. A home directory can be shared between machines. Claude Code
  on Windows runs a POSIX shell, and WSL and Git Bash are POSIX shells on Windows. The install-time
  OS proves nothing about the shell that runs the command.
- **The MCP prompt keeps the shell and the paste bridge.** A client of the MCP server can have a
  shell too: Codex, or Claude Code and VS Code with engmem added as a server. Then the shell is
  the only way to read the clock for `capture_minutes`. A `--read-only` server lists only the
  searches, so its client has no MCP tool to create or complete a draft. Some clients also let
  the user turn single tools off. So engmem's MCP tools may be missing, and the no-draft and
  paste branches stay.
- **The MCP start prompt knows the mode.** `_mode_line` reads it on every `prompts/get` and
  writes it as the first line. Step 0's other ways to find the mode are then never taken. A
  daily start prompt also leaves out step 1, which a daily session skips. The read that names
  the mode on the first line also decides the rendering, so the two cannot disagree. An
  unreadable mode is daily in both. The save prompts are rendered without a mode: a save
  follows the draft's mode, and the saved mode may have changed since the draft was created.
  For that reason a `research` branch is allowed only in the start template.

**The mechanism.** A branch is a run of whole lines between two marker lines,
`<!-- engmem-branch: files -->`, `mcp` or `research`, and `<!-- engmem-branch: end -->`. Markers
are matched only as a whole line. They do not nest, and an unknown name, an unclosed branch or a
stray end raises a `ValueError` that names the line. A broken marker otherwise drops or keeps a
branch in every copy without a word. A rendered copy never holds a marker line. The source stays
readable as one document, and the tests that read the source still see every branch. The MCP
server renders before it puts the user's `$ARGUMENTS` text in, so a marker in that text is never
parsed.

**Stamps and upgrade.** The stamp's digest covers the body install writes, so it covers the
rendered copy (`contracts/install.md`, "An installed template is engmem's until someone edits
it"). An unedited copy from an earlier release still matches its own stamp's digest, so the next
install replaces it with the rendered one. A user edit still breaks that match and is kept as a
conflict. `RELEASED_DIGESTS` lists the bodies v0.1.1–v0.1.4 wrote. They stay frozen: rendering
changes only what this version writes. `template_stamps.WORKFLOW` stays 1. Rendering drops no
step a target can take, and it adds no step the runtime refuses.

**What every copy must still hold.** The guards on the save templates hold on every copy,
since the save templates carry no marker: the short capture's example record, the lesson block,
the clock readings. The session id inside each search bullet, the no-draft branch first, and the
other step 3–6 guards are outside any branch, so they hold on every start copy too. The guards on
step 1, such as the Pre-reg source line and the single baseline path, read the source. Every
copy that keeps step 1 is rendered from that source, and a daily MCP prompt has no step 1 to
guard. `tests/test_template_render.py` checks each target's diff from the full source: a file
copy loses only the `mcp` lines, and an MCP prompt only the `files` lines and, when daily, the
`research` lines.

## Templates may not grow the tool

No template introduces a CLI flag, field, or command that this feature's contracts do not
already define. `ENGMEM-SPEC.md` §9 is a standing rejection list. If a template's
behaviour appears to need something from it, work stops and the author is asked
(`ENGMEM-SPEC.md` §10, principle IX) — it is not built anyway.

## A template step runs from the installed package

The wheel ships `src/engmem` only (`pyproject.toml`, `[tool.hatch.build.targets.wheel]`), so a
user installed from PyPI has no source checkout and no `tools/`. No template step runs a file
from either. The quote check reaches the agent through completion instead: refusals before the
write, warnings after it (`contracts/mcp-server.md`, "Reuse Log rows — refuse, then warn"),
each naming the defect and the row. `tools/verify_citations.py` and `tools/gate1_report.py`
stay store-wide review tools for a maintainer with the checkout (`docs/guide.md`). No template
cites a file the wheel leaves out either: not `ENGMEM-SPEC.md`, a smoke id or a story id. An
installed agent cannot open them, so a template states the one fact it needs instead. The
contracts keep their references, since their readers have the checkout. Pinned by
`tests/test_docs_match_the_code.py`: the steps on every template, the cited files on every
installed copy.

## Writes go through engmem (US-24)

The start and save templates create and complete the session document only through engmem:
`engmem draft create|complete` with a shell, `engmem_create_draft|complete_draft` over MCP.
Neither tells an agent to write `sessions/<id>.md` itself, even when it could. A plain file
write skips the checks those commands make (`contracts/mcp-server.md`, "One writer for every
surface"), and the guarantee then depended on which client happened to run the task. The
start draft records the working repository in `repos` next to `id` and `mode`. It spells it as
the full save does, the checkout's top-level directory name, since `engmem search --repo` filters
on that spelling and the agent has seen no document yet to copy one from.

When neither channel is there, or a write fails for a reason the document cannot fix, there is
no silent fallback:
- start creates no draft and says so;
- save tells the user the document was not saved and why, and keeps the text in the reply.

Only an explicit request from the user makes the agent write the file by hand, and the agent
then says it was saved outside engmem's checks. The full save spells out how the store path
resolves for that one case. Start no longer needs it, because engmem resolves the store when it
writes.

## Every rewrite names the version read (US-25)

Both rewriting calls of the save now require the version the agent read
(`contracts/mcp-server.md`, "The version is required"). Completion already passed the version
create gave. The template used to allow leaving it out when the agent no longer had it, and
that was the one path where a changed draft was overwritten. It now says to pass an empty
version and read the refusal, which carries the current draft and its version. Empty, not
omitted: the schema lists the argument as required, and a client that enforces that would
never send the call, so the refusal would never come back. The server treats blank as missing.

The full save's supersede step offers `engmem_mark_superseded` first, ahead of a direct file
edit. The version it passes is the one the new document was built on: the `<id>@<version>` row
of its Reuse Log, as the `cite as` line of a search or `engmem_read` gave it. A fresh read just
before the call would hand over a version only seconds old, and the check would then cover
those seconds and not the time since the agent built its document, which is when a colleague's
change goes missing. So the template forbids re-reading for a newer version, and reads now
only when the agent has no version from building the document. A refusal means someone changed
the older document after that read, possibly with a rule the new document drops. The step
tells the agent to read what changed and tell the user, and to call again only if the new
document still replaces the old one. The direct edit stays for an agent without the tool, a recorded limit until the
CLI supersede story (`contracts/mcp-server.md`, "One writer for every surface"). The template
tells that agent to read the document again just before it edits it, since nothing checks the
edit.

## The session id belongs inside the search instruction

`engmem.start.md` step 3 states the session id in the CLI bullet and in the MCP bullet
themselves, not as a paragraph after them: an agent acts on the one bullet that matches its
runtime and stops reading, so a requirement placed after the bullets is one it has already
walked past. For the same reason step 2's "If you can do neither" fallback says nothing about
a session-less search still being a search: a draft that could not be created is a thing to
report, never a sanctioned way to log an unattributed row.

The id is composed in step 2 *before* the draft is written, so an agent whose write failed
still holds the string while both bullets tell it to always pass one. Step 3 therefore opens
with the no-draft branch, ahead of the bullets it qualifies: search without the id and report
the missing draft. Passing the composed id anyway is the one outcome named as forbidden. Such
a row is attributed to a document that does not exist, and `gate1_audit` reports it as an
orphan (`orphan_session_ids` / `orphan_row_count`, printed by `tools/gate1_report.py` as
"telemetry rows whose session_id matches no document in this store"), whereas a row carrying
no id at all is simply absent from that join (`telemetry.read_session_rows` keeps only rows
naming a non-blank `session_id`).

The no-draft branch stays first, for the reason in this section's first paragraph (review of
2026-11-07).

Nothing about the id is enforced — `session_id` stays optional in the MCP schema and
`--session` optional on the CLI, because a search before any draft exists is legitimate. The
MCP tool surface asks for it in three places (`contracts/mcp-server.md`, "`session_id`:
optional to the schema, asked for in every wording"); the CLI has only `--session`'s argparse
help (`cli.py`), so on that path the template is the one place that asks before the search —
which is why its wording is a constraint and not a nicety. Both channels also end an
unattributed search with a trailing `note: unattributed search — pass ...` line
(`telemetry.UNATTRIBUTED_CLI_NOTE` / `UNATTRIBUTED_MCP_NOTE`; the recorded reversal in that
same section), a reminder at the point of use that the template's wording cannot give.

## The start's budget: two searches, five documents (2026-11-07)

`engmem.start.md` step 3 runs one more search, with other terms and the same session id, when no
result of the first is relevant: a document that says the same thing in other words is a
wording gap that lexical search cannot close, and synonyms or the component and file names can.
It stops at two searches before step 4, so the retry cannot turn into a search loop whose output
is paid for in context.

Step 4 loads at most 5 documents in all, `related` documents included: the found ones first,
then their related ones in the order the found documents list them. A `related` list has no
length limit, so the old rule of 3 found documents plus each one's related documents had no
total, and a rich store pulled 3 + 3×N documents into every start. The cap is stated once, so no
wording leaves a related read uncounted.

Step 3 keeps the default, the whole store, with its one-sentence reason, and names the scope
options in one line: `--repo` (repeatable), `--unscoped`, `--all-repos`, and the MCP tool's
`repo`, `repos`, `unscoped` and `all_repos`. Every `/engmem` reads that paragraph and few use
it, so what each option does is left to `engmem search --help` and the tool's own schema
descriptions (`mcp_tools.tools_list`). The warning that a scoped miss says nothing about the rest
of the store stays, because that is the misreading a scoped result invites.

## A search over MCP is still `shell`

The Search Trace records how the search was *executed*, not how it travelled. An agent
calling the `engmem_search` MCP tool ran the search itself, and writes the same telemetry
line as a shell invocation, so both are `shell`. Only a human pasting output back is
`paste`, and only a search that never happened is `miss`. A search that found nothing is still
`shell` or `paste`: telemetry already records the result (`prior context: none found`), and the
Trace records the execution path. The start template says "whatever it found" and "regardless
of the result" on the two bullets, and no longer spells out this reason.

## The short capture keeps the schema it has

`engmem.save.quick.md` is the short capture (US-07): the decision, its reason, the rejected
alternative, the source and a short context for the next task, in one preview. It replaced
the earlier quick save, which wrote three unstructured bullets and no primer, so its records
showed no preview line in search. One command stays one command: a second short-save template
would leave two near-identical paths to keep in step, and the story asks for one preview,
not one more choice.

The five components map onto roles that already exist. The first four are labelled lines of
`## Decision Log` (`decisions`), the fifth is `## Future LLM Context (cold-start primer)`
(`primer`). No front matter field and no canonical role was added. A new role would grow
`sections.CANONICAL_ALIASES`, the `--role` vocabulary and the role count the docs quote, and
it would split one decision across sections that search scores separately; a document scores
by its single best section (`contracts/scoring.md`). Kept together, the decision and its
reason sit in one section, and a query naming either finds it. Lessons Learned is written only
for a pitfall actually hit, so the record holds nothing that was filled in for form's sake.

The record is a valid `active` document for every existing check, with no new code:
`spine.load_store` reads it without a warning when `entities` is filled, which the template
asks for; `engmem_complete_draft` completes it under `expected_version`; `gate1.evaluate`
counts its `Prior docs used: none.` as a clean none report; and `gate1_audit` finds its
Pre-reg, Reuse Log and Search Trace. `tests/test_short_capture.py` runs the template's own
example record through all of them, and checks that every installed copy (claude, copilot-ide,
copilot-cli, codex) and the `engmem-save-quick` MCP prompt carry that example unchanged.

### Absence is written, in one fixed wording

A component the session's material (the conversation, the diff, the git history) does not
contain keeps its label and reads `not stated in the available material.` A dropped line
cannot be told apart from a forgotten one, and a filled-in guess is the invention AC-07.2
forbids; a Source in particular is copied as written, never built from a pattern. One wording
for every absent component keeps the marker a single phrase that a reader and a test both
recognise. Search reads it as ordinary body text, and has no stopword list to do otherwise: a
query that contains `available` or `material` matches the marker like any other prose, at the
low weight words shared by many records get. What the marker never carries is a decision
term, so a query naming one record's decision still ranks that record first
(`test_ac_07_2_marker_and_label_words_do_not_outrank_the_decision`). The labels are indexed
body text too, shared by every short record: a query made only of label words
(`decision reason`, `rejected alternative`) matches all of them alike. Handling such a weak
match is US-14's question, not this template's. The same wording marks a lesson's missing
`Failed because:`, `Fixed by:` or `Files:` in both save templates.

### One confirmation, and a decline publishes nothing

The preview is the finished document. A confirmation publishes it as shown; edits are applied
and published without showing it again, since asking twice about the same data is the cost
this path removes. The one second showing is a refused `expected_version`: the document then
changed under the agent, and what gets published is no longer what the user saw.

A decline, or a reply the agent cannot read as confirmation or edits, publishes nothing: an
unconfirmed document is harder to undo than a draft left in place. The draft is kept, not
removed. It is outside search already (US-01, `contracts/scoring.md`), and it is the document
this session's search rows are attributed to: removing it would turn them into
`gate1_audit` orphans (`orphan_session_ids`). `/engmem.save` or `/engmem.save.quick` can
finish it later.

### What the acceptance transcript proves

`docs/design/acceptance/us-07-short-capture.md` is a scripted session, a confirmed and a
declined preview, that `tests/test_short_capture.py` replays against a fresh store through the
MCP server. It proves the tool side: the calls the template prescribes succeed in that order,
the published content is the confirmed preview, nothing is published after a decline, and the
declined record is not found. The agent's turns are written by hand, so it does not prove that
a real agent produces them from the template; that recorded session is owner-side acceptance
(E-05).

## A lesson is tried, failed, fixed, where (US-28)

Both save templates write a pitfall as one bullet with four labelled parts: `Tried:`,
`Failed because:`, `Fixed by:` and `Files:`. Those are what a later agent needs to avoid the
same error: what not to repeat, why it fails, what to do instead and where. Free prose made
the reader dig the fix and the files out of paragraphs.

- **No code.** Nothing precedes the first bullet, so the section snippet (its first sentence,
  `output._section_snippet`) starts with `- Tried:` and, with the full stop the template asks
  for, ends before `Failed because:`. A `--role lessons` search shows what was tried with no
  change to the output.
- **`Tried:` is always filled.** A pitfall hit means something was tried, and an absent one
  would make the snippet the absence marker. The other three parts use the marker above.
- **`Files:` stays out of `covers_files`.** That field means the files the decision rests on,
  and the snapshot line compares them between the record's `verified_at` commit and the
  checkout's `HEAD` (`contracts/provenance.md`); lesson files would flag a decision as possibly
  stale for unrelated commits. `Files:` uses the same path form.
- **`None hit.` stays, and nothing is rewritten.** The change is to what a new save writes;
  search reads both forms as body text.
- **`template_stamps.WORKFLOW` stays 1.** The runtime refuses no step an older template takes
  (`contracts/doctor.md`), so an older installed copy keeps working until the next install.

`tests/test_lesson_shape.py` pins the block, its labels, both templates carrying it, and the
snippet on a search.

## The usefulness question is the full save's, asked once (US-15)

`/engmem.save` asks the user once, after the document is written, whether the documents the
session's searches showed helped, did not apply or did harm, and records only the answers given
(`contracts/gate1.md`, "Usefulness feedback"). It comes last, so declining it costs the save
nothing, and an unanswered question records nothing: the find stays unknown, which is not a
failure. `/engmem.save.quick` does not ask. Its contract is one preview and one confirmation,
and a second question would break it. The user can still record an assessment later with
`engmem feedback record`.

## The Reuse Log starts from what the session found (US-27)

Both save templates run `engmem feedback summary --session <id>` before the Reuse Log, and walk
its finds marked `[read]` and its `read, not surfaced:` line: each either gets a row or is left
out on purpose. The Reuse Log used to rest on the agent's recollection and the `cite as` lines
it kept, and `Prior docs used: none.` was the line that cost least to write. The quick save runs
the command too: it asks the user nothing, so its one preview and one confirmation stay.
Through MCP only there is no such command (US-15 D5), so the kept `cite as` lines are the list.

When completion warns that a none Reuse Log left reads out, both templates say to tell the user
and not to edit the published file. The document is already active, engmem will not rewrite it,
and writing `sessions/<id>.md` by hand is what both templates forbid; the user decides whether a
row is missing. `template_stamps.WORKFLOW` stays 1: the runtime refuses no step an older
template takes.

## Reads go through engmem, and the agent's own count is not a measurement (US-16)

`/engmem` asks the agent to load found documents with `engmem read` (MCP: `engmem_read`) and to
pass the draft id on each read, as on each search. Only a read made that way is counted in
`engmem cost summary`; a file opened directly is allowed, still unobserved, and the report names
that gap (`contracts/output.md`, "Observed cost"). The instruction asks for sections by role
because a whole document is the largest single cost a session can take on. The read carries the
session id under step 3's rule, including its exception: with no draft written, it is left out,
never an id whose document does not exist (ARCH-003). Each quote is cited by the `cite as`
reference of the response it came from, the read's or the search's, in `/engmem.save` and
`/engmem.save.quick` alike: a read can deliver a later version than the search showed
(ARCH-001).

`context_bytes` used to be the agent's own count of what it pulled into context. A model cannot
count bytes, so the field now holds engmem's measurement (next section). A document saved with
templates from before this change keeps the old count, and nothing in the code reads the field.
A baseline figure goes to `engmem cost record-baseline` (MCP: `engmem_record_baseline`) only
when the runtime reported it, after the draft exists, because the record needs the session
document. With nothing reported the template records nothing, and the report shows the baseline
as missing data.

## What the save takes from engmem (2026-11-07)

`/engmem` used to ask the agent to remember numbers and the Search Trace path until
`/engmem.save`. Nothing wrote them down, so context compaction, a crash or a save in another
conversation lost them, or the agent invented them. The save now takes from engmem what engmem
can derive, and writes what only the agent knows only while the agent still has it.

Derived, with a shell, from `engmem cost summary --session <id>` at save:

- **`context_bytes`** is the bytes on its `total:` line: what this session's searches and reads
  through engmem delivered. A file opened directly is not in it, and the report says so.
- **`baseline_tokens`** is the sum of the tokens on the lines under its `baseline call` line,
  as the client reported them to `engmem cost record-baseline`. Each line is one call the
  no-memory path made: a second report of the same call id replaces its line
  (`cost.summarize`), so a retried sub-agent adds a line, and both calls were spent on the
  baseline. The lines keep the order each call id first appeared, so no line can be read as the
  latest. A line with seconds only adds nothing; with no tokens on any line, the field is
  omitted. The cost record is authoritative: `cost.jsonl` holds the figure, and the
  front-matter field is a copy for a reader of the document. `/engmem` names only that one way
  to record it.
- **The Search Trace** is `shell` when its `search responses` count is above 0, or `paste`
  when the user pasted the output. A search, hit or miss, writes one delivery attributed to the
  session (`search_report.compose`), so a count above 0 proves a search ran. A count of 0 proves
  nothing: a search leaves no attributed delivery when it ran without `--session` because no
  draft existed, when `record_delivery` failed (the output says `note: cost not recorded`), or
  on a `--read-only` MCP server, which records no cost (`mcp_server.py`). At 0 the agent writes
  the value it still knows, else the absence marker below; it never infers `miss` from the
  count. A remembered `shell` with no telemetry row then still reaches `gate1_audit`'s
  cross-check (`unreconstructable_docs`), so that failure stays loud.

Known only to the agent: `answer_steps`, `answer_tokens`, and whether the user pasted the
output (`paste`). Each is written only if the agent still has it, and omitted otherwise; never
reconstructed. A Search Trace the agent can neither derive nor recall reads
`not stated in the available material.`, the absence marker both save templates already use, so
every save still carries the section. The audit reads no trace value from that line.

There is no MCP tool that shows the cost summary, and adding one would grow the tool. An agent
with MCP only therefore omits `context_bytes` and `baseline_tokens`, and writes the Search Trace
it still knows. The baseline it recorded through `engmem_record_baseline` stays in `cost.jsonl`
either way. `template_stamps.WORKFLOW` stays 1: the runtime refuses no step an older template
takes.

## `capture_minutes` is two readings of the shell clock

Both save templates fill `capture_minutes` from two readings of the UTC clock in the agent's
shell: `date -u +%Y-%m-%dT%H:%M:%SZ` in a POSIX shell, `(Get-Date).ToUniversalTime().ToString('o')`
in PowerShell. The first reading is the save's first action, an unnumbered line before step 1,
so no step number other documents cite moves. The field had asked for "a time you recorded",
and no template said to record one. An agent has no clock of its own, and the draft's `date:`
has day resolution, so the field stayed blank on almost every save. The commands are the
system's own, so the templates add no engmem command or field.

- **Blank still means never measured** (`data-model.md`). An agent without a shell, or with only
  one reading, leaves the field blank: a guess corrupts the cost side, and a gap does not.
- **The second reading is the last change to the document.** The full save takes it at close-out,
  just before it publishes. The quick save takes it as it writes the front matter, before the
  preview: the user confirms the document as shown, and a value changed after that would publish
  a document the user did not see. The quick count leaves out the wait for that confirmation.
- **It measures the save ritual, not the task.** `/engmem` does not read the clock, since the
  field has never covered the work itself.
- **`template_stamps.WORKFLOW` stays 1.** The runtime checks no reading, so an older installed
  copy keeps working and still leaves the field blank.

Pinned by `tests/test_docs_match_the_code.py`, on both templates and every installed copy.

## A template states the rule; this file keeps the reason

An agent loads the whole template on every run, so each sentence costs context. A template keeps
every instruction. It keeps at most one sentence of reason, and only where the reason stops an
agent from optimising the rule away. The longer reasons live here, or in the contract named.
`tests/test_docs_match_the_code.py` holds `engmem.save.md` at or below 70 % of its size in
release v0.1.10, 26,511 bytes. `template_stamps.WORKFLOW` stays 1: the cut changes no step.

Reasons the templates no longer spell out:

- **Ids, never filenames.** A `.md` suffix names no document. In `related`, a citation that
  should read `adjacent` can read `distant` (`gate1._distance`), and a stored document renders
  as `(not in store)`. In a Reuse Log `prior-doc`, the row is excluded as citing a document not
  in the store (`contracts/gate1.md`).
- **One front-matter round.** More than one interactive round for the draft is a design
  failure (`ENGMEM-SPEC.md` §6, `/engmem.save` step 1). The step's heading says "not
  field-by-field", so the step no longer repeats it.
- **Column order.** The count reads a Reuse Log row's cells by position, so a list or another
  column order is never counted (`gate1.py`).
- **Why and terms.** A reader who sees only the "what" cannot judge whether the choice still
  applies to their case. A term used before its definition sends the reader to look for it
  mid-flow.
- **Production Considerations.** A real search that shaped the template answered an HTTP 400
  from a missing request header with this section, in a document with no other place for it.
- **Status at a Glance is 17.** It was appended, so the sixteen numbers other documents cite
  still point at the same sections. Lessons Learned keeps the skill's own name; `Landmines`
  resolves to the same role (`contracts/sections.md`).
- **A stale citation is asked about at once.** After the session, a row that reused superseded
  knowledge looks like honest reuse. The review side keeps it a flag (`contracts/gate1.md`,
  "Supersession is a flag, not a filter").
- **A navigation miss needs its query.** The id says a miss happened. The query says which kind:
  a wording gap, where the document says the same thing in other words that lexical matching
  cannot see, or a ranking gap, where it matched and lost to the documents shown. Embeddings
  are parked (`ENGMEM-SPEC.md` §9), and a bare count cannot say which gap these entries show.
  An invented entry corrupts that evidence as much as a missing one.
- **The cost fields answer two questions.** *Did memory help?* is the Pre-reg against the
  final plan: secondary, and read only from `pre-reg source: sub-agent` sessions (above). *Did
  memory pay for itself in context?* is `context_bytes` against `answer_steps`. A few kilobytes
  and almost no later tool calls is cheap. Tens of kilobytes and as many calls as without
  memory means the document was read but did not shorten the path. The cost of the task
  without memory is not measured: the baseline is a plan, not a run. Only the same task done
  twice, with and without the store, gives that number. The fields are optional because the
  parser ignores fields it does not know (`data-model.md`).
- **The start's measured numbers.** `baseline_tokens` is the one cost of the no-memory path
  that is measured, not guessed. `answer_steps` near zero is the saving being measured. A
  fabricated `answer_tokens` corrupts the analysis the project exists to produce.
- **The start's draft.** Placeholder values with every field present keep the draft valid to
  the parser from the moment it exists. Empty `entities` is a warning, not an error, until the
  save fills it. A daily draft's empty body draws no `document is empty` warning
  (`spine.py`). Over MCP, no shell quoting or pipe encoding stands between the text and
  engmem. A `store show` error means another directory, which is another store. The YAML
  comments say only what the agent acts on (2026-11-07). The `id` comment names the characters
  `spine.validate_doc_id` accepts, and drops the filename rule, since engmem names the file. The
  `task_date` comment says it equals `date` except in a backfilled record (`data-model.md`).
- **Plain words for internal names.** An installed agent cannot look up "Gate 1", so the start
  template says "the research comparison" and "engmem's audit". The Search Trace values keep
  their spelling, since `gate1_audit` matches them exactly.
- **What the short capture skips.** With no supersede question, a document the decision
  overturns keeps surfacing as current until someone marks it. With no Reuse Log review, reuse
  evidence may be missing. Its headings carry no numbers because a short record numbered 8 and
  13 reads as a full one with gaps.
