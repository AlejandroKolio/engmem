# Constraints on the prompt templates

The templates themselves — `src/engmem/templates/engmem.start.md`, `engmem.save.md`,
`engmem.save.quick.md` — are the instructions, and they are plain markdown you can read.
This file does not restate them. It records the rules they must satisfy, which are not
visible from inside any one of them.

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

## A search over MCP is still `shell`

The Search Trace records how the search was *executed*, not how it travelled. An agent
calling the `engmem_search` MCP tool ran the search itself, and writes the same telemetry
line as a shell invocation, so both are `shell`. Only a human pasting output back is
`paste`, and only a search that never happened is `miss`.
