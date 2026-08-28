# Contract: rendering search results, the scoreboard, telemetry and backfill previews

Source: `src/engmem/output.py`. Everything an agent or a human reads from engmem is
built here. Every value it renders — a path, an id, a heading, a body snippet, a
proposed field value — comes out of a markdown file that engmem did not write and did
not validate beyond its spine. The renderer is the last place that can be true about
it.

## Nothing untrusted may forge a line

The output has structure a reader acts on: `### <id> (score: N)` opens a hit block,
`path:` names the file to open, `  <- ` names the evidence behind a field about to be
written. A value carrying a line break splits its own line and the fragment after the
break becomes a line of that structure — a second hit block naming a document that
does not exist, or an extra field on a backfill preview a human is about to approve.

`_escape_controls` is the single answer, applied to every untrusted value on its way
into a rendered line. It escapes, never strips: the tampering stays visible to whoever
reads the output, and a document with a stray control character still renders.

The escaped set (`_CONTROL_RE`) is every C0 and C1 control character, DEL, and
`U+2028`/`U+2029`. It started as `\n` and `\r` alone, which was too narrow twice over:

- `str.splitlines()` and most renderers also break on `\v`, `\f`, `\x1c`–`\x1e`,
  `U+0085`, `U+2028` and `U+2029`, so each of those forged a line just as well as `\n`.
- `ESC` forges nothing, but `engmem search` writes to a terminal. A filename or a
  document body carrying `\x1b]0;…\x07` retitles the user's window; `\x1b[2J` clears
  it. A control character has no legitimate place in an id, a path, a locator or a
  one-line excerpt, so all of them are escaped rather than enumerated by effect.

The backfill preview is escaped for a stronger reason than the search output: it is
the only thing shown before the `[y/N]` write prompt, so a forged line there is a
field a human approves without ever having seen what will be written.

## One parser decides which heading is the primer

`_primer_section` asks `sections.split_sections` and takes the first section whose
canonical role is `primer`, falling back to `_PRIMER_PHRASE_RE` for spellings the
alias table does not carry (the unhyphenated "Cold Start Primer").

It used to run its own regex over the raw body instead, which disagreed with the alias
table in both directions: `## Future LLM Context` is a primer to `--role primer` and
was not one here, and a `## Cold-start primer` heading quoted inside a fenced code
block — which a document *about* the session format contains — was read as that
document's own primer. `split_sections` already handles fences, setext headings and
ordinal prefixes; a second opinion on the same question is a second set of defects.

The excerpt stops at the first line starting with `#`. That is the section's lead-in
either way, but it is also load-bearing: the excerpt is appended as its own line, and
a body line starting with `### ` would be indistinguishable from a real hit header.

## The line explaining a trim must survive the trim

`_trim_to_bytes` cuts from the end. The withheld-count line ("N more document(s)
matched below the top 3") is the one line whose whole purpose is to be read when the
output was cut, so appending it to the block list put it first in line to be cut.

`_rendered` reserves its bytes before trimming and appends it afterwards. Both
renderers go through it, so the search and `--role` paths cannot drift apart on a rule
that only shows up under a full store.

The budget is `MAX_OUTPUT_BYTES - SCOREBOARD_RESERVE`: the scoreboard footer is printed
by the caller, not by these functions, and still counts against what the reader pays.

## Known limits

`surfaced_ids` reports what the renderer *selected*, not what survived the trim, so a
trimmed run can log an id the reader never saw. Telemetry reads it as "prior context
surfaced", which over-counts in exactly the runs where the output was too long.

`_selected` admits a redirect only when its score is strictly greater than the
last shown hit's. A superseded document tied with the third hit would have taken that
place on the `(-score, -date, id)` order `scoring` actually sorts by, and its redirect
is dropped. The failure is silence about a stale document, never handing one over, and
closing it would mean restating `scoring`'s sort key in the renderer.
