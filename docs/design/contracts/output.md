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

## A scoped result says so, and shows every link (US-09)

A search run with a scope (`--repo` / `--unscoped`, MCP `repo` / `unscoped`;
`contracts/scoring.md`, "A repository scope narrows the corpus") renders through the same
functions with a `scope_lead`: `scope_line` opens the result —
`scope: repo <name> (<n> linked document(s) searched; records linked to no repository are left
out)` or `scope: unscoped (<n> document(s) linked to no repository searched)` — and every
block, hit, successor and role hit alike, carries a `repos:` line after its `path:`
(`_repos_line`). `<n>` is the size of the scoped ranking corpus, drafts excluded.

- The lead line exists because a miss inside a scope is not a miss in the store: without it,
  `prior context: none found` from `--repo` read exactly like an empty store, and a reader had
  no way to see that the other repositories, and the unlinked records, were never searched.
- `repos:` lists every link as written (`a, b` for a record shared by two repositories, which
  is shown once — AC-09.2), `none` for a record with no link, and `unreadable` when `repos`
  could not be read. Every name, and the scope's own name, goes through `_escape_controls`:
  the scope is typed by a caller and the links come from a file, so either could forge a line.
- Both lines sit inside `_rendered`'s budget — the lead is the first block, so the trim never
  reaches it — and so inside `context_bytes`: they are text the reader pays for. A miss is
  not trimmed, so the scope's name is shown to `SCOPE_NAME_DISPLAY_MAX` characters and cut
  with `…`: a caller-typed name must not be able to carry the output past the cap.
- Without a scope nothing changes: no lead, no `repos:` lines, byte for byte the earlier
  output (US-11's `snapshot:` line, below, is the one later addition to it). Showing links
  on every result would also have served US-10, but it would have moved
  `context_bytes` for every search already in the telemetry history (an owner decision left
  open). US-10 took the opt-in road instead: `--all-repos`, below.

## Several repositories, and the whole store by choice (US-10)

A scope of several repositories (`--repo a --repo b`, MCP `repos`) leads with
`scope: repos <a>, <b> (<n> linked document(s) searched; records linked to no repository are
left out)`; one repository keeps US-09's `scope: repo <name> (…)` byte for byte, so a single
`--repo` result did not change. A record linked to two chosen repositories is one block whose
`repos:` line names both (AC-10.2); the union is formed before ranking (`scoring.md`), so no
renderer has to deduplicate.

`--all-repos` (MCP `all_repos: true`) ranks the default's corpus and leads with
`scope: all repositories (<n> document(s) searched; the whole store, records linked to no
repository included)`, then shows every block's `repos:` like any scope (AC-10.3). It is the
explicit cross-project view: the same records as a plain search, with the mode and each
result's origin on the page, at the price of those lines in `context_bytes`. A plain search
still prints neither, so its output and its telemetry history stay comparable.

The names are typed by the caller, and a miss is never trimmed, so the list is bounded twice:
each name to `SCOPE_NAME_DISPLAY_MAX` characters (US-09), and the names listed stop before the
joined list passes `SCOPE_NAMES_DISPLAY_MAX` characters, the rest counted as `and <k> more`
(`_scope_names`). The first name is always shown, so the line never lists nothing. With both
caps the lead stays a few hundred characters whatever the caller passes, well inside the
budget left after `SCOREBOARD_RESERVE`. The telemetry row keeps more, but not without bound:
up to `SCOPE_NAMES_RECORD_MAX` names, each to `SCOPE_NAME_RECORD_MAX` characters, and the query
to `QUERY_RECORD_MAX` characters; a capped row says so (`ENGMEM-SPEC.md` §5, search step 6c).

## Each block shows its snapshot anchors (US-11)

Every hit, successor and `--role` block of a record that carries an anchor or a repository link
has a `snapshot:` line after `path:` (and after `repos:` when scoped): per repository, the
recorded commit next to the current checkout's `HEAD`, as `same commit as HEAD`,
`HEAD is now <sha>, re-check`, or `unknown, <reason>` (`_snapshot_line`; the rules are
`contracts/provenance.md`). A record with neither gets no line.

When the commits differ and the record lists `covers_files`, US-12 replaces the bare
`re-check` with the result of comparing those files (`_covered_part`): `covered files
unchanged`, `covered file(s) changed: <path>[ (deleted)], … and N more, re-check` (at most
`COVERED_DISPLAY_MAX` paths named, each cut to `COVERED_PATH_DISPLAY_MAX` characters from the
front), or `covered files not checked: <reason>, re-check`. The commit difference stays on the
line in every case.

This is the first change to a plain search's output since the scope work kept it
byte-identical: the line is not behind a flag, so a record with an anchor or a link now costs
one more line, and `context_bytes` grows with it for those records. A plain search remains
unscoped and still prints no `scope:` or `repos:` line. Owner decision (2026-10-09): the line is
shown in the default search output, always; recorded in
`contracts/provenance.md`, "Where it is shown".

## Query and scope names are bounded in the telemetry row, and a capped row says so

`compose` writes the row whatever the caller typed, so one call with a book-length query or
thousands of names would otherwise add that much to the line, and every reader of
`telemetry.jsonl` reads every line. `query` and the scope's names are capped; `session_id` is
the one caller-typed field no cap applies to, kept whole by design (below), so the row as a
whole is not bounded. The caps are far above real use and the marks are additive:

- `query` keeps its first `QUERY_RECORD_MAX` (1000) characters. A real query is a few words
  to a sentence; at 1000 an analysis still sees what was asked. A cut query adds
  `"query_truncated": true` right after it.
- `scope.repos` keeps its first `SCOPE_NAMES_RECORD_MAX` (50) names, each to
  `SCOPE_NAME_RECORD_MAX` characters, the display cap `SCOPE_NAME_DISPLAY_MAX` (120). A real
  union is a handful of repositories, and 120 is past a GitHub repository name's 100-character
  limit, so a real name is never cut. When any name was cut or dropped the scope adds
  `"repos_truncated": true` and `"repos_total": N`, the number of distinct names chosen, so an
  analysis can tell a capped row from a real one and knows how many there were.
- A row under every cap is byte-identical to the row before the caps: no new key, no new
  value. The caps change only what is recorded: the search, `n_searched`, `context_bytes`
  (measured on the rendered result, never on the row) and `session_id` are the same.
  `session_id` is not capped: it is the attribution key the Gate 1 join matches against a
  draft's id, and a cut id would quietly attribute the row to no session.
- Every reader keeps working on old and new rows: `summarize` reads `channel`, `result` and
  the context counts, `read_session_rows` reads `session_id` and `ts`, and neither reads
  `query` or `scope`.

## One composed result for both channels

`search_report.compose` builds the whole stdout of a search once: the unlistable-`sessions/`
line, stray-scan warnings, the ranked result, the stray note, the scoreboard, the telemetry
note and the channel's unattributed note, in that order, and it writes the one telemetry row.
The scope is one more argument to it, so CLI and MCP cannot apply different scope rules
(AC-09.4); the row's `scope` field records it (`ENGMEM-SPEC.md` §5, search step 6b).
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
