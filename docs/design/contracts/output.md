# Contract: rendering search results, the scoreboard, telemetry and backfill previews

Source: `src/engmem/output.py`. Everything an agent or a human reads from engmem is built
here, and every value it renders — a path, an id, a heading, a body snippet, a proposed field
value — comes out of a markdown file engmem did not write and validated only as far as its
spine. The renderer is the last place that can be true about it.

## Nothing untrusted may forge a line

The output has structure a reader acts on: `### <id> (score: N)` opens a hit block, `path:`
names the file to open, `  <- ` names the evidence behind a field about to be written. A
value carrying a line break would make the fragment after the break a line of that structure
— a second hit block naming a document that does not exist, or an extra field on a backfill
preview a human is about to approve.

`_escape_controls` is applied to every untrusted value on its way into a rendered line. It
escapes, never strips, so the tampering stays visible and a document with a stray control
character still renders. The escaped set (`_CONTROL_RE`) is every C0 and C1 control
character, DEL, and `U+2028`/`U+2029`; `\n` and `\r` alone were too narrow twice over.
`str.splitlines()` and most renderers also break on `\v`, `\f`, `\x1c`–`\x1e`, `U+0085`,
`U+2028` and `U+2029`, so each of those forged a line just as well. `ESC` forges nothing, but
`engmem search` writes to a terminal, where `\x1b]0;…\x07` in a filename retitles the window
and `\x1b[2J` clears it. A control character has no legitimate place in an id, a path, a
locator or a one-line excerpt, so all of them are escaped rather than enumerated by effect.

The backfill preview has the stronger reason: it is the only thing shown before the `[y/N]`
write prompt, so a forged line there is a field a human approves without ever having seen
what will be written.

## One parser decides which heading is the primer

`_primer_section` takes the first section `sections.split_sections` returns whose canonical
role is `primer` or whose heading matches `_PRIMER_PHRASE_RE` (a spelling the alias table does
not carry, such as the unhyphenated "Cold Start Primer"). The rejected alternative, a regex of
its own over the raw body, disagreed with the alias table in both directions: `## Future LLM
Context` is a primer to `--role primer` and was not one here, and a `## Cold-start primer`
quoted inside a fenced code block — which a document *about* the session format contains —
was read as that document's own primer. `split_sections` already handles fences, setext
headings and ordinal prefixes; a second opinion on the same question is a second set of
defects.

The excerpt stops at the first line starting with `#`. That is the section's lead-in either
way, and it is load-bearing: the excerpt is appended as its own line, and a body line starting
with `### ` would be indistinguishable from a real hit header.

## The line explaining a trim must survive the trim

`_trim_to_bytes` cuts from the end, and the withheld-count line ("N more document(s) matched
below the top 3") is the one line whose whole purpose is to be read when the output was cut,
so appending it to the block list put it first in line to be cut. `_rendered` reserves its
bytes before trimming and appends it afterwards; both renderers go through it, so the search
and `--role` paths cannot drift apart on a rule that only shows up under a full store.

The budget is `MAX_OUTPUT_BYTES - SCOREBOARD_RESERVE`: the scoreboard footer is printed by the
caller, not by these functions, and still counts against what the reader pays. The two
trailing notes the caller may add after it — `note: telemetry not recorded (...)` and
`note: unattributed search — pass --session <draft-id>` (`ENGMEM-SPEC.md` §5, search step 6)
— draw on the same reserve and are not trimmed. The reserve does not always hold them: the
unattributed note is 55 bytes, and a scoreboard carrying both of its count notes (`partial
spine`, `failed to load`) plus that line already exceeds the 128 before any telemetry note, so
stdout can pass `MAX_OUTPUT_BYTES` on a fully trimmed body. Accepted: both notes are
diagnostics about the instrument, and truncating them would hide the one thing they say.

## One composed result for both channels

`search_report.compose` builds the whole stdout of a search once: the unlistable-`sessions/`
line, stray-scan warnings, the ranked result, the stray note, the scoreboard, the telemetry
note and the channel's unattributed note, in that order, and it writes the one telemetry row.
`engmem search` prints the string; `engmem_search` and `engmem_search_by_role` return it as the
tool text. Only the unattributed note differs by channel, and stderr diagnostics stay with each
caller. There were four copies of this composition, and they had drifted: the MCP ones never
read `scan_error`, so the MCP-only channel showed an unlistable `sessions/` as
`prior context: none found / docs: 0`, the phantom empty the spec forbids, while the CLI said
"unknown, not zero". `render_result` is the pure half — the text `context_bytes` measures,
with no I/O; only `compose` writes telemetry. `tools/baseline_cost.py` measures through it, so
a calibration run reports exactly what telemetry records and adds no row to the store it measures.

## Known limits

`surfaced_ids` reports what the renderer *selected*, not what survived the trim, so a trimmed
run can log an id the reader never saw. Telemetry reads it as "prior context surfaced", which
over-counts in exactly the runs where the output was too long.

`_selected` admits a redirect only when its score is strictly greater than the last shown
hit's. A superseded document tied with the third hit would have taken that place on the
`(-score, -date, id)` order `scoring` actually sorts by, and its redirect is dropped. The
failure is silence about a stale document, never handing one over, and closing it would mean
restating `scoring`'s sort key in the renderer.
