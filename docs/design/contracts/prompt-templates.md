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
