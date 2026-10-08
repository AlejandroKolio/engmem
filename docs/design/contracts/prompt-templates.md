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

It is obtained before any search, from a sub-agent that knows only the task description —
or, failing that, written by the agent itself before it opens a single prior document.

A template that prompts the user for it, waits on them, or refuses to proceed without it
has broken the experiment twice over: it contaminates the baseline with the user's own
thinking, and it turns the capture ritual into a precondition for getting an answer.
Recording *which* source was used is what keeps the two cases distinguishable later.

## Templates may not grow the tool

No template introduces a CLI flag, field, or command that this feature's contracts do not
already define. `ENGMEM-SPEC.md` §9 is a standing rejection list. If a template's
behaviour appears to need something from it, work stops and the author is asked
(`ENGMEM-SPEC.md` §10, principle IX) — it is not built anyway.

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
match is US-14's question, not this template's.

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
