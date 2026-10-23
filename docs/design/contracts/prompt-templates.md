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

The draft's YAML shows `mode: <daily|research from step 0>`, not a real value: a shell agent's
draft is not checked by any tool, and a placeholder that is a valid word would be copied as a
claim. Copied literally, this one is an unrecognized mode and counts nowhere.

The mode is written once, into the draft, and the save templates copy `mode` and
`baseline_unavailable` unchanged — never added, edited or dropped, whatever the saved mode is by
then. `engmem_complete_draft` enforces that for MCP clients; a shell save has the template's word
for it, as it has for the Reuse Log rules. Both save templates carry the Pre-reg over only when
the draft has one. The short capture's example record is a daily one, with no Pre-reg, since a
short save is the daily path the story is about.

## Templates may not grow the tool

No template introduces a CLI flag, field, or command that this feature's contracts do not
already define. `ENGMEM-SPEC.md` §9 is a standing rejection list. If a template's
behaviour appears to need something from it, work stops and the author is asked
(`ENGMEM-SPEC.md` §10, principle IX) — it is not built anyway.

## Writes go through engmem (US-24)

The start and save templates create and complete the session document only through engmem:
`engmem draft create|complete` with a shell, `engmem_create_draft|complete_draft` over MCP.
Neither tells an agent to write `sessions/<id>.md` itself, even when it could. A plain file
write skips the checks those commands make (`contracts/mcp-server.md`, "One writer for every
surface"), and the guarantee then depended on which client happened to run the task. The
start draft records the working repository in `repos` next to `id` and `mode`.

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

Nothing about the id is enforced — `session_id` stays optional in the MCP schema and
`--session` optional on the CLI, because a search before any draft exists is legitimate. The
MCP tool surface asks for it in three places (`contracts/mcp-server.md`, "`session_id`:
optional to the schema, asked for in every wording"); the CLI has only `--session`'s argparse
help (`cli.py`), so on that path the template is the one place that asks before the search —
which is why its wording is a constraint and not a nicety. Both channels also end an
unattributed search with a trailing `note: unattributed search — pass ...` line
(`telemetry.UNATTRIBUTED_CLI_NOTE` / `UNATTRIBUTED_MCP_NOTE`; the recorded reversal in that
same section), a reminder at the point of use that the template's wording cannot give.

## A search over MCP is still `shell`

The Search Trace records how the search was *executed*, not how it travelled. An agent
calling the `engmem_search` MCP tool ran the search itself, and writes the same telemetry
line as a shell invocation, so both are `shell`. Only a human pasting output back is
`paste`, and only a search that never happened is `miss`.

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

The `context_bytes` and `answer_steps` front-matter fields stay, as the agent's own count. The
templates call `context_bytes` that and point at `engmem cost summary` for what engmem measured;
nothing reads the field as a measurement. A baseline figure goes to `engmem cost record-baseline`
(MCP: `engmem_record_baseline`) only when the runtime reported it, after the draft exists,
because the record needs the session document; `baseline_tokens` in front matter keeps its old
rule. With nothing reported the template records nothing, and the report shows the baseline as
missing data.
